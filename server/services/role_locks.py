"""
Role-locked patterns: special blocks only accounts with a given server-only role
may own (and therefore place). Applied to every place the client reads its
account: the login reply (handlers/login.py) and DynamoDB G2_Accounts reads
(services/dynamo_ops.py). Server-only roles aren't game roles, so they are
stripped from the roleKeys the client sees.

Grant: add the role to the account's roleKeys (editor / Mongo), e.g. "blaze".
"""
import os

# Only accounts with the given role may own (and therefore place) the pattern.
# "larp" gates every custom block: only larp-role holders get them at all.
ROLE_LOCKED_PATTERNS = {"1x5x2_stuffedTextBlaze": "blaze",
                        "1x1_stuffed920Man": "larp",
                        "1x1_stuffedsdn": "larp",
                        "1x1_niche": "larp",
                        "1x1_giggle": "larp",
                        "1x1_920man": "larp",
                        "2x2_dancingskeleton": "larp",
                        "3x2x1_blazeScreenRole": "larp"}
# Roles hidden from the client. Neither "blaze" nor "larp" is hidden: the BLAZE
# blocks carry MapObject.requiredRoleKey = "blaze" (APK 131b4+), the custom
# blocks carry requiredRoleKey = "larp", and the game checks that against the
# roleKeys it sees, so it must see the role.
SERVER_ONLY_ROLES: set = set()
# Custom blocks every account owns (creative build menu). Kept out of the
# techweb list so survival players can't craft them. (Custom blocks are now
# larp-gated, so this is empty.)
ALWAYS_OWNED_PATTERNS: list = []
# Never served to anyone, even if stored on an account.
DISABLED_PATTERNS = {"3x3x1_blazeScreen",
                     # desert decorations with no mesh in 1.31 ("Could not find map
                     # object data with key ...") - placing them logs errors
                     "decoration_desert_arch0", "decoration_desert_pillar0",
                     "decoration_desert_pillar1", "decoration_desert_rock0",
                     "decoration_desert_rock1", "decoration_desert_cactus0",
                     "decoration_desert_cactus1", "decoration_desert_cactus2",
                     "decoration_desert_cactus3", "decoration_desert_cactus4",
                     "16x16x4_desertRock", "16x16x8_desertRock",
                     "2x2x2_desertRock", "8x8x4_desertRock"}


# Roles a server-side holder list controls. Each such role (it unlocks patterns
# and, for "blaze", saving shared maps in game) is only honoured for accounts on
# its list; anyone else carrying it has it stripped (login + every account read).
# Stored in globals {"key": "<role>_holders", "ids": [...]}; giving or taking the
# role in the editor keeps it in step. Env <ROLE>_HOLDERS seeds it (comma
# separated account IDs).
BLAZE_ROLE = "blaze"
LARP_ROLE = "larp"
_HOLDER_ROLES = (BLAZE_ROLE, LARP_ROLE)
_holders = {}
for _role in _HOLDER_ROLES:
    _seed = set(filter(None, (os.getenv(f"{_role.upper()}_HOLDERS") or "").replace(" ", "").split(",")))
    _holders[_role] = {"ids": _seed, "ts": 0.0}


async def load_holders(role: str) -> set:
    import time
    st = _holders[role]
    if time.monotonic() - st["ts"] > 30:
        try:
            from services.db import globals_col
            doc = await globals_col.find_one({"key": f"{role}_holders"}, {"_id": 0, "ids": 1})
            if doc and isinstance(doc.get("ids"), list):
                st["ids"] = set(doc["ids"])
        except Exception:
            pass
        st["ts"] = time.monotonic()
    return st["ids"]


async def set_holder(role: str, account_id: str, on: bool) -> None:
    from services.db import globals_col
    ids = set(await load_holders(role))
    (ids.add if on else ids.discard)(account_id)
    await globals_col.update_one({"key": f"{role}_holders"},
                                 {"$set": {"key": f"{role}_holders", "ids": sorted(ids)}}, upsert=True)
    _holders[role]["ids"], _holders[role]["ts"] = ids, 0.0


def is_holder(role: str, account_id) -> bool:
    return account_id in _holders[role]["ids"]


async def load_blaze_holders() -> set:
    return await load_holders(BLAZE_ROLE)


async def set_blaze_holder(account_id: str, on: bool) -> None:
    await set_holder(BLAZE_ROLE, account_id, on)


def is_blaze_holder(account_id) -> bool:
    return is_holder(BLAZE_ROLE, account_id)


def apply(owned, roles, account_id=None) -> tuple[list, list]:
    """(ownedPatterns, roleKeys) as the client should see them."""
    roles = list(roles or [])
    if account_id is not None:
        for _role in _HOLDER_ROLES:
            if _role in roles and not is_holder(_role, account_id):
                roles = [r for r in roles if r != _role]
    owned = list(owned or [])
    have = set(roles)
    owned = [p for p in owned if p not in DISABLED_PATTERNS]
    for pattern in ALWAYS_OWNED_PATTERNS:
        if pattern not in owned:
            owned.append(pattern)
    for pattern, role in ROLE_LOCKED_PATTERNS.items():
        if role in have:
            if pattern not in owned:
                owned.append(pattern)
        elif pattern in owned:
            owned.remove(pattern)
    return owned, [r for r in roles if r not in SERVER_ONLY_ROLES]
