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
MAP_SIZE = 5000  # Величезна відкрита карта

# База користувачів та кланів
registered_users = {}  # { username: { password, clan } }

# Спільний стан світу
game_state = {
    'players': {},
    'placed_walls': [],  # Стіни гравців
    'chests': [],        # Скрині з лутом
    'resources': [],     # Дерева та каміння
    'bullets': [],
    'events': []
}

# Генерація початкових ресурсів (дерева, камінь) та структур зі скринями
def init_world():
    # Дерева та каміння
    for _ in range(120):
        game_state['resources'].append({
            'id': random.random(),
            'x': random.randint(100, MAP_SIZE - 100),
            'y': random.randint(100, MAP_SIZE - 100),
            'type': random.choice(['wood', 'stone']),
            'hp': 100,
            'maxHp': 100
        })
    # Скрині з лутом
    for _ in range(45):
        game_state['chests'].append({
            'id': random.random(),
            'x': random.randint(150, MAP_SIZE - 150),
            'y': random.randint(150, MAP_SIZE - 150),
            'opened': False,
            'respawnTimer': 0
        })

init_world()

SPECIAL_WEAPONS = ['banana', 'boomerang', 'eye_laser', 'water_pistol', 'nuke_remote']

# --- ОБРОБКА SOCKET.IO ---

@sio.event
async def connect(sid, environ):
    pass

@sio.event
async def loginPlayer(sid, data):
    username = (data.get('username') or 'Мандрівник')[:14].strip()
    password = data.get('password') or ''
    clan = (data.get('clan') or '').upper()[:6].strip()

    # Проста перевірка пароля
    if username in registered_users:
        if registered_users[username]['password'] != password:
            await sio.emit('authError', 'Невірний пароль для цього нікнейму!', room=sid)
            return
        clan = registered_users[username]['clan']
    else:
        registered_users[username] = {'password': password, 'clan': clan}

    # Створення персонажа
    game_state['players'][sid] = {
        'id': sid,
        'name': username,
        'clan': clan,
        'x': random.randint(1000, MAP_SIZE - 1000),
        'y': random.randint(1000, MAP_SIZE - 1000),
        'angle': 0,
        'hp': 100,
        'maxHp': 100,
        'armor': 0,
        'isAlive': True,
        # Ресурси та крафт
        'wood': 30,
        'stone': 20,
        'scrap': 10,
        'wallKits': 3,
        'medkits': 1,
        # Зброя
        'weapon': 'stick',
        'ammo': 20,
        'maxAmmo': 20,
        'shootCd': 0,
        'dashCd': 0
    }
    await sio.emit('authSuccess', {'id': sid, 'player': game_state['players'][sid]}, room=sid)

@sio.event
async def playerInput(sid, data):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    p['angle'] = data.get('angle', 0)
    keys = data.get('keys', {})
    stick = data.get('joystick', {'x': 0, 'y': 0})
    
    speed = 4.3

    # Керування з клавіатури або віртуального джойстика
    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0) + stick.get('x', 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0) + stick.get('y', 0)

    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed

    # Ривок / Dash
    if data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 80
        p['y'] += math.sin(p['angle']) * 80
        p['dashCd'] = 60
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(40, min(MAP_SIZE - 40, p['x']))
    p['y'] = max(40, min(MAP_SIZE - 40, p['y']))

    if p['dashCd'] > 0:
        p['dashCd'] -= 1

    # Постріл / Удар
    if data.get('isShooting') and p['shootCd'] <= 0:
        await handle_shooting(p)

    if p['shootCd'] > 0:
        p['shootCd'] -= 1

async def handle_shooting(p):
    w = p['weapon']

    if w == 'stick':
        # Ближній бій / Добування ресурсів
        p['shootCd'] = 14
        hit_range = 55
        target_x = p['x'] + math.cos(p['angle']) * hit_range
        target_y = p['y'] + math.sin(p['angle']) * hit_range

        # Удар по ресурсах (дерево/камінь)
        for res in game_state['resources']:
            if math.hypot(res['x'] - target_x, res['y'] - target_y) < 35:
                res['hp'] -= 35
                if res['type'] == 'wood':
                    p['wood'] += 15
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': '+15 🪵 Дерево', 'color': '#c28b57'})
                else:
                    p['stone'] += 15
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': '+15 🪨 Камінь', 'color': '#a3a3a3'})
                game_state['events'].append({'type': 'hit_res', 'x': res['x'], 'y': res['y']})
                if res['hp'] <= 0:
                    res['x'] = random.randint(100, MAP_SIZE - 100)
                    res['y'] = random.randint(100, MAP_SIZE - 100)
                    res['hp'] = 100
                return

        # Удар по ворогах
        for pid, other in game_state['players'].items():
            if other['id'] != p['id'] and other['isAlive'] and (not p['clan'] or other['clan'] != p['clan']):
                if math.hypot(other['x'] - target_x, other['y'] - target_y) < 32:
                    deal_damage(other, 25, p)
                    game_state['events'].append({'type': 'punch', 'x': target_x, 'y': target_y})
                    return

    elif w == 'nuke_remote':
        # Пульт від ядерки / Пульт життя (50% шанс рулетки)
        p['shootCd'] = 60
        p['weapon'] = 'stick'  # Одноразове використання
        is_life = random.random() < 0.5

        if is_life:
            # Пульт Життя: повністю лікує всіх поблизу
            for pl in game_state['players'].values():
                if math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 600:
                    pl['hp'] = pl['maxHp']
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 40, 'text': '💚 ПУЛЬТ ЖИТТЯ: ВСІ ЗЦІЛЕНІ!', 'color': '#00ff88'})
            game_state['events'].append({'type': 'heal_wave', 'x': p['x'], 'y': p['y']})
        else:
            # Фатальний вибух ядерки: вбиває власника та завдає шкоди ворогам
            p['hp'] = 0
            p['isAlive'] = False
            game_state['events'].append({'type': 'nuke_explode', 'x': p['x'], 'y': p['y']})
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 40, 'text': '☢️ ЯДЕРНИЙ ВИБУХ: САМОЗНИЩЕННЯ!', 'color': '#ff2255'})
            for pl in game_state['players'].values():
                if pl['id'] != p['id'] and pl['isAlive'] and math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 350:
                    deal_damage(pl, 90, p)

    else:
        # Вогнепальна та спеціальна зброя
        if p['ammo'] <= 0:
            await reload(p['id'])
            return

        p['ammo'] -= 1
        bullet_cfg = {
            'rifle': {'dmg': 26, 'speed': 15, 'color': '#ffcc00', 'cd': 12, 'type': 'bullet'},
            'minigun': {'dmg': 17, 'speed': 17, 'color': '#ff5500', 'cd': 5, 'type': 'bullet'},
            'banana': {'dmg': 35, 'speed': 11, 'color': '#ffe600', 'cd': 20, 'type': 'banana'},
            'boomerang': {'dmg': 40, 'speed': 12, 'color': '#a35d27', 'cd': 25, 'type': 'boomerang'},
            'eye_laser': {'dmg': 20, 'speed': 24, 'color': '#ff0055', 'cd': 4, 'type': 'laser'},
            'water_pistol': {'dmg': 12, 'speed': 13, 'color': '#00d0ff', 'cd': 7, 'type': 'water'}
        }.get(w, {'dmg': 20, 'speed': 14, 'color': '#fff', 'cd': 10, 'type': 'bullet'})

        p['shootCd'] = bullet_cfg['cd']
        game_state['bullets'].append({
            'x': p['x'], 'y': p['y'],
            'vx': math.cos(p['angle']) * bullet_cfg['speed'],
            'vy': math.sin(p['angle']) * bullet_cfg['speed'],
            'ownerId': p['id'],
            'ownerClan': p['clan'],
            'dmg': bullet_cfg['dmg'],
            'color': bullet_cfg['color'],
            'bType': bullet_cfg['type'],
            'life': 70
        })
        game_state['events'].append({'type': 'shoot', 'weapon': w})

def deal_damage(target, dmg, attacker=None):
    # Зменшення шкоди бронею
    actual_dmg = max(5, dmg - target.get('armor', 0))
    target['hp'] -= actual_dmg
    game_state['events'].append({'type': 'damage', 'x': target['x'], 'y': target['y'], 'val': actual_dmg})
    if target['hp'] <= 0:
        target['isAlive'] = False
        target['hp'] = 0
        game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '☠️ ЗАГИБЕЛЬ', 'color': '#ff2255'})

# --- КРАФТ, БУДІВНИЦТВО ТА ВЗАЄМОДІЯ ---

@sio.event
async def buildWall(sid):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    if p['wallKits'] > 0 or (p['wood'] >= 20 and p['stone'] >= 10):
        if p['wallKits'] > 0:
            p['wallKits'] -= 1
        else:
            p['wood'] -= 20
            p['stone'] -= 10

        wall_x = p['x'] + math.cos(p['angle']) * 50
        wall_y = p['y'] + math.sin(p['angle']) * 50

        game_state['placed_walls'].append({
            'id': random.random(),
            'x': wall_x,
            'y': wall_y,
            'hp': 250,
            'maxHp': 250,
            'ownerClan': p['clan']
        })
        game_state['events'].append({'type': 'floatText', 'x': wall_x, 'y': wall_y, 'text': '🧱 Стіну збудовано!', 'color': '#00f0ff'})

@sio.event
async def craftItem(sid, item_type):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    if item_type == 'wall' and p['wood'] >= 25 and p['stone'] >= 15:
        p['wood'] -= 25; p['stone'] -= 15; p['wallKits'] += 1
    elif item_type == 'medkit' and p['wood'] >= 10 and p['scrap'] >= 10:
        p['wood'] -= 10; p['scrap'] -= 10; p['medkits'] += 1
    elif item_type == 'armor' and p['stone'] >= 40 and p['scrap'] >= 25:
        p['stone'] -= 40; p['scrap'] -= 25; p['armor'] = min(60, p['armor'] + 20)
    elif item_type == 'ammo' and p['stone'] >= 15 and p['scrap'] >= 15:
        p['stone'] -= 15; p['scrap'] -= 15; p['ammo'] += 30

@sio.event
async def interactChest(sid):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    for chest in game_state['chests']:
        if not chest['opened'] and math.hypot(chest['x'] - p['x'], chest['y'] - p['y']) < 65:
            chest['opened'] = True
            chest['respawnTimer'] = 180  # 6 секунд перезарядки

            # Випадання луту
            p['ammo'] += 25
            p['medkits'] += 1
            p['wood'] += 20
            p['scrap'] += 15

            # Шанс знайти спеціальну або рідкісну зброю
            if random.random() < 0.65:
                p['weapon'] = random.choice(SPECIAL_WEAPONS)
                game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 35, 'text': f"🎁 Знайдено: {p['weapon'].upper()}!", 'color': '#ffe600'})
            else:
                game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 35, 'text': '📦 Лут зібрано!', 'color': '#00ff88'})
            return

@sio.event
async def useMedkit(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive'] and p['medkits'] > 0:
        p['medkits'] -= 1
        p['hp'] = min(p['maxHp'], p['hp'] + 45)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+45 HP', 'color': '#00ff88'})

@sio.event
async def reload(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive']:
        await asyncio.sleep(1.0)
        p['ammo'] = p['maxAmmo']

@sio.event
async def respawn(sid):
    p = game_state['players'].get(sid)
    if p and not p['isAlive']:
        p['hp'] = p['maxHp']
        p['x'] = random.randint(1000, MAP_SIZE - 1000)
        p['y'] = random.randint(1000, MAP_SIZE - 1000)
        p['isAlive'] = True
        p['weapon'] = 'stick'
        p['ammo'] = 20

# --- ГОЛОВНИЙ ЦИКЛ (30 FPS) ---
async def game_loop():
    while True:
        # Оновлення куль
        for i in range(len(game_state['bullets']) - 1, -1, -1):
            b = game_state['bullets'][i]
            b['x'] += b['vx']
            b['y'] += b['vy']
            b['life'] -= 1

            # Влучання у стіни
            hit_wall = False
            for w in game_state['placed_walls']:
                if math.hypot(w['x'] - b['x'], w['y'] - b['y']) < 28:
                    w['hp'] -= b['dmg']
                    game_state['bullets'].pop(i)
                    hit_wall = True
                    break
            if hit_wall:
                continue

            # Влучання у гравців
            hit_player = False
            for pid, pl in game_state['players'].items():
                if pl['isAlive'] and pl['id'] != b['ownerId']:
                    # Перевірка кланового імунітету
                    if b['ownerClan'] and pl['clan'] == b['ownerClan']:
                        continue
                    if math.hypot(pl['x'] - b['x'], pl['y'] - b['y']) < 20:
                        deal_damage(pl, b['dmg'])
                        game_state['bullets'].pop(i)
                        hit_player = True
                        break
            if hit_player:
                continue

            if b['life'] <= 0 and i < len(game_state['bullets']):
                game_state['bullets'].pop(i)

        # Очищення зруйнованих стін
        game_state['placed_walls'] = [w for w in game_state['placed_walls'] if w['hp'] > 0]

        # Перезарядка скринь
        for chest in game_state['chests']:
            if chest['opened']:
                chest['respawnTimer'] -= 1
                if chest['respawnTimer'] <= 0:
                    chest['opened'] = False

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
