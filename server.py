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

# Постійна база акаунтів
registered_users = {}

SKINS = {
    'default': {'name': 'Новобранець', 'color': '#0284c7', 'stroke': '#38bdf8', 'cost': 0},
    'camo': {'name': 'Лісовий Камуфляж', 'color': '#166534', 'stroke': '#22c55e', 'cost': 150},
    'cyber': {'name': 'Кібер-Ніндзя', 'color': '#701a75', 'stroke': '#f43f5e', 'cost': 300},
    'gold': {'name': 'Золотий Лорд', 'color': '#854d0e', 'stroke': '#facc15', 'cost': 600},
    'hazmat': {'name': 'Хімзахист', 'color': '#ea580c', 'stroke': '#fbbf24', 'cost': 200}
}

clans = {
    'ALPHA': {'tag': 'ALPHA', 'name': 'Альфа Загін', 'leader': 'Admin', 'desc': 'Елітні бійці', 'members': []}
}

game_state = {
    'players': {},
    'bots': [],
    'placed_walls': [],
    'chests': {},       # { chest_id: { x, y, opened, slots: [8] } }
    'airdrops': [],     # Аірдропи з парашутом
    'resources': [],
    'bullets': [],
    'killfeed': [],
    'events': []
}

LOOT_POOL = [
    {'type': 'rifle', 'name': 'Автомат M4', 'icon': '🔫', 'count': 1, 'category': 'weapon'},
    {'type': 'minigun', 'name': 'Мініган', 'icon': '💥', 'count': 1, 'category': 'weapon'},
    {'type': 'banana', 'name': 'Банан-Бумеранг', 'icon': '🍌', 'count': 1, 'category': 'weapon'},
    {'type': 'eye_laser', 'name': 'Очі-Лазери', 'icon': '👁️', 'count': 1, 'category': 'weapon'},
    {'type': 'water_pistol', 'name': 'Водяний Бластер', 'icon': '🔫', 'count': 1, 'category': 'weapon'},
    {'type': 'nuke_remote', 'name': 'Пульт Рулетка', 'icon': '☢️', 'count': 1, 'category': 'special'},
    {'type': 'ammo', 'name': 'Набої', 'icon': '📦', 'count': 30, 'category': 'ammo'},
    {'type': 'plate', 'name': 'Бронепластина', 'icon': '🛡️', 'count': 2, 'category': 'armor'},
    {'type': 'medkit', 'name': 'Велика Аптечка', 'icon': '💉', 'count': 2, 'category': 'med'},
    {'type': 'wood', 'name': 'Дерево', 'icon': '🪵', 'count': 40, 'category': 'res'},
    {'type': 'scrap', 'name': 'Металобрухт', 'icon': '⚙️', 'count': 25, 'category': 'res'}
]

def generate_chest_loot():
    slots = []
    for _ in range(6):
        if random.random() < 0.75:
            item = dict(random.choice(LOOT_POOL))
            item['id'] = random.random()
            slots.append(item)
        else:
            slots.append(None)
    return slots

def init_world():
    for _ in range(150):
        game_state['resources'].append({
            'id': random.random(),
            'x': random.randint(150, MAP_SIZE - 150),
            'y': random.randint(150, MAP_SIZE - 150),
            'type': random.choice(['wood', 'stone']),
            'hp': 100, 'maxHp': 100,
            'size': random.randint(28, 42)
        })

    for i in range(40):
        cid = f"chest_{i}"
        game_state['chests'][cid] = {
            'id': cid,
            'x': random.randint(250, MAP_SIZE - 250),
            'y': random.randint(250, MAP_SIZE - 250),
            'slots': generate_chest_loot(),
            'respawnTimer': 0
        }

    for i in range(16):
        game_state['bots'].append({
            'id': f'bot_{i}',
            'name': f'Рейдер_{i+1}',
            'x': random.randint(400, MAP_SIZE - 400),
            'y': random.randint(400, MAP_SIZE - 400),
            'angle': 0, 'hp': 90, 'maxHp': 90,
            'weapon': random.choice(['rifle', 'minigun', 'pickaxe']),
            'cd': 0, 'state': 'patrol',
            'strafeDir': 1, 'strafeTimer': 30,
            'patrolAngle': random.uniform(0, math.pi * 2)
        })

init_world()

def add_xp_and_gems(p, xp_amount, gems_amount=0):
    p['xp'] += xp_amount
    p['gems'] += gems_amount
    if p['name'] in registered_users:
        registered_users[p['name']]['gems'] += gems_amount
        registered_users[p['name']]['xp'] += xp_amount

    if p['xp'] >= p['xpToNext']:
        p['xp'] -= p['xpToNext']
        p['level'] += 1
        p['statPoints'] += 1
        p['gems'] += 50
        p['xpToNext'] = int(p['xpToNext'] * 1.35)
        p['hp'] = p['maxHp']
        game_state['events'].append({'type': 'level_up', 'x': p['x'], 'y': p['y'], 'lvl': p['level']})

# --- АВТОРИЗАЦІЯ ТА ЛОББІ ---

@sio.event
async def loginPlayer(sid, data):
    username = (data.get('username') or 'Боєць')[:14].strip()
    password = data.get('password') or ''
    clan_tag = (data.get('clan') or '').upper()[:6].strip()

    if username in registered_users:
        if registered_users[username]['password'] != password:
            await sio.emit('authError', 'Невірний пароль для цього акаунту!', room=sid)
            return
        user_data = registered_users[username]
    else:
        user_data = {
            'password': password,
            'clan': clan_tag,
            'level': 1, 'xp': 0, 'gems': 100,
            'unlockedSkins': ['default'],
            'currentSkin': 'default'
        }
        registered_users[username] = user_data

    # Створення сітки інвентаря (16 слотів) + 6 слотів хотбара
    inv_slots = [None] * 16
    inv_slots[0] = {'id': 1, 'type': 'pickaxe', 'name': 'Тактична Кирка', 'icon': '⛏️', 'count': 1}
    inv_slots[1] = {'id': 2, 'type': 'rifle', 'name': 'Автомат M4', 'icon': '🔫', 'count': 1}
    inv_slots[2] = {'id': 3, 'type': 'plate', 'name': 'Бронепластина', 'icon': '🛡️', 'count': 3}
    inv_slots[3] = {'id': 4, 'type': 'medkit', 'name': 'Аптечка', 'icon': '💉', 'count': 2}
    inv_slots[4] = {'id': 5, 'type': 'wood', 'name': 'Дерево', 'icon': '🪵', 'count': 60}
    inv_slots[5] = {'id': 6, 'type': 'stone', 'name': 'Камінь', 'icon': '🪨', 'count': 40}

    hotbar_indices = [0, 1, 2, 3, 4, 5]

    game_state['players'][sid] = {
        'id': sid,
        'name': username,
        'clan': user_data['clan'],
        'x': random.randint(1200, MAP_SIZE - 1200),
        'y': random.randint(1200, MAP_SIZE - 1200),
        'angle': 0,
        'hp': 100, 'maxHp': 100,
        'armorPlates': 3, 'maxPlates': 3,  # 3 пластини броні (як у Warzone)
        'isAlive': True,
        'isDowned': False,  # Падіння на коліна (DBNO)
        'downTimer': 0,
        'skin': user_data['currentSkin'],
        'level': user_data['level'],
        'xp': user_data['xp'],
        'xpToNext': user_data['level'] * 120,
        'gems': user_data['gems'],
        'statPoints': 0,
        'stats': {'hp': 0, 'speed': 0, 'damage': 0, 'gather': 0},
        'inventory': inv_slots,
        'hotbarMap': hotbar_indices,
        'activeSlot': 0,
        'ammo': 60, 'maxAmmo': 60,
        'shootCd': 0, 'dashCd': 0,
        'openedChestId': None
    }
    await sio.emit('authSuccess', {
        'id': sid,
        'player': game_state['players'][sid],
        'skins': SKINS,
        'userData': user_data,
        'clans': clans
    }, room=sid)

@sio.event
async def buySkin(sid, skin_id):
    p = game_state['players'].get(sid)
    if not p or skin_id not in SKINS: return
    u = registered_users[p['name']]
    cfg = SKINS[skin_id]

    if skin_id not in u['unlockedSkins'] and u['gems'] >= cfg['cost']:
        u['gems'] -= cfg['cost']
        u['unlockedSkins'].append(skin_id)
        u['currentSkin'] = skin_id
        p['gems'] = u['gems']
        p['skin'] = skin_id
        await sio.emit('userDataUpdate', u, room=sid)
    elif skin_id in u['unlockedSkins']:
        u['currentSkin'] = skin_id
        p['skin'] = skin_id
        await sio.emit('userDataUpdate', u, room=sid)

# --- ІНВЕНТАР ТА СКРИНІ ЗІ СЛОТАМИ ---

@sio.event
async def openChestRequest(sid, chest_id):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive'] or p['isDowned']: return
    chest = game_state['chests'].get(chest_id)
    if chest and math.hypot(chest['x'] - p['x'], chest['y'] - p['y']) < 80:
        p['openedChestId'] = chest_id
        await sio.emit('openChestUI', {'chestId': chest_id, 'slots': chest['slots']}, room=sid)

@sio.event
async def closeChestRequest(sid):
    p = game_state['players'].get(sid)
    if p: p['openedChestId'] = None

@sio.event
async def transferChestItem(sid, data):
    p = game_state['players'].get(sid)
    if not p: return
    cid = p.get('openedChestId')
    chest = game_state['chests'].get(cid)
    if not chest: return

    direction = data.get('direction') # 'to_inv' або 'to_chest'
    c_idx = data.get('chestIdx')
    i_idx = data.get('invIdx')

    if direction == 'to_inv':
        if 0 <= c_idx < len(chest['slots']) and 0 <= i_idx < len(p['inventory']):
            c_item = chest['slots'][c_idx]
            p_item = p['inventory'][i_idx]
            chest['slots'][c_idx] = p_item
            p['inventory'][i_idx] = c_item
    elif direction == 'to_chest':
        if 0 <= c_idx < len(chest['slots']) and 0 <= i_idx < len(p['inventory']):
            c_item = chest['slots'][c_idx]
            p_item = p['inventory'][i_idx]
            chest['slots'][c_idx] = p_item
            p['inventory'][i_idx] = c_item

    await sio.emit('chestUpdate', {'chestId': cid, 'slots': chest['slots']}, room=sid)

@sio.event
async def swapInventorySlots(sid, data):
    p = game_state['players'].get(sid)
    if not p: return
    from_s, to_s = data.get('from'), data.get('to')
    if 0 <= from_s < 16 and 0 <= to_s < 16:
        p['inventory'][from_s], p['inventory'][to_s] = p['inventory'][to_s], p['inventory'][from_s]

@sio.event
async def selectHotbarSlot(sid, slot_idx):
    p = game_state['players'].get(sid)
    if p and 0 <= slot_idx < 6:
        p['activeSlot'] = slot_idx

# --- ПАДІННЯ НА КОЛІНА (DBNO), ДОБИВАННЯ ТА РЕВУЙВ ---

@sio.event
async def executePlayer(sid, target_id):
    p = game_state['players'].get(sid)
    target = game_state['players'].get(target_id)
    if p and target and target['isDowned'] and math.hypot(p['x'] - target['x'], p['y'] - target['y']) < 65:
        target['isDowned'] = False
        target['isAlive'] = False
        target['hp'] = 0
        add_xp_and_gems(p, 100, 25)
        game_state['events'].append({'type': 'execute_blood', 'x': target['x'], 'y': target['y']})
        add_killfeed(p['name'], target['name'], '💀 ДОБИТТЯ')

@sio.event
async def reviveComplete(sid, target_id):
    p = game_state['players'].get(sid)
    target = game_state['players'].get(target_id)
    if p and target and target['isDowned'] and math.hypot(p['x'] - target['x'], p['y'] - target['y']) < 75:
        target['isDowned'] = False
        target['hp'] = 50
        add_xp_and_gems(p, 50, 10)
        game_state['events'].append({'type': 'heal_wave', 'x': target['x'], 'y': target['y']})
        game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '💚 ПІДНЯТИЙ!', 'color': '#00ff88'})

@sio.event
async def useArmorPlate(sid):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive'] or p['isDowned']: return
    # Шукаємо пластини в інвентарі
    for item in p['inventory']:
        if item and item['type'] == 'plate' and item['count'] > 0 and p['armorPlates'] < p['maxPlates']:
            item['count'] -= 1
            p['armorPlates'] += 1
            if item['count'] <= 0: p['inventory'][p['inventory'].index(item)] = None
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+1 ПЛАСТИНА 🛡️', 'color': '#38bdf8'})
            break

def add_killfeed(killer, victim, weapon):
    game_state['killfeed'].insert(0, {'id': random.random(), 'killer': killer, 'victim': victim, 'weapon': weapon, 'time': 150})
    if len(game_state['killfeed']) > 5: game_state['killfeed'].pop()

def deal_damage(target, dmg, attacker=None):
    # Спочатку пошкоджуються бронепластини
    if target.get('armorPlates', 0) > 0:
        target['armorPlates'] -= 1
        dmg = max(5, int(dmg * 0.4))
        game_state['events'].append({'type': 'plate_shatter', 'x': target['x'], 'y': target['y']})

    target['hp'] -= dmg
    game_state['events'].append({'type': 'damage_blood', 'x': target['x'], 'y': target['y'], 'val': dmg})

    if target['hp'] <= 0:
        if not target.get('isDowned', False):
            # Вхід у стан нокауту / падіння на коліна (DBNO)
            target['isDowned'] = True
            target['hp'] = 50
            target['downTimer'] = 25 * 30  # 25 секунд до повної загибелі
            game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '🩸 НА КОЛІНАХ!', 'color': '#ff2255'})
        else:
            # Якщо вже був на колінах - повна смерть
            target['isDowned'] = False
            target['isAlive'] = False
            target['hp'] = 0
            if attacker:
                add_xp_and_gems(attacker, 80, 15)
                add_killfeed(attacker['name'], target['name'], 'Автомат')

@sio.event
async def playerInput(sid, data):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']: return

    p['angle'] = data.get('angle', 0)
    keys = data.get('keys', {})
    stick = data.get('joystick', {'x': 0, 'y': 0})
    
    # Якщо на колінах - швидкість сильно обмежена
    speed = 1.3 if p['isDowned'] else (4.4 + p['stats']['speed'] * 0.35)

    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0) + stick.get('x', 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0) + stick.get('y', 0)

    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed

    if not p['isDowned'] and data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 90
        p['y'] += math.sin(p['angle']) * 90
        p['dashCd'] = 55
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(40, min(MAP_SIZE - 40, p['x']))
    p['y'] = max(40, min(MAP_SIZE - 40, p['y']))

    if p['dashCd'] > 0: p['dashCd'] -= 1

    if not p['isDowned'] and data.get('isShooting') and p['shootCd'] <= 0:
        await handle_combat_action(p)

    if p['shootCd'] > 0: p['shootCd'] -= 1

async def handle_combat_action(p):
    active_item_idx = p['hotbarMap'][p['activeSlot']]
    item = p['inventory'][active_item_idx] if 0 <= active_item_idx < 16 else None
    if not item: return

    itype = item['type']
    dmg_mult = 1.0 + p['stats']['damage'] * 0.15

    if itype == 'pickaxe':
        p['shootCd'] = 12
        tx = p['x'] + math.cos(p['angle']) * 60
        ty = p['y'] + math.sin(p['angle']) * 60
        for res in game_state['resources']:
            if math.hypot(res['x'] - tx, res['y'] - ty) < (res.get('size', 32) + 15):
                res['hp'] -= 35
                add_xp_and_gems(p, 10, 1)
                game_state['events'].append({'type': 'tree_chips' if res['type'] == 'wood' else 'stone_sparks', 'x': tx, 'y': ty})
                return
        for pl in game_state['players'].values():
            if pl['id'] != p['id'] and pl['isAlive'] and math.hypot(pl['x'] - tx, pl['y'] - ty) < 35:
                deal_damage(pl, int(25 * dmg_mult), p)
                return
    elif itype in ['rifle', 'minigun', 'banana', 'eye_laser', 'water_pistol']:
        if p['ammo'] <= 0:
            p['ammo'] = p['maxAmmo']
            return
        p['ammo'] -= 1
        p['shootCd'] = 6 if itype == 'minigun' else (4 if itype == 'eye_laser' else 11)
        game_state['bullets'].append({
            'x': p['x'], 'y': p['y'],
            'vx': math.cos(p['angle']) * 16,
            'vy': math.sin(p['angle']) * 16,
            'ownerId': p['id'], 'ownerClan': p['clan'],
            'dmg': int(26 * dmg_mult), 'color': '#ffcc00', 'bType': itype, 'life': 70
        })
        game_state['events'].append({'type': 'shoot_muzzle', 'x': p['x'], 'y': p['y'], 'weapon': itype})

@sio.event
async def respawn(sid):
    p = game_state['players'].get(sid)
    if p and not p['isAlive']:
        p['hp'] = p['maxHp']
        p['armorPlates'] = 3
        p['x'] = random.randint(1000, MAP_SIZE - 1000)
        p['y'] = random.randint(1000, MAP_SIZE - 1000)
        p['isAlive'] = True
        p['isDowned'] = False
        p['ammo'] = 60

# --- АІРДРОП ТА ІГРОВИЙ ЦИКЛ ---
async def airdrop_spawner():
    while True:
        await asyncio.sleep(90)  # Новий аірдроп кожні 90 сек
        aid = f"airdrop_{random.random()}"
        ax = random.randint(500, MAP_SIZE - 500)
        ay = random.randint(500, MAP_SIZE - 500)
        game_state['chests'][aid] = {
            'id': aid, 'x': ax, 'y': ay,
            'slots': [dict(random.choice(LOOT_POOL)) for _ in range(6)],
            'isAirdrop': True
        }
        game_state['events'].append({'type': 'airdrop_fall', 'x': ax, 'y': ay})

async def game_loop():
    while True:
        # DBNO таймери стікання кров'ю
        for p in game_state['players'].values():
            if p['isAlive'] and p['isDowned']:
                p['downTimer'] -= 1
                if p['downTimer'] <= 0:
                    p['isDowned'] = False
                    p['isAlive'] = False
                    p['hp'] = 0

        # Кулі та влучання
        for i in range(len(game_state['bullets']) - 1, -1, -1):
            b = game_state['bullets'][i]
            b['x'] += b['vx']; b['y'] += b['vy']; b['life'] -= 1

            for pid, pl in game_state['players'].items():
                if pl['isAlive'] and pl['id'] != b['ownerId']:
                    if b['ownerClan'] and pl['clan'] == b['ownerClan']: continue
                    if math.hypot(pl['x'] - b['x'], pl['y'] - b['y']) < 22:
                        attacker = game_state['players'].get(b['ownerId'])
                        deal_damage(pl, b['dmg'], attacker)
                        game_state['bullets'].pop(i)
                        break

            if b['life'] <= 0 and i < len(game_state['bullets']):
                game_state['bullets'].pop(i)

        # Оновлення Killfeed
        for k in game_state['killfeed']: k['time'] -= 1
        game_state['killfeed'] = [k for k in game_state['killfeed'] if k['time'] > 0]

        await sio.emit('stateUpdate', game_state)
        game_state['events'] = []
        await asyncio.sleep(1 / 30)

async def index_handler(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), 'public', 'index.html'))

app.router.add_get('/', index_handler)

async def start_tasks(app):
    app['game_loop'] = asyncio.create_task(game_loop())
    app['airdrop_loop'] = asyncio.create_task(airdrop_spawner())

async def cleanup_tasks(app):
    app['game_loop'].cancel()
    app['airdrop_loop'].cancel()

app.on_startup.append(start_tasks)
app.on_cleanup.append(cleanup_tasks)

if __name__ == '__main__':
    web.run_app(app, port=PORT)
