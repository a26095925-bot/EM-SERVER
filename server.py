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

# 30+ БОЖЕВІЛЬНИХ ЕВЕНТІВ
CUBE_MUTATION_EVENTS = [
    "CUBE_EXPLODE", "NO_FLOOR", "CUBE_SHRINK", "CUBE_EXPAND", 
    "LOW_CEILING", "BOUNCY_WALLS", "CUBE_TWIST", "ZERO_G_CUBE"
]

OTHER_EVENTS = [
    "GRAVITY_UP", "MOON_GRAVITY", "SUPER_SPEED", "ICE_PHYSICS",
    "HEAVY_WEIGHT", "INVERT_KEYS", "HYPER_JUMP", "SLOW_MO",
    "TURBO_MAX", "GHOST_PLAYERS", "METEOR_RAIN", "LASER_SWEEP",
    "DARKNESS", "ACID_RAIN", "SCREEN_SHAKE", "RED_ALERT",
    "COLOR_MADNESS", "FLOOR_IS_LAVA", "SUPER_FRICTION", "REVERSE_TIME",
    "RANDOM_TELEPORT", "TINY_PLAYERS", "GIANT_PLAYERS", "VOLATILE_WALLS"
]

ALL_EVENTS_POOL = CUBE_MUTATION_EVENTS + OTHER_EVENTS

server_accounts = {}
rooms = {
    name: {
        "state": "IN_GAME", # Одразу починаємо матч для тесту або 10 сек лобі
        "timer": 10.0,
        "madness": 0.0,
        "active_events": ["CUBE_EXPLODE"],
        "players": {}
    } for name in ROOM_NAMES
}
client_rooms = {}

async def handler(websocket):
    client_nick = None
    room_name = None
    try:
        raw = await websocket.recv()
        data = json.loads(raw)
        req_nick = data.get("nick", "Player")
        client_skin = data.get("skin", "Classic")

        # Вибір кімнати
        room_name = ROOM_NAMES[0]
        for r in ROOM_NAMES:
            if len(rooms[r]["players"]) < MAX_PLAYERS:
                room_name = r
                break

        client_rooms[websocket] = room_name
        used_nicks = [p["nick"] for p in rooms[room_name]["players"].values()]
        client_nick = req_nick if req_nick not in used_nicks else f"{req_nick}_{random.randint(2,9)}"

        if client_nick not in server_accounts:
            server_accounts[client_nick] = {"coins": data.get("coins", 0), "skins": ["Classic"], "wins": 0}
        acc = server_accounts[client_nick]

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": random.uniform(-1.0, 1.0),
            "y": -2.4,
            "alive": True,
            "skin": client_skin,
            "emote": "",
            "emote_time": 0
        }

        await websocket.send(json.dumps({
            "type": "init",
            "nick": client_nick,
            "room": room_name,
            "account": acc
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
            elif pkt.get("type") == "emote":
                p["emote"] = pkt["emote"]
                p["emote_time"] = time.time() + 2.5
            elif pkt.get("type") == "chat":
                chat_pkt = json.dumps({"type": "chat", "nick": client_nick, "text": pkt["text"]})
                for ws in list(rooms[room_name]["players"].keys()):
                    try: await ws.send(chat_pkt)
                    except: pass
            elif pkt.get("type") == "buy_skin":
                s_name = pkt["skin"]
                cost = pkt["cost"]
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
        now = time.time()

        for r_name, r in rooms.items():
            pls = r["players"]
            n_pls = len(pls)

            if r["state"] == "WAITING":
                r["active_events"] = []
                r["madness"] = 0.0
                if n_pls > 0:
                    r["timer"] -= 0.033
                    if r["timer"] <= 0 or n_pls >= MAX_PLAYERS:
                        r["state"] = "IN_GAME"
                        r["timer"] = 60.0
                        for p in pls.values(): p["alive"] = True
                        r["active_events"] = [random.choice(CUBE_MUTATION_EVENTS)]
                else:
                    r["timer"] = 10.0

            elif r["state"] == "IN_GAME":
                # Заповнення шкали
                r["madness"] = min(100.0, r["madness"] + 0.033 * 8.0) # швидше заповнення
                if r["madness"] >= 100.0:
                    r["madness"] = 0.0
                    avail = [e for e in ALL_EVENTS_POOL if e not in r["active_events"]]
                    if avail:
                        r["active_events"].append(random.choice(avail))

                alive_players = [p for p in pls.values() if p["alive"]]
                if (len(alive_players) <= 1 and n_pls > 1) or (n_pls == 1 and not alive_players):
                    r["state"] = "ROUND_OVER"
                    r["timer"] = 5.0
                    if len(alive_players) == 1:
                        winner = alive_players[0]
                        server_accounts[winner["nick"]]["coins"] += 50
                        server_accounts[winner["nick"]]["wins"] += 1
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
                    r["timer"] = 10.0
                    r["active_events"] = []
                    for p in pls.values(): p["alive"] = True

            # Відправка стану
            packet = json.dumps({
                "type": "sync",
                "state": r["state"],
                "timer": max(0, int(r["timer"])),
                "madness": r["madness"],
                "events": r["active_events"],
                "players": {
                    p["nick"]: {
                        "x": p["x"], "y": p["y"], "alive": p["alive"],
                        "skin": p["skin"],
                        "emote": p["emote"] if p["emote_time"] > now else ""
                    } for p in pls.values()
                }
            })

            for ws in list(pls.keys()):
                try: asyncio.create_task(ws.send(packet))
                except: pass

async def main():
    print(f"[*] СЕРВЕР ЗАПУЩЕНО НА {PORT}")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
