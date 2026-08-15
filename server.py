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

banned_players = {}

ALL_EVENTS_POOL = [
    "CUBE_EXPLODE", "CUBE_SHRINK", "CUBE_EXPAND", "BOUNCY_WALLS", "CUBE_TWIST",
    "GRAVITY_UP", "MOON_GRAVITY", "SUPER_SPEED", "ICE_PHYSICS",
    "HEAVY_WEIGHT", "INVERT_KEYS", "HYPER_JUMP", "SLOW_MO",
    "DARKNESS", "SCREEN_SHAKE", "RED_ALERT", "COLOR_MADNESS"
]

server_accounts = {}
rooms = {
    name: {
        "state": "IDLE", # IDLE, WAITING, IN_GAME, ROUND_OVER
        "timer": 10.0,
        "madness": 0.0,
        "active_events": [],
        "players": {}
    } for name in ROOM_NAMES
}
client_rooms = {}

async def safe_send(ws, message):
    try:
        await ws.send(message)
    except:
        pass

async def handler(websocket):
    client_nick = None
    room_name = None

    try:
        raw = await websocket.recv()
        data = json.loads(raw)
        req_nick = data.get("nick", "Player")
        client_skin = data.get("skin", "Classic")
        pref_room = data.get("preferred_room", None)

        now = time.time()
        if req_nick in banned_players and banned_players[req_nick] > now:
            left_sec = int(banned_players[req_nick] - now)
            await safe_send(websocket, json.dumps({
                "type": "banned",
                "msg": f"Античит: Бан! Залишилось {left_sec} сек."
            }))
            await websocket.close()
            return

        target_room = None
        if pref_room in ROOM_NAMES and len(rooms[pref_room]["players"]) < MAX_PLAYERS:
            target_room = pref_room
        else:
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
            "y": -2.6,
            "alive": is_alive_now,
            "skin": client_skin,
            "air_time": 0.0
        }

        if rooms[room_name]["state"] == "IDLE":
            rooms[room_name]["state"] = "WAITING"
            rooms[room_name]["timer"] = 10.0

        await safe_send(websocket, json.dumps({
            "type": "init",
            "nick": client_nick,
            "room": room_name,
            "account": server_accounts[client_nick]
        }))

        async for msg in websocket:
            try:
                pkt = json.loads(msg)
                p = rooms[room_name]["players"].get(websocket)
                if not p: continue

                if pkt.get("type") == "pos":
                    p["x"] = pkt["x"]
                    p["y"] = pkt["y"]
                    on_ground = pkt.get("on_ground", False)
                    on_wall = pkt.get("on_wall", False)

                    if not on_ground and not on_wall and p["alive"]:
                        p["air_time"] += 0.033
                        if p["air_time"] > 7.0 and p["y"] > -5.0:
                            banned_players[client_nick] = time.time() + 15.0
                            await safe_send(websocket, json.dumps({
                                "type": "banned",
                                "msg": "Античит: Fly-Hack (>7 сек у повітрі)! Бан на 15 сек."
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
                        await safe_send(ws, chat_pkt)
                elif pkt.get("type") == "buy_skin":
                    s_name, cost = pkt["skin"], pkt["cost"]
                    acc = server_accounts[client_nick]
                    if acc["coins"] >= cost and s_name not in acc["skins"]:
                        acc["coins"] -= cost
                        acc["skins"].append(s_name)
                        await safe_send(websocket, json.dumps({"type": "account_update", "account": acc}))
            except: pass
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

        lobby_stats = {
            r_name: {
                "players": f"{len(r['players'])}/{MAX_PLAYERS}",
                "state": r["state"],
                "events_count": len(r["active_events"])
            } for r_name, r in rooms.items()
        }

        for r_name, r in rooms.items():
            pls = r["players"]
            n_pls = len(pls)

            if n_pls == 0:
                r["state"] = "IDLE"
                r["timer"] = 10.0
                r["active_events"] = []
                r["madness"] = 0.0
                continue

            if r["state"] == "WAITING":
                r["active_events"] = []
                r["madness"] = 0.0
                r["timer"] -= 0.033
                if r["timer"] <= 0 or n_pls >= MAX_PLAYERS:
                    r["state"] = "IN_GAME"
                    for p in pls.values():
                        p["alive"] = True
                        p["air_time"] = 0.0
                    r["active_events"] = [random.choice(ALL_EVENTS_POOL)]

            elif r["state"] == "IN_GAME":
                # Заповнення шкали божевілля (кожні ~12 сек новий евент)
                r["madness"] = min(100.0, r["madness"] + 0.033 * 8.5)

                if r["madness"] >= 100.0:
                    r["madness"] = 0.0
                    avail = [e for e in ALL_EVENTS_POOL if e not in r["active_events"]]
                    if avail:
                        r["active_events"].append(random.choice(avail))
                    # Підтримуємо максимум 3 активних евенти
                    if len(r["active_events"]) > 3:
                        r["active_events"].pop(0)

                alive_players = [p for p in pls.values() if p["alive"]]
                all_dead = (len(alive_players) == 0 and n_pls > 0)
                one_survivor = (len(alive_players) == 1 and n_pls > 1)

                # Перезапуск ТІЛЬКИ коли визначено переможця чи всі впали
                if all_dead or one_survivor:
                    r["state"] = "ROUND_OVER"
                    r["timer"] = 4.0
                    if len(alive_players) == 1:
                        winner = alive_players[0]
                        server_accounts[winner["nick"]]["coins"] += 50
                        for ws, p_data in pls.items():
                            if p_data["nick"] == winner["nick"]:
                                await safe_send(ws, json.dumps({
                                    "type": "account_update",
                                    "account": server_accounts[winner["nick"]],
                                    "win": True
                                }))

            elif r["state"] == "ROUND_OVER":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["state"] = "WAITING"
                    r["timer"] = 8.0
                    r["active_events"] = []
                    for p in pls.values():
                        p["alive"] = True
                        p["x"] = random.uniform(-1.0, 1.0)
                        p["y"] = -2.6
                        p["air_time"] = 0.0

            packet = json.dumps({
                "type": "sync",
                "state": r["state"],
                "room": r_name,
                "timer": max(0, int(r["timer"])),
                "madness": r["madness"],
                "events": r["active_events"],
                "lobby_stats": lobby_stats,
                "players": {
                    p["nick"]: {
                        "x": p["x"], "y": p["y"], "alive": p["alive"],
                        "skin": p["skin"]
                    } for p in pls.values()
                }
            })

            send_tasks = [safe_send(ws, packet) for ws in list(pls.keys())]
            if send_tasks:
                await asyncio.gather(*send_tasks, return_exceptions=True)

async def main():
    print(f"[*] СЕРВЕР ЗАПУЩЕНО НА {PORT} (REAL-TIME ENGINE)")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
