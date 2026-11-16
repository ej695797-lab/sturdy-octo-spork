"""Explicit, backup-first installation of the host's compact Buddy public world."""
import hashlib
import json
from pathlib import Path

from bson import json_util
from services.client_profile import room_defaults
from services.dynamo_ops import _full_room
from services.mapsync_serializer import serialize_blocks_spam

LAYOUT_ID = 'buddy-compact-v1'
PUBLIC_KEYS = ('nexus', 'official_0', 'official_1', 'wild_0')
SIDES = (('Up', 0, 1, 'Down'), ('Right', 1, 0, 'Left'),
         ('Down', 0, -1, 'Up'), ('Left', -1, 0, 'Right'))


def _public_blocks(room_key: str) -> list:
    if room_key == 'wild_0':
        return []
    blocks = []

    def add(name, x, y, z, color='white'):
        blocks.append(dict(name=name, x=x, y=y, z=z, fwd=0, up=2,
                           color=color, owner=''))

    # A broad, flat plaza. Central orange lanes mark the exits; leave the
    # native floor in place beneath and around it, including the doorways.
    for x in range(8, 56, 4):
        for z in range(8, 56, 4):
            color = 'orange' if x in (28, 32) or z in (28, 32) else 'blue_dark'
            add('4x4_stuffed', x, 0, z, color)
    add('navigationSignpostBlock', 36, 1, 36)
    if room_key == 'nexus':
        add('privateWorldsPortal', 20, 1, 36)
        for x, z in ((12, 12), (48, 12), (12, 48), (48, 48)):
            for y in (1, 3, 5):
                add('2x2x2_stuffed', x, y, z)
    elif room_key == 'official_0':
        # Low climbing steps and bounce pads, clear of the central travel lanes.
        for index in range(5):
            for level in range(index + 1):
                add('2x2x2_stuffed', 12 + index * 3, 1 + level * 2, 16, 'orange')
        for x, z in ((16, 44), (44, 44), (44, 16)):
            add('4x4_trampoline', x, 1, z, 'orange')
    elif room_key == 'official_1':
        for x in (16, 44):
            for z in (16, 44):
                add('2x2x2_stuffed', x, 1, z)
    return blocks


def build_compact_world(session_id: int) -> dict:
    if not 0 < session_id < 2**31:
        raise ValueError('Session must fit the client Int32 format')
    definitions = [
        ('nexus', 'Public Hub', [0, 0]),
        ('official_0', 'Playground', [1, 0]),
        ('wild_0', 'Wilds / Building', [2, 0]),
        ('official_1', 'Welcome', [0, 1]),
    ]
    nodes = [dict(roomKey=key, roomName=name, themeKey='suburb', gridPosition=pos,
                  isOfficial=int(key != 'wild_0'), visitVersion=1) for key, name, pos in definitions]
    nodes.append(dict(transitionKey='private', themeKey='sandbox', gridPosition=[-1, 0]))
    positions = {tuple(n['gridPosition']) for n in nodes}
    rooms, imports = {}, {}
    for node in nodes:
        x, y = node['gridPosition']
        for side, dx, dy, _ in SIDES:
            node['block' + side] = int((x + dx, y + dy) not in positions)
        key = node.get('roomKey')
        if not key:
            continue
        room = {**room_defaults(), **_full_room(key, node['roomName'], [64, 80, 64],
                                              theme='suburb', lobby=1)}
        room.update(isOfficial=int(key != 'wild_0'), isCreative=int(key == 'wild_0'),
                    isModeLocked=int(key != 'wild_0'),
                    # Native public creative joins require noSave=1. Saveable
                    # public rooms reject nonprivileged creative players.
                    # Wilds has no imported build; hubs still load theirs.
                    noSave=int(key == 'wild_0'), isNoTerrain=0, isNoFloor=0, floorDepth=0,
                    lastEditedSessionID=session_id, roomDataVersion=1,
                    terrainDataVersion=1, _buddyLayout=LAYOUT_ID)
        for side, *_ in SIDES:
            room['door_' + side.lower()] = {'block': node['block' + side], 'position': [0, 0]}
        rooms[key] = room
        blocks = _public_blocks(key)
        imports[key] = dict(roomKey=key, blocks=blocks, blockCount=len(blocks),
                            dimensions=room['dimensions'], mapDataVersion=3,
                            saveData=serialize_blocks_spam(blocks),
                            saveInternalStates='U1BBTQMAAAAA', _buddyLayout=LAYOUT_ID)
    return {'graph': {'uniqueID': 'trickOrTreat_0', 'data': nodes, 'version': session_id},
            'rooms': rooms, 'imports': imports}


async def _private_digest(db) -> str:
    snapshot = {}
    for name, fields in [('rooms', ['roomKey']), ('maps', ['roomKey', 'mapKey']),
                         ('imported_maps', ['roomKey']), ('room_maps', ['versionKey', 'roomMapKey'])]:
        query = {'$or': [{field: {'$regex': '^p_'}} for field in fields]}
        docs = await db[name].find(query, {'_id': 0}).to_list(None)
        snapshot[name] = sorted(json_util.dumps(doc, sort_keys=True) for doc in docs)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


async def install_compact_world(db, backup_path: Path, session_id: int) -> dict:
    if db.name != 'Yeeps232Buddy':
        raise ValueError('Compact world installation is restricted to Yeeps232Buddy')
    before = await _private_digest(db)
    existing = await db.room_maps.find_one({'roomMapKey': 'trickOrTreat_0'})
    public = await db.room_maps.find_one({'roomMapKey': 'public'})
    if all(doc and doc.get('_buddyLayout') == LAYOUT_ID for doc in (existing, public)):
        return {'changed': False, 'privateBefore': before, 'privateAfter': before}
    world = build_compact_world(session_id)
    queries = {
        'rooms': {'roomKey': {'$in': list(PUBLIC_KEYS)}},
        'imported_maps': {'roomKey': {'$in': list(PUBLIC_KEYS)}},
        'maps': {'$or': [{'roomKey': {'$in': list(PUBLIC_KEYS)}},
                          {'mapKey': {'$regex': r'^(nexus|official_0|official_1|wild_0)\$'}}]},
        'room_maps': {'$or': [{'versionKey': {'$in': ['beta4', 'trickOrTreat_0', 'public']}},
                               {'roomMapKey': {'$in': ['trickOrTreat_0', 'public']}}]},
    }
    backup = {'layout': LAYOUT_ID, 'privateDigest': before}
    for name, query in queries.items():
        backup[name] = await db[name].find(query).to_list(None)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never destroy the original recovery snapshot.
    with backup_path.open('x') as stream:
        stream.write(json_util.dumps(backup, indent=2))
    for key, room in world['rooms'].items():
        await db.rooms.update_one({'roomKey': key}, {'$set': room}, upsert=True)
        await db.imported_maps.update_one({'roomKey': key}, {'$set': world['imports'][key]}, upsert=True)
    # Old saved terrain otherwise takes precedence over the new suburb preset.
    # Its original bytes are in the backup; retain the old block snapshots.
    await db.maps.update_many({'mapKey': {'$in': [key + '$current' for key in PUBLIC_KEYS]}},
                             {'$unset': {'terrainSaveData': '', 'terrainDataVersion': ''}})
    # Publish the graph last. Dual shapes let Lambda and Dynamo agree, while
    # the legacy beta4 graph remains available for rollback.
    for alias in ('public', 'trickOrTreat_0'):
        graph = {**world['graph'], 'uniqueID': alias}
        query = {'$or': [{'roomMapKey': alias}, {'versionKey': alias}]}
        await db.room_maps.update_one(query, {'$set': {
            'roomMapKey': alias, 'versionKey': alias, 'graph': graph,
            'data': graph['data'], 'version': graph['version'], '_buddyLayout': LAYOUT_ID}}, upsert=True)
    after = await _private_digest(db)
    if before != after:
        raise RuntimeError('Private data changed during installation; inspect the backup before proceeding')
    return {'changed': True, 'privateBefore': before, 'privateAfter': after,
            'rooms': len(world['rooms']), 'nodes': len(world['graph']['data']),
            'backup': str(backup_path)}
