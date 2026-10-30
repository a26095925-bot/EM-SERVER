import os
import json
import uuid
import time
import math
import re
import secrets
import datetime
import asyncio
import threading
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import (
    FastAPI,
    HTTPException,
    Header,
    Depends,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
    File,
    Form,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator
import bcrypt
import jwt
import uvicorn

try:  # optional convenience for local development
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

# Configuration
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage"))).resolve()
MODELS_DIR = STORAGE_DIR / "models"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "4cf18f6b81f018297ba7018547ad6005")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "aient")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")

APP_VERSION = "2.0.0"
STARTED_AT = time.time()
SERVER_INSTANCE_ID = uuid.uuid4().hex
MAX_MODEL_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 15 * 1024 * 1024
MODEL_EXTENSIONS = {".obj": "model/obj", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json"}
ALLOWED_MODEL_CATEGORIES = ("Shapes", "Props", "Nature", "Gameplay", "Custom")
PLACE_ID_PATTERN = r"[a-fA-F0-9-]{1,36}"


def _load_jwt_secret() -> str:
    """Use JWT_SECRET when provided, otherwise persist a random local secret.

    The repository used to ship a hard-coded signing key.  For local play we
    generate a random secret once and keep it in storage; production
    deployments should always set JWT_SECRET explicitly.
    """
    configured = os.getenv("JWT_SECRET", "").strip()
    if len(configured.encode("utf-8")) >= 32:
        return configured
    if configured:
        print("WARNING: JWT_SECRET is shorter than 32 bytes, generating a strong local secret instead.")
    secret_file = STORAGE_DIR / ".jwt_secret"
    try:
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if len(stored.encode("utf-8")) >= 32:
                return stored
        generated = secrets.token_urlsafe(48)
        secret_file.write_text(generated, encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        print("WARNING: JWT_SECRET was not configured. Generated storage/.jwt_secret for local use.")
        return generated
    except OSError:
        generated = secrets.token_urlsafe(48)
        print("WARNING: JWT_SECRET was not configured and storage is not writable; using an ephemeral secret.")
        return generated


JWT_SECRET = _load_jwt_secret()
JWT_ALGORITHM = "HS256"

ADMIN_USERS = {name.strip().lower() for name in os.getenv("ADMIN_USERS", "").split(",") if name.strip()}
# Without an explicit admin list any signed-in account can open the developer
# menu.  This keeps the local prototype usable out of the box; set
# ALLOW_OPEN_DEV=0 together with ADMIN_USERS for a public deployment.
ALLOW_OPEN_DEV = os.getenv("ALLOW_OPEN_DEV", "1").strip().lower() not in {"0", "false", "no"}

app = FastAPI(title="Roblox-Style Game Backend", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Запит занадто великий"})
        except ValueError:
            pass
    return await call_next(request)

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
    # Accept both spellings used by different client generations.
    if "rot" not in block and isinstance(block.get("rotation"), list):
        block["rot"] = block["rotation"]
    if "rotation" not in block and isinstance(block.get("rot"), list):
        block["rotation"] = block["rot"]
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
_rate_lock = threading.Lock()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str, action: str, max_calls: int, window_sec: int = 60):
    """Protects against bot spam and brute force attacks."""
    now = time.time()
    key = f"{ip}:{action}"
    with _rate_lock:
        timestamps = [t for t in RATE_LIMIT_STORE.get(key, []) if now - t < window_sec]
        if len(timestamps) >= max_calls:
            retry = max(1, int(window_sec - (now - timestamps[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Забагато запитів від вашого IP. Спробуйте через {retry} с.",
            )
        timestamps.append(now)
        RATE_LIMIT_STORE[key] = timestamps
        if len(RATE_LIMIT_STORE) > 5000:
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

storage_lock = threading.RLock()

def storage_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with storage_lock:
            return function(*args, **kwargs)
    return wrapped


def local_storage_path(path_key: str) -> Path:
    root = STORAGE_DIR.resolve()
    candidate = (root / path_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Storage key escapes storage root") from exc
    return candidate


def write_json_file(path_key: str, data: dict):
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    if s3_client:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=path_key,
            Body=payload,
            ContentType="application/json",
        )
    # Keep a local mirror so the server still works while R2 is unreachable.
    full_path = local_storage_path(path_key)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(payload)


def read_json_file(path_key: str) -> Optional[dict]:
    if s3_client:
        try:
            resp = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=path_key)
            content = resp["Body"].read().decode("utf-8")
            return json.loads(content)
        except Exception:
            pass  # fall back to the local mirror
    full_path = local_storage_path(path_key)
    if not full_path.exists():
        return None
    try:
        with full_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def delete_storage_file(path_key: str) -> None:
    if s3_client:
        try:
            s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=path_key)
        except Exception:
            pass
    full_path = local_storage_path(path_key)
    if full_path.exists():
        try:
            full_path.unlink()
        except OSError:
            pass


def write_model_bytes(filename: str, data: bytes) -> None:
    """Model files always land on the server disk, then mirror to R2."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    (MODELS_DIR / filename).write_bytes(data)
    if s3_client:
        try:
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=f"models/{filename}",
                Body=data,
                ContentType=MODEL_EXTENSIONS.get(Path(filename).suffix.lower(), "application/octet-stream"),
            )
        except Exception as exc:
            print("R2 model upload failed:", exc)


def read_model_bytes(filename: str) -> Optional[bytes]:
    local_path = MODELS_DIR / filename
    if local_path.exists():
        return local_path.read_bytes()
    if s3_client:
        try:
            resp = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=f"models/{filename}")
            return resp["Body"].read()
        except Exception:
            return None
    return None


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_jwt_token(username: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": username,
        "exp": now + datetime.timedelta(days=30),
        "iat": now,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(authorization: Optional[str], required: bool) -> Optional[str]:
    if not authorization:
        if required:
            raise HTTPException(status_code=401, detail="Missing Authorization Header")
        return None
    try:
        token = authorization.replace("Bearer ", "").strip()
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token subject")
        return username
    except jwt.PyJWTError:
        if required:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return None


def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    username = _decode_token(authorization, required=True)
    assert username is not None
    return username


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[str]:
    return _decode_token(authorization, required=False)


def is_admin(username: str) -> bool:
    """Destructive actions on other people's content need an explicit admin."""
    return username in ADMIN_USERS


def has_dev_access(username: str) -> bool:
    """Developer menu visibility.  Open in local mode when no admin list is set."""
    return is_admin(username) or (not ADMIN_USERS and ALLOW_OPEN_DEV)


def require_dev_access(username: str) -> None:
    if not has_dev_access(username):
        raise HTTPException(status_code=403, detail="Потрібен доступ розробника (ADMIN_USERS)")


def require_username(value: str) -> str:
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        raise HTTPException(status_code=400, detail="Логін: 3–24 символи a-z, 0-9 або _")
    return username


# Models
class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=24)
    password: str = Field(..., min_length=4, max_length=64)

    @field_validator("password")
    @classmethod
    def bcrypt_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password exceeds bcrypt's 72-byte limit")
        return value

class AuthResponse(BaseModel):
    status: str
    username: str
    token: str

class EventRequest(BaseModel):
    event_id: str = Field(..., min_length=32, max_length=36, pattern=r"^[a-fA-F0-9-]{32,36}$")

class FriendRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=24, pattern=r"^[A-Za-z0-9_]+$")

class PlaceBlock(BaseModel):
    """A workspace part.

    `extra="allow"` keeps every future client field, while the fields below
    are validated so teleport pads and Geometry-Dash style triggers survive a
    save/load round-trip instead of being silently dropped.
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(
        default="solid",
        pattern="^(solid|spawn|checkpoint|lava|trampoline|speedpad|light|door|teleport|trigger)$",
    )
    name: str = Field(default="Part", max_length=64)
    shape: str = Field(default="cube", pattern="^(cube|cylinder|wedge|sphere)$")
    pos: List[FiniteFloat] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    size: List[FiniteFloat] = Field(default_factory=lambda: [1, 1, 1], min_length=3, max_length=3)
    color: str = "#3B82F6"
    rotation: List[FiniteFloat] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    power: Optional[FiniteFloat] = Field(default=10.0, ge=-1000, le=1000)
    anchored: bool = True
    cancollide: bool = True
    model_path: Optional[str] = Field(default=None, max_length=80)
    texture_path: Optional[str] = Field(default=None, max_length=80)
    script: Optional[str] = Field(default=None, max_length=8000)
    link_id: Optional[str] = Field(default="", max_length=32)
    trigger_mode: str = Field(default="speed", pattern="^(speed|gravity|jump|kill|checkpoint|boost|color)$")
    trigger_value: FiniteFloat = Field(default=1.0, ge=-1000, le=1000)

    @field_validator("pos", "size")
    @classmethod
    def validate_vector(cls, values, info):
        if len(values) != 3 or any(not math.isfinite(float(v)) or abs(float(v)) > 10000 for v in values):
            raise ValueError(f"{info.field_name} must contain three finite values within +/-10000")
        if info.field_name == "size" and any(float(v) <= 0 or float(v) > 512 for v in values):
            raise ValueError("size values must be greater than 0 and at most 512")
        return values

    @field_validator("rotation")
    @classmethod
    def validate_rotation(cls, values):
        if len(values) != 3 or any(not math.isfinite(float(v)) or abs(float(v)) > 36000 for v in values):
            raise ValueError("rotation must contain three finite values")
        return values

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("color must be a six-digit hex value")
        return value

    @field_validator("model_path", "texture_path")
    @classmethod
    def validate_asset_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if Path(value).name != value or "/" in value or "\\" in value:
            raise ValueError("asset path must be a plain file name")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", value):
            raise ValueError("asset path contains unsupported characters")
        # Texture fields may also hold preset names such as "Checkerboard".
        return value

    @field_validator("link_id")
    @classmethod
    def validate_link_id(cls, value: Optional[str]) -> str:
        value = (value or "").strip()
        if not re.fullmatch(r"[\w .\-]{0,32}", value, re.UNICODE):
            raise ValueError("link_id may contain letters, digits, spaces, dots and dashes")
        return value

class PlaceLayerData(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    visible: bool = True

class PlaceData(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = Field(default=None, pattern=r"^[a-fA-F0-9-]{1,36}$")
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

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value.strip()

    @field_validator("description")
    @classmethod
    def bound_description(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value) > 2000:
            raise ValueError("description is too long")
        return value



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


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load_place_index() -> dict:
    data = read_json_file("places_index.json") or {"places": []}
    places = []
    for entry in data.get("places", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        entry.setdefault("title", "Untitled")
        entry.setdefault("author", "unknown")
        entry.setdefault("block_count", 0)
        entry.setdefault("updated_at", "")
        entry["has_thumbnail"] = bool(entry.get("has_thumbnail"))
        entry["likes"] = int(entry.get("likes") or 0)
        entry["plays"] = int(entry.get("plays") or 0)
        entry["views"] = int(entry.get("views") or 0)
        entry["featured"] = bool(entry.get("featured"))
        places.append(entry)
    return {"places": places}


def save_place_index(index: dict) -> None:
    write_json_file("places_index.json", index)


def upsert_place_index(record: dict) -> None:
    index = load_place_index()
    places = [p for p in index["places"] if p.get("id") != record.get("id")]
    places.insert(0, {
        "id": record.get("id"),
        "title": record.get("title", "Untitled"),
        "author": record.get("author", "unknown"),
        "block_count": len(record.get("blocks") or []),
        "has_thumbnail": bool(record.get("thumbnail_png")),
        "plays": int(record.get("plays") or 0),
        "views": int(record.get("views") or 0),
        "likes": int(record.get("likes") if isinstance(record.get("likes"), int) else len(record.get("liked_by") or [])),
        "liked_by": list(record.get("liked_by") or []),
        "featured": bool(record.get("featured")),
        "updated_at": record.get("updated_at", now_iso()),
    })
    places.sort(key=lambda p: (not p.get("featured"), -(p.get("plays") or 0)))
    index["places"] = places[:200]
    save_place_index(index)


def public_place_summary(entry: dict, viewer: Optional[str]) -> dict:
    likes = int(entry.get("likes") or 0)
    return {
        "id": entry.get("id"),
        "title": entry.get("title", "Untitled"),
        "author": entry.get("author", "unknown"),
        "block_count": int(entry.get("block_count") or 0),
        "updated_at": entry.get("updated_at", ""),
        "likes": likes,
        "plays": int(entry.get("plays") or 0),
        "views": int(entry.get("views") or 0),
        "featured": bool(entry.get("featured")),
        "has_thumbnail": bool(entry.get("has_thumbnail")),
    }


def public_place_record(record: dict, viewer: Optional[str]) -> dict:
    result = dict(record)
    liked_by = result.pop("liked_by", [])
    if not isinstance(liked_by, list):
        liked_by = []
    result["likes"] = int(record.get("likes") if isinstance(record.get("likes"), int) else len(liked_by))
    result["views"] = int(record.get("views") or 0)
    result["plays"] = int(record.get("plays") or 0)
    result["featured"] = bool(record.get("featured"))
    result["liked_by_me"] = bool(viewer and viewer in liked_by)
    return result


VIEW_HITS: Dict[str, float] = {}


def track_view(place_id: str, ip: str) -> bool:
    """Only one counted view per IP/place per 10 minutes."""
    key = f"{place_id}:{ip}"
    now = time.time()
    with _rate_lock:
        last = VIEW_HITS.get(key, 0.0)
        if now - last < 600:
            return False
        VIEW_HITS[key] = now
        if len(VIEW_HITS) > 20000:
            for stale_key, value in list(VIEW_HITS.items()):
                if now - value > 3600:
                    VIEW_HITS.pop(stale_key, None)
    return True


def live_player_count() -> int:
    return sum(len(room.clients) for room in live_rooms.values())


def list_usernames() -> List[str]:
    names = set()
    users_dir = STORAGE_DIR / "users"
    if users_dir.exists():
        names.update(path.stem for path in users_dir.glob("*.json"))
    if s3_client:
        try:
            response = s3_client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix="users/")
            for item in response.get("Contents", []):
                key = item.get("Key", "")
                if key.startswith("users/") and key.endswith(".json"):
                    names.add(key[len("users/"):-len(".json")])
        except Exception:
            pass
    return sorted(name for name in names if re.fullmatch(r"[a-z0-9_]{3,24}", name))


# ---------------------------------------------------------------------------
# Built-in toolbox models (generated once, stored on the server disk)
# ---------------------------------------------------------------------------
class ObjBuilder:
    def __init__(self) -> None:
        self.vertices: List[tuple[float, float, float]] = []
        self.faces: List[tuple[int, ...]] = []

    def vert(self, x: float, y: float, z: float) -> int:
        self.vertices.append((x, y, z))
        return len(self.vertices)

    def face(self, *indices: int) -> None:
        self.faces.append(tuple(indices))

    def box(self, cx: float, cy: float, cz: float, w: float, h: float, d: float) -> None:
        hx, hy, hz = w / 2, h / 2, d / 2
        b = len(self.vertices)
        for sx, sy, sz in (
            (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
            (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
        ):
            self.vert(cx + sx, cy + sy, cz + sz)
        # outward winding (counter-clockwise seen from outside)
        self.face(b + 5, b + 6, b + 7, b + 8)  # +Z
        self.face(b + 2, b + 1, b + 4, b + 3)  # -Z
        self.face(b + 1, b + 5, b + 8, b + 4)  # -X
        self.face(b + 6, b + 2, b + 3, b + 7)  # +X
        self.face(b + 8, b + 7, b + 3, b + 4)  # +Y
        self.face(b + 1, b + 2, b + 6, b + 5)  # -Y

    def cylinder(self, cx: float, cy: float, cz: float, radius: float, height: float, sides: int = 16) -> None:
        import math as _math

        half = height / 2
        bottom: List[int] = []
        top: List[int] = []
        for i in range(sides):
            angle = 2 * _math.pi * i / sides
            x = cx + radius * _math.cos(angle)
            z = cz + radius * _math.sin(angle)
            bottom.append(self.vert(x, cy - half, z))
            top.append(self.vert(x, cy + half, z))
        for i in range(sides):
            n = (i + 1) % sides
            self.face(bottom[i], top[i], top[n], bottom[n])
        bottom_center = self.vert(cx, cy - half, cz)
        top_center = self.vert(cx, cy + half, cz)
        for i in range(sides):
            n = (i + 1) % sides
            self.face(bottom_center, bottom[i], bottom[n])
            self.face(top_center, top[n], top[i])

    def pyramid(self, cx: float, cy: float, cz: float, radius: float, height: float, sides: int = 4) -> None:
        import math as _math

        base: List[int] = []
        for i in range(sides):
            angle = 2 * _math.pi * i / sides
            base.append(self.vert(cx + radius * _math.cos(angle), cy, cz + radius * _math.sin(angle)))
        apex = self.vert(cx, cy + height, cz)
        for i in range(sides):
            n = (i + 1) % sides
            self.face(base[i], apex, base[n])
        base_center = self.vert(cx, cy, cz)
        for i in range(sides):
            n = (i + 1) % sides
            self.face(base_center, base[i], base[n])

    def star_prism(self, cx: float, cy: float, cz: float, radius: float, height: float, points: int = 5) -> None:
        import math as _math

        outer = radius
        inner = radius * 0.45
        count = points * 2
        bottom: List[int] = []
        top: List[int] = []
        for i in range(count):
            angle = -_math.pi / 2 + _math.pi * i / points
            r = outer if i % 2 == 0 else inner
            x = cx + r * _math.cos(angle)
            z = cz + r * _math.sin(angle)
            bottom.append(self.vert(x, cy - height / 2, z))
            top.append(self.vert(x, cy + height / 2, z))
        for i in range(count):
            n = (i + 1) % count
            self.face(bottom[i], top[i], top[n], bottom[n])
        bottom_center = self.vert(cx, cy - height / 2, cz)
        top_center = self.vert(cx, cy + height / 2, cz)
        for i in range(count):
            n = (i + 1) % count
            self.face(bottom_center, bottom[i], bottom[n])
            self.face(top_center, top[n], top[i])

    def icosahedron(self, cx: float, cy: float, cz: float, radius: float) -> None:
        import math as _math

        t = (1 + _math.sqrt(5)) / 2
        raw = [
            (-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0),
            (0, -1, t), (0, 1, t), (0, -1, -t), (0, 1, -t),
            (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1),
        ]
        scale = radius / _math.sqrt(1 + t * t)
        base = len(self.vertices)
        for i, (x, y, z) in enumerate(raw):
            wobble = 1.0 + 0.14 * _math.sin(i * 2.399)
            self.vert(cx + x * scale * wobble, cy + y * scale * wobble, cz + z * scale * wobble)
        faces = [
            (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
            (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
            (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
            (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
        ]
        for a, b, c in faces:
            self.face(base + a + 1, base + b + 1, base + c + 1)

    def build(self, name: str) -> str:
        lines = [f"# Roblox-style toolbox model: {name}", f"o {name}"]
        lines += [f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in self.vertices]
        lines += ["f " + " ".join(str(i) for i in face) for face in self.faces]
        return "\n".join(lines) + "\n"


def _model_crystal() -> str:
    b = ObjBuilder()
    b.icosahedron(0.0, 0.55, 0.0, 0.55)
    return b.build("crystal")


def _model_spike() -> str:
    b = ObjBuilder()
    b.pyramid(0.0, 0.0, 0.0, 0.42, 1.35, 4)
    return b.build("spike")


def _model_pyramid() -> str:
    b = ObjBuilder()
    b.pyramid(0.0, 0.0, 0.0, 0.7, 1.2, 4)
    return b.build("pyramid")


def _model_crate() -> str:
    b = ObjBuilder()
    b.box(0.0, 0.5, 0.0, 1.0, 1.0, 1.0)
    b.box(0.0, 0.5, 0.51, 0.9, 0.12, 0.02)
    b.box(0.0, 0.5, -0.51, 0.9, 0.12, 0.02)
    return b.build("crate")


def _model_barrel() -> str:
    b = ObjBuilder()
    b.cylinder(0.0, 0.45, 0.0, 0.42, 0.9, 18)
    b.cylinder(0.0, 0.15, 0.0, 0.45, 0.06, 18)
    b.cylinder(0.0, 0.75, 0.0, 0.45, 0.06, 18)
    return b.build("barrel")


def _model_coin() -> str:
    b = ObjBuilder()
    b.cylinder(0.0, 0.06, 0.0, 0.5, 0.12, 24)
    return b.build("coin")


def _model_star() -> str:
    b = ObjBuilder()
    b.star_prism(0.0, 0.12, 0.0, 0.55, 0.24, 5)
    return b.build("star")


def _model_tree() -> str:
    b = ObjBuilder()
    b.cylinder(0.0, 0.7, 0.0, 0.16, 1.4, 10)
    b.pyramid(0.0, 1.0, 0.0, 0.95, 1.1, 8)
    b.pyramid(0.0, 1.75, 0.0, 0.7, 0.95, 8)
    b.pyramid(0.0, 2.45, 0.0, 0.45, 0.8, 8)
    return b.build("tree")


def _model_rock() -> str:
    b = ObjBuilder()
    b.icosahedron(0.0, 0.5, 0.0, 0.62)
    return b.build("rock")


def _model_mushroom() -> str:
    b = ObjBuilder()
    b.cylinder(0.0, 0.32, 0.0, 0.16, 0.64, 12)
    b.cylinder(0.0, 0.72, 0.0, 0.52, 0.22, 18)
    b.pyramid(0.0, 0.83, 0.0, 0.52, 0.28, 18)
    return b.build("mushroom")


def _model_arch() -> str:
    b = ObjBuilder()
    b.box(-0.55, 0.8, 0.0, 0.22, 1.6, 0.35)
    b.box(0.55, 0.8, 0.0, 0.22, 1.6, 0.35)
    b.box(0.0, 1.68, 0.0, 1.32, 0.22, 0.35)
    return b.build("arch")


def _model_sword() -> str:
    b = ObjBuilder()
    b.box(0.0, 1.05, 0.0, 0.13, 1.3, 0.05)   # blade
    b.box(0.0, 1.72, 0.0, 0.09, 0.22, 0.05)  # tip
    b.box(0.0, 0.38, 0.0, 0.62, 0.09, 0.12)  # guard
    b.box(0.0, 0.05, 0.0, 0.1, 0.55, 0.1)    # handle
    b.box(0.0, -0.24, 0.0, 0.18, 0.09, 0.18) # pommel
    return b.build("sword")


def _model_checkpoint_flag() -> str:
    b = ObjBuilder()
    b.cylinder(0.0, 0.9, 0.0, 0.04, 1.8, 8)
    b.box(0.28, 1.55, 0.0, 0.52, 0.34, 0.03)
    b.cylinder(0.0, 0.05, 0.0, 0.28, 0.1, 12)
    return b.build("checkpoint_flag")


BUILTIN_MODEL_BUILDERS = {
    "crystal.obj": ("РљСЂРёСЃС‚Р°Р»", "Shapes", _model_crystal),
    "pyramid.obj": ("РџС–СЂР°РјС–РґР°", "Shapes", _model_pyramid),
    "spike.obj": ("РЁРёРї (Geometry Dash)", "Gameplay", _model_spike),
    "checkpoint_flag.obj": ("РџСЂР°РїРѕСЂРµС†СЊ С‡РµРєРїРѕС—РЅС‚Р°", "Gameplay", _model_checkpoint_flag),
    "coin.obj": ("РњРѕРЅРµС‚Р°", "Gameplay", _model_coin),
    "crate.obj": ("РЇС‰РёРє", "Props", _model_crate),
    "barrel.obj": ("Р‘РѕС‡РєР°", "Props", _model_barrel),
    "star.obj": ("Р—С–СЂРєР°", "Props", _model_star),
    "sword.obj": ("РњРµС‡", "Props", _model_sword),
    "arch.obj": ("РђСЂРєР°", "Props", _model_arch),
    "tree.obj": ("Р”РµСЂРµРІРѕ", "Nature", _model_tree),
    "rock.obj": ("РљР°РјС–РЅСЊ", "Nature", _model_rock),
    "mushroom.obj": ("Р“СЂРёР±", "Nature", _model_mushroom),
}


def _normalize_model_entry(entry: dict, file_name: str) -> dict:
    return {
        "file": file_name,
        "name": entry.get("name") or Path(file_name).stem,
        "category": entry.get("category") if entry.get("category") in ALLOWED_MODEL_CATEGORIES else "Custom",
        "author": entry.get("author") or "toolbox",
        "size": int(entry.get("size") or 0),
        "uploaded_at": entry.get("uploaded_at") or "",
        "builtin": bool(entry.get("builtin")),
    }


def ensure_builtin_models() -> None:
    """Create the toolbox OBJ files and index entries if they are missing."""
    index = read_json_file("models_index.json") or {"models": []}
    models: List[dict] = [m for m in index.get("models", []) if isinstance(m, dict) and m.get("file")]
    by_file = {m["file"]: m for m in models}

    for file_name, (display_name, category, builder) in BUILTIN_MODEL_BUILDERS.items():
        local_path = MODELS_DIR / file_name
        if not local_path.exists():
            try:
                data = builder().encode("utf-8")
                local_path.write_bytes(data)
            except Exception as exc:  # pragma: no cover - defensive
                print("Failed to generate builtin model", file_name, exc)
                continue
        entry = by_file.get(file_name)
        meta = {
            "file": file_name,
            "name": display_name,
            "category": category,
            "author": "toolbox",
            "size": local_path.stat().st_size,
            "uploaded_at": entry.get("uploaded_at") if entry else "",
            "builtin": True,
        }
        by_file[file_name] = meta

    # Adopt files that were already downloaded/uploaded before the index existed.
    for path in MODELS_DIR.iterdir():
        if not path.is_file() or path.name in by_file:
            continue
        suffix = path.suffix.lower()
        if suffix not in MODEL_EXTENSIONS:
            continue
        by_file[path.name] = _normalize_model_entry(
            {
                "name": path.stem,
                "category": "Custom",
                "author": "server",
                "size": path.stat().st_size,
                "uploaded_at": now_iso(),
                "builtin": False,
            },
            path.name,
        )

    ordered = sorted(by_file.values(), key=lambda m: (not m["builtin"], m["category"], m["name"].lower()))
    write_json_file("models_index.json", {"models": ordered})


def model_index() -> List[dict]:
    data = read_json_file("models_index.json") or {"models": []}
    return [m for m in data.get("models", []) if isinstance(m, dict) and m.get("file")]


def save_model_index(models: List[dict]) -> None:
    write_json_file("models_index.json", {"models": models})


# Routes
@app.get("/")
def root():
    websocket_route = any(getattr(route, "path", None) == "/ws/{place_id}" for route in app.routes)
    return {
        "status": "online",
        "service": "Roblox-Style Game API with Stats, Friends, Likes, Models and Multiplayer",
        "api_version": app.version,
        "features": [
            "stats", "friends", "place_likes", "place_plays", "place_views",
            "model_toolbox", "dev_menu",
        ] + (["multiplayer"] if websocket_route else []),
        "multiplayer_websocket": websocket_route,
        "websocket_path": "/ws/{place_id}" if websocket_route else None,
        "storage": "Cloudflare R2 + local disk" if s3_client else "Local disk",
        "models": len(model_index()),
        "places": len(load_place_index()["places"]),
        "time": now_iso(),
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION, "time": now_iso()}



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
    check_rate_limit(client_ip(request), "register", max_calls=4, window_sec=60)

    username = require_username(req.username)
    user_key = f"users/{username}.json"

    existing = read_json_file(user_key)
    if existing:
        raise HTTPException(status_code=400, detail="Користувач з таким логіном вже існує")

    user_record = {
        "username": username,
        "password_hash": hash_password(req.password),
        "created_at": now_iso(),
        "friends": [],
        "incoming_requests": [],
    }
    write_json_file(user_key, user_record)

    token = create_jwt_token(username)
    return AuthResponse(status="success", username=username, token=token)

@app.post("/api/auth/login", response_model=AuthResponse)
def login(req: AuthRequest, request: Request):
    check_rate_limit(client_ip(request), "login", max_calls=12, window_sec=60)

    username = require_username(req.username)
    user_key = f"users/{username}.json"

    user_record = read_json_file(user_key)
    if not user_record or not verify_password(req.password, user_record.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="Невірний логін або пароль")

    token = create_jwt_token(username)
    return AuthResponse(status="success", username=username, token=token)

@app.get("/api/auth/me")
def me(current_user: str = Depends(get_current_user)):
    return {
        "status": "ok",
        "username": current_user,
        "admin": is_admin(current_user),
        "dev_access": has_dev_access(current_user),
        "dev_open": not ADMIN_USERS and ALLOW_OPEN_DEV,
    }

@app.post("/api/places/save")
@storage_locked
def save_place(place: PlaceData, request: Request, current_user: str = Depends(get_current_user)):
    check_rate_limit(client_ip(request), "place_save", max_calls=30, window_sec=60)

    place_id = place.id or uuid.uuid4().hex[:8]
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=400, detail="Некоректний ID плейса")
    place_key = f"places/{place_id}.json"

    existing = read_json_file(place_key)
    if existing and existing.get("author") != current_user:
        raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")

    gui_elements = validate_gui_elements(place.gui_elements)
    if gui_elements is None:
        gui_elements = (existing or {}).get("gui_elements", [])

    now_str = now_iso()
    liked_by = (existing or {}).get("liked_by", [])
    if not isinstance(liked_by, list):
        liked_by = []
    thumbnail = place.thumbnail_png
    if thumbnail is None and existing:
        thumbnail = existing.get("thumbnail_png")

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
        "thumbnail_png": thumbnail,
        "layers": [layer.model_dump() for layer in place.layers],
        "walk_speed": place.walk_speed,
        "jump_force": place.jump_force,
        "gravity": place.gravity,
        "sprint_enabled": place.sprint_enabled,
        "wall_jump_enabled": place.wall_jump_enabled,
        "baseplate_enabled": place.baseplate_enabled,
        "death_height": place.death_height,
        "plays": int((existing or {}).get("plays", 0)),
        "views": int((existing or {}).get("views", 0)),
        "featured": bool((existing or {}).get("featured", False)),
        "liked_by": list(liked_by),
    }
    place_record["likes"] = len(liked_by)

    write_json_file(place_key, place_record)
    upsert_place_index(place_record)

    return {
        "status": "success",
        "id": place_id,
        "updated_at": now_str,
        "message": "Плейс збережено",
    }

@app.get("/api/places/list")
def list_places(
    q: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 200,
    current_user: Optional[str] = Depends(get_optional_user),
):
    index_data = load_place_index()
    places = index_data["places"]
    if q:
        needle = q.strip().lower()
        places = [
            entry for entry in places
            if needle in str(entry.get("title", "")).lower()
            or needle in str(entry.get("author", "")).lower()
        ]
    if author:
        needle_author = author.strip().lower()
        places = [entry for entry in places if str(entry.get("author", "")).lower() == needle_author]
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 200

    result = []
    for entry in places[:limit]:
        summary = public_place_summary(entry, current_user)
        likers = entry.get("liked_by")
        if likers is None and current_user:
            record = read_json_file(f"places/{entry.get('id', '')}.json") or {}
            likers = record.get("liked_by") or []
        liked = bool(current_user and isinstance(likers, list) and current_user in likers)
        summary["liked"] = liked
        summary["liked_by_me"] = liked
        result.append(summary)
    return {"places": result, "total": len(places)}

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
def record_place_play(place_id: str, request: Request, current_user: str = Depends(get_current_user)):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    check_rate_limit(client_ip(request), "play", max_calls=60, window_sec=60)
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        record["plays"] = int(record.get("plays", 0)) + 1
        write_json_file(f"places/{place_id}.json", record)
        stats = _get_stats(current_user)
        stats["places_played"] = int(stats.get("places_played", 0)) + 1
        write_json_file(_stats_key(current_user), stats)
        upsert_place_index(record)
        return {"plays": record["plays"]}

@app.post("/api/places/{place_id}/view")
def record_place_view(place_id: str, request: Request):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    check_rate_limit(client_ip(request), "view", max_calls=120, window_sec=60)
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        if track_view(place_id, client_ip(request)):
            record["views"] = int(record.get("views", 0)) + 1
            write_json_file(f"places/{place_id}.json", record)
            upsert_place_index(record)
        return {"views": int(record.get("views", 0))}

@app.post("/api/places/{place_id}/like")
def toggle_place_like(place_id: str, request: Request, current_user: str = Depends(get_current_user)):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    check_rate_limit(client_ip(request), "like", max_calls=40, window_sec=60)
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        likes = record.get("liked_by", [])
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
        upsert_place_index(record)
        return {"status": "success", "liked": liked, "likes": len(likes), "liked_by_me": liked}

@app.delete("/api/places/{place_id}")
def delete_place(place_id: str, current_user: str = Depends(get_current_user)):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        if record.get("author") != current_user and not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Ви не є автором цього плейса")
        delete_storage_file(f"places/{place_id}.json")
        index = load_place_index()
        index["places"] = [p for p in index["places"] if p.get("id") != place_id]
        save_place_index(index)
        live_rooms.pop(place_id, None)
        return {"status": "deleted", "id": place_id}

@app.post("/api/places/{place_id}/feature")
def feature_place(place_id: str, current_user: str = Depends(get_current_user)):
    require_dev_access(current_user)
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    with storage_lock:
        record = read_json_file(f"places/{place_id}.json")
        if not record:
            raise HTTPException(status_code=404, detail="Плейс не знайдено")
        record["featured"] = not bool(record.get("featured"))
        write_json_file(f"places/{place_id}.json", record)
        upsert_place_index(record)
        return {"status": "success", "featured": record["featured"]}


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
def get_place(place_id: str, current_user: Optional[str] = Depends(get_optional_user)):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    place_key = f"places/{place_id}.json"
    place_record = read_json_file(place_key)
    if not place_record:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    return public_place_record(place_record, current_user)

@app.get("/api/places/{place_id}/thumbnail")
def get_place_thumbnail(place_id: str):
    if not re.fullmatch(PLACE_ID_PATTERN, place_id):
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
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
    return Response(content=image, media_type="image/png")

# ---------------------------------------------------------------------------
# Developer menu API
# ---------------------------------------------------------------------------
@app.get("/api/dev/status")
def dev_status(current_user: str = Depends(get_current_user)):
    index = load_place_index()
    models = model_index()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "instance": SERVER_INSTANCE_ID,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "storage": "cloudflare-r2" if s3_client else "local-disk",
        "places": len(index["places"]),
        "blocks": sum(int(p.get("block_count") or 0) for p in index["places"]),
        "users": len(list_usernames()),
        "models": len(models),
        "builtin_models": sum(1 for m in models if m.get("builtin")),
        "online_players": live_player_count(),
        "rooms": {place_id: len(room.clients) for place_id, room in live_rooms.items()},
        "admin": is_admin(current_user),
        "dev_access": has_dev_access(current_user),
        "dev_open": not ADMIN_USERS and ALLOW_OPEN_DEV,
        "time": now_iso(),
    }


@app.get("/api/dev/users")
def dev_users(current_user: str = Depends(get_current_user)):
    require_dev_access(current_user)
    return {"users": list_usernames()}


@app.get("/api/dev/places")
def dev_places(current_user: str = Depends(get_current_user)):
    require_dev_access(current_user)
    index = load_place_index()
    return {"places": [public_place_summary(entry, current_user) for entry in index["places"]]}


@app.get("/api/dev/whoami")
def dev_whoami(current_user: str = Depends(get_current_user)):
    return {
        "username": current_user,
        "admin": is_admin(current_user),
        "dev_access": has_dev_access(current_user),
        "dev_open": not ADMIN_USERS and ALLOW_OPEN_DEV,
    }


# ---------------------------------------------------------------------------
# Model toolbox API
# ---------------------------------------------------------------------------
def validate_model_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", value):
        raise HTTPException(status_code=400, detail="Некоректна назва моделі")
    if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Підтримуються лише .obj, .glb та .gltf")
    return value


def _validate_model_payload(filename: str, data: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    if len(data) < 16:
        raise HTTPException(status_code=400, detail="Файл порожній або пошкоджений")
    if suffix == ".obj":
        text = data.decode("utf-8", errors="ignore")
        vertex_count = sum(1 for line in text.splitlines() if line.strip().startswith("v "))
        face_count = sum(1 for line in text.splitlines() if line.strip().startswith("f "))
        if vertex_count < 3 or face_count < 1:
            raise HTTPException(status_code=400, detail="OBJ не містить вершин/граней")
        if re.search(r"(?im)^\s*(mtllib|usemtl)\s+.*\.\.", text):
            raise HTTPException(status_code=400, detail="Некоректні посилання на матеріали")
    elif suffix == ".glb":
        if not data.startswith(b"glTF"):
            raise HTTPException(status_code=400, detail="GLB має починатися з магічного заголовка glTF")
    elif suffix == ".gltf":
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="GLTF має бути валідним JSON")
        if not isinstance(parsed, dict) or "meshes" not in parsed:
            raise HTTPException(status_code=400, detail="GLTF не містить мешів")


@app.get("/api/models")
def list_models(category: Optional[str] = None, q: Optional[str] = None):
    models = model_index()
    if category and category.lower() != "all":
        models = [m for m in models if str(m.get("category", "")).lower() == category.lower()]
    if q:
        needle = q.strip().lower()
        models = [
            m for m in models
            if needle in str(m.get("name", "")).lower() or needle in str(m.get("file", "")).lower()
        ]
    return {
        "models": models,
        "categories": list(ALLOWED_MODEL_CATEGORIES),
        "total": len(models),
        "max_bytes": MAX_MODEL_BYTES,
    }


@app.get("/api/models/{filename}")
def get_model(filename: str):
    filename = validate_model_name(filename)
    if not any(m.get("file") == filename for m in model_index()):
        raise HTTPException(status_code=404, detail="Модель не знайдено")
    data = read_model_bytes(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Файл моделі відсутній на сервері")
    media_type = MODEL_EXTENSIONS.get(Path(filename).suffix.lower(), "application/octet-stream")
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@app.post("/api/models/upload")
async def upload_model(
    request: Request,
    file: UploadFile = File(...),
    category: str = Form("Custom"),
    current_user: str = Depends(get_current_user),
):
    check_rate_limit(client_ip(request), "model_upload", max_calls=10, window_sec=60)
    if category not in ALLOWED_MODEL_CATEGORIES:
        category = "Custom"

    raw_name = Path(file.filename or "model.obj").name
    suffix = Path(raw_name).suffix.lower()
    if suffix not in MODEL_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Підтримуються лише .obj, .glb та .gltf")

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(raw_name).stem)[:40].strip("_") or "model"
    data = await file.read(MAX_MODEL_BYTES + 1)
    if len(data) > MAX_MODEL_BYTES:
        raise HTTPException(status_code=413, detail=f"Модель більша за {MAX_MODEL_BYTES // (1024 * 1024)} МБ")
    _validate_model_payload(f"{stem}{suffix}", data)

    with storage_lock:
        models = model_index()
        reserved = set(BUILTIN_MODEL_BUILDERS) | {m.get("file") for m in models}
        filename = f"{stem}{suffix}"
        counter = 2
        while filename in reserved:
            filename = f"{stem}_{counter}{suffix}"
            counter += 1

        write_model_bytes(filename, data)
        entry = {
            "file": filename,
            "name": Path(raw_name).stem[:48] or stem,
            "category": category,
            "author": current_user,
            "size": len(data),
            "uploaded_at": now_iso(),
            "builtin": False,
        }
        models.append(entry)
        models.sort(key=lambda m: (not m.get("builtin"), str(m.get("category")), str(m.get("name")).lower()))
        save_model_index(models)
    return {"status": "success", "model": entry}


@app.delete("/api/models/{filename}")
def delete_model(filename: str, current_user: str = Depends(get_current_user)):
    filename = validate_model_name(filename)
    with storage_lock:
        models = model_index()
        entry = next((m for m in models if m.get("file") == filename), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Модель не знайдено")
        if entry.get("builtin"):
            raise HTTPException(status_code=403, detail="Вбудовані моделі не можна видаляти")
        if entry.get("author") != current_user and not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Ви не є автором цієї моделі")
        save_model_index([m for m in models if m.get("file") != filename])
        delete_storage_file(f"models/{filename}")
        local_path = MODELS_DIR / filename
        if local_path.exists():
            try:
                local_path.unlink()
            except OSError:
                pass
    return {"status": "deleted", "file": filename}


ensure_builtin_models()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting server with Anti-Cheat & Bot Protection on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
