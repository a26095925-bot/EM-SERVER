# server.py
# Запуск: uvicorn server:app --host 0.0.0.0 --port 10000
import json
import sqlite3
from typing import Dict, List
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="University Battle Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "game_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            username TEXT PRIMARY KEY,
            stipend INTEGER DEFAULT 500,
            inventory TEXT DEFAULT '[]',
            stats TEXT DEFAULT '{"hp":100, "speed":1.0, "damage":20}'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS maps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            author TEXT,
            data TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

class SavePayload(BaseModel):
    username: str
    stipend: int
    inventory: List[str]
    stats: dict

class BuyPayload(BaseModel):
    username: str
    item_id: str
    price: int

SHOP_CATALOG = {
    "coffee": {"name": "Еспресо з автомата", "price": 100, "stat": "speed", "boost": 0.25},
    "cheat_sheet": {"name": "Шпаргалка з матану", "price": 250, "stat": "damage", "boost": 15},
    "energy_drink": {"name": "Студентський енергетик", "price": 150, "stat": "hp", "boost": 50},
    "diploma_shield": {"name": "Тверда палітурка диплома", "price": 400, "stat": "defense", "boost": 0.2}
}

@app.post("/api/save")
def save_game(data: SavePayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO players (username, stipend, inventory, stats)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            stipend=excluded.stipend,
            inventory=excluded.inventory,
            stats=excluded.stats
    """, (data.username, data.stipend, json.dumps(data.inventory), json.dumps(data.stats)))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Прогрес збережено"}

@app.get("/api/load/{username}")
def load_game(username: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT stipend, inventory, stats FROM players WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"username": username, "stipend": 500, "inventory": [], "stats": {"hp": 100, "speed": 1.0, "damage": 20}}
    return {
        "username": username,
        "stipend": row[0],
        "inventory": json.loads(row[1]),
        "stats": json.loads(row[2])
    }

@app.post("/api/shop/buy")
def buy_item(payload: BuyPayload):
    if payload.item_id not in SHOP_CATALOG:
        raise HTTPException(status_code=400, detail="Товар не знайдено")
    item = SHOP_CATALOG[payload.item_id]
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT stipend, inventory, stats FROM players WHERE username=?", (payload.username,))
    row = cur.fetchone()
    if not row or row[0] < item["price"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Недостатньо стипендії")

    stipend = row[0] - item["price"]
    inv = json.loads(row[1])
    inv.append(payload.item_id)
    stats = json.loads(row[2])
    stats[item["stat"]] = stats.get(item["stat"], 1.0) + item["boost"]

    cur.execute("UPDATE players SET stipend=?, inventory=?, stats=? WHERE username=?",
                (stipend, json.dumps(inv), json.dumps(stats), payload.username))
    conn.commit()
    conn.close()
    return {"status": "success", "stipend": stipend, "stats": stats, "inventory": inv}

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, List[WebSocket]] = {}

    async def connect(self, room_id: str, websocket: WebSocket):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = []
        self.rooms[room_id].append(websocket)

    def disconnect(self, room_id: str, websocket: WebSocket):
        if room_id in self.rooms:
            self.rooms[room_id].remove(websocket)
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast(self, room_id: str, message: str, sender: WebSocket):
        if room_id in self.rooms:
            for connection in self.rooms[room_id]:
                if connection != sender:
                    await connection.send_text(message)

manager = ConnectionManager()

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await manager.connect(room_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.broadcast(room_id, data, websocket)
    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)
