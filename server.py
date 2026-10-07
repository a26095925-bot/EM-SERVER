# server.py
import os
import json
import sqlite3
from typing import Dict, List
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

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
        CREATE TABLE IF NOT EXISTS saves (
            username TEXT PRIMARY KEY,
            stipend INTEGER,
            stats TEXT,
            inventory TEXT
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

# --- СХЕМИ ТА ЕНДПОІНТИ ЗБЕРЕЖЕННЯ ---
class SavePayload(BaseModel):
    username: str
    stipend: int
    inventory: List[str]
    stats: dict

@app.get("/")
def health_check():
    return {"status": "online", "game": "University Battle 3D"}

@app.post("/api/save")
def save_game(data: SavePayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO saves (username, stipend, stats, inventory)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            stipend=excluded.stipend,
            stats=excluded.stats,
            inventory=excluded.inventory
    """, (data.username, data.stipend, json.dumps(data.stats), json.dumps(data.inventory)))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Прогрес збережено"}

@app.get("/api/load/{username}")
def load_game(username: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT stipend, stats, inventory FROM saves WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {
            "username": username,
            "stipend": 500,
            "stats": {"hp": 100, "speed": 1.0, "dmg": 30},
            "inventory": []
        }
    return {
        "username": username,
        "stipend": row[0],
        "stats": json.loads(row[1]),
        "inventory": json.loads(row[2])
    }

# --- МУЛЬТИПЛЕЄР: WEBSOCKET КІМНАТИ ---
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
            if websocket in self.rooms[room_id]:
                self.rooms[room_id].remove(websocket)
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast(self, room_id: str, message: str, sender: WebSocket):
        if room_id in self.rooms:
            for connection in self.rooms[room_id]:
                if connection != sender:
                    try:
                        await connection.send_text(message)
                    except Exception:
                        pass

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


# --- ТОЧКА ВХОДУ (ОБОВ'ЯЗКОВО ДЛЯ RENDER) ---
if __name__ == "__main__":
    # Render передає номер порту через змінну середовища PORT
    port = int(os.environ.get("PORT", 10000))
    # Запускаємо uvicorn так, щоб процес слухав 0.0.0.0 і не завершувався
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
