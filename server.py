# server.py
# Запуск локально або на Render: uvicorn server:app --host 0.0.0.0 --port 10000
import json
import sqlite3
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="University Battle Backend")

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
    conn.commit()
    conn.close()

init_db()

class SaveData(BaseModel):
    username: str
    stipend: int
    stats: dict
    inventory: list

@app.post("/api/save")
def save_progress(data: SaveData):
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
    return {"status": "ok"}

@app.get("/api/load/{username}")
def load_progress(username: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT stipend, stats, inventory FROM saves WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"username": username, "stipend": row[0], "stats": json.loads(row[1]), "inventory": json.loads(row[2])}
    return {"username": username, "stipend": 600, "stats": {"hp": 100, "speed": 1.0, "dmg": 30}, "inventory": []}

# WebSockets для онлайн мультиплеєрних кімнат
class RoomHub:
    def __init__(self):
        self.rooms: Dict[str, List[WebSocket]] = {}

    async def join(self, room: str, ws: WebSocket):
        await ws.accept()
        if room not in self.rooms:
            self.rooms[room] = []
        self.rooms[room].append(ws)

    def leave(self, room: str, ws: WebSocket):
        if room in self.rooms:
            if ws in self.rooms[room]:
                self.rooms[room].remove(ws)
            if not self.rooms[room]:
                del self.rooms[room]

    async def broadcast(self, room: str, msg: str, sender: WebSocket):
        if room in self.rooms:
            for client in self.rooms[room]:
                if client != sender:
                    try:
                        await client.send_text(msg)
                    except:
                        pass

hub = RoomHub()

@app.websocket("/ws/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str):
    await hub.join(room_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await hub.broadcast(room_id, data, websocket)
    except WebSocketDisconnect:
        hub.leave(room_id, websocket)
