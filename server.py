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

SKINS = {
    'default': {'name': 'Виживальник', 'color': '#334155', 'stroke': '#64748b', 'cost': 0},
    'camo': {'name': 'Камуфляж Сталкера', 'color': '#14532d', 'stroke': '#22c55e', 'cost': 150},
    'cyber': {'name': 'Рейдер Пустки', 'color': '#581c87', 'stroke': '#a855f7', 'cost': 300},
    'gold': {'name': 'Золотий Воїн', 'color': '#854d0e', 'stroke': '#facc15', 'cost': 600}
}

clans = {
    'ALPHA': {'tag': 'ALPHA', 'name': 'Альфа Загін', 'leader': 'Admin', 'desc': 'Еліта виживання', 'members': []}
}

game_state = {
    'players': {},
    'bots': [],
    'placed_walls': [],
    'chests': {},
    'resources': [],
    'bullets': [],
    'killfeed': [],
    'events': []
}

# Хардкорний лут-пул з точними ймовірностями
def generate_hardcore_chest_loot(is_airdrop=False):
    slots = [None] * 6
    for i in range(6):
        roll = random.random()
        
        # ТОТЕМ БЕЗСМЕРТЯ: 0.5% у звичайній скрині, 4% в Аірдропі
        totem_chance = 0.04 if is_airdrop else 0.005
        if random.random() < totem_chance:
            slots[i] = {
                'id': random.random(),
                'type': 'totem_undying',
                'name': 'Тотем Безсмертя',
                'icon': '🗿',
                'count': 1,
                'category': 'artifact'
            }
            continue

        if roll < 0.25:
            # Ресурси
            res_type = random.choice(['wood', 'stone', 'scrap'])
            res_meta = {'wood': ('Дерево', '🪵', random.randint(20, 45)),
                        'stone': ('Камінь', '🪨', random.randint(15, 35)),
                        'scrap': ('Металобрухт', '⚙️', random.randint(10, 25))}[res_type]
            slots[i] = {'id': random.random(), 'type': res_type, 'name': res_meta[0], 'icon': res_meta[1], 'count': res_meta[2], 'category': 'res'}
        elif roll < 0.40:
            # Медицина
            slots[i] = {'id': random.random(), 'type': 'bandage', 'name': 'Бинт', 'icon': '🩹', 'count': random.randint(1, 2), 'category': 'med'}
        elif roll < 0.48:
            # Дефіцитні набої (лише 4-8 шт!)
            slots[i] = {'id': random.random(), 'type': 'ammo', 'name': 'Рідкісні Набої', 'icon': '📦', 'count': random.randint(4, 8), 'category': 'ammo'}
        elif roll < 0.55:
            # Бронепластина
            slots[i] = {'id': random.random(), 'type': 'plate', 'name': 'Бронепластина', 'icon': '🛡️', 'count': 1, 'category': 'armor'}
        elif roll < 0.62 or is_airdrop:
            # Рідкісна вогнепальна зброя
            w_choice = random.choice([
                ('revolver', 'Саморобний Револьвер', '🔫'),
                ('shotgun', 'Дробовик-Труба', '💥'),
                ('rifle', 'Автомат M4', '🔫')
            ])
            slots[i] = {'id': random.random(), 'type': w_choice[0], 'name': w_choice[1], 'icon': w_choice[2], 'count': 1, 'category': 'weapon'}
        else:
            slots[i] = None
    return slots

def init_world():
    for _ in range(160):
        game_state['resources'].append({
            'id': random.random(),
            'x': random.randint(150, MAP_SIZE - 150),
            'y': random.randint(150, MAP_SIZE - 150),
            'type': random.choice(['wood', 'stone']),
            'hp': 100, 'maxHp': 100,
            'size': random.randint(28, 42)
        })

    for i in range(45):
        cid = f"chest_{i}"
        game_state['chests'][cid] = {
            'id': cid,
            'x': random.randint(250, MAP_SIZE - 250),
            'y': random.randint(250, MAP_SIZE - 250),
            'slots': generate_hardcore_chest_loot(False),
            'isAirdrop': False,
            'respawnTimer': 0
        }

    for i in range(14):
        game_state['bots'].append({
            'id': f'bot_{i}',
            'name': f'Дикун_{i+1}',
            'x': random.randint(400, MAP_SIZE - 400),
            'y': random.randint(400, MAP_SIZE - 400),
            'angle': 0, 'hp': 85, 'maxHp': 85,
            'weapon': random.choice(['hatchet', 'pickaxe', 'revolver']),
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
        p['gems'] += 40
        p['xpToNext'] = int(p['xpToNext'] * 1.4)
        p['hp'] = p['maxHp']
        game_state['events'].append({'type': 'level_up', 'x': p['x'], 'y': p['y'], 'lvl': p['level']})

@sio.event
async def loginPlayer(sid, data):
    username = (data.get('username') or 'Виживальник')[:14].strip()
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
            'level': 1, 'xp': 0, 'gems': 50,
            'unlockedSkins': ['default'],
            'currentSkin': 'default'
        }
        registered_users[username] = user_data

    # ХАРДКОРНИЙ ПОЧАТКОВИЙ ІНВЕНТАР: Тільки Сокира і Кайло!
    inv_slots = [None] * 16
    inv_slots[0] = {'id': 1, 'type': 'hatchet', 'name': 'Кам\'яна Сокира', 'icon': '🪓', 'count': 1}
    inv_slots[1] = {'id': 2, 'type': 'pickaxe', 'name': 'Кам\'яне Кайло', 'icon': '⛏️', 'count': 1}

    hotbar_indices = [0, 1, 2, 3, 4, 5]

    game_state['players'][sid] = {
        'id': sid,
        'name': username,
        'clan': user_data['clan'],
        'x': random.randint(1200, MAP_SIZE - 1200),
        'y': random.randint(1200, MAP_SIZE - 1200),
        'angle': 0,
        'hp': 100, 'maxHp': 100,
        'armorPlates': 0, 'maxPlates': 3,
        'isAlive': True,
        'isDowned': False,
        'downTimer': 0,
        'shieldTimer': 0, # Таймер невразливості від тотема
        'skin': user_data['currentSkin'],
        'level': user_data['level'],
        'xp': user_data['xp'],
        'xpToNext': user_data['level'] * 120,
        'gems': user_data['gems'],
        'inventory': inv_slots,
        'hotbarMap': hotbar_indices,
        'activeSlot': 0,
        'ammo': 0,
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
async def openChestRequest(sid, chest_id):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive'] or p['isDowned']: return
    chest = game_state['chests'].get(chest_id)
    if chest and math.hypot(chest['x'] - p['x'], chest['y'] - p['y']) < 85:
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

    direction = data.get('direction')
    c_idx = data.get('chestIdx')
    i_idx = data.get('invIdx')

    if direction == 'to_inv' and 0 <= c_idx < len(chest['slots']) and 0 <= i_idx < len(p['inventory']):
        chest['slots'][c_idx], p['inventory'][i_idx] = p['inventory'][i_idx], chest['slots'][c_idx]
    elif direction == 'to_chest' and 0 <= c_idx < len(chest['slots']) and 0 <= i_idx < len(p['inventory']):
        chest['slots'][c_idx], p['inventory'][i_idx] = p['inventory'][i_idx], chest['slots'][c_idx]

    # Синхронізація набоїв, якщо переклали патрони
    sync_player_ammo(p)
    await sio.emit('chestUpdate', {'chestId': cid, 'slots': chest['slots']}, room=sid)

def sync_player_ammo(p):
    total_ammo = 0
    for it in p['inventory']:
        if it and it['type'] == 'ammo':
            total_ammo += it.get('count', 0)
    p['ammo'] = total_ammo

@sio.event
async def selectHotbarSlot(sid, slot_idx):
    p = game_state['players'].get(sid)
    if p and 0 <= slot_idx < 6:
        p['activeSlot'] = slot_idx

# ХАРДКОРНИЙ КРАФТ
@sio.event
async def craftItem(sid, recipe):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive'] or p['isDowned']: return

    # Підрахунок матеріалів у гравця
    def get_resource_count(rtype):
        return sum(it['count'] for it in p['inventory'] if it and it['type'] == rtype)

    def deduct_resource(rtype, amount):
        remaining = amount
        for idx, it in enumerate(p['inventory']):
            if it and it['type'] == rtype:
                if it['count'] > remaining:
                    it['count'] -= remaining
                    return
                else:
                    remaining -= it['count']
                    p['inventory'][idx] = None
                if remaining <= 0: return

    def find_free_slot():
        for idx, it in enumerate(p['inventory']):
            if it is None: return idx
        return -1

    free_idx = find_free_slot()
    if free_idx == -1:
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '⚠️ Інвентар повний!', 'color': '#ff2255'})
        return

    if recipe == 'wood_wall' and get_resource_count('wood') >= 40:
        deduct_resource('wood', 40)
        p['inventory'][free_idx] = {'id': random.random(), 'type': 'wood_wall', 'name': 'Дерев\'яна Стіна', 'icon': '🧱', 'count': 2}
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+2 Стіни 🧱', 'color': '#22c55e'})
    elif recipe == 'bandage' and get_resource_count('wood') >= 20:
        deduct_resource('wood', 20)
        p['inventory'][free_idx] = {'id': random.random(), 'type': 'bandage', 'name': 'Бинт', 'icon': '🩹', 'count': 2}
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+2 Бинти 🩹', 'color': '#22c55e'})
    elif recipe == 'plate' and get_resource_count('stone') >= 35 and get_resource_count('scrap') >= 20:
        deduct_resource('stone', 35); deduct_resource('scrap', 20)
        p['inventory'][free_idx] = {'id': random.random(), 'type': 'plate', 'name': 'Бронепластина', 'icon': '🛡️', 'count': 1}
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+1 Пластина 🛡️', 'color': '#38bdf8'})
    elif recipe == 'ammo_craft' and get_resource_count('stone') >= 30 and get_resource_count('scrap') >= 30:
        deduct_resource('stone', 30); deduct_resource('scrap', 30)
        p['inventory'][free_idx] = {'id': random.random(), 'type': 'ammo', 'name': 'Набої', 'icon': '📦', 'count': 10}
        sync_player_ammo(p)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+10 Набоїв 📦', 'color': '#facc15'})

@sio.event
async def useArmorPlate(sid):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive'] or p['isDowned']: return
    for idx, item in enumerate(p['inventory']):
        if item and item['type'] == 'plate' and item['count'] > 0 and p['armorPlates'] < p['maxPlates']:
            item['count'] -= 1
            p['armorPlates'] += 1
            if item['count'] <= 0: p['inventory'][idx] = None
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+1 ПЛАСТИНА 🛡️', 'color': '#38bdf8'})
            break

# МЕХАНІКА УРОНУ, ТОТЕМУ БЕЗСМЕРТЯ ТА DBNO
def deal_damage(target, dmg, attacker=None):
    if target.get('shieldTimer', 0) > 0:
        game_state['events'].append({'type': 'shield_deflect', 'x': target['x'], 'y': target['y']})
        return

    # Бронепластини поглинають частину шкоди
    if target.get('armorPlates', 0) > 0:
        target['armorPlates'] -= 1
        dmg = max(5, int(dmg * 0.4))
        game_state['events'].append({'type': 'plate_shatter', 'x': target['x'], 'y': target['y']})

    target['hp'] -= dmg
    game_state['events'].append({'type': 'damage_blood', 'x': target['x'], 'y': target['y'], 'val': dmg})

    if target['hp'] <= 0:
        # ПЕРЕВІРКА ТОТЕМУ БЕЗСМЕРТЯ В ІНВЕНТАРІ!
        totem_slot = -1
        for idx, it in enumerate(target['inventory']):
            if it and it['type'] == 'totem_undying':
                totem_slot = idx
                break

        if totem_slot != -1:
            # СПРАЦЮВАВ ТОТЕМ БЕЗСМЕРТЯ!
            target['inventory'][totem_slot] = None # Знищується після порятунку
            target['hp'] = target['maxHp']
            target['isDowned'] = False
            target['shieldTimer'] = 5 * 30 # 5 секунд золотого невразливого щита
            game_state['events'].append({'type': 'totem_resurrect', 'x': target['x'], 'y': target['y']})
            game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 40, 'text': '🗿 ТОТЕМ БЕЗСМЕРТЯ СПРАЦЮВАВ!', 'color': '#facc15'})
            return

        # Якщо тотема немає - звичайне падіння на коліна (DBNO)
        if not target.get('isDowned', False):
            target['isDowned'] = True
            target['hp'] = 50
            target['downTimer'] = 25 * 30
            game_state['events'].append({'type': 'floatText', 'x': target['x'], 'y': target['y'] - 30, 'text': '🩸 НА КОЛІНАХ!', 'color': '#ff2255'})
        else:
            target['isDowned'] = False
            target['isAlive'] = False
            target['hp'] = 0
            if attacker:
                add_xp_and_gems(attacker, 80, 15)
                add_killfeed(attacker['name'], target['name'], 'Зброя')

def add_killfeed(killer, victim, weapon):
    game_state['killfeed'].insert(0, {'id': random.random(), 'killer': killer, 'victim': victim, 'weapon': weapon, 'time': 150})
    if len(game_state['killfeed']) > 5: game_state['killfeed'].pop()

@sio.event
async def playerInput(sid, data):
    p = game_state['players'].get(sid)
    if not p or not p['isAlive']: return

    p['angle'] = data.get('angle', 0)
    keys = data.get('keys', {})
    stick = data.get('joystick', {'x': 0, 'y': 0})
    
    # Пасивний бонус швидкості від Тотема Безсмертя (+25% швидкості!)
    has_totem = any(it and it['type'] == 'totem_undying' for it in p['inventory'])
    speed_mult = 1.25 if has_totem else 1.0

    speed = 1.2 if p['isDowned'] else (4.3 * speed_mult)

    vx = (1 if keys.get('d') else 0) - (1 if keys.get('a') else 0) + stick.get('x', 0)
    vy = (1 if keys.get('s') else 0) - (1 if keys.get('w') else 0) + stick.get('y', 0)

    length = math.hypot(vx, vy)
    if length > 0:
        p['x'] += (vx / length) * speed
        p['y'] += (vy / length) * speed

    if not p['isDowned'] and data.get('isDash') and p['dashCd'] <= 0:
        p['x'] += math.cos(p['angle']) * 85
        p['y'] += math.sin(p['angle']) * 85
        p['dashCd'] = 55
        game_state['events'].append({'type': 'dash', 'x': p['x'], 'y': p['y']})

    p['x'] = max(40, min(MAP_SIZE - 40, p['x']))
    p['y'] = max(40, min(MAP_SIZE - 40, p['y']))

    if p['dashCd'] > 0: p['dashCd'] -= 1
    if p['shieldTimer'] > 0: p['shieldTimer'] -= 1

    if not p['isDowned'] and data.get('isShooting') and p['shootCd'] <= 0:
        await handle_combat_action(p)

    if p['shootCd'] > 0: p['shootCd'] -= 1

async def handle_combat_action(p):
    active_idx = p['hotbarMap'][p['activeSlot']]
    item = p['inventory'][active_idx] if 0 <= active_idx < 16 else None
    if not item: return

    itype = item['type']

    if itype in ['hatchet', 'pickaxe']:
        p['shootCd'] = 13
        tx = p['x'] + math.cos(p['angle']) * 58
        ty = p['y'] + math.sin(p['angle']) * 58
        
        # Добування ресурсів сокирою/кайлом
        for res in game_state['resources']:
            if math.hypot(res['x'] - tx, res['y'] - ty) < (res.get('size', 32) + 15):
                res['hp'] -= 35
                add_xp_and_gems(p, 10, 1)
                
                # Додавання ресурсів в інвентар
                for inv_it in p['inventory']:
                    if inv_it and inv_it['type'] == res['type']:
                        inv_it['count'] += 15
                        break
                else:
                    for s_i, inv_it in enumerate(p['inventory']):
                        if inv_it is None:
                            p['inventory'][s_i] = {'id': random.random(), 'type': res['type'], 'name': 'Дерево' if res['type']=='wood' else 'Камінь', 'icon': '🪵' if res['type']=='wood' else '🪨', 'count': 15}
                            break

                game_state['events'].append({'type': 'tree_chips' if res['type'] == 'wood' else 'stone_sparks', 'x': tx, 'y': ty})
                if res['hp'] <= 0:
                    res['x'] = random.randint(100, MAP_SIZE - 100)
                    res['y'] = random.randint(100, MAP_SIZE - 100)
                    res['hp'] = 100
                return

        # Удар по ворогу в ближньому бою
        for pl in game_state['players'].values():
            if pl['id'] != p['id'] and pl['isAlive'] and math.hypot(pl['x'] - tx, pl['y'] - ty) < 35:
                deal_damage(pl, 28, p)
                return

    elif itype == 'wood_wall':
        p['shootCd'] = 20
        item['count'] -= 1
        if item['count'] <= 0: p['inventory'][active_idx] = None
        wx = p['x'] + math.cos(p['angle']) * 55
        wy = p['y'] + math.sin(p['angle']) * 55
        game_state['placed_walls'].append({'id': random.random(), 'x': wx, 'y': wy, 'hp': 250, 'maxHp': 250})
        game_state['events'].append({'type': 'floatText', 'x': wx, 'y': wy, 'text': '🧱 СТІНА', 'color': '#22c55e'})

    elif itype == 'bandage':
        p['shootCd'] = 25
        item['count'] -= 1
        if item['count'] <= 0: p['inventory'][active_idx] = None
        p['hp'] = min(p['maxHp'], p['hp'] + 35)
        game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '+35 HP 🩹', 'color': '#22c55e'})

    elif itype in ['revolver', 'shotgun', 'rifle']:
        # Постріл вимагає дефіцитних набоїв!
        ammo_item = next((it for it in p['inventory'] if it and it['type'] == 'ammo'), None)
        if not ammo_item or ammo_item['count'] <= 0:
            game_state['events'].append({'type': 'floatText', 'x': p['x'], 'y': p['y'] - 30, 'text': '🚫 НЕМАЄ НАБОЇВ!', 'color': '#ef4444'})
            p['shootCd'] = 15
            return

        ammo_item['count'] -= 1
        if ammo_item['count'] <= 0:
            p['inventory'][p['inventory'].index(ammo_item)] = None
        sync_player_ammo(p)

        dmg = 35 if itype == 'shotgun' else (28 if itype == 'revolver' else 24)
        p['shootCd'] = 14 if itype == 'shotgun' else 9
        
        game_state['bullets'].append({
            'x': p['x'], 'y': p['y'],
            'vx': math.cos(p['angle']) * 16,
            'vy': math.sin(p['angle']) * 16,
            'ownerId': p['id'], 'ownerClan': p['clan'],
            'dmg': dmg, 'color': '#f59e0b', 'bType': itype, 'life': 65
        })
        game_state['events'].append({'type': 'shoot_muzzle', 'x': p['x'], 'y': p['y'], 'weapon': itype})

@sio.event
async def respawn(sid):
    p = game_state['players'].get(sid)
    if p and not p['isAlive']:
        p['hp'] = p['maxHp']
        p['armorPlates'] = 0
        p['x'] = random.randint(1000, MAP_SIZE - 1000)
        p['y'] = random.randint(1000, MAP_SIZE - 1000)
        p['isAlive'] = True
        p['isDowned'] = False
        p['shieldTimer'] = 0
        p['inventory'] = [None] * 16
        p['inventory'][0] = {'id': 1, 'type': 'hatchet', 'name': 'Кам\'яна Сокира', 'icon': '🪓', 'count': 1}
        p['inventory'][1] = {'id': 2, 'type': 'pickaxe', 'name': 'Кам\'яне Кайло', 'icon': '⛏️', 'count': 1}
        sync_player_ammo(p)

# АІРДРОПИ ТА ЦИКЛ
async def airdrop_spawner():
    while True:
        await asyncio.sleep(100)
        aid = f"airdrop_{random.random()}"
        ax = random.randint(600, MAP_SIZE - 600)
        ay = random.randint(600, MAP_SIZE - 600)
        game_state['chests'][aid] = {
            'id': aid, 'x': ax, 'y': ay,
            'slots': generate_hardcore_chest_loot(True),
            'isAirdrop': True
        }
        game_state['events'].append({'type': 'airdrop_fall', 'x': ax, 'y': ay})

async def game_loop():
    while True:
        for p in game_state['players'].values():
            if p['isAlive'] and p['isDowned']:
                p['downTimer'] -= 1
                if p['downTimer'] <= 0:
                    p['isDowned'] = False
                    p['isAlive'] = False
                    p['hp'] = 0

        # Кулі
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
