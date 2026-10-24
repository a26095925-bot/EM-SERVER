import os
import json
import uuid
import time
import math
import re
import datetime
import asyncio
import threading
from collections import defaultdict, deque
from functools import wraps
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
import bcrypt
import jwt
import uvicorn

# Configuration
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "4cf18f6b81f018297ba7018547ad6005")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "aient")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "super_secret_roblox_style_jwt_key_2026")
JWT_ALGORITHM = "HS256"

app = FastAPI(title="Roblox-Style Game Backend", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class LivePlaceRoom:
    def __init__(self, owner: str, blocks: List[dict], gui_elements: List[dict]):
        self.owner = owner
        self.blocks = {block["id"]: block for block in blocks}
        self.gui_elements = gui_elements
        self.clients: Dict[WebSocket, str] = {}
        self.rate_windows: Dict[tuple, deque] = defaultdict(deque)


live_rooms: Dict[str, LivePlaceRoom] = {}
live_rooms_lock = asyncio.Lock()


def _bounded_number(value: Any, lower: float, upper: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and lower <= value <= upper)


def _safe_live_block(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    block = dict(raw)
    block_id = block.get("id")
    if not isinstance(block_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", block_id):
        return None
    if len(json.dumps(block, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 16_000:
        return None
    for key, default in (("pos", [0, 0, 0]), ("size", [1, 1, 1]), ("rot", [0, 0, 0])):
        values = block.get(key, default)
        if (not isinstance(values, list) or len(values) != 3 or
                any(not _bounded_number(value, 0.000001 if key == "size" else -10000,
                                        512 if key == "size" else 10000) for value in values)):
            return None
        block[key] = [float(value) for value in values]
    color = block.get("color", "#3B82F6")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        return None
    return block


def _safe_live_gui_elements(raw: Any) -> Optional[List[dict]]:
    try:
        return validate_gui_elements(raw)
    except (HTTPException, TypeError, ValueError):
        return None


def _allow_live_message(room: LivePlaceRoom, websocket: WebSocket, kind: str) -> bool:
    now = time.monotonic()
    window = room.rate_windows[(websocket, kind)]
    limit = {"state": 15, "chat": 5, "block_upsert": 20, "block_remove": 20, "place_state": 4}.get(kind, 0)
    while window and now - window[0] >= 1:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


async def _send_block_snapshot(websocket: WebSocket, blocks: List[dict]):
    await websocket.send_json({"type": "block_snapshot_start"})
    chunk: List[dict] = []
    for block in blocks:
        candidate = chunk + [block]
        if chunk and len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) > 48_000:
            await websocket.send_json({"type": "block_snapshot_chunk", "blocks": chunk})
            chunk = [block]
        else:
            chunk = candidate
    if chunk:
        await websocket.send_json({"type": "block_snapshot_chunk", "blocks": chunk})
    await websocket.send_json({"type": "block_snapshot_end"})


async def _broadcast_live(room: LivePlaceRoom, payload: dict):
    disconnected = []
    for connection in list(room.clients):
        try:
            await connection.send_json(payload)
        except Exception:
            disconnected.append(connection)
    for connection in disconnected:
        room.clients.pop(connection, None)


# In-memory Rate Limiting / Bot Protection
RATE_LIMIT_STORE: Dict[str, List[float]] = {}

def check_rate_limit(client_ip: str, action: str, max_calls: int, window_sec: int = 60):
    """Protects against bot spam and brute force attacks."""
    now = time.time()
    key = f"{client_ip}:{action}"
    timestamps = RATE_LIMIT_STORE.get(key, [])
    # keep only within window
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail=f"Забагато запитів від вашого IP. Зачекайте {window_sec} секунд (Захист від ботів)."
        )
    timestamps.append(now)
    RATE_LIMIT_STORE[key] = timestamps

# R2 Storage Provider with fallback to local storage
s3_client = None
if R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    try:
        import boto3
        from botocore.config import Config
        s3_client = boto3.client(
            service_name="s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4")
        )
        print("Connected to Cloudflare R2 bucket:", R2_BUCKET_NAME)
    except Exception as e:
        print("Failed to initialize R2 client:", e)

LOCAL_STORAGE_DIR = "storage"
os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)
storage_lock = threading.RLock()

def storage_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with storage_lock:
            return function(*args, **kwargs)
    return wrapped

def write_json_file(path_key: str, data: dict):
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    if s3_client:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=path_key,
            Body=payload,
            ContentType="application/json"
        )
    else:
        full_path = os.path.join(LOCAL_STORAGE_DIR, path_key)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(payload)

def read_json_file(path_key: str) -> Optional[dict]:
    if s3_client:
        try:
            resp = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=path_key)
            content = resp["Body"].read().decode("utf-8")
            return json.loads(content)
        except Exception:
            return None
    else:
        full_path = os.path.join(LOCAL_STORAGE_DIR, path_key)
        if not os.path.exists(full_path):
            return None
        with open(full_path, "r", encoding="utf-8") as f:
            return json.load(f)

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_jwt_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=30),
        "iat": datetime.datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")
    try:
        token = authorization.replace("Bearer ", "").strip()
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token subject")
        return username
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# Models
class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=24)
    password: str = Field(..., min_length=4, max_length=64)

class AuthResponse(BaseModel):
    status: str
    username: str
    token: str

class EventRequest(BaseModel):
    event_id: str = Field(..., min_length=32, max_length=36, pattern=r"^[a-fA-F0-9-]{32,36}$")

class FriendRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=24, pattern=r"^[A-Za-z0-9_]+$")

class PlaceBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "solid"
    pos: List[FiniteFloat] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    size: List[FiniteFloat] = Field(default_factory=lambda: [1, 1, 1], min_length=3, max_length=3)
    color: str = "#3B82F6"
    power: Optional[FiniteFloat] = Field(default=10.0, ge=0, le=100)
    script: Optional[str] = Field(default=None, max_length=4000)

class PlaceLayerData(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    visible: bool = True

class PlaceData(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = ""
    author: Optional[str] = ""
    spawn: List[FiniteFloat] = Field(default_factory=lambda: [0, 2, 0], min_length=3, max_length=3)
    blocks: List[PlaceBlock] = Field(default_factory=list, max_length=4000)
    gui_elements: Optional[List[dict]] = Field(default=None, max_length=32)
    thumbnail_png: Optional[str] = Field(default=None, max_length=1_400_000)
    layers: List[PlaceLayerData] = Field(default_factory=lambda: [PlaceLayerData(name="Default")], max_length=64)
    walk_speed: FiniteFloat = Field(default=8.5, ge=1, le=40)
    jump_force: FiniteFloat = Field(default=13.5, ge=1, le=80)
    gravity: FiniteFloat = Field(default=32, ge=1, le=100)
    sprint_enabled: bool = True
    wall_jump_enabled: bool = True
    baseplate_enabled: bool = True
    death_height: FiniteFloat = Field(default=-20, ge=-1000, le=-1)


def validate_gui_elements(raw: Any) -> Optional[List[dict]]:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 32:
        raise HTTPException(status_code=400, detail="Некоректний список елементів інтерфейсу плейса")

    result = []
    ids = set()
    for element in raw:
        if not isinstance(element, dict):
            raise HTTPException(status_code=400, detail="Некоректний елемент інтерфейсу")
        element_id = element.get("Id", element.get("id", ""))
        element_type = element.get("Type", element.get("type", "label"))
        text = element.get("Text", element.get("text", ""))
        script = element.get("OnClickScript", element.get("onClickScript", ""))
        if (not isinstance(element_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", element_id) or
            element_id in ids or element_type not in ("label", "button") or
            not isinstance(text, str) or len(text) > 200 or
            not isinstance(script, str) or len(script) > 1000):
            raise HTTPException(status_code=400, detail="Некоректні властивості елемента інтерфейсу")

        clean = {"Id": element_id, "Type": element_type, "Text": text, "OnClickScript": script}
        for key, lower, upper, default in (
            ("X", 0, 100, 50), ("Y", 0, 100, 50),
            ("Width", 1, 100, 24), ("Height", 1, 100, 7),
        ):
            value = element.get(key, element.get(key.lower(), default))
            if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(float(value)) or not lower <= value <= upper):
                raise HTTPException(status_code=400, detail="Некоректний розмір або позиція елемента інтерфейсу")
            clean[key] = float(value)
        ids.add(element_id)
        result.append(clean)
    return result


# Routes
@app.get("/")
def root():
    websocket_route = any(getattr(route, "path", None) == "/ws/{place_id}" for route in app.routes)
    return {
        "status": "online",
        "service": "Roblox-Style Game API with Stats, Friends, Likes, and Multiplayer",
        "api_version": app.version,
        "features": ["stats", "friends", "place_likes", "place_plays"] + (["multiplayer"] if websocket_route else []),
        "multiplayer_websocket": websocket_route,
        "websocket_path": "/ws/{place_id}" if websocket_route else None,
        "storage": "Cloudflare R2" if s3_client else "Local Cache",
        "time": datetime.datetime.utcnow().isoformat()
    }


@app.websocket("/ws/{place_id}")
async def multiplayer_socket(websocket: WebSocket, place_id: str, token: str):
    await websocket.accept()
    if not re.fullmatch(r"[a-fA-F0-9-]{1,36}", place_id):
        await websocket.close(code=4404, reason="Invalid place ID")
        return
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise jwt.InvalidTokenError("Missing subject")
    except jwt.PyJWTError:
        await websocket.close(code=4401, reason="Session is invalid or expired; sign in again")
        return

    place_record = read_json_file(f"places/{place_id}.json")
    if not place_record:
        await websocket.close(code=4404, reason="Place not found; check the place ID")
        return
    owner = place_record.get("author", "")
    if not isinstance(owner, str) or not owner:
        await websocket.close(code=4403, reason="Place has no valid owner")
        return

    async with live_rooms_lock:
        room = live_rooms.get(place_id)
        if room is None or room.owner.lower() != owner.lower():
            blocks = []
            stored_blocks = place_record.get("blocks", [])
            if isinstance(stored_blocks, list):
                for raw_block in stored_blocks[:4000]:
                    if isinstance(raw_block, dict):
                        block = dict(raw_block)
                        block.setdefault("id", uuid.uuid4().hex)
                        clean = _safe_live_block(block)
                        if clean:
                            blocks.append(clean)
            gui_elements = _safe_live_gui_elements(place_record.get("gui_elements", [])) or []
            room = LivePlaceRoom(owner, blocks, gui_elements)
            live_rooms[place_id] = room

    room.clients[websocket] = username
    await websocket.send_json({"type": "welcome", "username": username, "is_owner": username.lower() == owner.lower()})
    await websocket.send_json({"type": "roster", "players": list(room.clients.values())})
    await _send_block_snapshot(websocket, list(room.blocks.values()))
    await websocket.send_json({"type": "place_state", "gui_elements": room.gui_elements})
    await _broadcast_live(room, {"type": "joined", "username": username})

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > 65_536:
                await websocket.close(code=1009, reason="Message too large")
                break
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind not in ("state", "chat", "block_upsert", "block_remove", "place_state"):
                continue
            if not _allow_live_message(room, websocket, kind):
                continue

            is_owner = username.lower() == room.owner.lower()
            if kind == "state":
                position = message.get("position")
                yaw = message.get("yaw", 0)
                scale = message.get("head_scale", 1)
                if (not isinstance(position, list) or len(position) != 3 or
                        any(not _bounded_number(value, -100000, 100000) for value in position) or
                        not _bounded_number(yaw, -36000, 36000) or not _bounded_number(scale, 1, 3)):
                    continue
                skin = message.get("skin", {})
                if not isinstance(skin, dict):
                    skin = {}
                skin = {key: value[:80] for key, value in skin.items()
                        if isinstance(key, str) and isinstance(value, str) and len(value) <= 80}
                await _broadcast_live(room, {
                    "type": "state", "username": username, "is_owner": is_owner,
                    "position": [float(value) for value in position],
                    "yaw": float(yaw), "head_scale": float(scale), "skin": skin,
                })
            elif kind == "chat":
                text = message.get("message")
                if not isinstance(text, str):
                    continue
                text = "".join(character for character in text if character.isprintable()).strip()[:220]
                if text:
                    await _broadcast_live(room, {
                        "type": "chat", "username": username,
                        "is_owner": is_owner, "message": text,
                    })
            elif not is_owner:
                await websocket.send_json({"type": "notice", "message": "Тільки автор плейса може змінювати блоки та скрипти."})
            elif kind == "block_upsert":
                block = _safe_live_block(message.get("block"))
                if not block or (block["id"] not in room.blocks and len(room.blocks) >= 4000):
                    continue
                room.blocks[block["id"]] = block
                await _broadcast_live(room, {"type": "block_upsert", "block": block})
            elif kind == "block_remove":
                block_id = message.get("id")
                if isinstance(block_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", block_id):
                    if room.blocks.pop(block_id, None) is not None:
                        await _broadcast_live(room, {"type": "block_remove", "id": block_id})
            else:
                gui_elements = _safe_live_gui_elements(message.get("gui_elements"))
                if gui_elements is not None:
                    room.gui_elements = gui_elements
                    await _broadcast_live(room, {"type": "place_state", "gui_elements": gui_elements})
    except WebSocketDisconnect:
        pass
    finally:
        room.clients.pop(websocket, None)
        room.rate_windows = defaultdict(deque, {
            key: stamps for key, stamps in room.rate_windows.items() if key[0] is not websocket
        })
        await _broadcast_live(room, {"type": "left", "username": username})


@app.post("/api/auth/register", response_model=AuthResponse)
def register(req: AuthRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip, "register", max_calls=4, window_sec=60)

    username = req.username.strip().lower()
    user_key = f"users/{username}.json"
    
    existing = read_json_file(user_key)
    if existing:
        raise HTTPException(status_code=400, detail="Користувач з таким логіном вже існує")
    
    user_record = {
        "username": username,
        "password_hash": hash_password(req.password),
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    write_json_file(user_key, user_record)
    
    token = create_jwt_token(username)
    return AuthResponse(status="success", username=username, token=token)

@app.post("/api/auth/login", response_model=AuthResponse)
def login(req: AuthRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip, "login", max_calls=12, window_sec=60)

    username = req.username.strip().lower()
    user_key = f"users/{username}.json"
    
    user_record = read_json_file(user_key)
    if not user_record:
        raise HTTPException(status_code=400, detail="Невірний логін або пароль")
    
    if not verify_password(req.password, user_record["password_hash"]):
        raise HTTPException(status_code=400, detail="Невірний логін або пароль")
    
    token = create_jwt_token(username)
    return AuthResponse(status="success", username=username, token=token)

@app.get("/api/auth/me")
def me(current_user: str = Depends(get_current_user)):
    return {"status": "ok", "username": current_user}

@app.post("/api/places/save")
@storage_locked
def save_place(place: PlaceData, current_user: str = Depends(get_current_user)):
    # Server-side validation against malicious / oversized payloads
    if len(place.blocks) > 4000:
        raise HTTPException(status_code=400, detail="Перевищено ліміт: максимум 4 000 блоків на один плейс")

    place_id = place.id or str(uuid.uuid4())[:8]
    place_key = f"places/{place_id}.json"
    
    existing = read_json_file(place_key)
    if existing and existing.get("author") != current_user:
        raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")
    gui_elements = validate_gui_elements(place.gui_elements)
    if gui_elements is None:
        gui_elements = existing.get("gui_elements", [])
    
    now_str = datetime.datetime.utcnow().isoformat()
    liked_by = (existing or {}).get("liked_by", (existing or {}).get("likes", []))
    if not isinstance(liked_by, list):
        liked_by = []
    place_record = {
        "id": place_id,
        "title": place.title,
        "description": place.description or "",
        "author": current_user,
        "created_at": existing.get("created_at") if existing else now_str,
        "updated_at": now_str,
        "spawn": place.spawn,
        "blocks": [b.model_dump() for b in place.blocks],
        "gui_elements": gui_elements,
        "thumbnail_png": place.thumbnail_png,
        "layers": [layer.model_dump() for layer in place.layers],
        "walk_speed": place.walk_speed,
        "jump_force": place.jump_force,
        "gravity": place.gravity,
        "sprint_enabled": place.sprint_enabled,
        "wall_jump_enabled": place.wall_jump_enabled,
        "baseplate_enabled": place.baseplate_enabled,
        "death_height": place.death_height,
        "plays": int((existing or {}).get("plays", 0)),
        "liked_by": list(liked_by)
    }
    place_record["likes"] = len(place_record["liked_by"])
    
    write_json_file(place_key, place_record)
    
    index_data = read_json_file("places_index.json") or {"places": []}
    places_list = [p for p in index_data.get("places", []) if p.get("id") != place_id]
    places_list.insert(0, {
        "id": place_id,
        "title": place.title,
        "author": current_user,
        "block_count": len(place.blocks),
        "has_thumbnail": bool(place.thumbnail_png),
        "plays": place_record["plays"],
        "likes": place_record["likes"],
        "updated_at": now_str
    })
    index_data["places"] = places_list[:100]
    write_json_file("places_index.json", index_data)
    
    return {"status": "success", "id": place_id, "message": "Плейс збережено в Cloudflare R2"}

@app.get("/api/places/list")
def list_places(authorization: Optional[str] = Header(None)):
    index_data = read_json_file("places_index.json") or {"places": []}
    username = None
    if authorization:
        try:
            username = jwt.decode(authorization.replace("Bearer ", "").strip(), JWT_SECRET, algorithms=[JWT_ALGORITHM]).get("sub")
        except jwt.PyJWTError:
            pass
    places = []
    for item in index_data.get("places", []):
        result = dict(item)
        result.setdefault("plays", 0)
        result.setdefault("likes", 0)
        if username:
            place = read_json_file(f"places/{result.get('id', '')}.json") or {}
            liked_by = place.get("liked_by", place.get("likes", []))
            result["liked_by_me"] = isinstance(liked_by, list) and username in liked_by
        places.append(result)
    return {"places": places}

def _stats_key(username: str) -> str:
    return f"stats/{username}.json"

def _get_stats(username: str) -> dict:
    return read_json_file(_stats_key(username)) or {"deaths": 0, "places_played": 0, "death_events": []}

@app.get("/api/stats/me")
def get_my_stats(current_user: str = Depends(get_current_user)):
    stats = _get_stats(current_user)
    return {"deaths": int(stats.get("deaths", 0)), "places_played": int(stats.get("places_played", 0))}

@app.post("/api/stats/death")
def record_death(req: EventRequest, current_user: str = Depends(get_current_user)):
    with storage_lock:
        stats = _get_stats(current_user)
        event_ids = stats.get("death_events", [])
        if not isinstance(event_ids, list):
            event_ids = []
        if req.event_id not in event_ids:
            event_ids.append(req.event_id)
            stats["death_events"] = event_ids[-4096:]
            stats["deaths"] = int(stats.get("deaths", 0)) + 1
            write_json_file(_stats_key(current_user), stats)
        return {"deaths": int(stats.get("deaths", 0)), "recorded": req.event_id in event_ids}

@app.post("/api/places/{place_id}/play")
def record_place_play(place_id: str, current_user: str = Depends(get_current_user)):
    if not re.fullmatch(r"[a-fA-F0-9-]{1,36}", place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        record["plays"] = int(record.get("plays", 0)) + 1
        write_json_file(f"places/{place_id}.json", record)
        stats = _get_stats(current_user)
        stats["places_played"] = int(stats.get("places_played", 0)) + 1
        write_json_file(_stats_key(current_user), stats)
        index_data = read_json_file("places_index.json") or {"places": []}
        for item in index_data.get("places", []):
            if item.get("id") == place_id:
                item["plays"] = record["plays"]
                break
        write_json_file("places_index.json", index_data)
        return {"plays": record["plays"]}

@app.post("/api/places/{place_id}/like")
def toggle_place_like(place_id: str, current_user: str = Depends(get_current_user)):
    if not re.fullmatch(r"[a-fA-F0-9-]{1,36}", place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        likes = record.get("liked_by", record.get("likes", []))
        if not isinstance(likes, list):
            likes = []
        liked = current_user not in likes
        if liked:
            likes.append(current_user)
        else:
            likes.remove(current_user)
        record["liked_by"] = likes
        record["likes"] = len(likes)
        write_json_file(f"places/{place_id}.json", record)
        index_data = read_json_file("places_index.json") or {"places": []}
        for item in index_data.get("places", []):
            if item.get("id") == place_id:
                item["likes"] = record["likes"]
                break
        write_json_file("places_index.json", index_data)
        return {"liked": liked, "likes": len(likes)}

def _get_user_record(username: str) -> dict:
    user = read_json_file(f"users/{username}.json")
    if not user:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")
    user.setdefault("friends", [])
    user.setdefault("incoming_requests", [])
    return user

@app.get("/api/friends")
def get_friends(current_user: str = Depends(get_current_user)):
    user = _get_user_record(current_user)
    return {"friends": user["friends"], "incoming_requests": user["incoming_requests"]}

@app.post("/api/friends/request")
def request_friend(req: FriendRequest, current_user: str = Depends(get_current_user)):
    target_name = req.username.strip().lower()
    if target_name == current_user.lower():
        raise HTTPException(status_code=400, detail="Не можна додати себе в друзі")
    with storage_lock:
        sender = _get_user_record(current_user)
        target = _get_user_record(target_name)
        if target_name in sender["friends"]:
            raise HTTPException(status_code=409, detail="Цей користувач уже у друзях")
        if current_user in target["incoming_requests"]:
            return {"status": "already_requested"}
        if target_name in sender["incoming_requests"]:
            raise HTTPException(status_code=409, detail="Прийміть вхідний запит від цього користувача")
        if len(target["incoming_requests"]) >= 100:
            raise HTTPException(status_code=429, detail="У користувача забагато запитів")
        target["incoming_requests"].append(current_user)
        write_json_file(f"users/{target_name}.json", target)
        return {"status": "requested"}

@app.post("/api/friends/accept")
def accept_friend(req: FriendRequest, current_user: str = Depends(get_current_user)):
    other_name = req.username.strip().lower()
    if other_name == current_user.lower():
        raise HTTPException(status_code=400, detail="Не можна додати себе в друзі")
    with storage_lock:
        user = _get_user_record(current_user)
        other = _get_user_record(other_name)
        if other_name not in user["incoming_requests"]:
            raise HTTPException(status_code=404, detail="Запит у друзі не знайдено")
        user["incoming_requests"].remove(other_name)
        if other_name not in user["friends"]:
            user["friends"].append(other_name)
        if current_user not in other["friends"]:
            other["friends"].append(current_user)
        other["incoming_requests"] = [name for name in other["incoming_requests"] if name != current_user]
        write_json_file(f"users/{current_user}.json", user)
        write_json_file(f"users/{other_name}.json", other)
        return {"status": "accepted"}

@app.post("/api/friends/remove")
def remove_friend(req: FriendRequest, current_user: str = Depends(get_current_user)):
    other_name = req.username.strip().lower()
    if other_name == current_user.lower():
        raise HTTPException(status_code=400, detail="Не можна видалити себе з друзів")
    with storage_lock:
        user = _get_user_record(current_user)
        other = _get_user_record(other_name)
        user["friends"] = [name for name in user["friends"] if name != other_name]
        other["friends"] = [name for name in other["friends"] if name != current_user]
        user["incoming_requests"] = [name for name in user["incoming_requests"] if name != other_name]
        other["incoming_requests"] = [name for name in other["incoming_requests"] if name != current_user]
        write_json_file(f"users/{current_user}.json", user)
        write_json_file(f"users/{other_name}.json", other)
        return {"status": "removed"}

@app.get("/api/places/{place_id}")
def get_place(place_id: str):
    if not re.fullmatch(r"[a-fA-F0-9-]{1,36}", place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    place_key = f"places/{place_id}.json"
    place_record = read_json_file(place_key)
    if not place_record:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    result = dict(place_record)
    liked_by = result.pop("liked_by", result.get("likes", []))
    result["likes"] = len(liked_by) if isinstance(liked_by, list) else int(result.get("likes", 0))
    return result

@app.get("/api/places/{place_id}/thumbnail")
def get_place_thumbnail(place_id: str):
    place_record = read_json_file(f"places/{place_id}.json")
    if not place_record or not place_record.get("thumbnail_png"):
        raise HTTPException(status_code=404, detail="Обкладинку плейса не знайдено")
    try:
        import base64
        image = base64.b64decode(place_record["thumbnail_png"], validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Некоректний формат обкладинки")
    if len(image) > 1_000_000 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=422, detail="Некоректний PNG-файл")
    from fastapi.responses import Response
    return Response(content=image, media_type="image/png")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting server with Anti-Cheat & Bot Protection on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
