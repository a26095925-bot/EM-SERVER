import os
import json
import uuid
import time
import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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

app = FastAPI(title="Roblox-Style Game Backend", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

class PlaceBlock(BaseModel):
    type: str
    pos: List[float]
    size: List[float]
    color: str
    power: Optional[float] = 10.0
    script: Optional[str] = None

class PlaceData(BaseModel):
    id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = ""
    author: Optional[str] = ""
    spawn: Optional[List[float]] = [0, 2, 0]
    blocks: List[PlaceBlock] = []

# Routes
@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Roblox-Style Game API with Anti-Cheat & Bot Protection",
        "storage": "Cloudflare R2" if s3_client else "Local Cache",
        "time": datetime.datetime.utcnow().isoformat()
    }

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
def save_place(place: PlaceData, current_user: str = Depends(get_current_user)):
    # Server-side validation against malicious / oversized payloads
    if len(place.blocks) > 10000:
        raise HTTPException(status_code=400, detail="Перевищено ліміт: максимум 10 000 блоків на один плейс")

    place_id = place.id or str(uuid.uuid4())[:8]
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
        "blocks": [b.dict() for b in place.blocks]
    }
    
    write_json_file(place_key, place_record)
    
    index_data = read_json_file("places_index.json") or {"places": []}
    places_list = [p for p in index_data.get("places", []) if p.get("id") != place_id]
    places_list.insert(0, {
        "id": place_id,
        "title": place.title,
        "author": current_user,
        "block_count": len(place.blocks),
        "updated_at": now_str
    })
    index_data["places"] = places_list[:100]
    write_json_file("places_index.json", index_data)
    
    return {"status": "success", "id": place_id, "message": "Плейс збережено в Cloudflare R2"}

@app.get("/api/places/list")
def list_places():
    index_data = read_json_file("places_index.json") or {"places": []}
    return index_data

@app.get("/api/places/{place_id}")
def get_place(place_id: str):
    place_key = f"places/{place_id}.json"
    place_record = read_json_file(place_key)
    if not place_record:
        raise HTTPException(status_code=404, detail="Плейс не знайдено")
    return place_record

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting server with Anti-Cheat & Bot Protection on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
