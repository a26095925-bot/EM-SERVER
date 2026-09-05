import os
import math
import random
import asyncio
import socketio
from aiohttp import web

# Налаштування Socket.IO сервера для стабільного з'єднання на Render
sio = socketio.AsyncServer(
    async_mode='aiohttp',
    cors_allowed_origins='*',
    ping_timeout=60,
    ping_interval=25
)
app = web.Application()
sio.attach(app)

PORT = int(os.environ.get('PORT', 3000))
SERVER_URL = "https://em-server-e9ib.onrender.com"
MAP_SIZE = 3000
BASE_CENTER = {'x': 1500, 'y': 1500}
EVAC_ZONE = {'x': 300, 'y': 300, 'r': 180}

# Спільний стан гри
game_state = {
    'phase': 1,  # 1 = Штурм, 2 = Евакуація, 3 = Перемога
    'evacTimer': 45,
    'teamFuel': 0,
    'maxFuel': 100,
    'players': {},
    'bullets': [],
    'enemies': [],
    'pickups': [],
    'structures': [
        {'id': 1, 'name': "Головний Реактор", 'x': 1500, 'y': 1500, 'w': 120, 'h': 120, 'hp': 600, 'maxHp': 600, 'icon': "💥", 'color': "#ff2255"},
        {'id': 2, 'name': "Радарний Комплекс", 'x': 1250, 'y': 1350, 'w': 90, 'h': 90, 'hp': 350, 'maxHp': 350, 'icon': "📡", 'color': "#00f0ff"},
        {'id': 3, 'name': "Казарми Кіборгів", 'x': 1750, 'y': 1350, 'w': 100, 'h': 100, 'hp': 400, 'maxHp': 400, 'icon': "🏢", 'color': "#b026ff"},
        {'id': 4, 'name': "Склад Бензину", 'x': 1500, 'y': 1750, 'w': 100, 'h': 100, 'hp': 350, 'maxHp': 350, 'icon': "⛽", 'color': "#ffaa00"}
    ]
}

def init_enemies():
    game_state['enemies'] = []
    turrets = [
        {'x': 1350, 'y': 1350}, {'x': 1650, 'y': 1350},
        {'x': 1350, 'y': 1650}, {'x': 1650, 'y': 1650}
    ]
    for t in turrets:
        game_state['enemies'].append({
            'id': random.random(),
            'x': t['x'], 'y': t['y'],
            'hp': 150, 'type': 'turret',
            'cd': 0, 'r': 22
        })

    for _ in range(30):
        game_state['enemies'].append({
            'id': random.random(),
            'x': BASE_CENTER['x'] + (random.random() - 0.5) * 800,
            'y': BASE_CENTER['y'] + (random.random() - 0.5) * 800,
            'hp': 45, 'type': 'soldier',
            'cd': 0, 'r': 15, 'angle': 0
        })

init_enemies()

# --- ОБРОБКА ПІДКЛЮЧЕНЬ ТА СИНХРОНІЗАЦІЯ ---

@sio.event
async def connect(sid, environ):
    print(f"[{sid}] Гравець успішно підключився до {SERVER_URL}")
    game_state['players'][sid] = {
        'id': sid,
        'name': "Боєць",
        'x': EVAC_ZONE['x'] + (random.random() - 0.5) * 60,
        'y': EVAC_ZONE['y'] + (random.random() - 0.5) * 60,
        'vx': 0, 'vy': 0,
        'angle': 0,
        'hp': 100, 'maxHp': 100,
        'credits': 50,
        'ammo': 30, 'maxAmmo': 30,
        'weapon': 'rifle',
        'shootCd': 0,
        'isAlive': True
    }

@sio.event
async def setNickname(sid, name):
    if sid in game_state['players']:
        game_state['players'][sid]['name'] = (name or "Боєць")[:14]

@sio.event
async def playerInput(sid, data):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    p['angle'] = data.get('angle', 0)
    keys = data.get('keys', {})
    speed = 4.2
    
    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0)
    
    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed

    p['x'] = max(50, min(MAP_SIZE - 50, p['x']))
    p['y'] = max(50, min(MAP_SIZE - 50, p['y']))

    # Обробка пострілів
    if data.get('isShooting') and p['shootCd'] <= 0:
        if p['ammo'] > 0:
            p['ammo'] -= 1
            game_state['bullets'].append({
                'x': p['x'], 'y': p['y'],
                'vx': math.cos(p['angle']) * 14,
                'vy': math.sin(p['angle']) * 14,
                'ownerId': p['id'],
                'isEnemy': False,
                'dmg': 18 if p['weapon'] == 'minigun' else 28,
                'color': '#00f0ff',
                'life': 80
            })
            p['shootCd'] = 6 if p['weapon'] == 'minigun' else 14
            
    if p['shootCd'] > 0:
        p['shootCd'] -= 1

@sio.event
async def reload(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive']:
        await asyncio.sleep(1.2)
        p['ammo'] = p['maxAmmo']

@sio.event
async def buyArmory(sid, item_type):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    if item_type == 'armor' and p['credits'] >= 60:
        p['credits'] -= 60
        p['maxHp'] += 50
        p['hp'] += 50
    elif item_type == 'gun' and p['credits'] >= 120:
        p['credits'] -= 120
        p['weapon'] = 'minigun'
        p['maxAmmo'] = 60
        p['ammo'] = 60
    elif item_type == 'heal' and p['credits'] >= 35:
        p['credits'] -= 35
        p['hp'] = p['maxHp']

@sio.event
async def disconnect(sid):
    print(f"[{sid}] Гравець вийшов з гри")
    if sid in game_state['players']:
        del game_state['players'][sid]

async def evac_countdown():
    while game_state['evacTimer'] > 0:
        await asyncio.sleep(1)
        game_state['evacTimer'] -= 1

def check_base_condition():
    destroyed = len([s for s in game_state['structures'] if s['hp'] <= 0])
    if destroyed == len(game_state['structures']) and game_state['teamFuel'] >= 80 and game_state['phase'] == 1:
        game_state['phase'] = 2
        asyncio.create_task(evac_countdown())
        for _ in range(20):
            game_state['enemies'].append({
                'id': random.random(),
                'x': EVAC_ZONE['x'] + (random.random() - 0.5) * 600,
                'y': EVAC_ZONE['y'] + (random.random() - 0.5) * 600,
                'hp': 50, 'type': 'soldier', 'cd': 0, 'r': 15, 'angle': 0
            })

# --- ІГРОВИЙ ЦИКЛ СЕРВЕРА (30 FPS) ---
async def game_loop():
    while True:
        # 1. Кулі та влучання
        for i in range(len(game_state['bullets']) - 1, -1, -1):
            b = game_state['bullets'][i]
            b['x'] += b['vx']
            b['y'] += b['vy']
            b['life'] -= 1

            if b['isEnemy']:
                hit = False
                for pid, pl in game_state['players'].items():
                    if pl['isAlive'] and math.hypot(pl['x'] - b['x'], pl['y'] - b['y']) < 18:
                        pl['hp'] -= b['dmg']
                        if pl['hp'] <= 0:
                            pl['isAlive'] = False
                        game_state['bullets'].pop(i)
                        hit = True
                        break
                if hit:
                    continue
            else:
                hit_enemy = False
                for j in range(len(game_state['enemies']) - 1, -1, -1):
                    e = game_state['enemies'][j]
                    if math.hypot(e['x'] - b['x'], e['y'] - b['y']) < e['r']:
                        e['hp'] -= b['dmg']
                        game_state['bullets'].pop(i)
                        hit_enemy = True
                        if e['hp'] <= 0:
                            owner = game_state['players'].get(b['ownerId'])
                            if owner:
                                owner['credits'] += 15
                            if random.random() < 0.65:
                                game_state['pickups'].append({
                                    'id': random.random(),
                                    'x': e['x'], 'y': e['y'],
                                    'type': 'fuel' if random.random() < 0.6 else 'medkit'
                                })
                            game_state['enemies'].pop(j)
                        break
                if hit_enemy:
                    continue

                hit_struct = False
                for s in game_state['structures']:
                    if s['hp'] > 0 and (s['x'] - s['w']/2 < b['x'] < s['x'] + s['w']/2) and (s['y'] - s['h']/2 < b['y'] < s['y'] + s['h']/2):
                        s['hp'] -= b['dmg']
                        game_state['bullets'].pop(i)
                        hit_struct = True
                        if s['hp'] <= 0:
                            owner = game_state['players'].get(b['ownerId'])
                            if owner:
                                owner['credits'] += 80
                            game_state['pickups'].append({'id': random.random(), 'x': s['x'], 'y': s['y'], 'type': 'fuel_big'})
                            check_base_condition()
                        break
                if hit_struct:
                    continue

            if b['life'] <= 0 and i < len(game_state['bullets']):
                game_state['bullets'].pop(i)

        # 2. ШІ ворогів
        active_players = [p for p in game_state['players'].values() if p['isAlive']]
        for e in game_state['enemies']:
            if not active_players:
                break
            
            target = min(active_players, key=lambda p: math.hypot(p['x'] - e['x'], p['y'] - e['y']))
            dist = math.hypot(target['x'] - e['x'], target['y'] - e['y'])

            if dist < 600:
                e['angle'] = math.atan2(target['y'] - e['y'], target['x'] - e['x'])
                if e['type'] == 'soldier':
                    e['x'] += math.cos(e['angle']) * 2
                    e['y'] += math.sin(e['angle']) * 2
                
                e['cd'] -= 1
                if e['cd'] <= 0:
                    game_state['bullets'].append({
                        'x': e['x'], 'y': e['y'],
                        'vx': math.cos(e['angle']) * 7.5,
                        'vy': math.sin(e['angle']) * 7.5,
                        'isEnemy': True,
                        'dmg': 14 if e['type'] == 'turret' else 10,
                        'color': '#ff3366',
                        'life': 80
                    })
                    e['cd'] = 35 if e['type'] == 'turret' else 45

        # 3. Підбір палива та аптечок
        for i in range(len(game_state['pickups']) - 1, -1, -1):
            pick = game_state['pickups'][i]
            for pl in game_state['players'].values():
                if pl['isAlive'] and math.hypot(pl['x'] - pick['x'], pl['y'] - pick['y']) < 35:
                    if pick['type'] == 'fuel':
                        game_state['teamFuel'] = min(game_state['maxFuel'], game_state['teamFuel'] + 15)
                    elif pick['type'] == 'fuel_big':
                        game_state['teamFuel'] = min(game_state['maxFuel'], game_state['teamFuel'] + 40)
                    elif pick['type'] == 'medkit':
                        pl['hp'] = min(pl['maxHp'], pl['hp'] + 40)
                    game_state['pickups'].pop(i)
                    check_base_condition()
                    break

        # 4. Перевірка евакуації
        if game_state['phase'] == 2 and game_state['evacTimer'] <= 0:
            in_zone = [p for p in active_players if math.hypot(p['x'] - EVAC_ZONE['x'], p['y'] - EVAC_ZONE['y']) < EVAC_ZONE['r']]
            if in_zone and len(in_zone) == len(active_players):
                game_state['phase'] = 3

        # Синхронізація з усіма клієнтами через Socket.IO
        await sio.emit('stateUpdate', game_state)
        await asyncio.sleep(1 / 30)

async def index_handler(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), 'public', 'index.html'))

app.router.add_get('/', index_handler)

async def start_background_tasks(app):
    app['game_loop'] = asyncio.create_task(game_loop())

async def cleanup_background_tasks(app):
    app['game_loop'].cancel()
    await app['game_loop']

app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == '__main__':
    print(f"[СЕРВЕР ЗАПУЩЕНО]: Порт {PORT} | Адреса: {SERVER_URL}")
    web.run_app(app, port=PORT)
