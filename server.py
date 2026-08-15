import asyncio
import websockets
import json
import os

PORT = int(os.environ.get("PORT", 10000))
HOST = "0.0.0.0"

clients = {}  # {websocket: player_id}
players = {}  # {player_id: {"x": 0.0, "y": 0.0, "color": [...]}}
player_counter = 0

COLORS = [
    [0.2, 0.6, 1.0],  # Синій
    [1.0, 0.3, 0.3],  # Червоний
    [0.3, 0.9, 0.3],  # Зелений
    [0.9, 0.3, 0.9],  # Фіолетовий
]

async def handler(websocket):
    global player_counter
    player_counter += 1
    p_id = f"Player_{player_counter}"
    assigned_color = COLORS[(player_counter - 1) % len(COLORS)]
    
    clients[websocket] = p_id
    players[p_id] = {"x": 0.0, "y": -2.6, "color": assigned_color}
    print(f"[+] {p_id} підключився!")

    # Відправляємо клієнту його ID
    await websocket.send(json.dumps({"type": "init", "id": p_id, "color": assigned_color}))

    try:
        async for message in websocket:
            data = json.loads(message)
            if data.get("type") == "pos":
                if p_id in players:
                    players[p_id]["x"] = data["x"]
                    players[p_id]["y"] = data["y"]

            # Розсилаємо всім стан світу
            world_packet = json.dumps({"type": "world", "players": players})
            await websocket.send(world_packet)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[-] {p_id} вийшов")
        if websocket in clients:
            del clients[websocket]
        if p_id in players:
            del players[p_id]

async def main():
    print(f"[*] WebSocket Сервер запущено на {HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
