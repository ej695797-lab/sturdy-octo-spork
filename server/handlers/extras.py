"""Yeeps extras (patched in by apply_extras_patch.py -> server/handlers/extras.py).

  * login streaks + challenge rewards
  * shop-rotation overrides you can set yourself
  * LOCAL community worlds (stored in your own database, never contacting real Yeeps)
  * room purchases switched back on
  * a log of every Lambda the server has no handler for

Switches (server/.env, or Environment on Render). 1 = on, 0 = off:
  EXTRAS_CHALLENGES=1        login streaks + challenge rewards
  EXTRAS_COMMUNITY=1         local community worlds
  EXTRAS_COMMUNITY_LISTS=0   featured/popular lists (the entry format is a GUESS, see README)
  EXTRAS_ROOM_PURCHASES=1    allow buying extra private rooms / expanding the private world
  EXTRAS_LOG_CALLS=1         log unknown Lambdas and rotation requests
  CHALLENGE_REWARD_LOGIN / _EASY / _HARD   currency per category (25 / 50 / 100)
"""
import os
import re
from datetime import datetime, timedelta, timezone

from services.db import accounts, db, globals_col

TS = "%Y %m/%d %H:%M:%S"
FAR = "2099 01/01 00:00:00"
MAX_CURRENCY = 2_000_000_000          # the client keeps currency in an int32

worlds = db["community_worlds"]
extra_calls = db["extra_calls"]        # recent request/response pairs (newest 500)
extra_unknown = db["extra_unknown"]    # one row per Lambda / rotation the server doesn't know


# ───────────────────────────────────────────────────────────── switches / helpers
def flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def challenges_on() -> bool: return True
def community_on() -> bool: return True
def lists_on() -> bool: return True
def room_purchases_on() -> bool: return True
def log_calls_on() -> bool: return flag("EXTRAS_LOG_CALLS", True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def _parse(s):
    try:
        return datetime.strptime(str(s), TS).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def clean(body: dict) -> dict:
    return {k: v for k, v in (body or {}).items() if not str(k).startswith("_")}


async def log_call(kind: str, fn: str, body: dict, resp: dict | None = None) -> None:
    if not log_calls_on():
        return
    try:
        await extra_calls.insert_one({"at": _fmt(_now()), "kind": kind, "fn": fn,
                                      "body": clean(body), "resp": resp})
        n = await extra_calls.estimated_document_count()
        if n > 600:
            old = extra_calls.find({}, {"_id": 1}).sort("_id", 1).limit(n - 500)
            ids = [d["_id"] async for d in old]
            if ids:
                await extra_calls.delete_many({"_id": {"$in": ids}})
    except Exception as e:
        print(f"[extras] call log failed: {e}")


_seen: set = set()


async def note(fn: str, body: dict) -> None:
    """Remember that the game asked for something the server doesn't know (once per name in the log)."""
    if not log_calls_on():
        return
    fn = str(fn or "?")[:80]
    if fn not in _seen:
        _seen.add(fn)
        print(f"[extras] no handler for: {fn}")
    try:
        await extra_unknown.update_one(
            {"fn": fn}, {"$inc": {"count": 1},
                         "$set": {"lastAt": _fmt(_now()), "lastBody": clean(body)}}, upsert=True)
    except Exception as e:
        print(f"[extras] unknown-call log failed: {e}")


async def log_unknown(body: dict) -> None:
    await note(body.get("_fn") or body.get("functionName") or "?", body)


# ──────────────────────────────────────────────────── login streaks + challenges
# Tier names come from the game's own badge cosmetics (cosmetic_zipper_loginStreakBadge_*);
# the day counts are this patch's choice.
TIERS = [("bronze", 3), ("silver", 7), ("gold", 14), ("platinum", 30), ("diamond", 60)]


def streak_data(n: int) -> dict:
    cur, nxt = "", TIERS[0]
    for i, (name, th) in enumerate(TIERS):
        if n >= th:
            cur = name
            nxt = TIERS[i + 1] if i + 1 < len(TIERS) else (name, th)
    return {"length": n, "currentTier": cur, "nextTierThreshold": nxt[1], "nextTier": nxt[0]}


async def apply_login_streak(account: dict, now: datetime | None = None) -> None:
    """Count consecutive UTC days of logging in; update the fields the client reads."""
    if not challenges_on() or not account.get("accountID"):
        return
    now = now or _now()
    today = now.date().isoformat()
    row = await accounts.find_one({"accountID": account["accountID"]}, {"_id": 0, "_streakDay": 1,
                                                                        "loginStreakLength": 1,
                                                                        "loginStreakLongestLength": 1})
    row = row or {}
    last = row.get("_streakDay") or ""
    length = max(0, _int(row.get("loginStreakLength", account.get("loginStreakLength")), 0))
    if last == today:
        length = max(length, 1)
    elif last == (now.date() - timedelta(days=1)).isoformat():
        length += 1
    else:
        length = 1
    longest = max(length, _int(row.get("loginStreakLongestLength", account.get("loginStreakLongestLength")), 0))
    expire = _fmt(datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=2))
    fields = {"loginStreakLength": length, "loginStreakLongestLength": longest,
              "loginStreakExpireTimestamp": expire, "loginStreakData": streak_data(length)}
    account.update(fields)
    await accounts.update_one({"accountID": account["accountID"]},
                              {"$set": {**fields, "_streakDay": today,
                                        "loginStreakLongestLastUpdatedTimestamp": _fmt(now)}})


CATEGORIES = ("login", "easy", "hard")
_SKIP_KEYS = {"accountID", "functionName", "applicationVersion", "platform"}


def _reward(cat: str) -> int:
    return max(0, _int(os.getenv(f"CHALLENGE_REWARD_{cat.upper()}"), {"login": 25, "easy": 50, "hard": 100}[cat]))


def challenge_category(body: dict) -> str:
    """The request format isn't in the kit: look for login/easy/hard in any value or key."""
    for k, v in (body or {}).items():
        if str(k).startswith("_") or k in _SKIP_KEYS:
            continue
        s = f"{k} {v}".lower()
        for cat in CATEGORIES:
            if cat in s:
                return cat
    return "login"


async def redeem_challenge(body: dict) -> dict:
    if not challenges_on():
        return {"success": 1, "currencyAmount": 0, "newCurrency": 0, "items": [],
                "newLastChallengeRedeemedTime": FAR}
    account_id = str(body.get("accountID") or "")
    cat = challenge_category(body)
    now = _now()
    field = f"lastChallengeRedeemedTime_{cat}"
    acc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "currency": 1, field: 1}) or {}
    last = _parse(acc.get(field))
    currency = _int(acc.get("currency"), 0)
    reward, new_last = _reward(cat), _fmt(now)
    new_currency = min(MAX_CURRENCY, currency + reward)
    if account_id:
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"currency": new_currency, field: new_last}})
    resp = {"success": 1, "currencyAmount": reward, "newCurrency": new_currency, "items": [],
            "newLastChallengeRedeemedTime": new_last}
    await log_call("challenge", cat, body, resp)
    return resp


async def rotation(body: dict, orig) -> dict:
    """Wrap fetchGlobalRotation: real streak data for 'challenges' + host overrides for any key."""
    res = await orig(body)
    key = str(body.get("rotationKey") or "")
    await note(f"rotation:{key or '-'}", body)
    if key == "challenges" and challenges_on():
        acc = await accounts.find_one({"accountID": str(body.get("accountID") or "")},
                                      {"_id": 0, "loginStreakLength": 1}) or {}
        n = _int(acc.get("loginStreakLength"), 0)
        su = res.setdefault("streakUpdate", {"itemKeysToRemove": [], "itemKeysToAdd": []})
        su["loginStreakData"] = streak_data(n)
    try:
        doc = await globals_col.find_one({"key": "rotation_overrides"}, {"_id": 0, "value": 1})
        ov = ((doc or {}).get("value") or {}).get(key)
        if isinstance(ov, dict):
            res.update(ov)
    except Exception as e:
        print(f"[extras] rotation override failed: {e}")
    return res


# ─────────────────────────────────────────────────────────── community worlds
_NAME = re.compile(r"^[A-Z0-9_]{3,24}$")


def norm_name(v) -> str:
    s = re.sub(r"\s+", "_", str(v or "").strip().upper())
    if s.startswith("C_"):
        s = s[2:]
    return s if _NAME.match(s) else ""


def _body_world(body: dict) -> str:
    for k in ("worldName", "name", "roomName", "communityWorldName", "roomKey", "worldKey"):
        n = norm_name(body.get(k))
        if n:
            return n
    return ""


async def get_world(name) -> dict | None:
    n = norm_name(name)
    return await worlds.find_one({"worldKey": n}, {"_id": 0}) if n else None


async def world_for_room(room_key: str) -> dict | None:
    rk = str(room_key or "")
    if not rk.startswith("c_"):
        return None
    if rk.endswith("_sandbox"):
        rk = rk[:-len("_sandbox")]
    return await get_world(rk)


async def world_room_name(room_key: str, default: str) -> str:
    w = await world_for_room(room_key)
    return (w or {}).get("worldName") or default


async def may_edit_world(account_id: str, room_key: str) -> bool:
    w = await world_for_room(room_key)
    if not w or not account_id:
        return False
    if w.get("adminAccountID") == account_id:
        return True
    for key in ("staffPermissions", "staff", "permissions", "editors", "moderators"):
        val = (w.get(key) or {}).get(account_id) if isinstance(w.get(key), dict) else None
        if val is None:
            lst = w.get(key)
            if isinstance(lst, list) and account_id in lst:
                return True
        else:
            if val not in (False, 0, None, "0", "false", "False"):
                return True
    return False


async def create_world(name, admin_id: str, description: str = "", featured: bool = False):
    """-> (ok, message, doc)"""
    n = norm_name(name)
    if not n:
        return False, "Use 3-24 letters, numbers or underscores.", None
    if await worlds.find_one({"worldKey": n}):
        return False, f"{n} already exists.", None
    admin_name = ""
    if admin_id:
        a = await accounts.find_one({"accountID": admin_id}, {"_id": 0, "displayName": 1})
        admin_name = (a or {}).get("displayName", "")
    doc = {"worldKey": n, "roomKey": f"c_{n}", "worldName": n, "adminAccountID": admin_id,
           "adminName": admin_name, "description": str(description or "")[:200],
           "createdAt": _fmt(_now()), "fuelExpireTimestamp": FAR, "visitCount": 0,
           "featured": bool(featured), "staffPermissions": {}}
    await worlds.insert_one(dict(doc))
    try:
        from services.dynamo_ops import _full_room
        from services.db import rooms
        rdoc = _full_room(f"c_{n}", n, [80, 80, 80], theme="suburb", read_only=False)
        rdoc.pop("_id", None)
        await rooms.update_one({"roomKey": f"c_{n}"}, {"$set": rdoc}, upsert=True)
    except Exception as e:
        print(f"[cw] room doc create failed for {n}: {e}")
    return True, "created", doc


async def delete_world(name) -> bool:
    n = norm_name(name)
    return bool(n) and (await worlds.delete_one({"worldKey": n})).deleted_count > 0


async def list_worlds() -> list:
    return [w async for w in worlds.find({}, {"_id": 0}).sort("createdAt", -1)]


async def set_featured(name, on: bool) -> bool:
    n = norm_name(name)
    return bool(n) and (await worlds.update_one({"worldKey": n}, {"$set": {"featured": bool(on)}})).matched_count > 0


# The game's lambdas ----------------------------------------------------------
async def cw_get_can_enter(body: dict) -> dict:
    if not community_on():
        return {"canEnter": 0, "reason": "Community worlds are off on this server."}
    name = _body_world(body)
    w = await get_world(name) if name else None
    resp = ({"canEnter": 1, "reason": "", "roomKey": w["roomKey"]} if w else
            {"canEnter": 0, "reason": f"There is no community world called {name or 'that'}."})
    await log_call("cw", "cwGetCanEnter", body, resp)
    return resp


async def cw_track_visit(body: dict) -> dict:
    if community_on():
        n = _body_world(body)
        if n:
            await worlds.update_one({"worldKey": n}, {"$inc": {"visitCount": 1},
                                                      "$set": {"lastVisitAt": _fmt(_now())}})
        await log_call("cw", "cwTrackVisit", body, {"success": 1})
    return {"success": 1}


async def cw_try_purchase_fuel(body: dict) -> dict:
    """Fuel keeps a world open on real Yeeps; here every world has fuel until 2099 and it's free."""
    fuel, currency = FAR, 0
    if community_on():
        w = await get_world(_body_world(body))
        fuel = (w or {}).get("fuelExpireTimestamp") or FAR
        acc = await accounts.find_one({"accountID": str(body.get("accountID") or "")}, {"_id": 0, "currency": 1})
        currency = min(MAX_CURRENCY, _int((acc or {}).get("currency"), 0))
        await log_call("cw", "cwTryPurchaseFuel", body, {"fuel": fuel})
    return {"success": 1, "newCurrency": currency, "fuelExpireTimestamp": fuel, "cost": 0}


async def cw_edit_permissions(body: dict) -> dict:
    """Admin sets staff permissions. Handles several request shapes (logged for diagnosis)."""
    if not community_on():
        return {"success": 1}
    await log_call("cw", "cwEditPermissions", body)
    w = await get_world(_body_world(body))
    if not w:
        return {"success": 1}
    admin_id = str(body.get("accountID") or "")
    if w.get("adminAccountID") != admin_id:
        return {"success": 1}
    perms = None
    for k in ("staffPermissions", "permissions", "newPermissions", "staff", "staffList"):
        v = body.get(k)
        if isinstance(v, dict):
            perms = v
            break
        if isinstance(v, list):
            perms = {str(x): True for x in v}
            break
    target = (body.get("staffAccountID") or body.get("targetAccountID") or
              body.get("targetAccount") or body.get("staffID"))
    if perms is None and target:
        val = body.get("canEdit", body.get("permission", body.get("value", True)))
        perms = {str(target): val}
    if perms is None:
        return {"success": 1}
    clean_perms = {str(k)[:80]: (v if not isinstance(v, dict) else True) for k, v in list(perms.items())[:50]}
    existing = w.get("staffPermissions") or {}
    merged = dict(existing)
    merged.update(clean_perms)
    await worlds.update_one({"worldKey": w["worldKey"]},
                            {"$set": {"staffPermissions": merged}})
    return {"success": 1}


async def world_list(body: dict, which: str) -> dict:
    """Featured / popular lists. The entry fields are a GUESS (the real format isn't in the kit),
    so this is off unless EXTRAS_COMMUNITY_LISTS=1; the request is logged either way."""
    if not (community_on() and lists_on()):
        return {"worlds": []}
    query = {"featured": True} if which == "featured" else {}
    sort = [("visitCount", -1)] if which == "popular" else [("createdAt", -1)]
    rows = [w async for w in worlds.find(query, {"_id": 0}).sort(sort).limit(50)]
    out = [{"worldName": w["worldName"], "roomKey": w["roomKey"], "adminAccountID": w.get("adminAccountID", ""),
            "adminName": w.get("adminName", ""), "description": w.get("description", ""),
            "visitCount": w.get("visitCount", 0)} for w in rows]
    await log_call("cw", f"{which}WorldList", body, {"count": len(out)})
    return {"worlds": out}


async def purchase_world(body: dict):
    """tryPurchaseCommunityWorld: make the world. Returns None if the request has no usable name
    (the original handler then logs the request, so its format can be checked)."""
    n = _body_world(body)
    acct = str(body.get("accountID") or "")
    if not n or not acct:
        return None
    price = max(0, _int(body.get("expectedPrice", body.get("cost", 1000)), 1000))
    acc = await accounts.find_one({"accountID": acct}, {"_id": 0, "currency": 1}) or {}
    currency = _int(acc.get("currency"), 0)
    if currency < price:
        await log_call("cw", "tryPurchaseCommunityWorld", body, {"refused": "insufficient funds"})
        return {"_lambda_error": "INSUFFICIENT_FUNDS"}
    ok, msg, _ = await create_world(n, acct)
    if not ok:
        await log_call("cw", "tryPurchaseCommunityWorld", body, {"refused": msg})
        return {"_lambda_error": "COMMUNITY_WORLD_NAME_TAKEN" if "exists" in msg else "COMMUNITY_WORLD_BAD_NAME"}
    new_currency = min(MAX_CURRENCY, currency - price)
    if acct:
        await accounts.update_one({"accountID": acct}, {"$set": {"currency": new_currency}})
    resp = {"success": 1, "cost": price, "currencyValueIndex": 0,
            "newCurrency": new_currency}
    await log_call("cw", "tryPurchaseCommunityWorld", body, resp)
    return resp


async def delete_world_for(body: dict) -> None:
    n = _body_world(body)
    w = await get_world(n) if n else None
    if w and w.get("adminAccountID") == str(body.get("accountID") or ""):
        await delete_world(n)
