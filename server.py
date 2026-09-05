import os
import math
import random
import asyncio
import socketio
from aiohttp import web

sio = socketio.AsyncServer(
    async_mode='aiohttp',
    cors_allowed_origins='*',
    ping_timeout=60,
    ping_interval=25
)
app = web.Application()
sio.attach(app)

PORT = int(os.environ.get('PORT', 3000))
MAP_SIZE = 3000
BASE_CENTER = {'x': 1500, 'y': 1500}
EVAC_ZONE = {'x': 300, 'y': 300, 'r': 180}

# Стан гри
game_state = {
    'phase': 1,  # 1 = Штурм, 2 = Евакуація, 3 = Перемога
    'evacTimer': 45,
    'teamFuel': 0,
    'maxFuel': 100,
    'players': {},
    'allies': [],
    'bullets': [],
    'enemies': [],
    'pickups': [],
    'events': [],  # звукові події та вибухи для клієнта
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

    for _ in range(35):
        game_state['enemies'].append({
            'id': random.random(),
            'x': BASE_CENTER['x'] + (random.random() - 0.5) * 850,
            'y': BASE_CENTER['y'] + (random.random() - 0.5) * 850,
            'hp': 45, 'type': 'soldier',
            'cd': 0, 'r': 15, 'angle': 0
        })

init_enemies()

@sio.event
async def connect(sid, environ):
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
        'dashCd': 0,
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

    # Ривок на ПРОБІЛ
    if data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 65
        p['y'] += math.sin(p['angle']) * 65
        p['dashCd'] = 60
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(50, min(MAP_SIZE - 50, p['x']))
    p['y'] = max(50, min(MAP_SIZE - 50, p['y']))

    if p['dashCd'] > 0:
        p['dashCd'] -= 1

    # Стрільба
    if data.get('isShooting') and p['shootCd'] <= 0:
        if p['ammo'] > 0:
            p['ammo'] -= 1
            spread = (random.random() - 0.5) * (0.16 if p['weapon'] == 'minigun' else 0.05)
            game_state['bullets'].append({
                'x': p['x'], 'y': p['y'],
                'vx': math.cos(p['angle'] + spread) * 14,
                'vy': math.sin(p['angle'] + spread) * 14,
                'ownerId': p['id'],
                'isEnemy': False,
                'dmg': 18 if p['weapon'] == 'minigun' else 28,
                'color': '#00f0ff',
                'life': 80
            })
            p['shootCd'] = 6 if p['weapon'] == 'minigun' else 14
            game_state['events'].append({'type': 'shoot', 'weapon': p['weapon']})
            
    if p['shootCd'] > 0:
        p['shootCd'] -= 1

@sio.event
async def reload(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive']:
        game_state['events'].append({'type': 'reload_start', 'sid': sid})
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
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+50 МАКС HP', 'color': '#00ff88'})
    elif item_type == 'gun' and p['credits'] >= 120:
        p['credits'] -= 120
        p['weapon'] = 'minigun'
        p['maxAmmo'] = 60
        p['ammo'] = 60
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': 'КУЛЕМЕТ ВУЛКАН!', 'color': '#00f0ff'})
    elif item_type == 'ally' and p['credits'] >= 80:
        p['credits'] -= 80
        game_state['allies'].append({
            'id': random.random(),
            'x': p['x'] + (random.random() - 0.5) * 40,
            'y': p['y'] + (random.random() - 0.5) * 40,
            'hp': 90, 'cd': 0
        })
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+1 БОЄЦЬ ДЕСАНТУ', 'color': '#ffaa00'})
    elif item_type == 'heal' and p['credits'] >= 35:
        p['credits'] -= 35
        p['hp'] = p['maxHp']
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '100% ЗДОРОВ\'Я', 'color': '#00ff88'})

@sio.event
async def disconnect(sid):
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
        game_state['events'].append({'type': 'alarm'})
        for _ in range(25):
            game_state['enemies'].append({
                'id': random.random(),
                'x': EVAC_ZONE['x'] + (random.random() - 0.5) * 650,
                'y': EVAC_ZONE['y'] + (random.random() - 0.5) * 650,
                'hp': 50, 'type': 'soldier', 'cd': 0, 'r': 15, 'angle': 0
            })

# --- ІГРОВИЙ ЦИКЛ (30 FPS) ---
async def game_loop():
    while True:
        # 1. Союзні десантники (Allies AI)
        active_players = [p for p in game_state['players'].values() if p['isAlive']]
        for al in game_state['allies']:
            if active_players:
                leader = active_players[0]
                d = math.hypot(leader['x'] - al['x'], leader['y'] - al['y'])
                if d > 70:
                    a = math.atan2(leader['y'] - al['y'], leader['x'] - al['x'])
                    al['x'] += math.cos(a) * 3.5
                    al['y'] += math.sin(a) * 3.5
                
                al['cd'] -= 1
                if al['cd'] <= 0 and game_state['enemies']:
                    target_e = min(game_state['enemies'], key=lambda e: math.hypot(e['x'] - al['x'], e['y'] - al['y']))
                    if math.hypot(target_e['x'] - al['x'], target_e['y'] - al['y']) < 400:
                        shoot_a = math.atan2(target_e['y'] - al['y'], target_e['x'] - al['x'])
                        game_state['bullets'].append({
                            'x': al['x'], 'y': al['y'],
                            'vx': math.cos(shoot_a) * 14,
                            'vy': math.sin(shoot_a) * 14,
                            'ownerId': 'ally',
                            'isEnemy': False,
                            'dmg': 15,
                            'color': '#00ff88',
                            'life': 80
                        })
                        al['cd'] = 24

        # 2. Кулі та влучання
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
                        game_state['events'].append({'type': 'hit', 'x': pl['x'], 'y': pl['y'], 'dmg': b['dmg']})
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
                        game_state['events'].append({'type': 'spark', 'x': b['x'], 'y': b['y']})
                        game_state['bullets'].pop(i)
                        hit_enemy = True
                        if e['hp'] <= 0:
                            owner = game_state['players'].get(b['ownerId'])
                            if owner:
                                owner['credits'] += 15
                                game_state['events'].append({'type': 'floatText', 'x': e['x'], 'y': e['y'], 'text': '+15$', 'color': '#00ff88'})
                            game_state['events'].append({'type': 'explode', 'x': e['x'], 'y': e['y']})
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
                        game_state['events'].append({'type': 'spark', 'x': b['x'], 'y': b['y']})
                        game_state['bullets'].pop(i)
                        hit_struct = True
                        if s['hp'] <= 0:
                            owner = game_state['players'].get(b['ownerId'])
                            if owner:
                                owner['credits'] += 80
                            game_state['events'].append({'type': 'big_explode', 'x': s['x'], 'y': s['y']})
                            game_state['events'].append({'type': 'floatText', 'x': s['x'], 'y': s['y'] - 40, 'text': f"💥 ЗНИЩЕНО: {s['name']}!", 'color': '#ff2255'})
                            game_state['pickups'].append({'id': random.random(), 'x': s['x'], 'y': s['y'], 'type': 'fuel_big'})
                            check_base_condition()
                        break
                if hit_struct:
                    continue

            if b['life'] <= 0 and i < len(game_state['bullets']):
                game_state['bullets'].pop(i)

        # 3. ШІ ворогів
        for e in game_state['enemies']:
            if not active_players:
                break
            target = min(active_players, key=lambda p: math.hypot(p['x'] - e['x'], p['y'] - e['y']))
            dist = math.hypot(target['x'] - e['x'], target['y'] - e['y'])

            if dist < 650:
                e['angle'] = math.atan2(target['y'] - e['y'], target['x'] - e['x'])
                if e['type'] == 'soldier':
                    e['x'] += math.cos(e['angle']) * 2.2
                    e['y'] += math.sin(e['angle']) * 2.2
                
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

        # 4. Підбір предметів
        for i in range(len(game_state['pickups']) - 1, -1, -1):
            pick = game_state['pickups'][i]
            for pl in game_state['players'].values():
                if pl['isAlive'] and math.hypot(pl['x'] - pick['x'], pl['y'] - pick['y']) < 35:
                    if pick['type'] == 'fuel':
                        game_state['teamFuel'] = min(game_state['maxFuel'], game_state['teamFuel'] + 15)
                        game_state['events'].append({'type': 'floatText', 'x': pl['x'], 'y': pl['y'] - 30, 'text': '+15л Бензину', 'color': '#ffaa00'})
                    elif pick['type'] == 'fuel_big':
                        game_state['teamFuel'] = min(game_state['maxFuel'], game_state['teamFuel'] + 40)
                        game_state['events'].append({'type': 'floatText', 'x': pl['x'], 'y': pl['y'] - 30, 'text': '+40л Бензину зі сховища!', 'color': '#ffaa00'})
                    elif pick['type'] == 'medkit':
                        pl['hp'] = min(pl['maxHp'], pl['hp'] + 35)
                        game_state['events'].append({'type': 'floatText', 'x': pl['x'], 'y': pl['y'] - 30, 'text': '+35 HP Аптечка', 'color': '#00ff88'})
                    game_state['events'].append({'type': 'pickup'})
                    game_state['pickups'].pop(i)
                    check_base_condition()
                    break

        # 5. Перевірка евакуації
        if game_state['phase'] == 2 and game_state['evacTimer'] <= 0:
            in_zone = [p for p in active_players if math.hypot(p['x'] - EVAC_ZONE['x'], p['y'] - EVAC_ZONE['y']) < EVAC_ZONE['r']]
            if in_zone and len(in_zone) == len(active_players):
                game_state['phase'] = 3

        # Відправка стану гри та очищення подій
        await sio.emit('stateUpdate', game_state)
        game_state['events'] = []
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
    web.run_app(app, port=PORT)
