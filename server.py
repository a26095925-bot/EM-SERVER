import asyncio
import websockets
import json
import hashlib
import os
import random
import math

PORT = int(os.environ.get('PORT', 10000))
DB_FILE = 'database.json'
ISLAND_RADIUS = 70.0

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {"users": {}}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)

def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode('utf-8')).hexdigest()

db = load_db()

CRAFTING_RECIPES = {
    "wood_spear": {"cost": {"wood": 300}},
    "stone_hatchet": {"cost": {"wood": 200, "stone": 100}},
    "stone_pickaxe": {"cost": {"wood": 200, "stone": 100}},
    "building_plan": {"cost": {"wood": 20}},
    "wood_wall": {"cost": {"wood": 100}},
    "wood_foundation": {"cost": {"wood": 150}}
}

# Генерація ресурсів
nodes = {}
for i in range(50):
    ang = random.uniform(0, math.pi * 2)
    dst = random.uniform(5, ISLAND_RADIUS - 10)
    nodes[i] = {"id": i, "type": "tree", "x": math.cos(ang)*dst, "z": math.sin(ang)*dst, "hp": 5}
for i in range(50, 80):
    ang = random.uniform(0, math.pi * 2)
    dst = random.uniform(5, ISLAND_RADIUS - 8)
    nodes[i] = {"id": i, "type": "rock", "x": math.cos(ang)*dst, "z": math.sin(ang)*dst, "hp": 7}

buildings = []
building_counter = 0
clients = {}  # ws: player_data

async def broadcast():
    while True:
        await asyncio.sleep(0.05)
        if not clients:
            continue
        
        plist = [{"username": d["username"], "x": d["x"], "y": d["y"], "z": d["z"], "yaw": d["yaw"]} for d in clients.values()]
        
        for ws, data in list(clients.items()):
            data["hunger"] = max(0, data["hunger"] - 0.008)
            data["thirst"] = max(0, data["thirst"] - 0.012)
            if data["hunger"] == 0 or data["thirst"] == 0:
                data["hp"] = max(0, data["hp"] - 0.04)

            packet = {
                "type": "sync",
                "players": plist,
                "hp": data["hp"],
                "hunger": data["hunger"],
                "thirst": data["thirst"],
                "inv": data["inv"],
                "friends": data["friends"],
                "nodes": nodes,
                "buildings": buildings
            }
            try:
                await ws.send(json.dumps(packet))
            except:
                pass

async def handler(websocket):
    global building_counter
    username = None
    try:
        async for message in websocket:
            req = json.loads(message)
            action = req.get("action")

            if action == "register":
                u = req.get("username", "").strip()
                p = req.get("password", "")
                if u in db["users"]:
                    await websocket.send(json.dumps({"status": "error", "msg": "Нік зайнятий!"}))
                elif len(u) < 3 or len(p) < 4:
                    await websocket.send(json.dumps({"status": "error", "msg": "Короткий логін/пароль"}))
                else:
                    db["users"][u] = {"pwd": hash_pwd(p), "inv": {"wood": 100, "stone": 50, "cloth": 30}, "friends": []}
                    save_db(db)
                    await websocket.send(json.dumps({"status": "ok", "msg": "Успішна реєстрація!"}))

            elif action == "login":
                u = req.get("username", "").strip()
                p = req.get("password", "")
                if u in db["users"] and db["users"][u]["pwd"] == hash_pwd(p):
                    username = u
                    udata = db["users"][u]
                    clients[websocket] = {
                        "username": username,
                        "x": random.uniform(-10, 10), "y": 1.8, "z": random.uniform(-10, 10),
                        "yaw": 0, "pitch": 0, "hp": 100, "hunger": 100, "thirst": 100,
                        "inv": udata.get("inv", {"wood": 50, "stone": 0}),
                        "friends": udata.get("friends", [])
                    }
                    await websocket.send(json.dumps({"status": "ok", "msg": "Вхід успішний!"}))
                else:
                    await websocket.send(json.dumps({"status": "error", "msg": "Невірні дані!"}))

            elif username and websocket in clients:
                p_data = clients[websocket]
                if action == "move":
                    p_data["x"], p_data["y"], p_data["z"] = req["pos"]
                    p_data["yaw"], p_data["pitch"] = req["rot"]

                elif action == "harvest":
                    nid = req.get("node_id")
                    if nid in nodes and nodes[nid]["hp"] > 0:
                        nodes[nid]["hp"] -= 1
                        res_type = nodes[nid]["type"]
                        p_data["inv"]["wood" if res_type == "tree" else "stone"] = p_data["inv"].get("wood" if res_type == "tree" else "stone", 0) + 15
                        db["users"][username]["inv"] = p_data["inv"]
                        save_db(db)

                elif action == "craft":
                    iid = req.get("item_id")
                    if iid in CRAFTING_RECIPES:
                        cost = CRAFTING_RECIPES[iid]["cost"]
                        if all(p_data["inv"].get(r, 0) >= c for r, c in cost.items()):
                            for r, c in cost.items():
                                p_data["inv"][r] -= c
                            p_data["inv"][iid] = p_data["inv"].get(iid, 0) + 1
                            db["users"][username]["inv"] = p_data["inv"]
                            save_db(db)

                elif action == "build":
                    btype = req.get("build_type")
                    cost = 150 if btype == "wood_foundation" else 100
                    if p_data["inv"].get("wood", 0) >= cost:
                        p_data["inv"]["wood"] -= cost
                        building_counter += 1
                        buildings.append({
                            "id": building_counter, "type": btype,
                            "x": req["pos"][0], "y": req["pos"][1], "z": req["pos"][2],
                            "owner": username
                        })
                        db["users"][username]["inv"] = p_data["inv"]
                        save_db(db)

                elif action == "add_friend":
                    tgt = req.get("target")
                    if tgt in db["users"] and tgt != username and tgt not in p_data["friends"]:
                        p_data["friends"].append(tgt)
                        db["users"][username]["friends"] = p_data["friends"]
                        save_db(db)
    finally:
        if websocket in clients:
            del clients[websocket]

async def main():
    print(f"[*] Rust WS Server працює на порту {PORT}")
    asyncio.create_task(broadcast())
    async with websockets.serve(handler, "0.0.0.0", PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
