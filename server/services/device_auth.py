"""
Per-headset device keys: proof of WHO is making a request.

The game API has no player login of its own (requests only carry an accountID).
From build 131b14 the client puts a device key in every API URL, /k/<64 hex>/...
(sha256 of the headset's Android ID: stable across reinstalls, different per
headset, never sent raw). main.py strips it into request.state.device_key.

Binding: the first login of a brand-new account from a key binds that key to the
account. Existing accounts can be rebound only after an admin resets their
headset lock. After binding, requests for that account must carry one of its
bound keys.

DEVICE_AUTH env: "observe" (default) binds and logs but never blocks - used while
players move to the new build; "enforce" blocks. Keys are stored peppered +
hashed, so a database leak doesn't hand out usable keys.
"""

import hashlib
import os
import re
import time

MODE = os.getenv("DEVICE_AUTH", "observe").strip().lower()
_PEPPER = os.getenv("DEVICE_KEY_PEPPER") or os.getenv("EDITOR_SECRET", "") or "blaze-device"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

_cache: dict[str, tuple[float, list]] = {}      # accountID -> (expires, deviceKeys)
_CACHE_TTL = 60.0
_logged: dict[str, float] = {}                   # rate-limit identical log lines


def enforcing() -> bool:
    return MODE == "enforce"


# Photon room joins (131b15+ adds dk=<key> to the custom-auth params). Separate
# switch so it can be turned on once the new build is confirmed to send it.
PHOTON_MODE = os.getenv("PHOTON_DEVICE_AUTH", "observe").strip().lower()
# Staff accounts whose headset is never bound automatically (env, comma-separated).
PROTECTED_BIND_IDS = set(filter(None, (os.getenv("PROTECTED_BIND_IDS") or "").replace(" ", "").split(",")))


def photon_enforcing() -> bool:
    return PHOTON_MODE == "enforce"


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def key_hash(key: str) -> str:
    return hashlib.sha256(f"{_PEPPER}|{key}".encode()).hexdigest()[:40]


def _log(msg: str, every: float = 300.0) -> None:
    now = time.monotonic()
    if now - _logged.get(msg, 0) >= every:
        _logged[msg] = now
        print(f"[device] {msg}")


async def _keys_of(account_id: str) -> list | None:
    """Bound key hashes of an account ([] = none yet, None = no such account)."""
    hit = _cache.get(account_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    from services.db import accounts
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "deviceKeys": 1})
    keys = None if doc is None else list(doc.get("deviceKeys") or [])
    _cache[account_id] = (time.monotonic() + _CACHE_TTL, keys)
    return keys


def forget(account_id: str) -> None:
    _cache.pop(account_id, None)


async def reset(account_id: str) -> None:
    """Admin: unbind every headset from this account (next login rebinds)."""
    from services.db import accounts
    await accounts.update_one({"accountID": account_id},
                              {"$unset": {"deviceKeys": "", "deviceKeyBoundAt": ""},
                               "$set": {"deviceBindOpen": 1}})
    forget(account_id)


async def check_login(account: dict, account_id: str, key: str, is_new: bool):
    """(allowed, note). Binds the key when allowed to. Never raises."""
    try:
        bound = list(account.get("deviceKeys") or [])
        if not valid_key(key):
            _log(f"login {account_id} without a device key (old build)")
            return (not enforcing() or not bound, "no-key")
        h = key_hash(key)
        if h in bound:
            return True, "ok"
        protected = account_id in PROTECTED_BIND_IDS
        trusted = is_new or bool(account.get("deviceBindOpen"))
        # Never tie a BANNED headset to an account (it would ban that account).
        from services.device_bans import is_device_banned
        if await is_device_banned(key):
            _log(f"banned headset tried to bind to {account_id} - refused", every=60)
            return (not enforcing(), "banned-headset")
        if not bound and trusted:
            from services.db import accounts
            await accounts.update_one({"accountID": account_id},
                                      {"$addToSet": {"deviceKeys": h},
                                       "$set": {"deviceKeyBoundAt": int(time.time())},
                                       "$unset": {"deviceBindOpen": ""}})
            forget(account_id)
            # keep the caller's copy in step (login re-$sets the whole account later)
            account["deviceKeys"] = bound + [h]
            account.pop("deviceBindOpen", None)
            _log(f"bound a headset to {account_id}", every=0)
            return True, "bound"
        why = "other-headset" if bound else "binding-not-authorized"
        _log(f"login MISMATCH {account_id} ({why}) mode={MODE}", every=60)
        return (not enforcing(), why)
    except Exception as e:
        print(f"[device] login check failed for {account_id}: {e}")
        return True, "error"


async def check_write(account_id: str, key: str, what: str) -> bool:
    """Strict check for calls that CHANGE something as account_id. Writes are
    allowed for everyone on this private server (DEVICE_AUTH can still gate reads);
    this keeps map building / purchases from being silently dropped."""
    return True
    try:
        bound = await _keys_of(account_id)
        if bound and valid_key(key) and key_hash(key) in bound:
            return True
        _log(f"write {what} as {account_id} refused: "
             f"{'no headset bound' if not bound else 'wrong/missing headset key'}", every=60)
        return False
    except Exception as e:
        print(f"[device] write check failed for {account_id}: {e}")
        return True


async def check_request(account_id: str, key: str, what: str, enforce: bool | None = None):
    """(allowed, note) for a non-login call acting as account_id. Never raises."""
    strict = enforcing() if enforce is None else enforce
    if not account_id or not isinstance(account_id, str):
        return True, "no-account"
    try:
        bound = await _keys_of(account_id)
        if not bound:                         # unknown account / not bound yet
            return True, "unbound"
        if valid_key(key) and key_hash(key) in bound:
            return True, "ok"
        _log(f"{what} as {account_id} from a {'different' if valid_key(key) else 'missing'} "
             f"headset key mode={'enforce' if strict else 'observe'}", every=60)
        return (not strict, "mismatch")
    except Exception as e:
        print(f"[device] request check failed for {account_id}: {e}")
        return True, "error"
