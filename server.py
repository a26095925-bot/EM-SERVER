import asyncio
import websockets
import json
import os
import time
import random

PORT = int(os.environ.get("PORT", 10000))
HOST = "0.0.0.0"

MAX_PLAYERS = 5
ROOM_NAMES = ["Cube-1", "Cube-2", "Cube-3"]

# Евенти розділені на групи для уникнення комбінацій, де неможливо вижити
CUBE_MUTATIONS = ["CUBE_EXPLODE", "NO_FLOOR", "CUBE_SHRINK", "CUBE_EXPAND", "LOW_CEILING", "BOUNCY_WALLS", "CUBE_TWIST"]
OTHER_EVENTS = [
    "GRAVITY_UP", "MOON_GRAVITY", "SUPER_SPEED", "ICE_PHYSICS",
    "HEAVY_WEIGHT", "INVERT_KEYS", "HYPER_JUMP", "SLOW_MO",
    "DARKNESS", "SCREEN_SHAKE", "RED_ALERT", "COLOR_MADNESS", "LASER_SWEEP"
]

server_accounts = {}
rooms = {
    name: {
        "state": "IDLE", # IDLE, WAITING, IN_GAME, ROUND_OVER
        "timer": 20.0,
        "madness": 0.0,
        "active_events": [],
        "players": {} # ws: {nick, x, y, alive, skin}
    } for name in ROOM_NAMES
}
client_rooms = {}

def get_fair_event(current_events):
    """Вибирає тільки сумісні евенти, щоб гравець ЗАВЖДИ міг вижити"""
    avail = [e for e in (CUBE_MUTATIONS + OTHER_EVENTS) if e not in current_events]
    
    # Не можна поєднувати зникнення підлоги і розліт стін (інакше немає за що чіплятися)
    if "NO_FLOOR" in current_events:
        avail = [e for e in avail if e not in ["CUBE_EXPLODE", "LOW_CEILING"]]
    if "CUBE_EXPLODE" in current_events:
        avail = [e for e in avail if e not in ["NO_FLOOR", "CUBE_SHRINK"]]
    if "LOW_CEILING" in current_events:
        avail = [e for e in avail if e != "GRAVITY_UP"]

    return random.choice(avail) if avail else None

async def handler(websocket):
    client_nick = None
    room_name = None

    try:
        raw = await websocket.recv()
        data = json.loads(raw)
        req_nick = data.get("nick", "Player")
        client_skin = data.get("skin", "Classic")

        # 1. Пошук кімнати в стані WAITING або IDLE
        target_room = None
        for r in ROOM_NAMES:
            if rooms[r]["state"] in ["IDLE", "WAITING"] and len(rooms[r]["players"]) < MAX_PLAYERS:
                target_room = r
                break

        # 2. Якщо всі кімнати вже грають - саджаємо в кімнату з найменшим залишком часу як глядача
        if not target_room:
            for r in ROOM_NAMES:
                if len(rooms[r]["players"]) < MAX_PLAYERS:
                    target_room = r
                    break
        if not target_room:
            target_room = ROOM_NAMES[0]

        room_name = target_room
        client_rooms[websocket] = room_name
        
        used_nicks = [p["nick"] for p in rooms[room_name]["players"].values()]
        client_nick = req_nick if req_nick not in used_nicks else f"{req_nick}_{random.randint(2,9)}"

        if client_nick not in server_accounts:
            server_accounts[client_nick] = {"coins": data.get("coins", 0), "skins": ["Classic"], "wins": 0}

        # Якщо зайшов під час раунду - грає тільки з наступного раунду
        is_alive_now = (rooms[room_name]["state"] in ["IDLE", "WAITING"])

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": random.uniform(-1.0, 1.0),
            "y": -2.4,
            "alive": is_alive_now,
            "skin": client_skin
        }

        # Якщо кімната спала - будимо її
        if rooms[room_name]["state"] == "IDLE":
            rooms[room_name]["state"] = "WAITING"
            rooms[room_name]["timer"] = 20.0

        await websocket.send(json.dumps({
            "type": "init",
            "nick": client_nick,
            "room": room_name,
            "account": server_accounts[client_nick]
        }))

        async for msg in websocket:
            pkt = json.loads(msg)
            p = rooms[room_name]["players"].get(websocket)
            if not p: continue

            if pkt.get("type") == "pos":
                p["x"] = pkt["x"]
                p["y"] = pkt["y"]
            elif pkt.get("type") == "dead":
                p["alive"] = False
            elif pkt.get("type") == "chat":
                chat_pkt = json.dumps({"type": "chat", "nick": client_nick, "text": pkt["text"]})
                for ws in list(rooms[room_name]["players"].keys()):
                    try: await ws.send(chat_pkt)
                    except: pass
            elif pkt.get("type") == "buy_skin":
                s_name, cost = pkt["skin"], pkt["cost"]
                acc = server_accounts[client_nick]
                if acc["coins"] >= cost and s_name not in acc["skins"]:
                    acc["coins"] -= cost
                    acc["skins"].append(s_name)
                    await websocket.send(json.dumps({"type": "account_update", "account": acc}))
    except: pass
    finally:
        if websocket in client_rooms:
            r = client_rooms[websocket]
            if websocket in rooms[r]["players"]:
                del rooms[r]["players"][websocket]
            del client_rooms[websocket]

async def game_loop():
    while True:
        await asyncio.sleep(0.033)

        for r_name, r in rooms.items():
            pls = r["players"]
            n_pls = len(pls)

            # СПЛЯЧИЙ РЕЖИМ: якщо немає людей - кімната вимикається
            if n_pls == 0:
                r["state"] = "IDLE"
                r["timer"] = 20.0
                r["active_events"] = []
                r["madness"] = 0.0
                continue

            if r["state"] == "WAITING":
                r["active_events"] = []
                r["madness"] = 0.0
                r["timer"] -= 0.033
                if r["timer"] <= 0 or n_pls >= MAX_PLAYERS:
                    r["state"] = "IN_GAME"
                    r["timer"] = 65.0
                    for p in pls.values(): p["alive"] = True
                    first_ev = random.choice(["CUBE_SHRINK", "CUBE_EXPAND", "BOUNCY_WALLS", "CUBE_TWIST"])
                    r["active_events"] = [first_ev]

            elif r["state"] == "IN_GAME":
                r["timer"] -= 0.033
                r["madness"] = min(100.0, r["madness"] + 0.033 * 7.5)
                
                # Додавання чесних евентів
                if r["madness"] >= 100.0:
                    r["madness"] = 0.0
                    new_ev = get_fair_event(r["active_events"])
                    if new_ev:
                        r["active_events"].append(new_ev)

                alive_players = [p for p in pls.values() if p["alive"]]
                if (len(alive_players) <= 1 and n_pls > 1) or (n_pls == 1 and not alive_players) or r["timer"] <= 0:
                    r["state"] = "ROUND_OVER"
                    r["timer"] = 5.0
                    if len(alive_players) == 1:
                        winner = alive_players[0]
                        server_accounts[winner["nick"]]["coins"] += 50
                        for ws, p_data in pls.items():
                            if p_data["nick"] == winner["nick"]:
                                try:
                                    asyncio.create_task(ws.send(json.dumps({
                                        "type": "account_update",
                                        "account": server_accounts[winner["nick"]],
                                        "win": True
                                    })))
                                except: pass

            elif r["state"] == "ROUND_OVER":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["state"] = "WAITING"
                    r["timer"] = 20.0
                    r["active_events"] = []
                    for p in pls.values(): p["alive"] = True

            # Розсилка оновлень
            packet = json.dumps({
                "type": "sync",
                "state": r["state"],
                "timer": max(0, int(r["timer"])),
                "madness": r["madness"],
                "events": r["active_events"],
                "players": {
                    p["nick"]: {
                        "x": p["x"], "y": p["y"], "alive": p["alive"],
                        "skin": p["skin"]
                    } for p in pls.values()
                }
            })

            for ws in list(pls.keys()):
                try: asyncio.create_task(ws.send(packet))
                except: pass

async def main():
    print(f"[*] СЕРВЕР ЗАПУЩЕНО НА {PORT} (IDLE LOGIC + FAIR EVENTS)")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
