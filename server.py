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

registered_users = {}  # { username: { password, clan } }
clans = {
    'ALPHA': {'tag': 'ALPHA', 'name': 'Альфа Загін', 'leader': 'Admin', 'desc': 'Елітні бійці сектору', 'members': []},
    'OMEGA': {'tag': 'OMEGA', 'name': 'Омега Рейдери', 'leader': 'Admin', 'desc': 'Знищуємо все на шляху', 'members': []}
}

game_state = {
    'players': {},
    'bots': [],
    'placed_walls': [],
    'chests': [],
    'resources': [],
    'bullets': [],
    'events': []
}

def init_world():
    for _ in range(150):
        game_state['resources'].append({
            'id': random.random(),
            'x': random.randint(150, MAP_SIZE - 150),
            'y': random.randint(150, MAP_SIZE - 150),
            'type': random.choice(['wood', 'stone']),
            'hp': 100,
            'maxHp': 100,
            'size': random.randint(28, 42)
        })
    for _ in range(50):
        game_state['chests'].append({
            'id': random.random(),
            'x': random.randint(200, MAP_SIZE - 200),
            'y': random.randint(200, MAP_SIZE - 200),
            'opened': False,
            'respawnTimer': 0
        })
    bot_types = [
        {'type': 'raider', 'hp': 80, 'weapon': 'rifle', 'name': 'Рейдер-Бот'},
        {'type': 'cyborg', 'hp': 140, 'weapon': 'minigun', 'name': 'Кіборг-Вартовий'},
        {'type': 'berserk', 'hp': 100, 'weapon': 'pickaxe', 'name': 'Дикун-Берсерк'}
    ]
    for i in range(20):
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

SPECIAL_WEAPONS = ['banana', 'boomerang', 'eye_laser', 'water_pistol', 'nuke_remote', 'minigun']

def add_xp(p, amount):
    p['xp'] += amount
    if p['xp'] >= p['xpToNext']:
        p['xp'] -= p['xpToNext']
        p['level'] += 1
        p['statPoints'] += 1
        p['xpToNext'] = int(p['xpToNext'] * 1.4)
        p['hp'] = p['maxHp']
        game_state['events'].append({'type': 'level_up', 'x': p['x'], 'y': p['y'], 'lvl': p['level']})

@sio.event
async def loginPlayer(sid, data):
    username = (data.get('username') or 'Боєць')[:14].strip()
    password = data.get('password') or ''
    clan_tag = (data.get('clan') or '').upper()[:6].strip()

    if username in registered_users:
        if registered_users[username]['password'] != password:
            await sio.emit('authError', 'Невірний пароль для цього нікнейму!', room=sid)
            return
        clan_tag = registered_users[username]['clan']
    else:
        registered_users[username] = {'password': password, 'clan': clan_tag}

    if clan_tag and clan_tag in clans:
        if username not in clans[clan_tag]['members']:
            clans[clan_tag]['members'].append(username)

    # Початковий інвентар/хотбар
    hotbar = [
        {'slot': 0, 'type': 'pickaxe', 'name': 'Кирка/Сокира', 'icon': '⛏️'},
        {'slot': 1, 'type': 'rifle', 'name': 'Автомат M4', 'icon': '🔫'},
        {'slot': 2, 'type': 'banana', 'name': 'Банан-Бумеранг', 'icon': '🍌'},
        {'slot': 3, 'type': 'wall', 'name': 'Стіна', 'icon': '🧱', 'count': 5},
        {'slot': 4, 'type': 'medkit', 'name': 'Аптечка', 'icon': '💉', 'count': 2},
        {'slot': 5, 'type': 'empty', 'name': 'Порожньо', 'icon': '▫️'}
    ]

    game_state['players'][sid] = {
        'id': sid,
        'name': username,
        'clan': clan_tag,
        'x': random.randint(1200, MAP_SIZE - 1200),
        'y': random.randint(1200, MAP_SIZE - 1200),
        'angle': 0,
        'hp': 100,
        'maxHp': 100,
        'armor': 0,
        'isAlive': True,
        # Прокачка
        'level': 1,
        'xp': 0,
        'xpToNext': 100,
        'statPoints': 0,
        'stats': {'hp': 0, 'speed': 0, 'damage': 0, 'gather': 0},
        # Ресурси
        'wood': 50,
        'stone': 30,
        'scrap': 20,
        'hotbar': hotbar,
        'activeSlot': 0,
        'ammo': 40,
        'maxAmmo': 40,
        'shootCd': 0,
        'dashCd': 0,
        'chatMsg': '',
        'chatTimer': 0
    }
    await sio.emit('authSuccess', {'id': sid, 'player': game_state['players'][sid], 'clans': clans}, room=sid)

@sio.event
async def selectSlot(sid, slot_idx):
    p = game_state['players'].get(sid)
    if p and 0 <= slot_idx < len(p['hotbar']):
        p['activeSlot'] = slot_idx

@sio.event
async def upgradeStat(sid, stat_name):
    p = game_state['players'].get(sid)
    if p and p['statPoints'] > 0 and stat_name in p['stats']:
        p['statPoints'] -= 1
        p['stats'][stat_name] += 1
        if stat_name == 'hp':
            p['maxHp'] += 20
            p['hp'] += 20
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': f"+1 {stat_name.upper()}!", 'color': '#38bdf8'})

@sio.event
async def playerInput(sid, data):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']:
        return

    p['angle'] = data.get('angle', 0)
    keys = data.get('keys', {})
    stick = data.get('joystick', {'x': 0, 'y': 0})
    
    speed_boost = p['stats']['speed'] * 0.35
    speed = 4.4 + speed_boost

    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0) + stick.get('x', 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0) + stick.get('y', 0)

    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed
        if random.random() < 0.25:
            game_state['events'].append({'type': 'step_dust', 'x': p['x'], 'y': p['y']})

    if data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 90
        p['y'] += math.sin(p['angle']) * 90
        p['dashCd'] = 55
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(40, min(MAP_SIZE - 40, p['x']))
    p['y'] = max(40, min(MAP_SIZE - 40, p['y']))

    if p['dashCd'] > 0:
        p['dashCd'] -= 1
    if p['chatTimer'] > 0:
        p['chatTimer'] -= 1

    if data.get('isShooting') and p['shootCd'] <= 0:
        await handle_action(p)

    if p['shootCd'] > 0:
        p['shootCd'] -= 1

async def handle_action(p):
    item = p['hotbar'][p['activeSlot']]
    itype = item['type']
    dmg_mult = 1.0 + p['stats']['damage'] * 0.15
    gather_mult = 1.0 + p['stats']['gather'] * 0.25

    if itype == 'pickaxe':
        p['shootCd'] = 12
        hit_range = 60
        target_x = p['x'] + math.cos(p['angle']) * hit_range
        target_y = p['y'] + math.sin(p['angle']) * hit_range

        # Видобуток ресурсів
        for res in game_state['resources']:
            if math.hypot(res['x'] - target_x, res['y'] - target_y) < (res.get('size', 32) + 15):
                res['hp'] -= 35 * gather_mult
                if res['type'] == 'wood':
                    gain = int(15 * gather_mult)
                    p['wood'] += gain
                    add_xp(p, 8)
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': f'+{gain} 🪵', 'color': '#c28b57'})
                    game_state['events'].append({'type': 'tree_chips', 'x': target_x, 'y': target_y})
                else:
                    gain = int(15 * gather_mult)
                    p['stone'] += gain
                    add_xp(p, 8)
                    game_state['events'].append({'type': 'floatText', 'x': res['x'], 'y': res['y'], 'text': f'+{gain} 🪨', 'color': '#a3a3a3'})
                    game_state['events'].append({'type': 'stone_sparks', 'x': target_x, 'y': target_y})
                
                if res['hp'] <= 0:
                    res['x'] = random.randint(100, MAP_SIZE - 100)
                    res['y'] = random.randint(100, MAP_SIZE - 100)
                    res['hp'] = 100
                return

        # Удар по ботах
        for bot in game_state['bots']:
            if math.hypot(bot['x'] - target_x, bot['y'] - target_y) < 32:
                deal_damage_bot(bot, int(30 * dmg_mult), p)
                game_state['events'].append({'type': 'punch_blood', 'x': target_x, 'y': target_y})
                return

        # Удар по гравцях
        for pid, other in game_state['players'].items():
            if other['id'] != p['id'] and other['isAlive'] and (not p['clan'] or other['clan'] != p['clan']):
                if math.hypot(other['x'] - target_x, other['y'] - target_y) < 32:
                    deal_damage(other, int(25 * dmg_mult), p)
                    game_state['events'].append({'type': 'punch_blood', 'x': target_x, 'y': target_y})
                    return

    elif itype == 'wall':
        if item.get('count', 0) > 0 or (p['wood'] >= 20 and p['stone'] >= 10):
            if item.get('count', 0) > 0: item['count'] -= 1
            else: p['wood'] -= 20; p['stone'] -= 10
            p['shootCd'] = 18
            wall_x = p['x'] + math.cos(p['angle']) * 55
            wall_y = p['y'] + math.sin(p['angle']) * 55
            game_state['placed_walls'].append({
                'id': random.random(),
                'x': wall_x, 'y': wall_y,
                'hp': 300, 'maxHp': 300,
                'ownerClan': p['clan']
            })
            add_xp(p, 12)
            game_state['events'].append({'type': 'build_dust', 'x': wall_x, 'y': wall_y})

    elif itype == 'medkit':
        if item.get('count', 0) > 0 and p['hp'] < p['maxHp']:
            item['count'] -= 1
            p['shootCd'] = 25
            p['hp'] = min(p['maxHp'], p['hp'] + 50)
            game_state['events'].append({'type': 'heal_particles', 'x': p['x'], 'y': p['y']})

    elif itype == 'nuke_remote':
        p['shootCd'] = 60
        item['type'] = 'empty'
        item['name'] = 'Порожньо'
        item['icon'] = '▫️'
        is_life = random.random() < 0.5
        if is_life:
            for pl in game_state['players'].values():
                if math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 600:
                    pl['hp'] = pl['maxHp']
            game_state['events'].append({'type': 'heal_wave', 'x': p['x'], 'y': p['y']})
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 40, 'text': '💚 ПУЛЬТ ЖИТТЯ!', 'color': '#00ff88'})
        else:
            p['hp'] = 0
            p['isAlive'] = False
            game_state['events'].append({'type': 'massive_nuke', 'x': p['x'], 'y': p['y']})
            for pl in game_state['players'].values():
                if pl['id'] != p['id'] and pl['isAlive'] and math.hypot(pl['x'] - p['x'], pl['y'] - p['y']) < 400:
                    deal_damage(pl, 100, p)

    elif itype != 'empty':
        if p['ammo'] <= 0:
            await reload(p['id'])
            return

        p['ammo'] -= 1
        bullet_cfg = {
            'rifle': {'dmg': int(26 * dmg_mult), 'speed': 16, 'color': '#ffcc00', 'cd': 10, 'type': 'bullet'},
            'minigun': {'dmg': int(18 * dmg_mult), 'speed': 18, 'color': '#ff5500', 'cd': 5, 'type': 'bullet'},
            'banana': {'dmg': int(38 * dmg_mult), 'speed': 12, 'color': '#ffe600', 'cd': 16, 'type': 'banana'},
            'boomerang': {'dmg': int(42 * dmg_mult), 'speed': 13, 'color': '#a35d27', 'cd': 20, 'type': 'boomerang'},
            'eye_laser': {'dmg': int(22 * dmg_mult), 'speed': 25, 'color': '#ff0055', 'cd': 4, 'type': 'laser'},
            'water_pistol': {'dmg': int(16 * dmg_mult), 'speed': 14, 'color': '#00d0ff', 'cd': 6, 'type': 'water'}
        }.get(itype, {'dmg': 20, 'speed': 14, 'color': '#fff', 'cd': 10, 'type': 'bullet'})

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
        game_state['events'].append({'type': 'shoot_muzzle', 'x': p['x'] + math.cos(p['angle'])*20, 'y': p['y'] + math.sin(p['angle'])*20, 'weapon': itype})

def deal_damage(target, dmg, attacker=None):
    actual_dmg = max(5, dmg - target.get('armor', 0))
    target['hp'] -= actual_dmg
    game_state['events'].append({'type': 'damage_blood', 'x': target['x'], 'y': target['y'], 'val': actual_dmg})
    if target['hp'] <= 0:
        target['isAlive'] = False
        target['hp'] = 0
        if attacker:
            add_xp(attacker, 75)
            attacker['scrap'] += 30
        game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '☠️ ЗАГИБЕЛЬ', 'color': '#ff2255'})

def deal_damage_bot(bot, dmg, attacker=None):
    bot['hp'] -= dmg
    game_state['events'].append({'type': 'damage_blood', 'x': bot['x'], 'y': bot['y'], 'val': dmg})
    if bot['hp'] <= 0:
        if attacker:
            add_xp(attacker, 45)
            attacker['scrap'] += 20
            attacker['ammo'] += 20
            game_state['events'].append({'type': 'floatText', 'x': bot['x'], 'y': bot['y'] - 20, 'text': '+45 XP +20⚙️', 'color': '#00ff88'})
        bot['x'] = random.randint(300, MAP_SIZE - 300)
        bot['y'] = random.randint(300, MAP_SIZE - 300)
        bot['hp'] = bot['maxHp']
        bot['state'] = 'patrol'

# --- КЛАНИ ТА ЧАТ ---

@sio.event
async def createClan(sid, data):
    p = game_state['players'].get(sid)
    if not p: return
    name = (data.get('name') or 'Клан')[:16].strip()
    tag = (data.get('tag') or 'CLAN').upper()[:6].strip()
    desc = (data.get('desc') or '')[:50].strip()

    if tag in clans:
        await sio.emit('chatMessage', {'sender': 'СИСТЕМА', 'text': f'Тег [{tag}] вже зайнято!', 'color': '#ff2255'}, room=sid)
        return

    clans[tag] = {'tag': tag, 'name': name, 'leader': p['name'], 'desc': desc, 'members': [p['name']]}
    p['clan'] = tag
    await sio.emit('clanUpdated', clans)
    await sio.emit('chatMessage', {'sender': 'СИСТЕМА', 'text': f'Клан [{tag}] "{name}" успішно створено!', 'color': '#00ff88'})

@sio.event
async def joinClan(sid, tag):
    p = game_state['players'].get(sid)
    if not p or tag not in clans: return
    p['clan'] = tag
    if p['name'] not in clans[tag]['members']:
        clans[tag]['members'].append(p['name'])
    await sio.emit('clanUpdated', clans)
    await sio.emit('chatMessage', {'sender': 'СИСТЕМА', 'text': f'Ви вступили в клан [{tag}]!', 'color': '#00ff88'}, room=sid)

@sio.event
async def sendChat(sid, data):
    p = game_state['players'].get(sid)
    if not p: return
    msg = (data.get('message') or '')[:80].strip()
    channel = data.get('channel', 'all')  # 'all' | 'clan'

    if not msg: return

    # Команди
    if msg.startswith('/'):
        parts = msg.split()
        cmd = parts[0].lower()
        if cmd == '/help':
            await sio.emit('chatMessage', {'sender': 'КОМАНДИ', 'text': '/help, /clan <tag>, /stats, /clear', 'color': '#38bdf8'}, room=sid)
        elif cmd == '/stats':
            txt = f"Рівень: {p['level']} | HP: {p['hp']}/{p['maxHp']} | Вбивств/Скрап: {p['scrap']}"
            await sio.emit('chatMessage', {'sender': 'СТАТИСТИКА', 'text': txt, 'color': '#38bdf8'}, room=sid)
        return

    p['chatMsg'] = msg
    p['chatTimer'] = 120  # 4 секунди бабл над головою

    payload = {
        'sender': p['name'],
        'clan': p['clan'],
        'text': msg,
        'channel': channel,
        'color': '#38bdf8' if channel == 'clan' else '#e2e8f0'
    }

    if channel == 'clan' and p['clan']:
        for pl_id, pl in game_state['players'].items():
            if pl['clan'] == p['clan']:
                await sio.emit('chatMessage', payload, room=pl_id)
    else:
        await sio.emit('chatMessage', payload)

@sio.event
async def interactChest(sid):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']: return

    for chest in game_state['chests']:
        if not chest['opened'] and math.hypot(chest['x'] - p['x'], chest['y'] - p['y']) < 65:
            chest['opened'] = True
            chest['respawnTimer'] = 180
            p['ammo'] += 30
            p['wood'] += 30
            p['scrap'] += 25
            add_xp(p, 35)

            # Додаємо спец-предмет у вільний або 3-й слот хотбара
            new_wpn = random.choice(SPECIAL_WEAPONS)
            p['hotbar'][2] = {'slot': 2, 'type': new_wpn, 'name': new_wpn.upper(), 'icon': '🎁'}
            game_state['events'].append({'type': 'chest_loot_sparks', 'x': chest['x'], 'y': chest['y']})
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 35, 'text': f"🎁 {new_wpn.upper()} В СЛОТІ 3!", 'color': '#ffe600'})
            return

@sio.event
async def craftItem(sid, item_type):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']: return

    if item_type == 'wall' and p['wood'] >= 25 and p['stone'] >= 15:
        p['wood'] -= 25; p['stone'] -= 15
        p['hotbar'][3]['count'] = p['hotbar'][3].get('count', 0) + 3
        add_xp(p, 15)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+3 Стіни 🧱', 'color': '#00ff88'})
    elif item_type == 'medkit' and p['wood'] >= 10 and p['scrap'] >= 10:
        p['wood'] -= 10; p['scrap'] -= 10
        p['hotbar'][4]['count'] = p['hotbar'][4].get('count', 0) + 1
        add_xp(p, 15)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+1 Аптечка 💉', 'color': '#00ff88'})
    elif item_type == 'armor' and p['stone'] >= 40 and p['scrap'] >= 25:
        p['stone'] -= 40; p['scrap'] -= 25
        p['armor'] = min(60, p['armor'] + 20)
        add_xp(p, 25)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '🛡️ БРОНЯ +20', 'color': '#38bdf8'})
    elif item_type == 'ammo' and p['stone'] >= 15 and p['scrap'] >= 15:
        p['stone'] -= 15; p['scrap'] -= 15
        p['ammo'] += 40
        add_xp(p, 10)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+40 Набоїв 📦', 'color': '#ffe600'})

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
        p['activeSlot'] = 0
        p['ammo'] = 30

@sio.event
async def disconnect(sid):
    if sid in game_state['players']:
        del game_state['players'][sid]

# --- ІГРОВИЙ ЦИКЛ (30 FPS) ---
async def game_loop():
    while True:
        active_players = [p for p in game_state['players'].values() if p['isAlive']]

        # ШІ ботів
        for bot in game_state['bots']:
            bot['strafeTimer'] -= 1
            if bot['strafeTimer'] <= 0:
                bot['strafeDir'] *= -1
                bot['strafeTimer'] = random.randint(25, 60)

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

                if bot['hp'] < bot['maxHp'] * 0.3:
                    bot['x'] -= math.cos(to_target_a) * 2.8
                    bot['y'] -= math.sin(to_target_a) * 2.8
                elif bot['type'] == 'berserk':
                    bot['x'] += math.cos(to_target_a) * 3.6
                    bot['y'] += math.sin(to_target_a) * 3.6
                    if min_dist < 45:
                        deal_damage(target, 18)
                else:
                    strafe_a = to_target_a + (math.pi / 2) * bot['strafeDir']
                    if min_dist > 280:
                        bot['x'] += math.cos(to_target_a) * 2.0
                        bot['y'] += math.sin(to_target_a) * 2.0
                    elif min_dist < 150:
                        bot['x'] -= math.cos(to_target_a) * 2.0
                        bot['y'] -= math.sin(to_target_a) * 2.0
                    bot['x'] += math.cos(strafe_a) * 1.8
                    bot['y'] += math.sin(strafe_a) * 1.8

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
                bot['state'] = 'patrol'
                bot['x'] += math.cos(bot['patrolAngle']) * 1.2
                bot['y'] += math.sin(bot['patrolAngle']) * 1.2
                bot['angle'] = bot['patrolAngle']
                if random.random() < 0.02:
                    bot['patrolAngle'] = random.uniform(0, math.pi * 2)

            bot['x'] = max(50, min(MAP_SIZE - 50, bot['x']))
            bot['y'] = max(50, min(MAP_SIZE - 50, bot['y']))

        # Кулі
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
                    game_state['events'].append({'type': 'wall_hit_debris', 'x': b['x'], 'y': b['y']})
                    game_state['bullets'].pop(i)
                    hit_wall = True
                    break
            if hit_wall: continue

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
                if hit_bot: continue

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
            if hit_player: continue

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
