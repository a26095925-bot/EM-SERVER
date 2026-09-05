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
MAP_SIZE = 5000

registered_users = {}

game_state = {
    'players': {},
    'bots': [],
    'placed_walls': [],
    'chests': [],
    'resources': [],
    'bullets': [],
    'events': []
}

# Генерація світу
def init_world():
    # Дерева та каміння
    for _ in range(140):
        game_state['resources'].append({
            'id': random.random(),
            'x': random.randint(150, MAP_SIZE - 150),
            'y': random.randint(150, MAP_SIZE - 150),
            'type': random.choice(['wood', 'stone']),
            'hp': 100,
            'maxHp': 100,
            'size': random.randint(28, 42)
        })
    # Скрині з лутом
    for _ in range(50):
        game_state['chests'].append({
            'id': random.random(),
            'x': random.randint(200, MAP_SIZE - 200),
            'y': random.randint(200, MAP_SIZE - 200),
            'opened': False,
            'respawnTimer': 0
        })
    # Боти із розумним ШІ
    bot_types = [
        {'type': 'raider', 'hp': 80, 'weapon': 'rifle', 'name': 'Рейдер-Бот'},
        {'type': 'cyborg', 'hp': 140, 'weapon': 'minigun', 'name': 'Кіборг-Вартовий'},
        {'type': 'berserk', 'hp': 100, 'weapon': 'stick', 'name': 'Дикун-Берсерк'}
    ]
    for i in range(18):
        cfg = random.choice(bot_types)
        game_state['bots'].append({
            'id': f'bot_{i}',
            'name': cfg['name'],
            'type': cfg['type'],
            'x': random.randint(400, MAP_SIZE - 400),
            'y': random.randint(400, MAP_SIZE - 400),
            'angle': 0,
            'hp': cfg['hp'],
            'maxHp': cfg['hp'],
            'weapon': cfg['weapon'],
            'cd': 0,
            'state': 'patrol',
            'strafeDir': random.choice([-1, 1]),
            'strafeTimer': random.randint(20, 50),
            'patrolAngle': random.uniform(0, math.pi * 2)
        })

init_world()

SPECIAL_WEAPONS = ['banana', 'boomerang', 'eye_laser', 'water_pistol', 'nuke_remote', 'minigun', 'rifle']

@sio.event
async def connect(sid, environ):
    pass

@sio.event
async def loginPlayer(sid, data):
    username = (data.get('username') or 'Мандрівник')[:14].strip()
    password = data.get('password') or ''
    clan = (data.get('clan') or '').upper()[:6].strip()

    if username in registered_users:
        if registered_users[username]['password'] != password:
            await sio.emit('authError', 'Невірний пароль для цього нікнейму!', room=sid)
            return
        clan = registered_users[username]['clan']
    else:
        registered_users[username] = {'password': password, 'clan': clan}

    game_state['players'][sid] = {
        'id': sid,
        'name': username,
        'clan': clan,
        'x': random.randint(1200, MAP_SIZE - 1200),
        'y': random.randint(1200, MAP_SIZE - 1200),
        'angle': 0,
        'hp': 100,
        'maxHp': 100,
        'armor': 0,
        'isAlive': True,
        'wood': 40,
        'stone': 25,
        'scrap': 15,
        'wallKits': 3,
        'medkits': 1,
        'weapon': 'stick',
        'ammo': 30,
        'maxAmmo': 30,
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
    speed = 4.4

    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0) + stick.get('x', 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0) + stick.get('y', 0)

    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed

    if data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 85
        p['y'] += math.sin(p['angle']) * 85
        p['dashCd'] = 55
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(40, min(MAP_SIZE - 40, p['x']))
    p['y'] = max(40, min(MAP_SIZE - 40, p['y']))

    if p['dashCd'] > 0:
        p['dashCd'] -= 1

    if data.get('isShooting') and p['shootCd'] <= 0:
        await handle_shooting(p)

    if p['shootCd'] > 0:
        p['shootCd'] -= 1

async def handle_shooting(p):
    w = p['weapon']
    if w == 'stick':
        p['shootCd'] = 14
        hit_range = 60
        target_x = p['x'] + math.cos(p['angle']) * hit_range
        target_y = p['y'] + math.sin(p['angle']) * hit_range

        # Добування ресурсів
        for res in game_state['resources']:
            if math.hypot(res['x'] - target_x, res['y'] - target_y) < (res.get('size', 32) + 15):
                res['hp'] -= 35
                if res['type'] == 'wood':
                    p['wood'] += 15
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': '+15 🪵', 'color': '#c28b57'})
                else:
                    p['stone'] += 15
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': '+15 🪨', 'color': '#a3a3a3'})
                game_state['events'].append({'type': 'hit_res', 'x': res['x'], 'y': res['y']})
                if res['hp'] <= 0:
                    res['x'] = random.randint(100, MAP_SIZE - 100)
                    res['y'] = random.randint(100, MAP_SIZE - 100)
                    res['hp'] = 100
                return

        # Удар по ботах
        for bot in game_state['bots']:
            if math.hypot(bot['x'] - target_x, bot['y'] - target_y) < 32:
                deal_damage_bot(bot, 30, p)
                game_state['events'].append({'type': 'punch', 'x': target_x, 'y': target_y})
                return

        # Удар по гравцях
        for pid, other in game_state['players'].items():
            if other['id'] != p['id'] and other['isAlive'] and (not p['clan'] or other['clan'] != p['clan']):
                if math.hypot(other['x'] - target_x, other['y'] - target_y) < 32:
                    deal_damage(other, 25, p)
                    game_state['events'].append({'type': 'punch', 'x': target_x, 'y': target_y})
                    return

    elif w == 'nuke_remote':
        p['shootCd'] = 60
        p['weapon'] = 'stick'
        is_life = random.random() < 0.5
        if is_life:
            for pl in game_state['players'].values():
                if math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 600:
                    pl['hp'] = pl['maxHp']
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 40, 'text': '💚 ПУЛЬТ ЖИТТЯ: ВСІ ЗЦІЛЕНІ!', 'color': '#00ff88'})
            game_state['events'].append({'type': 'heal_wave', 'x': p['x'], 'y': p['y']})
        else:
            p['hp'] = 0
            p['isAlive'] = False
            game_state['events'].append({'type': 'nuke_explode', 'x': p['x'], 'y': p['y']})
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 40, 'text': '☢️ ЯДЕРНИЙ ВИБУХ!', 'color': '#ff2255'})
            for pl in game_state['players'].values():
                if pl['id'] != p['id'] and pl['isAlive'] and math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 350:
                    deal_damage(pl, 90, p)
    else:
        if p['ammo'] <= 0:
            await reload(p['id'])
            return

        p['ammo'] -= 1
        bullet_cfg = {
            'rifle': {'dmg': 26, 'speed': 15, 'color': '#ffcc00', 'cd': 12, 'type': 'bullet'},
            'minigun': {'dmg': 17, 'speed': 17, 'color': '#ff5500', 'cd': 5, 'type': 'bullet'},
            'banana': {'dmg': 35, 'speed': 11, 'color': '#ffe600', 'cd': 18, 'type': 'banana'},
            'boomerang': {'dmg': 40, 'speed': 12, 'color': '#a35d27', 'cd': 22, 'type': 'boomerang'},
            'eye_laser': {'dmg': 20, 'speed': 24, 'color': '#ff0055', 'cd': 4, 'type': 'laser'},
            'water_pistol': {'dmg': 14, 'speed': 13, 'color': '#00d0ff', 'cd': 7, 'type': 'water'}
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
    actual_dmg = max(5, dmg - target.get('armor', 0))
    target['hp'] -= actual_dmg
    game_state['events'].append({'type': 'damage', 'x': target['x'], 'y': target['y'], 'val': actual_dmg})
    if target['hp'] <= 0:
        target['isAlive'] = False
        target['hp'] = 0
        game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '☠️ ЗАГИБЕЛЬ', 'color': '#ff2255'})

def deal_damage_bot(bot, dmg, attacker=None):
    bot['hp'] -= dmg
    game_state['events'].append({'type': 'damage', 'x': bot['x'], 'y': bot['y'], 'val': dmg})
    if bot['hp'] <= 0:
        if attacker:
            attacker['scrap'] += 20
            attacker['ammo'] += 15
            game_state['events'].append({'type': 'floatText', 'x': bot['x'], 'y': bot['y'] - 20, 'text': '+20 ⚙️ +15 📦', 'color': '#00ff88'})
        # Респавн бота в іншому місці
        bot['x'] = random.randint(300, MAP_SIZE - 300)
        bot['y'] = random.randint(300, MAP_SIZE - 300)
        bot['hp'] = bot['maxHp']
        bot['state'] = 'patrol'

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

        wall_x = p['x'] + math.cos(p['angle']) * 55
        wall_y = p['y'] + math.sin(p['angle']) * 55

        game_state['placed_walls'].append({
            'id': random.random(),
            'x': wall_x,
            'y': wall_y,
            'hp': 300,
            'maxHp': 300,
            'ownerClan': p['clan']
        })
        game_state['events'].append({'type': 'floatText', 'x': wall_x, 'y': wall_y, 'text': '🧱 СТІНА', 'color': '#00f0ff'})

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
            chest['respawnTimer'] = 180

            p['ammo'] += 25
            p['medkits'] += 1
            p['wood'] += 25
            p['scrap'] += 20

            if random.random() < 0.7:
                p['weapon'] = random.choice(SPECIAL_WEAPONS)
                game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 35, 'text': f"🎁 ЗБРОЯ: {p['weapon'].upper()}!", 'color': '#ffe600'})
            else:
                game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 35, 'text': '📦 ЛУТ ЗІБРАНО!', 'color': '#00ff88'})
            return

@sio.event
async def useMedkit(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive'] and p['medkits'] > 0:
        p['medkits'] -= 1
        p['hp'] = min(p['maxHp'], p['hp'] + 45)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+45 HP 💉', 'color': '#00ff88'})

@sio.event
async def reload(sid):
    p = game_state['players'].get(sid)
    if p and p['isAlive']:
        await asyncio.sleep(0.9)
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
        p['ammo'] = 30

@sio.event
async def disconnect(sid):
    if sid in game_state['players']:
        del game_state['players'][sid]

# --- ШТУЧНИЙ ІНТЕЛЕКТ БОТІВ ТА ІГРОВИЙ ЦИКЛ (30 FPS) ---
async def game_loop():
    while True:
        active_players = [p for p in game_state['players'].values() if p['isAlive']]

        # Оновлення ботів (Розумний ШІ з маневруванням і відступом)
        for bot in game_state['bots']:
            # Зміна напрямку стрейфу
            bot['strafeTimer'] -= 1
            if bot['strafeTimer'] <= 0:
                bot['strafeDir'] *= -1
                bot['strafeTimer'] = random.randint(25, 60)

            # Пошук найближчого живого гравця
            target = None
            min_dist = 600
            for pl in active_players:
                d = math.hypot(pl['x'] - bot['x'], pl['y'] - bot['y'])
                if d < min_dist:
                    min_dist = d
                    target = pl

            if target:
                bot['state'] = 'combat'
                to_target_a = math.atan2(target['y'] - bot['y'], target['x'] - bot['x'])
                bot['angle'] = to_target_a

                # Тактична логіка
                if bot['hp'] < bot['maxHp'] * 0.3:
                    # Відступ / Тікає, якщо мало здоров'я
                    bot['x'] -= math.cos(to_target_a) * 2.8
                    bot['y'] -= math.sin(to_target_a) * 2.8
                elif bot['type'] == 'berserk':
                    # Берсерк біжить прямо на гравця
                    bot['x'] += math.cos(to_target_a) * 3.6
                    bot['y'] += math.sin(to_target_a) * 3.6
                    if min_dist < 45:
                        deal_damage(target, 18)
                else:
                    # Стрільці тримають дистанцію (200-300px) і стрейфлять навколо
                    strafe_a = to_target_a + (math.pi / 2) * bot['strafeDir']
                    if min_dist > 280:
                        bot['x'] += math.cos(to_target_a) * 2.0
                        bot['y'] += math.sin(to_target_a) * 2.0
                    elif min_dist < 150:
                        bot['x'] -= math.cos(to_target_a) * 2.0
                        bot['y'] -= math.sin(to_target_a) * 2.0
                    
                    bot['x'] += math.cos(strafe_a) * 1.8
                    bot['y'] += math.sin(strafe_a) * 1.8

                    # Стрільба бота
                    bot['cd'] -= 1
                    if bot['cd'] <= 0:
                        game_state['bullets'].append({
                            'x': bot['x'], 'y': bot['y'],
                            'vx': math.cos(bot['angle'] + (random.random()-0.5)*0.15) * 11,
                            'vy': math.sin(bot['angle'] + (random.random()-0.5)*0.15) * 11,
                            'ownerId': bot['id'],
                            'ownerClan': None,
                            'dmg': 15 if bot['type'] == 'cyborg' else 20,
                            'color': '#ff3366',
                            'bType': 'bullet',
                            'life': 65
                        })
                        bot['cd'] = 22 if bot['type'] == 'cyborg' else 40
            else:
                # Патрулювання
                bot['state'] = 'patrol'
                bot['x'] += math.cos(bot['patrolAngle']) * 1.2
                bot['y'] += math.sin(bot['patrolAngle']) * 1.2
                bot['angle'] = bot['patrolAngle']
                if random.random() < 0.02:
                    bot['patrolAngle'] = random.uniform(0, math.pi * 2)

            bot['x'] = max(50, min(MAP_SIZE - 50, bot['x']))
            bot['y'] = max(50, min(MAP_SIZE - 50, bot['y']))

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

            # Влучання у ботів
            if not b['ownerId'].startswith('bot_'):
                hit_bot = False
                for bot in game_state['bots']:
                    if math.hypot(bot['x'] - b['x'], bot['y'] - b['y']) < 22:
                        attacker = game_state['players'].get(b['ownerId'])
                        deal_damage_bot(bot, b['dmg'], attacker)
                        game_state['bullets'].pop(i)
                        hit_bot = True
                        break
                if hit_bot:
                    continue

            # Влучання у гравців
            hit_player = False
            for pid, pl in game_state['players'].items():
                if pl['isAlive'] and pl['id'] != b['ownerId']:
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

        game_state['placed_walls'] = [w for w in game_state['placed_walls'] if w['hp'] > 0]

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

async def start_tasks(app):
    app['game_loop'] = asyncio.create_task(game_loop())

async def cleanup_tasks(app):
    app['game_loop'].cancel()
    await app['game_loop']

app.on_startup.append(start_tasks)
app.on_cleanup.append(cleanup_tasks)

if __name__ == '__main__':
    web.run_app(app, port=PORT)
