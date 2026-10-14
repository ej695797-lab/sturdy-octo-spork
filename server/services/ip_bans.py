"""
IP ban store with persistence + auto-ban on repeated editor-login failures.

- Bans are persisted in the `globals` collection (doc key "banned_ips", an array
  of IP strings) so they survive a Render redeploy (in-memory state resets, and a
  raider could otherwise just wait for the next deploy).
- An in-memory cache is loaded once per process; membership checks are then just
  a set lookup (no DB hit per request).
- Failed-login counts are in-memory only (per IP, within a rolling window). An IP
  that gets the editor password wrong MORE THAN _FAIL_LIMIT times is auto-banned.
"""

import time

from services.db import globals_col

_GLOBALS_KEY = "banned_ips"
_FAIL_LIMIT = 3            # ban after MORE THAN this many wrong passwords (i.e. the 4th)
_FAIL_WINDOW = 3600        # seconds; failures older than this don't count toward the streak

_cache = None             # set[str] | None  -- banned IPs, lazily loaded from DB
_fails: dict = {}         # ip -> [count, first_ts]


def client_ip(request) -> str:
    """Real client IP. The server sits behind a proxy, so request.client.host is
    the proxy. Behind Cloudflare (self-hosted tunnel) CF-Connecting-IP is set by
    Cloudflare and can't be spoofed -- X-Forwarded-For can (Cloudflare appends to a
    client-sent one, so its first hop is attacker-controlled). Fall back to the
    first X-Forwarded-For hop (Render), then the socket peer."""
    try:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


async def _load() -> set:
    global _cache
    if _cache is None:
        try:
            doc = await globals_col.find_one({"key": _GLOBALS_KEY}, {"_id": 0, "ips": 1})
            _cache = set((doc or {}).get("ips") or [])
        except Exception:
            _cache = set()
    return _cache


async def is_banned(ip: str) -> bool:
    if not ip or ip == "?":
        return False
    bans = await _load()
    if ip in bans:
        return True
    # Whole-network entries ("2a00:23c4:dc82:2401::/64"): home IPv6 addresses
    # rotate inside their /64, so a ban on one address alone is easy to dodge.
    nets = [b for b in bans if "/" in b]
    if nets:
        try:
            import ipaddress
            a = ipaddress.ip_address(ip)
            for n in nets:
                try:
                    if a in ipaddress.ip_network(n, strict=False):
                        return True
                except ValueError:
                    continue
        except ValueError:
            pass
    return False


async def ban_ip(ip: str, reason: str = "") -> None:
    if not ip or ip == "?":
        return
    cache = await _load()
    cache.add(ip)
    try:
        await globals_col.update_one(
            {"key": _GLOBALS_KEY},
            {"$addToSet": {"ips": ip},
             "$set": {f"reasons.{ip.replace('.', '_')}": {"reason": reason, "at": int(time.time())}}},
            upsert=True,
        )
    except Exception as e:
        print(f"[ip_bans] persist failed for {ip}: {e}")


async def unban_ip(ip: str) -> bool:
    """Remove an IP from the ban list. Returns True if it was banned."""
    if not ip:
        return False
    cache = await _load()
    had = ip in cache
    cache.discard(ip)
    try:
        await globals_col.update_one(
            {"key": _GLOBALS_KEY},
            {"$pull": {"ips": ip},
             "$unset": {f"reasons.{ip.replace('.', '_')}": ""}})
    except Exception as e:
        print(f"[ip_bans] unban persist failed for {ip}: {e}")
    return had


async def list_bans() -> dict:
    """Current banned IPs + their reasons."""
    await _load()
    doc = await globals_col.find_one({"key": _GLOBALS_KEY}, {"_id": 0}) or {}
    return {"ips": sorted(doc.get("ips") or []), "reasons": doc.get("reasons") or {}}


async def record_failed_login(ip: str) -> bool:
    """Record one wrong-password attempt from `ip`. Returns True if this attempt
    trips the auto-ban (i.e. more than _FAIL_LIMIT failures in the window)."""
    if not ip or ip == "?":
        return False
    now = time.time()
    rec = _fails.get(ip)
    if rec is None or now - rec[1] > _FAIL_WINDOW:
        _fails[ip] = [1, now]
    else:
        rec[0] += 1
    # bound memory
    if len(_fails) > 5000:
        for k in [k for k, v in list(_fails.items()) if now - v[1] > _FAIL_WINDOW]:
            _fails.pop(k, None)
    count = _fails[ip][0]
    if count > _FAIL_LIMIT:
        await ban_ip(ip, f"auto: {count} wrong editor passwords")
        return True
    return False


def clear_failures(ip: str) -> None:
    """Forget an IP's failed-attempt streak (call on a successful login)."""
    _fails.pop(ip, None)
