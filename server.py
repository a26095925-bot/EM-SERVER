import socket
import threading
import json
import hashlib
import time
import math
import random
import os

HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 5555))  # Підходить для Render

DB_FILE = 'database.json'
ISLAND_RADIUS = 70.0

# ----------------- БАЗА ДАНИХ ТА АУТЕНТИФІКАЦІЯ -----------------
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {"users": {}}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)

def hash_password(pwd):
    return hashlib.sha256(pwd.encode('utf-8')).hexdigest()

db = load_db()

# ----------------- РЕЦЕПТИ КРАФТУ -----------------
CRAFTING_RECIPES = {
    "wood_spear": {"name": "Дерев'яний спис", "cost": {"wood": 300}, "time": 2},
    "stone_hatchet": {"name": "Кам'яна сокира", "cost": {"wood": 200, "stone": 100}, "time": 3},
    "stone_pickaxe": {"name": "Кам'яне кайло", "cost": {"wood": 200, "stone": 100}, "time": 3},
    "building_plan": {"name": "План будівництва", "cost": {"wood": 20}, "time": 1},
    "bandage": {"name": "Бинт (+30 HP)", "cost": {"cloth": 10}, "time": 2},
    "wood_wall": {"name": "Дерев'яна стіна", "cost": {"wood": 100}, "time": 2},
    "wood_foundation": {"name": "Фундамент", "cost": {"wood": 150}, "time": 2}
}

# ----------------- ГЕНЕРАЦІЯ ОСТРОВА ТА СВІТУ -----------------
class World:
    def __init__(self):
        self.nodes = {}
        self.buildings = []  # [{"id": 0, "type": "wall", "x": 0, "y": 0, "z": 0, "rot": 0, "owner": "nick"}]
        self.building_id_counter = 0
        self.generate_resources()

    def generate_resources(self):
        node_id = 0
        # Дерева ближче до центру та в лісах
        for _ in range(60):
            angle = random.uniform(0, math.pi * 2)
            dist = random.uniform(5, ISLAND_RADIUS - 10)
            x = math.cos(angle) * dist
            z = math.sin(angle) * dist
            self.nodes[node_id] = {"id": node_id, "type": "tree", "x": x, "z": z, "hp": 5}
            node_id += 1
            
        # Каміння та корисні копалини
        for _ in range(35):
            angle = random.uniform(0, math.pi * 2)
            dist = random.uniform(5, ISLAND_RADIUS - 8)
            x = math.cos(angle) * dist
            z = math.sin(angle) * dist
            self.nodes[node_id] = {"id": node_id, "type": "rock", "x": x, "z": z, "hp": 7}
            node_id += 1

world = World()
clients = {}  # conn: {"username": str, "x": 0, "y": 0, "z": 0, "yaw": 0, "pitch": 0, "hp": 100, "hunger": 100, "thirst": 100, "inv": {...}, "friends": []}
lock = threading.Lock()

# ----------------- ОБРОБКА ПАКЕТІВ КЛІЄНТА -----------------
def handle_client(conn, addr):
    print(f"[+] Нове підключення: {addr}")
    username = None
    buffer = ""

    try:
        while True:
            data = conn.recv(4096).decode('utf-8')
            if not data:
                break
            buffer += data
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                req = json.loads(line)
                action = req.get("action")

                # 1. Реєстрація
                if action == "register":
                    u = req.get("username", "").strip()
                    p = req.get("password", "")
                    with lock:
                        if u in db["users"]:
                            conn.sendall((json.dumps({"status": "error", "msg": "Користувач вже існує!"}) + "\n").encode())
                        elif len(u) < 3 or len(p) < 4:
                            conn.sendall((json.dumps({"status": "error", "msg": "Логін >= 3, Пароль >= 4 симв."}) + "\n").encode())
                        else:
                            db["users"][u] = {
                                "pwd": hash_password(p),
                                "inv": {"wood": 100, "stone": 50, "cloth": 30},
                                "friends": []
                            }
                            save_db(db)
                            conn.sendall((json.dumps({"status": "ok", "msg": "Успішна реєстрація!"}) + "\n").encode())

                # 2. Вхід
                elif action == "login":
                    u = req.get("username", "").strip()
                    p = req.get("password", "")
                    with lock:
                        if u in db["users"] and db["users"][u]["pwd"] == hash_password(p):
                            username = u
                            user_data = db["users"][u]
                            clients[conn] = {
                                "username": username,
                                "x": random.uniform(-10, 10), "y": 1.8, "z": random.uniform(-10, 10),
                                "yaw": 0, "pitch": 0,
                                "hp": 100, "hunger": 100, "thirst": 100,
                                "inv": user_data.get("inv", {"wood": 50, "stone": 0, "cloth": 10}),
                                "friends": user_data.get("friends", [])
                            }
                            resp = {
                                "status": "ok",
                                "msg": f"Ласкаво просимо, {username}!",
                                "player": clients[conn],
                                "nodes": world.nodes,
                                "buildings": world.buildings
                            }
                            conn.sendall((json.dumps(resp) + "\n").encode())
                            print(f"[AUTH] {username} зайшов у гру.")
                        else:
                            conn.sendall((json.dumps({"status": "error", "msg": "Невірний логін або пароль!"}) + "\n").encode())

                # Дії в грі (потрібна авторизація)
                elif username and conn in clients:
                    p_data = clients[conn]

                    if action == "move":
                        p_data["x"] = req["pos"][0]
                        p_data["y"] = req["pos"][1]
                        p_data["z"] = req["pos"][2]
                        p_data["yaw"] = req["rot"][0]
                        p_data["pitch"] = req["rot"][1]

                    elif action == "harvest":
                        nid = req.get("node_id")
                        with lock:
                            if nid in world.nodes and world.nodes[nid]["hp"] > 0:
                                node = world.nodes[nid]
                                node["hp"] -= 1
                                if node["type"] == "tree":
                                    p_data["inv"]["wood"] = p_data["inv"].get("wood", 0) + 20
                                elif node["type"] == "rock":
                                    p_data["inv"]["stone"] = p_data["inv"].get("stone", 0) + 15
                                db["users"][username]["inv"] = p_data["inv"]
                                save_db(db)

                    elif action == "craft":
                        item_id = req.get("item_id")
                        if item_id in CRAFTING_RECIPES:
                            recipe = CRAFTING_RECIPES[item_id]
                            can_craft = True
                            with lock:
                                for res, count in recipe["cost"].items():
                                    if p_data["inv"].get(res, 0) < count:
                                        can_craft = False
                                        break
                                if can_craft:
                                    for res, count in recipe["cost"].items():
                                        p_data["inv"][res] -= count
                                    p_data["inv"][item_id] = p_data["inv"].get(item_id, 0) + 1
                                    db["users"][username]["inv"] = p_data["inv"]
                                    save_db(db)

                    elif action == "build":
                        b_type = req.get("build_type") # "foundation" / "wall"
                        cost = 150 if b_type == "foundation" else 100
                        with lock:
                            if p_data["inv"].get("wood", 0) >= cost:
                                p_data["inv"]["wood"] -= cost
                                world.building_id_counter += 1
                                b_obj = {
                                    "id": world.building_id_counter,
                                    "type": b_type,
                                    "x": req["pos"][0], "y": req["pos"][1], "z": req["pos"][2],
                                    "rot": req.get("rot", 0),
                                    "owner": username
                                }
                                world.buildings.append(b_obj)
                                db["users"][username]["inv"] = p_data["inv"]
                                save_db(db)

                    elif action == "add_friend":
                        target = req.get("target")
                        with lock:
                            if target in db["users"] and target != username:
                                if target not in p_data["friends"]:
                                    p_data["friends"].append(target)
                                    db["users"][username]["friends"] = p_data["friends"]
                                    save_db(db)
    except Exception as e:
        print(f"[!] Помилка з клієнтом {addr}: {e}")
    finally:
        with lock:
            if conn in clients:
                del clients[conn]
        conn.close()
        print(f"[-] Відключено: {addr}")

# Постійна розсилка стану світу клієнтам (20 разів на сек)
def broadcast_loop():
    while True:
        time.sleep(0.05)
        with lock:
            if not clients:
                continue
            
            players_list = []
            for c, data in clients.items():
                players_list.append({
                    "username": data["username"],
                    "x": data["x"], "y": data["y"], "z": data["z"],
                    "yaw": data["yaw"]
                })

            for c, data in list(clients.items()):
                # Поступове зменшення їжі/води
                data["hunger"] = max(0, data["hunger"] - 0.01)
                data["thirst"] = max(0, data["thirst"] - 0.015)
                if data["hunger"] == 0 or data["thirst"] == 0:
                    data["hp"] = max(0, data["hp"] - 0.05)

                packet = {
                    "type": "sync",
                    "players": players_list,
                    "hp": data["hp"],
                    "hunger": data["hunger"],
                    "thirst": data["thirst"],
                    "inv": data["inv"],
                    "friends": data["friends"],
                    "nodes": world.nodes,
                    "buildings": world.buildings
                }
                try:
                    c.sendall((json.dumps(packet) + "\n").encode())
                except:
                    pass

def run_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(10)
    print(f"=== Rust Python Server запущено на порті {PORT} ===")
    threading.Thread(target=broadcast_loop, daemon=True).start()
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    run_server()
