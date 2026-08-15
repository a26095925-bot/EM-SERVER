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

# База забанених гравців: { ip_or_nick: unban_timestamp }
banned_players = {}

CUBE_MUTATIONS = ["CUBE_EXPLODE", "CUBE_SHRINK", "CUBE_EXPAND", "BOUNCY_WALLS", "CUBE_TWIST"]
OTHER_EVENTS = [
    "GRAVITY_UP", "MOON_GRAVITY", "SUPER_SPEED", "ICE_PHYSICS",
    "HEAVY_WEIGHT", "INVERT_KEYS", "HYPER_JUMP", "SLOW_MO",
    "DARKNESS", "SCREEN_SHAKE", "RED_ALERT", "COLOR_MADNESS"
]

server_accounts = {}
rooms = {
    name: {
        "state": "IDLE",
        "timer": 20.0,
        "madness": 0.0,
        "active_events": [],
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

        # Перевірка античит-бану
        now = time.time()
        if req_nick in banned_players and banned_players[req_nick] > now:
            left_sec = int(banned_players[req_nick] - now)
            await websocket.send(json.dumps({
                "type": "banned",
                "msg": f"Античит: Бан за нескінченний політ! Залишилось {left_sec} сек."
            }))
            await websocket.close()
            return

        # Пошук кімнати
        target_room = None
        for r in ROOM_NAMES:
            if rooms[r]["state"] in ["IDLE", "WAITING"] and len(rooms[r]["players"]) < MAX_PLAYERS:
                target_room = r
                break
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

        is_alive_now = (rooms[room_name]["state"] in ["IDLE", "WAITING"])

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": random.uniform(-1.0, 1.0),
            "y": -2.4,
            "alive": is_alive_now,
            "skin": client_skin,
            "air_time": 0.0,
            "last_y": -2.4
        }

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

                # АНТИЧИТ НА СЕРВЕРІ: відлік часу в повітрі
                on_ground = pkt.get("on_ground", False)
                on_wall = pkt.get("on_wall", False)
                
                if not on_ground and not on_wall and p["alive"]:
                    p["air_time"] += 0.033
                    if p["air_time"] > 7.0 and p["y"] > -5.0:
                        # БАН на 15 секунд
                        banned_players[client_nick] = time.time() + 15.0
                        await websocket.send(json.dumps({
                            "type": "banned",
                            "msg": "Античит: Виявлено Fly-Hack (>7 сек у повітрі)! Бан на 15 сек."
                        }))
                        await websocket.close()
                        break
                else:
                    p["air_time"] = 0.0

            elif pkt.get("type") == "dead":
                p["alive"] = False
                p["air_time"] = 0.0
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
                    r["timer"] = 60.0
                    for p in pls.values(): 
                        p["alive"] = True
                        p["air_time"] = 0.0
                    # ПЕРШИЙ ЕВЕНТ - ЗАВЖДИ ВИБУХ СТІН ТА ДАХУ!
                    r["active_events"] = ["CUBE_EXPLODE"]

            elif r["state"] == "IN_GAME":
                r["timer"] -= 0.033
                r["madness"] = min(100.0, r["madness"] + 0.033 * 7.5)
                
                if r["madness"] >= 100.0:
                    r["madness"] = 0.0
                    avail = [e for e in (CUBE_MUTATIONS + OTHER_EVENTS) if e not in r["active_events"]]
                    if avail:
                        r["active_events"].append(random.choice(avail))

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
                    for p in pls.values(): 
                        p["alive"] = True
                        p["air_time"] = 0.0

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
    print(f"[*] СЕРВЕР ЗАПУЩЕНО НА {PORT} (АНТИЧИТ + ПРОЗОРИЙ ДАХ)")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
