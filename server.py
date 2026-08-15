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

# База збережень на сервері: { nickname: {"coins": 0, "skins": ["Classic"], "wins": 0} }
server_accounts = {}

rooms = {
    name: {
        "state": "WAITING", # WAITING, IN_GAME, ROUND_OVER
        "timer": 20.0,
        "madness": 0.0,
        "active_events": [],
        "players": {} # ws: {nick, x, y, alive, color, skin, emote, emote_time}
    } for name in ROOM_NAMES
}
client_rooms = {}

EVENTS_POOL = ["GRAVITY_FLIP", "SPEED_TURBO", "CUBE_SHAKE", "INVERT_CONTROLS"]

def save_db():
    try:
        with open("server_db.json", "w") as f:
            json.dumps(server_accounts, f)
    except: pass

def get_unique_nick(room_name, base_nick):
    used_nicks = [p["nick"] for p in rooms[room_name]["players"].values()]
    if base_nick not in used_nicks:
        return base_nick
    return f"{base_nick}_2"

async def handler(websocket):
    client_nick = None
    room_name = None

    try:
        # Перший пакет - логін
        raw = await websocket.recv()
        data = json.loads(raw)
        req_nick = data.get("nick", f"Player_{random.randint(100,999)}")
        client_skin = data.get("skin", "Classic")

        # Шукаємо найкращу кімнату
        target_room = None
        for r in ROOM_NAMES:
            if len(rooms[r]["players"]) < MAX_PLAYERS and req_nick not in [p["nick"] for p in rooms[r]["players"].values()]:
                target_room = r
                break
        if not target_room:
            for r in ROOM_NAMES:
                if len(rooms[r]["players"]) < MAX_PLAYERS:
                    target_room = r
                    break
        if not target_room:
            target_room = random.choice(ROOM_NAMES)

        room_name = target_room
        client_rooms[websocket] = room_name
        client_nick = get_unique_nick(room_name, req_nick)

        # Завантаження акаунта
        if client_nick not in server_accounts:
            server_accounts[client_nick] = {"coins": data.get("coins", 0), "skins": ["Classic"], "wins": 0}
        acc = server_accounts[client_nick]

        rooms[room_name]["players"][websocket] = {
            "nick": client_nick,
            "x": random.uniform(-1.5, 1.5),
            "y": -2.6,
            "alive": True,
            "skin": client_skin,
            "emote": "",
            "emote_time": 0
        }

        # Відповідь клієнту
        await websocket.send(json.dumps({
            "type": "init",
            "nick": client_nick,
            "room": room_name,
            "account": acc
        }))

        # Обробка дій клієнта
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
                # Трансляція повідомлення в чат кімнати
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
        await asyncio.sleep(0.033) # 30 FPS
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
                        for p in pls.values():
                            p["alive"] = True
                else:
                    r["timer"] = 20.0

            elif r["state"] == "IN_GAME":
                # Заповнення шкали божевілля
                r["madness"] = min(100.0, r["madness"] + 0.033 * 4.5)
                if r["madness"] >= 100.0:
                    r["madness"] = 0.0
                    new_ev = random.choice([e for e in EVENTS_POOL if e not in r["active_events"]] or EVENTS_POOL)
                    r["active_events"].append(new_ev)

                # Перевірка тих, хто вижив
                alive_players = [p for p in pls.values() if p["alive"]]
                if (len(alive_players) <= 1 and n_pls > 1) or (n_pls == 1 and not alive_players):
                    r["state"] = "ROUND_OVER"
                    r["timer"] = 5.0
                    if len(alive_players) == 1:
                        winner = alive_players[0]
                        server_accounts[winner["nick"]]["coins"] += 50
                        server_accounts[winner["nick"]]["wins"] += 1
                        # Оновлюємо переможця
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
                    for p in pls.values():
                        p["alive"] = True

            # Пакет стану кімнати
            room_packet = json.dumps({
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
                try: asyncio.create_task(ws.send(room_packet))
                except: pass

async def main():
    print(f"[*] СЕРВЕР EVENT MADNESS ПРАЦЮЄ НА {PORT}")
    asyncio.create_task(game_loop())
    async with websockets.serve(handler, HOST, PORT, ping_interval=10, ping_timeout=5):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
