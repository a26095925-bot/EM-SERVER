import asyncio
import websockets
import json
import os
import time

PORT = int(os.environ.get("PORT", 10000))
HOST = "0.0.0.0"

MAX_PLAYERS_PER_ROOM = 5
ROOM_NAMES = ["Cube-1", "Cube-2", "Cube-3"]

# Структура кімнат: { "Cube-1": { ws: {"id": p_id, "x": 0.0, "y": -2.6, "color": [...]} } }
rooms = {name: {} for name in ROOM_NAMES}
client_to_room = {} # { websocket: room_name }

player_counter = 0

COLORS = [
    [0.2, 0.6, 1.0],  # Блакитний
    [1.0, 0.3, 0.3],  # Червоний
    [0.3, 0.9, 0.3],  # Зелений
    [0.9, 0.3, 0.9],  # Фіолетовий
    [1.0, 0.8, 0.2],  # Жовтий
]

def log_status(action=""):
    stats = " | ".join([f"{name}: {len(rooms[name])}/{MAX_PLAYERS_PER_ROOM}" for name in ROOM_NAMES])
    print(f"[{time.strftime('%H:%M:%S')}] {action} >> [{stats}]")

def find_available_room():
    for name in ROOM_NAMES:
        if len(rooms[name]) < MAX_PLAYERS_PER_ROOM:
            return name
    return None

async def handler(websocket):
    global player_counter
    
    # 1. Пошук доступного куба
    room_name = find_available_room()
    if room_name is None:
        await websocket.send(json.dumps({
            "type": "error",
            "msg": "Усі 3 куби заповнені! (15/15). Зачекайте звільнення місця."
        }))
        await websocket.close()
        log_status("[-] Відхилено підключення (Усі кімнати повні)")
        return

    player_counter += 1
    p_id = f"P_{player_counter}"
    assigned_color = COLORS[(player_counter - 1) % len(COLORS)]

    # Прив'язка до кімнати
    client_to_room[websocket] = room_name
    rooms[room_name][websocket] = {
        "id": p_id,
        "x": 0.0,
        "y": -2.6,
        "color": assigned_color
    }

    log_status(f"[+] {p_id} зайшов у {room_name}")

    # Відправляємо клієнту дані про його кімнату та ID
    await websocket.send(json.dumps({
        "type": "init",
        "id": p_id,
        "room": room_name,
        "color": assigned_color,
        "stats": {name: f"{len(rooms[name])}/{MAX_PLAYERS_PER_ROOM}" for name in ROOM_NAMES}
    }))

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "pos":
                    if websocket in rooms[room_name]:
                        rooms[room_name][websocket]["x"] = data["x"]
                        rooms[room_name][websocket]["y"] = data["y"]
            except json.JSONDecodeError:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[!] Помилка сокета: {e}")
    finally:
        # Автоматичне безпечне очищення при виході
        if websocket in client_to_room:
            r_name = client_to_room[websocket]
            if websocket in rooms[r_name]:
                leaving_id = rooms[r_name][websocket]["id"]
                del rooms[r_name][websocket]
                log_status(f"[-] {leaving_id} покинув {r_name}")
            del client_to_room[websocket]

async def broadcast_loop():
    """Фоновий такт сервера (30 FPS) для стабільної синхронізації без зависань"""
    while True:
        await asyncio.sleep(0.033) # ~30 разів на секунду
        
        stats_dict = {name: f"{len(rooms[name])}/{MAX_PLAYERS_PER_ROOM}" for name in ROOM_NAMES}

        for r_name, occupants in rooms.items():
            if not occupants:
                continue

            # Збираємо стан гравців тільки в межах цього конкретного куба
            room_players = {
                data["id"]: {
                    "x": data["x"],
                    "y": data["y"],
                    "color": data["color"]
                }
                for data in occupants.values()
            }

            packet = json.dumps({
                "type": "world",
                "room": r_name,
                "players": room_players,
                "stats": stats_dict
            })

            # Розсилаємо всім гравцям у цій кімнаті
            dead_sockets = []
            for ws in occupants.keys():
                try:
                    await ws.send(packet)
                except:
                    dead_sockets.append(ws)

            # Чистимо "мертві" клієнти
            for ws in dead_sockets:
                if ws in rooms[r_name]:
                    del rooms[r_name][ws]
                if ws in client_to_room:
                    del client_to_room[ws]

async def main():
    print("=" * 50)
    print(f"[*] СЕРВЕР EVENT MADNESS ЗАПУЩЕНО НА ПОРТУ {PORT}")
    print(f"[*] Кімнати: {', '.join(ROOM_NAMES)} (по {MAX_PLAYERS_PER_ROOM} гравців)")
    print("=" * 50)
    
    # Запускаємо фонову розсилку
    asyncio.create_task(broadcast_loop())

    # Сервер із захистом ping/pong
    async with websockets.serve(
        handler, 
        HOST, 
        PORT, 
        ping_interval=10, 
        ping_timeout=5
    ):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
