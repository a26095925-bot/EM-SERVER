import os
import json
import os
import math
import re
import uuid
import time
import datetime
import asyncio
import base64
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException, Header, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import bcrypt
import jwt
import uvicorn

# Configuration
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "4cf18f6b81f018297ba7018547ad6005")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "aient")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
if len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("JWT_SECRET must be configured with at least 32 bytes of random data")
JWT_ALGORITHM = "HS256"
OWNER_USERNAMES = {name.strip().lower() for name in os.getenv("OWNER_USERNAMES", "mrkotik,arsen2014a").split(",") if name.strip()}
ADMIN_USERNAMES = {name.strip().lower() for name in os.getenv("ADMIN_USERNAMES", "").split(",") if name.strip()}
PLACE_VISITS: Dict[str, int] = {}

app = FastAPI(title="Roblox-Style Game Backend", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    if len(RATE_LIMIT_STORE) > 10000:
        for stale_key, values in list(RATE_LIMIT_STORE.items()):
            if not values or now - values[-1] >= window_sec:
                RATE_LIMIT_STORE.pop(stale_key, None)

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

def local_storage_path(path_key: str) -> str:
    root = os.path.realpath(LOCAL_STORAGE_DIR)
    candidate = os.path.realpath(os.path.join(root, path_key))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("Storage key escapes storage root")
    return candidate

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
        full_path = local_storage_path(path_key)
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
        full_path = local_storage_path(path_key)
        if not os.path.exists(full_path):
            return None
        with open(full_path, "r", encoding="utf-8") as f:
            return json.load(f)

def delete_json_file(path_key: str):
    if s3_client:
        s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=path_key)
        return
    full_path = local_storage_path(path_key)
    if os.path.exists(full_path):
        os.remove(full_path)

def admin_assignment_state() -> tuple[set[str], set[str]]:
    state = read_json_file("admin/admins.json") or {}
    saved = state.get("admins", []) if isinstance(state, dict) else []
    revoked = state.get("revoked", []) if isinstance(state, dict) else []
    if not isinstance(saved, list): saved = []
    if not isinstance(revoked, list): revoked = []
    valid = lambda values: {str(name).strip().lower() for name in values
                            if re.fullmatch(r"[a-z0-9_]{3,24}", str(name).strip().lower())}
    return valid(saved), valid(revoked)

def assigned_admin_usernames() -> set[str]:
    return admin_assignment_state()[0]

def is_owner(username: str) -> bool:
    return username.strip().lower() in OWNER_USERNAMES

def is_admin(username: str) -> bool:
    normalized = username.strip().lower()
    assigned, revoked = admin_assignment_state()
    return normalized in OWNER_USERNAMES or (normalized in ADMIN_USERNAMES and normalized not in revoked) or normalized in assigned

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

def require_admin(username: str = Depends(get_current_user)) -> str:
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Адмін-доступ не налаштовано для цього акаунта")
    return username.lower()

def require_owner(username: str = Depends(get_current_user)) -> str:
    if not is_owner(username):
        raise HTTPException(status_code=403, detail="Ця дія доступна лише власнику гри")
    return username.lower()

# Models
class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=24)
    password: str = Field(..., min_length=4, max_length=64)

    @field_validator("password")
    @classmethod
    def bcrypt_byte_limit(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password exceeds bcrypt's 72-byte limit")
        return value

class AuthResponse(BaseModel):
    status: str
    username: str
    token: str

class PlaceBlock(BaseModel):
    type: str = Field(..., pattern="^(solid|spawn|checkpoint|lava|trampoline|speedpad|damage|light|door)$")
    pos: List[float]
    size: List[float]
    color: str
    power: Optional[float] = Field(default=10.0, ge=0, le=100)
    damage_interval: Optional[float] = Field(default=1.0, ge=0.1, le=10.0)
    script: Optional[str] = None

    @field_validator("pos", "size")
    @classmethod
    def validate_vector(cls, values, info):
        if len(values) != 3 or any(not math.isfinite(v) or abs(v) > 10000 for v in values):
            raise ValueError(f"{info.field_name} must contain three finite values within +/-10000")
        if info.field_name == "size" and any(v <= 0 or v > 512 for v in values):
            raise ValueError("size values must be greater than 0 and at most 512")
        return values

    @field_validator("script")
    @classmethod
    def bound_script(cls, value):
        if value is not None and len(value) > 2000:
            raise ValueError("script is too long")
        return value

    @field_validator("color")
    @classmethod
    def validate_color(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("color must be a six-digit hex value")
        return value

class PlaceData(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    title: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = ""
    author: Optional[str] = ""
    spawn: Optional[List[float]] = Field(default_factory=lambda: [0, 2, 0])
    blocks: List[PlaceBlock] = Field(default_factory=list)
    thumbnail_png: Optional[str] = Field(default=None, max_length=1_400_000)

    @field_validator("blocks")
    @classmethod
    def bound_blocks(cls, values):
        if len(values) > 2000:
            raise ValueError("A place may contain at most 2000 blocks")
        return values

    @field_validator("spawn")
    @classmethod
    def validate_spawn(cls, values):
        if values is not None and (len(values) != 3 or any(not math.isfinite(v) or abs(v) > 10000 for v in values)):
            raise ValueError("spawn must contain three finite coordinates")
        return values

    @field_validator("title")
    @classmethod
    def validate_title(cls, value):
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value.strip()

    @field_validator("description")
    @classmethod
    def bound_description(cls, value):
        if value is not None and len(value) > 2000:
            raise ValueError("description is too long")
        return value

    @field_validator("thumbnail_png")
    @classmethod
    def validate_thumbnail(cls, value):
        if not value:
            return value
        try:
            image = base64.b64decode(value, validate=True)
        except Exception:
            raise ValueError("thumbnail_png must be valid base64")
        if len(image) > 1_000_000 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("thumbnail_png must be a valid PNG no larger than 1 MB")
        return value

class PlaceMetadata(BaseModel):
    title: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("title")
    @classmethod
    def validate_metadata_title(cls, value):
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value.strip()

class AdminManagementRequest(BaseModel):
    action: str = Field(..., pattern="^(grant|revoke)$")
    username: str = Field(..., min_length=3, max_length=24)

def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        raise HTTPException(status_code=400, detail="Логін: 3–24 символи a-z, 0-9 або _")
    return username

def validate_place_id(value: Optional[str]) -> Optional[str]:
    if value is not None and not re.fullmatch(r"[a-fA-F0-9-]{1,36}", value):
        raise HTTPException(status_code=400, detail="Некоректний ID плейса")
    return value

# Routes
@app.get("/")
def root():
    websocket_route = any(
        getattr(route, "path", None) == "/ws/{place_id}"
        for route in app.routes
    )
    return {
        "status": "online",
        "service": "Roblox-Style Game API with multiplayer presence",
        "storage": "Cloudflare R2" if s3_client else "Local Cache",
        "multiplayer_websocket": websocket_route,
        "websocket_path": "/ws/{place_id}" if websocket_route else None,
        "time": datetime.datetime.utcnow().isoformat()
    }

@app.post("/api/auth/register", response_model=AuthResponse)
def register(req: AuthRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip, "register", max_calls=4, window_sec=60)

    username = normalize_username(req.username)
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

    username = normalize_username(req.username)
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
    return {"status": "ok", "username": current_user, "is_admin": is_admin(current_user), "is_owner": is_owner(current_user)}

@app.get("/api/skins/me")
def get_my_skin(current_user: str = Depends(get_current_user)):
    skin = read_json_file(f"skins/{current_user}.json")
    if not skin:
        raise HTTPException(status_code=404, detail="Скін ще не збережено")
    return skin

@app.put("/api/skins/me")
def save_my_skin(payload: dict, current_user: str = Depends(get_current_user)):
    if len(json.dumps(payload, ensure_ascii=False)) > 16_384:
        raise HTTPException(status_code=413, detail="Дані скіна завеликі")
    allowed = {"headColorHex", "torsoColorHex", "leftArmColorHex", "rightArmColorHex",
               "leftLegColorHex", "rightLegColorHex", "hat", "face", "faceAsset"}
    if set(payload) - allowed:
        raise HTTPException(status_code=400, detail="Невідомі поля скіна")
    for key in ("headColorHex", "torsoColorHex", "leftArmColorHex", "rightArmColorHex", "leftLegColorHex", "rightLegColorHex"):
        value = payload.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise HTTPException(status_code=400, detail="Некоректний колір скіна")
    if payload.get("hat") not in {"None", "Cap", "Fedora", "TopHat", "Helmet"}:
        raise HTTPException(status_code=400, detail="Некоректний головний убір")
    if payload.get("face") not in {"Smile", "Sunglasses", "Angry", "Cyborg"}:
        raise HTTPException(status_code=400, detail="Некоректне стандартне обличчя")
    face_asset = payload.get("faceAsset", "")
    if not isinstance(face_asset, str) or (face_asset and not re.fullmatch(r"CLASSIC_FACES/[^/\\]{1,180}\.png", face_asset)):
        raise HTTPException(status_code=400, detail="Некоректне ім’я ресурсу обличчя")
    write_json_file(f"skins/{current_user}.json", payload)
    return {"status": "success", "storage": "r2" if s3_client else "local"}

@app.post("/api/places/save")
def save_place(place: PlaceData, current_user: str = Depends(get_current_user)):
    # Server-side validation against malicious / oversized payloads
    place_id = validate_place_id(place.id) or uuid.uuid4().hex
    place_key = f"places/{place_id}.json"
    
    existing = read_json_file(place_key)
    if existing and existing.get("author") != current_user:
        raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")
    
    now_str = datetime.datetime.utcnow().isoformat()
    place_record = {
        "id": place_id,
        "title": place.title,
        "description": place.description or "",
        "author": current_user,
        "created_at": existing.get("created_at") if existing else now_str,
        "updated_at": now_str,
        "spawn": place.spawn,
        "blocks": [b.dict() for b in place.blocks],
        "thumbnail_png": place.thumbnail_png
    }
    
    write_json_file(place_key, place_record)
    
    index_data = read_json_file("places_index.json") or {"places": []}
    places_list = [p for p in index_data.get("places", []) if p.get("id") != place_id]
    places_list.insert(0, {
        "id": place_id,
        "title": place.title,
        "author": current_user,
        "block_count": len(place.blocks),
        "has_thumbnail": bool(place.thumbnail_png),
        "updated_at": now_str
    })
    index_data["places"] = places_list[:100]
    write_json_file("places_index.json", index_data)
    
    storage_message = "Cloudflare R2" if s3_client else "локальному сховищі сервера"
    return {"status": "success", "id": place_id, "message": f"Плейс збережено у {storage_message}."}

@app.get("/api/places/list")
def list_places():
    index_data = read_json_file("places_index.json") or {"places": []}
    return index_data

@app.get("/api/places/mine")
def list_my_places(current_user: str = Depends(get_current_user)):
    index_data = read_json_file("places_index.json") or {"places": []}
    places = [item for item in index_data.get("places", [])
              if isinstance(item, dict) and str(item.get("author", "")).lower() == current_user.lower()]
    return {"places": places}

@app.patch("/api/places/{place_id}")
def update_place_metadata(place_id: str, metadata: PlaceMetadata, current_user: str = Depends(get_current_user)):
    validate_place_id(place_id)
    key = f"places/{place_id}.json"
    place = read_json_file(key)
    if not place:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    if str(place.get("author", "")).lower() != current_user.lower():
        raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")
    place["title"] = metadata.title
    if metadata.description is not None:
        place["description"] = metadata.description
    place["updated_at"] = datetime.datetime.utcnow().isoformat()
    write_json_file(key, place)
    index_data = read_json_file("places_index.json") or {"places": []}
    for item in index_data.get("places", []):
        if isinstance(item, dict) and item.get("id") == place_id:
            item["title"] = place["title"]
            item["updated_at"] = place["updated_at"]
            break
    write_json_file("places_index.json", index_data)
    return {"status": "success", "id": place_id, "title": place["title"]}

@app.delete("/api/places/{place_id}")
def delete_place(place_id: str, current_user: str = Depends(get_current_user)):
    validate_place_id(place_id)
    key = f"places/{place_id}.json"
    place = read_json_file(key)
    if not place:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    if str(place.get("author", "")).lower() != current_user.lower():
        raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")
    delete_json_file(key)
    index_data = read_json_file("places_index.json") or {"places": []}
    index_data["places"] = [item for item in index_data.get("places", [])
                             if not isinstance(item, dict) or item.get("id") != place_id]
    write_json_file("places_index.json", index_data)
    return {"status": "success", "id": place_id}

@app.get("/api/places/{place_id}")
def get_place(place_id: str):
    validate_place_id(place_id)
    place_key = f"places/{place_id}.json"
    place_record = read_json_file(place_key)
    if not place_record:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    PLACE_VISITS[place_id] = PLACE_VISITS.get(place_id, 0) + 1
    return place_record

# Ephemeral multiplayer sessions: players receive presence/positions only;
# world saving and collision remain separate from this real-time channel.
class MultiplayerRooms:
    def __init__(self):
        self.rooms: Dict[str, Dict[WebSocket, str]] = {}
        self.lock = asyncio.Lock()
        try: state = read_json_file("admin/moderation.json") or {}
        except Exception: state = {}
        self.banned_users: set[str] = set(state.get("banned", []))
        self.muted_users: set[str] = set(state.get("muted", []))

    async def join(self, room: str, socket: WebSocket, username: str):
        async with self.lock:
            self.rooms.setdefault(room, {})[socket] = username
            peers = list(self.rooms[room].values())
        await socket.send_json({"type": "roster", "players": peers})
        await self.broadcast(room, {"type": "joined", "username": username}, exclude=socket)

    async def leave(self, room: str, socket: WebSocket):
        async with self.lock:
            peers = self.rooms.get(room, {})
            username = peers.pop(socket, None)
            if not peers:
                self.rooms.pop(room, None)
        if username:
            await self.broadcast(room, {"type": "left", "username": username})

    async def broadcast(self, room: str, payload: dict, exclude: Optional[WebSocket] = None):
        async with self.lock:
            peers = list(self.rooms.get(room, {}).keys())
        for peer in peers:
            if peer is exclude:
                continue
            try:
                await peer.send_json(payload)
            except Exception:
                await self.leave(room, peer)

    async def online_users(self) -> List[str]:
        async with self.lock:
            return sorted({name for peers in self.rooms.values() for name in peers.values()})

    async def moderate(self, action: str, target: str, value=None):
        async with self.lock:
            sockets = [(room, ws) for room, peers in self.rooms.items() for ws, name in peers.items() if name == target]
            if action == "ban": self.banned_users.add(target)
            elif action == "unban": self.banned_users.discard(target)
            elif action == "mute": self.muted_users.add(target)
            elif action == "unmute": self.muted_users.discard(target)
            moderation_state = {"banned": sorted(self.banned_users), "muted": sorted(self.muted_users)}
        if action in {"ban", "unban", "mute", "unmute"}:
            try: write_json_file("admin/moderation.json", moderation_state)
            except Exception: pass
        if action in {"kick", "ban"}:
            for room, ws in sockets:
                try: await ws.close(code=4001, reason="Removed by administrator")
                except Exception: pass
                await self.leave(room, ws)
        elif action in {"head", "teleport"}:
            payload = {"type": "admin_effect", "action": action, "value": value}
            for _, ws in sockets:
                try: await ws.send_json(payload)
                except Exception: pass

class AdminAction(BaseModel):
    action: str = Field(..., max_length=16)
    target: str = Field(..., min_length=3, max_length=24)
    value: Optional[List[float] | float] = None

multiplayer_rooms = MultiplayerRooms()

@app.get("/api/dev/overview")
async def developer_overview(current_user: str = Depends(get_current_user)):
    index_data = read_json_file("places_index.json") or {"places": []}
    own_places = []
    for place in index_data.get("places", [])[:100]:
        if not isinstance(place, dict): continue
        if str(place.get("author", "")).lower() != current_user.lower(): continue
        item = dict(place)
        item["plays"] = PLACE_VISITS.get(str(item.get("id", "")), 0)
        own_places.append(item)
    own_places.sort(key=lambda item: (item.get("plays", 0), item.get("updated_at", "")), reverse=True)
    return {"username": current_user, "my_places": own_places,
            "plays_scope": "відвідування власних плейсів з моменту запуску сервера"}

@app.get("/api/admin/overview")
async def admin_overview(current_user: str = Depends(require_admin)):
    index_data = read_json_file("places_index.json") or {"places": []}
    places = []
    for place in index_data.get("places", [])[:100]:
        if not isinstance(place, dict): continue
        item = dict(place)
        item["plays"] = PLACE_VISITS.get(str(item.get("id", "")), 0)
        places.append(item)
    places.sort(key=lambda item: (item.get("plays", 0), item.get("updated_at", "")), reverse=True)
    return {"username": current_user, "is_owner": is_owner(current_user), "places": places,
            "players": await multiplayer_rooms.online_users(),
            "muted": sorted(multiplayer_rooms.muted_users), "banned": sorted(multiplayer_rooms.banned_users),
            "plays_scope": "відвідування з моменту запуску сервера"}

@app.get("/api/admin/admins")
def get_admins(current_user: str = Depends(require_owner)):
    assigned, revoked = admin_assignment_state()
    return {"owners": sorted(OWNER_USERNAMES), "admins": sorted(OWNER_USERNAMES | assigned | (ADMIN_USERNAMES - revoked))}

@app.post("/api/admin/admins")
def update_admin(request: AdminManagementRequest, current_user: str = Depends(require_owner)):
    username = normalize_username(request.username)
    assigned, revoked = admin_assignment_state()
    if request.action == "grant":
        if username in OWNER_USERNAMES:
            return {"status": "success", "action": "already_owner", "username": username}
        if not read_json_file(f"users/{username}.json"):
            raise HTTPException(status_code=404, detail="Користувача з таким логіном не знайдено")
        assigned.add(username)
        revoked.discard(username)
    else:
        if username in OWNER_USERNAMES:
            raise HTTPException(status_code=400, detail="Власника гри не можна зняти адміністратором")
        if username not in assigned and username not in ADMIN_USERNAMES:
            raise HTTPException(status_code=404, detail="Цього адміністратора немає у списку")
        assigned.discard(username)
        revoked.add(username)
    write_json_file("admin/admins.json", {"admins": sorted(assigned), "revoked": sorted(revoked)})
    return {"status": "success", "action": request.action, "username": username,
            "admins": sorted(OWNER_USERNAMES | assigned | (ADMIN_USERNAMES - revoked))}

@app.post("/api/admin/action")
async def admin_action(request: AdminAction, current_user: str = Depends(require_admin)):
    target = request.target.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", target):
        raise HTTPException(status_code=400, detail="Некоректне ім’я гравця")
    if target not in {u for room in multiplayer_rooms.rooms.values() for u in room.values()} and request.action not in {"unban", "unmute"}:
        raise HTTPException(status_code=404, detail="Гравець не в мережі")
    allowed = {"kick", "ban", "unban", "mute", "unmute", "head", "teleport"}
    if request.action not in allowed:
        raise HTTPException(status_code=400, detail="Невідома команда адміністратора")
    if request.action == "head":
        if not isinstance(request.value, (int, float)) or not math.isfinite(float(request.value)):
            raise HTTPException(status_code=400, detail="Некоректний розмір голови")
        value = max(1.0, min(3.0, float(request.value)))
    elif request.action == "teleport":
        if not isinstance(request.value, list) or len(request.value) != 3:
            raise HTTPException(status_code=400, detail="Для телепортації потрібні координати X, Y, Z")
        try: value = [float(v) for v in request.value]
        except (ValueError, TypeError): raise HTTPException(status_code=400, detail="Некоректні координати")
        if any(not math.isfinite(v) or abs(v) > 10000 for v in value):
            raise HTTPException(status_code=400, detail="Координати за межами плейса")
    else:
        value = None
    await multiplayer_rooms.moderate(request.action, target, value)
    return {"status": "success", "action": request.action, "target": target}

@app.websocket("/ws/{place_id}")
async def multiplayer_socket(websocket: WebSocket, place_id: str, token: str):
    try:
        validate_place_id(place_id)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise ValueError("missing subject")
    except Exception:
        await websocket.close(code=1008, reason="Invalid session or place")
        return

    if username.lower() in multiplayer_rooms.banned_users:
        await websocket.close(code=1008, reason="Account is banned from multiplayer")
        return
    await websocket.accept()
    room = place_id
    await multiplayer_rooms.join(room, websocket, username)
    last_chat_at = 0.0
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            if message.get("type") == "chat":
                now = time.monotonic()
                text = message.get("message")
                if now - last_chat_at < 0.6 or not isinstance(text, str):
                    continue
                text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()[:220]
                if not text:
                    continue
                if username.lower() in multiplayer_rooms.muted_users:
                    await websocket.send_json({"type": "notice", "message": "Ваш чат тимчасово вимкнено модератором."})
                    continue
                last_chat_at = now
                await multiplayer_rooms.broadcast(room, {"type": "chat", "username": username, "message": text}, exclude=websocket)
                continue
            if message.get("type") != "state":
                continue
            coords = message.get("position")
            if not isinstance(coords, list) or len(coords) != 3:
                continue
            try:
                coords = [float(v) for v in coords]
                if any(not math.isfinite(v) or abs(v) > 10000 for v in coords):
                    continue
            except (TypeError, ValueError):
                continue
            skin = message.get("skin", {})
            try:
                head_scale = float(message.get("head_scale", 1.0))
                if not math.isfinite(head_scale): head_scale = 1.0
                head_scale = max(1.0, min(3.0, head_scale))
            except (TypeError, ValueError):
                head_scale = 1.0
            if not isinstance(skin, dict) or len(json.dumps(skin, ensure_ascii=False)) > 4096:
                skin = {}
            else:
                allowed_skin_fields = {"headColorHex", "torsoColorHex", "leftArmColorHex", "rightArmColorHex",
                                       "leftLegColorHex", "rightLegColorHex", "hat", "face", "faceAsset"}
                skin = {key: value for key, value in skin.items() if key in allowed_skin_fields}
                for color_key in ("headColorHex", "torsoColorHex", "leftArmColorHex", "rightArmColorHex", "leftLegColorHex", "rightLegColorHex"):
                    if not isinstance(skin.get(color_key), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", skin[color_key]):
                        skin.pop(color_key, None)
                if not isinstance(skin.get("faceAsset", ""), str) or (skin.get("faceAsset") and not re.fullmatch(r"CLASSIC_FACES/[^/\\]{1,180}\.png", skin["faceAsset"])):
                    skin.pop("faceAsset", None)
            await multiplayer_rooms.broadcast(room, {
                "type": "state", "username": username,
                "position": coords,
                "yaw": float(message.get("yaw", 0.0)) % 360.0,
                "head_scale": head_scale,
                "skin": skin
            }, exclude=websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await multiplayer_rooms.leave(room, websocket)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting server with Anti-Cheat & Bot Protection on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)

