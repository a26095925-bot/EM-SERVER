import asyncio
import websockets
import json
import os
import time
import random

PORT = int(os.environ.get("PORT", 10000))
HOST = "0.0.0.0"

# Вестерн-арени: 5 кімнат 1v1 + 2 кімнати 1v1v1
ROOMS_CONFIG = {
    "Canyon-1": 2, "Canyon-2": 2, "Canyon-3": 2, "Canyon-4": 2, "Canyon-5": 2,
    "Saloon-1": 3, "Saloon-2": 3
}
ROOM_NAMES = list(ROOMS_CONFIG.keys())

banned_players = {}
server_accounts = {}

rooms = {
    name: {
        "state": "IDLE", # IDLE, WAITING, STANDOFF_WALK, COUNTDOWN, DUEL_COMBAT, ROUND_OVER
        "max_players": max_p,
        "timer": 4.0,
        "countdown": 3,
        "first_draw_winner": None,
        "is_bot_match": False,
        "players": {}
    } for name, max_p in ROOMS_CONFIG.items()
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
        req_nick = data.get("nick", "Sheriff")
        client_skin = data.get("skin", "Classic")
        client_prefix = data.get("prefix", "")
        client_avatar_b64 = data.get("avatar_b64", "")
        is_itch = data.get("is_itch", False)
        pref_room = data.get("preferred_room", None)

        now = time.time()
        if req_nick in banned_players and banned_players[req_nick] > now:
            left_sec = int(banned_players[req_nick] - now)
            await safe_send(websocket, json.dumps({
                "type": "banned",
                "msg": f"Banned! Remaining {left_sec}s"
            }))
            await websocket.close()
            return

        target_room = None
        if pref_room in ROOM_NAMES and len(rooms[pref_room]["players"]) < rooms[pref_room]["max_players"]:
            target_room = pref_room
        else:
            for r in ROOM_NAMES:
                if rooms[r]["state"] == "WAITING" and len(rooms[r]["players"]) < rooms[r]["max_players"]:
                    target_room = r; break
            if not target_room:
                for r in ROOM_NAMES:
                    if rooms[r]["state"] in ["IDLE", "WAITING"] and len(rooms[r]["players"]) < rooms[r]["max_players"]:
                        target_room = r; break
            if not target_room: target_room = ROOM_NAMES[0]

        room_name = target_room
        client_rooms[websocket] = room_name

        used_nicks = [p["nick"] for p in rooms[room_name]["players"].values()]
        client_nick = req_nick if req_nick not in used_nicks else f"{req_nick}_{random.randint(2,9)}"

        if client_nick not in server_accounts:
            server_accounts[client_nick] = {"cubixes": data.get("cubixes", 0), "skins": ["Classic"], "prefixes": [""], "wins": 0}

        spawn_x = -2.4 if len(rooms[room_name]["players"]) == 0 else 2.4

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": spawn_x,
            "y": -2.6,
            "hp": 100,
            "alive": True,
            "skin": client_skin,
            "prefix": client_prefix,
            "avatar_b64": client_avatar_b64,
            "is_itch": is_itch
        }

        if rooms[room_name]["state"] == "IDLE":
            rooms[room_name]["state"] = "WAITING"
            rooms[room_name]["timer"] = 6.0
            rooms[room_name]["is_bot_match"] = False

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

                elif pkt.get("type") == "start_bot":
                    rooms[room_name]["is_bot_match"] = True
                    rooms[room_name]["state"] = "STANDOFF_WALK"
                    rooms[room_name]["timer"] = 3.0

                elif pkt.get("type") == "quick_draw_claim":
                    if rooms[room_name]["state"] == "COUNTDOWN" and rooms[room_name]["countdown"] <= 0:
                        if not rooms[room_name]["first_draw_winner"]:
                            rooms[room_name]["first_draw_winner"] = client_nick
                            for ws_c in list(rooms[room_name]["players"].keys()):
                                await safe_send(ws_c, json.dumps({"type": "quick_draw_awarded", "winner": client_nick}))

                elif pkt.get("type") == "bullet_fired":
                    bullet_pkt = json.dumps({
                        "type": "bullet_tracer",
                        "from_x": pkt["from_x"], "from_y": pkt["from_y"],
                        "dir_x": pkt["dir_x"], "dir_y": pkt["dir_y"],
                        "shooter": client_nick
                    })
                    for ws_c in list(rooms[room_name]["players"].keys()):
                        await safe_send(ws_c, bullet_pkt)

                elif pkt.get("type") == "hit_damage":
                    target_nick = pkt.get("target")
                    dmg = pkt.get("dmg", 45)
                    for ws_c, po in rooms[room_name]["players"].items():
                        if po["nick"] == target_nick and po["alive"]:
                            po["hp"] = max(0, po["hp"] - dmg)
                            if po["hp"] <= 0: po["alive"] = False

                elif pkt.get("type") == "dead":
                    p["alive"] = False
                    p["hp"] = 0

                elif pkt.get("type") == "chat":
                    chat_pkt = json.dumps({"type": "chat", "nick": client_nick, "prefix": p.get("prefix", ""), "text": pkt["text"]})
                    for ws_c in list(rooms[room_name]["players"].keys()):
                        await safe_send(ws_c, chat_pkt)
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

        lobby_stats = {
            r_name: {
                "players": f"{len(r['players'])}/{r['max_players']}",
                "state": r["state"],
                "max": r["max_players"]
            } for r_name, r in rooms.items()
        }

        for r_name, r in rooms.items():
            pls = r["players"]
            n_pls = len(pls)

            if n_pls == 0:
                r["state"] = "IDLE"
                r["timer"] = 5.0
                r["is_bot_match"] = False
                continue

            if r["state"] == "WAITING":
                if not r["is_bot_match"]:
                    if n_pls >= r["max_players"]:
                        r["state"] = "STANDOFF_WALK"
                        r["timer"] = 3.0
                        r["first_draw_winner"] = None
                        for p in pls.values(): p["alive"] = True; p["hp"] = 100

            elif r["state"] == "STANDOFF_WALK":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["state"] = "COUNTDOWN"
                    r["countdown"] = 3
                    r["timer"] = 1.0

            elif r["state"] == "COUNTDOWN":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["countdown"] -= 1
                    r["timer"] = 1.0
                    if r["countdown"] < 0:
                        r["state"] = "DUEL_COMBAT"
                        r["timer"] = 60.0

            elif r["state"] == "DUEL_COMBAT":
                r["timer"] -= 0.033
                alive_pls = [p for p in pls.values() if p["alive"] and p["hp"] > 0]
                
                if (len(alive_pls) == 1 and n_pls > 1) or (len(alive_pls) == 0 and n_pls > 0) or r["timer"] <= 0:
                    r["state"] = "ROUND_OVER"
                    r["timer"] = 4.0
                    if len(alive_pls) == 1:
                        winner = alive_pls[0]
                        if winner["nick"] in server_accounts:
                            server_accounts[winner["nick"]]["cubixes"] += 50
                            server_accounts[winner["nick"]]["wins"] += 1
                            for ws_c, po in pls.items():
                                if po["nick"] == winner["nick"]:
                                    await safe_send(ws_c, json.dumps({"type": "account_update", "account": server_accounts[winner["nick"]], "win": True}))

            elif r["state"] == "ROUND_OVER":
                r["timer"] -= 0.033
                if r["timer"] <= 0:
                    r["state"] = "STANDOFF_WALK"
                    r["timer"] = 3.0
                    r["first_draw_winner"] = None
                    for po in pls.values():
                        po["alive"] = True
                        po["hp"] = 100

            packet = json.dumps({
                "type": "sync",
                "state": r["state"],
                "room": r_name,
                "server_time": now,
                "timer": max(0, int(r["timer"])),
                "countdown": r["countdown"],
                "is_bot_match": r["is_bot_match"],
                "lobby_stats": lobby_stats,
                "players": {
                    po["nick"]: {
                        "x": po["x"], "y": po["y"], "hp": po["hp"], "alive": po["alive"],
                        "skin": po["skin"], "prefix": po.get("prefix", ""),
                        "avatar_b64": po.get("avatar_b64", "")
                    } for po in pls.values()
                }
            })

            send_tasks = [safe_send(ws_c, packet) for ws_c in list(pls.keys())]
            if send_tasks: await asyncio.gather(*send_tasks, return_exceptions=True)

async def main():
    print(f"[*] CANYON DUEL SERVER ONLINE ON PORT {PORT}")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
