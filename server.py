import asyncio
import websockets
import json
import hashlib
import os
import random
import math
import time

PORT = int(os.environ.get('PORT', 10000))
DB_FILE = 'database.json'
ISLAND_RADIUS = 75.0

# Константи безпеки (Anti-Cheat Thresholds)
MAX_RUN_SPEED = 12.0       # Максимальна швидкість м/с (спринт + запас на пінг)
MAX_REACH_DIST = 5.5       # Максимальна дистанція збору/будівництва
MIN_HARVEST_INTERVAL = 0.22 # Cooldown між ударами (сек)
MIN_BUILD_INTERVAL = 0.3    # Cooldown між будівництвом (сек)

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {"users": {}, "bans": []}

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
for i in range(50, 85):
    ang = random.uniform(0, math.pi * 2)
    dst = random.uniform(5, ISLAND_RADIUS - 8)
    nodes[i] = {"id": i, "type": "rock", "x": math.cos(ang)*dst, "z": math.sin(ang)*dst, "hp": 7}

buildings = []
building_counter = 0
clients = {}

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
            except Exception:
                pass

async def handler(websocket):
    global building_counter
    username = None
    try:
        async for message in websocket:
            req = json.loads(message)
            action = req.get("action")
            now = time.time()

            # Реєстрація
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

            # Вхід
            elif action == "login":
                u = req.get("username", "").strip()
                p = req.get("password", "")
                if u in db.get("bans", []):
                    await websocket.send(json.dumps({"status": "error", "msg": "🚫 ВАШ АККАУНТ ЗАБАНЕНО АНТИЧІТОМ!"}))
                    await websocket.close()
                    return

                if u in db["users"] and db["users"][u]["pwd"] == hash_pwd(p):
                    username = u
                    udata = db["users"][u]
                    clients[websocket] = {
                        "username": username,
                        "x": random.uniform(-10, 10), "y": 1.7, "z": random.uniform(-10, 10),
                        "yaw": 0, "pitch": 0,
                        "hp": 100, "hunger": 100, "thirst": 100,
                        "inv": udata.get("inv", {"wood": 50, "stone": 0}),
                        "friends": udata.get("friends", []),
                        # Параметри безпеки для кожного гравця
                        "last_move_time": now,
                        "last_harvest_time": 0.0,
                        "last_build_time": 0.0,
                        "violation_flags": 0
                    }
                    await websocket.send(json.dumps({"status": "ok", "msg": "Вхід успішний!"}))
                else:
                    await websocket.send(json.dumps({"status": "error", "msg": "Невірні дані!"}))

            # Повідомлення від клієнтського античіта
            elif action == "ac_report":
                detected_process = req.get("detected")
                print(f"[🚨 АНТИЧІТ БАН] Гравець '{username}' заблокований! Виявлено: {detected_process}")
                if username:
                    if "bans" not in db:
                        db["bans"] = []
                    if username not in db["bans"]:
                        db["bans"].append(username)
                        save_db(db)
                await websocket.send(json.dumps({"status": "error", "msg": f"Античіт: Заборонений процес {detected_process}"}))
                await websocket.close()
                return

            # Авторизовані ігрові дії з перевіркою валідності
            elif username and websocket in clients:
                p_data = clients[websocket]

                # 1. ЗАХИСТ ВІД SPEEDHACK, TELEPORT ТА FLYHACK
                if action == "move":
                    new_pos = req.get("pos")
                    new_rot = req.get("rot")
                    if not new_pos or len(new_pos) != 3:
                        continue

                    dt = max(0.001, now - p_data["last_move_time"])
                    p_data["last_move_time"] = now

                    # Розрахунок дистанції переміщення
                    dx = new_pos[0] - p_data["x"]
                    dz = new_pos[2] - p_data["z"]
                    move_dist = math.hypot(dx, dz)
                    max_allowed = (MAX_RUN_SPEED * dt) + 0.8  # Допуск на пінг / ривок

                    # Перевірка висоти (Flyhack)
                    if new_pos[1] > 12.0 or new_pos[1] < 0.0:
                        p_data["violation_flags"] += 1
                        continue  # Ігноруємо спробу літати або провалюватися під мапу

                    # Перевірка швидкості (Speedhack/Teleport)
                    if move_dist > max_allowed:
                        p_data["violation_flags"] += 1
                        # Відхиляємо рух і залишаємо старі координати
                        continue

                    # Якщо все чисто — оновлюємо позицію
                    p_data["x"] = new_pos[0]
                    p_data["y"] = new_pos[1]
                    p_data["z"] = new_pos[2]
                    p_data["yaw"] = new_rot[0]
                    p_data["pitch"] = new_rot[1]

                # 2. ЗАХИСТ ВІД LONG-HIT ТА AUTO-CLICKER (HARVEST)
                elif action == "harvest":
                    nid = req.get("node_id")
                    if now - p_data["last_harvest_time"] < MIN_HARVEST_INTERVAL:
                        continue # Спам кліків / чіт-клікер блокується
                    
                    p_data["last_harvest_time"] = now

                    if nid in nodes and nodes[nid]["hp"] > 0:
                        node = nodes[nid]
                        # Перевірка дистанції (Reach Hack)
                        dist_to_node = math.hypot(node["x"] - p_data["x"], node["z"] - p_data["z"])
                        if dist_to_node <= MAX_REACH_DIST:
                            node["hp"] -= 1
                            res_type = node["type"]
                            add_amt = 15 if res_type == "tree" else 12
                            p_data["inv"]["wood" if res_type == "tree" else "stone"] = p_data["inv"].get("wood" if res_type == "tree" else "stone", 0) + add_amt
                            db["users"][username]["inv"] = p_data["inv"]
                            save_db(db)

                # 3. ЗАХИСТ ВІД ФЕЙКОВОГО БУДІВНИЦТВА ТА БЕЗКОШТОВНИХ СТІН
                elif action == "build":
                    btype = req.get("build_type")
                    bpos = req.get("pos")
                    if not bpos or now - p_data["last_build_time"] < MIN_BUILD_INTERVAL:
                        continue

                    p_data["last_build_time"] = now
                    dist_to_build = math.hypot(bpos[0] - p_data["x"], bpos[2] - p_data["z"])
                    if dist_to_build > MAX_REACH_DIST + 2.0:
                        continue # Будівництво за кілометр заборонено

                    cost = 150 if btype == "wood_foundation" else 100
                    if p_data["inv"].get("wood", 0) >= cost:
                        p_data["inv"]["wood"] -= cost
                        building_counter += 1
                        buildings.append({
                            "id": building_counter, "type": btype,
                            "x": bpos[0], "y": bpos[1], "z": bpos[2],
                            "owner": username
                        })
                        db["users"][username]["inv"] = p_data["inv"]
                        save_db(db)

                # 4. ЗАХИСТ ВІД ЧІТ-КРАФТУ
                elif action == "craft":
                    iid = req.get("item_id")
                    if iid in CRAFTING_RECIPES:
                        cost = CRAFTING_RECIPES[iid]["cost"]
                        # Сервер сам перевіряє наявність ресурсів
                        if all(p_data["inv"].get(r, 0) >= c for r, c in cost.items()):
                            for r, c in cost.items():
                                p_data["inv"][r] -= c
                            p_data["inv"][iid] = p_data["inv"].get(iid, 0) + 1
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
    print(f"[*] Rust Anti-Cheat Server захищено та запущено на порту {PORT}")
    asyncio.create_task(broadcast())
    async with websockets.serve(handler, "0.0.0.0", PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
