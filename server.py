import socket
import threading
import json
import time

HOST = "0.0.0.0"
PORT = 5555

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) # Без затримок
server.bind((HOST, PORT))
server.listen()

print(f"[*] СЕРВЕР ЗАПУЩЕНО НА ПОРТУ {PORT}")
print("[*] Очікування підключень гравців...")

clients = {}  # {client_socket: player_id}
players = {}  # {player_id: {"x": 0.0, "y": 0.0, "color": [r, g, b]}}
player_counter = 0
lock = threading.Lock()

COLORS = [
    [0.2, 0.6, 1.0],  # Блакитний
    [1.0, 0.3, 0.3],  # Червоний
    [0.3, 0.9, 0.3],  # Зелений
    [0.9, 0.3, 0.9],  # Фіолетовий
]

def handle_client(conn, addr):
    global player_counter
    with lock:
        player_counter += 1
        p_id = f"Player_{player_counter}"
        assigned_color = COLORS[(player_counter - 1) % len(COLORS)]
        clients[conn] = p_id
        players[p_id] = {"x": 0.0, "y": -2.6, "color": assigned_color}

    print(f"[+] Гравець {p_id} підключився з {addr}")

    # Відправляємо клієнту його власний ID та колір
    init_packet = json.dumps({"type": "init", "id": p_id, "color": assigned_color}) + "\n"
    try:
        conn.sendall(init_packet.encode())
    except:
        return

    buffer = ""
    while True:
        try:
            data = conn.recv(1024).decode()
            if not data:
                break
            buffer += data
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line)
                
                # Оновлюємо координати гравця
                if msg.get("type") == "pos":
                    with lock:
                        if p_id in players:
                            players[p_id]["x"] = msg["x"]
                            players[p_id]["y"] = msg["y"]

                # Відправляємо клієнту стан усіх гравців
                with lock:
                    world_state = json.dumps({"type": "world", "players": players}) + "\n"
                conn.sendall(world_state.encode())

        except Exception as e:
            break

    print(f"[-] Гравець {p_id} відключився")
    with lock:
        if conn in clients:
            del clients[conn]
        if p_id in players:
            del players[p_id]
    conn.close()

while True:
    conn, addr = server.accept()
    threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()