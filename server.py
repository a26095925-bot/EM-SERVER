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

ALL_EVENTS_POOL = [
    "CUBE_EXPLODE", "CUBE_SHRINK", "CUBE_EXPAND", "BOUNCY_WALLS", "CUBE_TWIST",
    "GRAVITY_UP", "MOON_GRAVITY", "SUPER_SPEED", "ICE_PHYSICS", "HEAVY_WEIGHT",
    "INVERT_KEYS", "HYPER_JUMP", "SLOW_MO", "DARKNESS", "SCREEN_SHAKE",
    "RED_ALERT", "COLOR_MADNESS", "METEOR_STORM", "GRAVITY_LEFT", "GRAVITY_RIGHT"
]

server_accounts = {}
rooms = {
    name: {
        "state": "IDLE",
        "mode": "BATTLE", # BATTLE, SPEEDRUN, SHERIFF
        "start_time": time.time(),
        "timer": 10.0,
        "madness": 0.0,
        "active_events": [],
        "battle_task": "CEILING_SMASH",
        "task_timer": 8.0,
        "sheriff_draw_time": 0.0,
        "sheriff_can_shoot": False,
        "speedrun_checkpoint": 1,
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
        client_prefix = data.get("prefix", "")
        client_trail = data.get("trail", "None")
        client_avatar_b64 = data.get("avatar_b64", "")
        is_itch = data.get("is_itch", False)
        pref_room = data.get("preferred_room", None)
        req_mode = data.get("mode", "BATTLE")

        if pref_room == "Cube-1" and not is_itch:
            pref_room = "Cube-2"

        eligible_rooms = ROOM_NAMES if is_itch else ["Cube-2", "Cube-3"]
        target_room = pref_room if pref_room in eligible_rooms and len(rooms[pref_room]["players"]) < MAX_PLAYERS else eligible_rooms[0]

        room_name = target_room
        client_rooms[websocket] = room_name

        used_nicks = [p["nick"] for p in rooms[room_name]["players"].values()]
        client_nick = req_nick if req_nick not in used_nicks else f"{req_nick}_{random.randint(2,9)}"

        if client_nick not in server_accounts:
            server_accounts[client_nick] = {"cubixes": data.get("cubixes", 0), "skins": ["Classic"], "prefixes": [""], "trails": ["None"], "wins": 0}

        rooms[room_name]["mode"] = req_mode
        is_alive_now = (rooms[room_name]["state"] in ["IDLE", "WAITING"])

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": random.uniform(-1.5, 1.5),
            "y": -4.2,
            "hp": 100,
            "alive": is_alive_now,
            "skin": client_skin,
            "prefix": client_prefix,
            "trail": client_trail,
            "avatar_b64": client_avatar_b64,
            "is_itch": is_itch,
            "air_time": 0.0
        }

        if rooms[room_name]["state"] == "IDLE":
            rooms[room_name]["state"] = "WAITING"
            rooms[room_name]["start_time"] = time.time()
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
                    p["hp"] = pkt.get("hp", p["hp"])

                elif pkt.get("type") == "damage_all":
                    dmg = pkt.get("dmg", 35)
                    for ws_other, p_other in rooms[room_name]["players"].items():
                        if p_other["nick"] != client_nick and p_other["alive"]:
                            p_other["hp"] = max(0, p_other["hp"] - dmg)
                            if p_other["hp"] <= 0: p_other["alive"] = False

                elif pkt.get("type") == "speedrun_win":
                    rooms[room_name]["state"] = "ROUND_OVER"
                    rooms[room_name]["timer"] = 4.0
                    server_accounts[client_nick]["cubixes"] += 50
                    server_accounts[client_nick]["wins"] += 1
                    await safe_send(websocket, json.dumps({"type": "account_update", "account": server_accounts[client_nick], "win": True}))

                elif pkt.get("type") == "sheriff_shot":
                    if rooms[room_name]["sheriff_can_shoot"] and rooms[room_name]["state"] == "IN_GAME":
                        # First valid shot wins duel
                        rooms[room_name]["state"] = "ROUND_OVER"
                        rooms[room_name]["timer"] = 4.0
                        server_accounts[client_nick]["cubixes"] += 50
                        server_accounts[client_nick]["wins"] += 1
                        for ws_o, po in rooms[room_name]["players"].items():
                            if po["nick"] != client_nick: po["alive"] = False; po["hp"] = 0
                        await safe_send(websocket, json.dumps({"type": "account_update", "account": server_accounts[client_nick], "win": True}))

                elif pkt.get("type") == "dead":
                    p["alive"] = False
                    p["hp"] = 0

                elif pkt.get("type") == "chat":
                    chat_pkt = json.dumps({"type": "chat", "nick": client_nick, "prefix": p.get("prefix", ""), "text": pkt["text"]})
                    for ws in list(rooms[room_name]["players"].keys()):
                        await safe_send(ws, chat_pkt)

                elif pkt.get("type") == "buy_item":
                    category, item, cost = pkt["category"], pkt["item"], pkt["cost"]
                    acc = server_accounts[client_nick]
                    if acc["cubixes"] >= cost and item not in acc.get(category, []):
                        acc["cubixes"] -= cost
                        if category not in acc: acc[category] = []
                        acc[category].append(item)
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
        now = time.time()

        for r_name, r in rooms.items():
            pls = r["players"]
            n_pls = len(pls)

            if n_pls == 0:
                r["state"] = "IDLE"
                r["timer"] = 10.0
                continue

            if r["state"] == "WAITING":
                r["timer"] -= 0.033
                if r["timer"] <= 0 or n_pls >= MAX_PLAYERS:
                    r["state"] = "IN_GAME"
                    r["start_time"] = time.time()
                    for p in pls.values():
                        p["alive"] = True
                        p["hp"] = 100
                    r["active_events"] = [random.choice(ALL_EVENTS_POOL)]
                    
                    if r["mode"] == "SHERIFF":
                        r["sheriff_draw_time"] = now + random.uniform(3.5, 7.0)
                        r["sheriff_can_shoot"] = False
                    elif r["mode"] == "BATTLE":
                        r["battle_task"] = random.choice(["CEILING_SMASH", "CLICK_CLASH", "GROUND_STOMP"])
                        r["task_timer"] = 10.0

            elif r["state"] == "IN_GAME":
                # Mode specific logic
                if r["mode"] == "SHERIFF":
                    if now >= r["sheriff_draw_time"]:
                        r["sheriff_can_shoot"] = True

                elif r["mode"] == "BATTLE":
                    r["task_timer"] -= 0.033
                    if r["task_timer"] <= 0:
                        r["task_timer"] = 10.0
                        r["battle_task"] = random.choice(["CEILING_SMASH", "CLICK_CLASH", "GROUND_STOMP"])

                # Check survivors in battle
                if r["mode"] == "BATTLE":
                    alive_pls = [p for p in pls.values() if p["alive"] and p["hp"] > 0]
                    if (len(alive_pls) == 1 and n_pls > 1) or (len(alive_pls) == 0 and n_pls > 0):
                        r["state"] = "ROUND_OVER"
                        r["timer"] = 4.0
                        if len(alive_pls) == 1:
                            w = alive_pls[0]
                            server_accounts[w["nick"]]["cubixes"] += 50
                            server_accounts[w["nick"]]["wins"] += 1
                            for ws, p in pls.items():
                                if p["nick"] == w["nick"]:
                                    await safe_send(ws, json.dumps({"type": "account_update", "account": server_accounts[w["nick"]], "win": True}))

            elif r["state"] == "ROUND_OVER":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["state"] = "WAITING"
                    r["timer"] = 6.0
                    for p in pls.values():
                        p["alive"] = True
                        p["hp"] = 100
                        p["x"] = random.uniform(-1.5, 1.5)
                        p["y"] = -4.2

            packet = json.dumps({
                "type": "sync",
                "state": r["state"],
                "mode": r["mode"],
                "room": r_name,
                "server_time": now,
                "timer": max(0, int(r["timer"])),
                "battle_task": r.get("battle_task", "CEILING_SMASH"),
                "task_timer": max(0, int(r.get("task_timer", 0))),
                "sheriff_can_shoot": r.get("sheriff_can_shoot", False),
                "events": r["active_events"],
                "players": {
                    p["nick"]: {
                        "x": p["x"], "y": p["y"], "hp": p["hp"], "alive": p["alive"],
                        "skin": p["skin"], "prefix": p.get("prefix", ""),
                        "avatar_b64": p.get("avatar_b64", "")
                    } for p in pls.values()
                }
            })

            send_tasks = [safe_send(ws, packet) for ws in list(pls.keys())]
            if send_tasks:
                await asyncio.gather(*send_tasks, return_exceptions=True)

async def main():
    print(f"[*] MULTI-MODE SERVER ONLINE ON PORT {PORT}")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
