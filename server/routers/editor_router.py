"""
Player data editor — runs on port 8001 (http://localhost:8001)
Provides a web UI + JSON API for browsing and editing player accounts.
"""

import json
import uvicorn
import httpx
from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from services.db import accounts, rooms, debug_logs, globals_col, db as _db
from config import YEEPS_PROXY_URL, EDITOR_PW_WEBHOOK, EDITOR_PW_ROTATE_SECONDS

import os
import sys
import re
import hmac
from datetime import datetime, timedelta, timezone
import hashlib
import time
from datetime import datetime, timezone
from fastapi import Depends
from fastapi.responses import RedirectResponse

# ─── Auth: password + Meta-name gate, with Discord audit webhook ──────────────
# Config via env (set these on Render): EDITOR_PASSWORD, EDITOR_SECRET (cookie
# signing), EDITOR_DISCORD_WEBHOOK (who's-using-it notifications).
# Password changed after a raid where the old EDITOR_PASSWORD (TSSLAND) leaked.
# New env key so the old leaked Render env var no longer applies; defaults here.
# The editor password auto-rotates every EDITOR_PW_ROTATE_SECONDS. The current
# code is derived from EDITOR_SECRET + the time window (so it needs no storage
# and survives restarts), and is posted to the password webhook on each change.
# An optional MASTER password (set EDITOR_PW on Render) is accepted as a backup;
# there is no hardcoded fallback password anymore.
_MASTER_PW = os.getenv("EDITOR_PW", "")


def _pw_window() -> int:
    return int(time.time() // EDITOR_PW_ROTATE_SECONDS)


# EDITOR_PW_SALT changes every code without touching EDITOR_SECRET (which also
# signs sessions, device cookies and the owner password hash). Set on
# 2026-10-10 after the rotating code leaked to a raider.
_PW_SALT = os.getenv("EDITOR_PW_SALT", "")
# EDITOR_PW_NOTIFY=0: don't post new codes to the password webhook.
_PW_NOTIFY = os.getenv("EDITOR_PW_NOTIFY", "1") != "0"


def _editor_password(window: int = None) -> str:
    w = _pw_window() if window is None else window
    msg = f"editorpw|{w}" + (f"|{_PW_SALT}" if _PW_SALT else "")
    mac = hmac.new(EDITOR_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{int(mac[:8], 16) % 1000000:06d}"

# Admin allowlist: ONLY these accountIDs may open the editor, even with the right
# password. Empty set = no restriction. Override via env EDITOR_ADMINS (comma-sep).
EDITOR_ADMINS = set(filter(None, (os.getenv("EDITOR_ADMINS") or "").replace(" ", "").split(",")))
# Only these editor usernames may SEE player IPs (lastIP/recentIPs and the IP-ban
# list). Player IPs are sensitive, so for everyone else they're stripped/masked.
# Override/extend via env IP_VIEW_NAMES (comma-separated display names).
IP_VIEW_NAMES = set(filter(None, (n.strip() for n in
    (os.getenv("IP_VIEW_NAMES") or "").split(","))))
# Signs editor sessions. start.bat/start.sh put a random one in .env; without it a
# fresh random secret is used (signed-in editors are signed out on restart).
EDITOR_SECRET          = os.getenv("EDITOR_SECRET") or __import__("secrets").token_hex(32)
from config import EDITOR_PASSWORD as KIT_EDITOR_PASSWORD
# Discord audit webhooks. Editor login + player/data edits go to the ADMIN
# channel; in-game activity goes to the INGAME channel. Both live in config so
# they can be overridden by env and shared with the other routers.
from config import ADMIN_DISCORD_WEBHOOK, INGAME_DISCORD_WEBHOOK
# Back-compat: an explicit EDITOR_DISCORD_WEBHOOK env still wins if set.
EDITOR_DISCORD_WEBHOOK = os.getenv("EDITOR_DISCORD_WEBHOOK", ADMIN_DISCORD_WEBHOOK)
_COOKIE = "yeeps_editor"
_TTL = 12 * 3600  # 12-hour sessions


def _ip_net(ip: str) -> str:
    from services.device_auth import _net
    return _net(ip or "")


def _sign(name: str, ip: str = "") -> str:
    # Bind the session to the current password window (rotation boots everyone).
    # Not locked to a network (removed 2026-10-09 at the owner's request, so the
    # editor works on mobile data / other Wi-Fi). The login network is signed
    # into the cookie only so a session used from somewhere else gets reported.
    w = _pw_window()
    exp = str(int(time.time()) + _TTL)
    net = _ip_net(ip)
    mac = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}|{net}".encode(), hashlib.sha256).hexdigest()
    return f"{name}|{w}|{exp}|{net}|{mac}"


# ---- editor IP log + "different network" alerts --------------------------
# Every sign-in attempt is stored in "editor_logins" with its IP and rough
# location. The admin webhook is told when someone signs in from a network the
# account doesn't play Yeeps from, or when a session is used from a different
# network than it signed in on.
editor_logins = _db["editor_logins"]
_session_nets: dict = {}      # cookie mac -> networks already seen/reported
_bg_tasks: set = set()


def _ip_info(ip: str) -> dict:
    from services import geo
    return {"ip": ip, "net": _ip_net(ip), "place": geo.describe(ip), "vpn": geo.vpn_hint(ip)}


def _where(info: dict) -> str:
    return info["place"] + (f", {info['vpn']} (VPN)" if info.get("vpn") else "")


async def _play_ips(acct_id) -> set:
    """IPs this account has played Yeeps from (EDITOR_ADMINS pool theirs: they're
    the same person's accounts)."""
    ids = list(EDITOR_ADMINS) if acct_id in EDITOR_ADMINS else [acct_id]
    known = set()
    async for a in accounts.find({"accountID": {"$in": [x for x in ids if x]}},
                                 {"_id": 0, "lastIP": 1, "recentIPs": 1}):
        known.update(filter(None, [a.get("lastIP")] + list(a.get("recentIPs") or [])))
    return known


def _km_from_play(ip: str, known: set):
    try:
        from services import geo
        km = geo.nearest_km(ip, known)
        return None if km is None else int(round(km))
    except Exception:
        return None


async def _log_editor_ip(kind: str, name: str, acct_id, info: dict, **extra) -> None:
    try:
        await editor_logins.insert_one({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind, "name": name, "accountID": acct_id, **info, **extra})
    except Exception as e:
        print(f"[editor] ip log failed: {e}")


def _session_hash(token: str) -> str:
    """Stable id of a session (its cookie MAC, hashed) for editor_logins lookups."""
    return hashlib.sha256((token or "").rsplit("|", 1)[-1].encode()).hexdigest()[:32]


async def _session_account(request: Request):
    """accountID this editor session signed in as (from its login record), or None.
    Display names aren't unique, so owner-only features check this, not the name."""
    if not _authed_name(request):
        return None
    tok = request.cookies.get(_COOKIE, "")
    doc = await editor_logins.find_one({"kind": "login", "sessionHash": _session_hash(tok)},
                                       {"_id": 0, "accountID": 1})
    if not doc:
        # Sessions signed in before login records kept a sessionHash: match the
        # cookie's own signed fields (name, login network, login time = exp - TTL)
        # to that sign-in's record instead.
        parts = tok.rsplit("|", 4)
        if len(parts) == 5:
            name, _w, exp, net, _mac = parts
            t0 = int(exp) - _TTL
            fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            async for d in editor_logins.find(
                    {"kind": "login", "net": net, "sessionHash": {"$exists": False},
                     "at": {"$gte": fmt(t0 - 5), "$lte": fmt(t0 + 5)}},
                    {"_id": 0, "accountID": 1, "name": 1}):
                if str(d.get("name", "")).lower() == name.lower():
                    doc = d
                    break
    return (doc or {}).get("accountID")


async def _report_session_move(name: str, login_net: str, ip: str) -> None:
    info = _ip_info(ip)
    doc = await _name_exists(name)
    acct_id = doc.get("accountID") if doc else None
    known = await _play_ips(acct_id)
    plays_here = ip in known or info["net"] in {_ip_net(x) for x in known}
    await _log_editor_ip("session-moved", name, acct_id, info, loginNet=login_net, playsHere=plays_here)
    km = _km_from_play(ip, known)
    far = f", ~{km} km from where they play" if km is not None and not plays_here else ""
    await _discord_notify(
        f"EDITOR ALERT: **{name}**'s editor session is now being used from a different network "
        f"[{ip}] ({_where(info)}{far}). It signed in from {login_net}."
        + ("" if plays_here else " That account doesn't play Yeeps from this network."))


def _check_session_net(request: Request, name: str, mac: str, login_net: str) -> None:
    """Report (once per session per network) a session used away from its login network."""
    try:
        from services.ip_bans import client_ip
        ip = client_ip(request) or ""
        net = _ip_net(ip)
        if not net or net == login_net:
            return
        seen = _session_nets.setdefault(mac, set())
        if net in seen:
            return
        seen.add(net)
        if len(_session_nets) > 500:
            _session_nets.pop(next(iter(_session_nets)))
        import asyncio
        t = asyncio.get_running_loop().create_task(_report_session_move(name, login_net, ip))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    except Exception:
        pass


# KILL SWITCH: when True the whole editor is locked -- every session is treated
# as logged out, every API call is unauthorized, and login is refused. Flip to
# False (and redeploy) to bring the editor back. Now protected by EDITOR_ADMINS
# allowlist + rotated password, so it's safe to leave unlocked.
EDITOR_LOCKED = False


def _authed_name(request: Request):
    """Return the Meta name from a valid signed cookie, else None."""
    if EDITOR_LOCKED:
        return None
    tok = request.cookies.get(_COOKIE, "")
    try:
        # name|window|exp|loginNet|mac (sessions signed before the IP log
        # existed have no loginNet and are accepted without move reports).
        parts = tok.rsplit("|", 4)
        if len(parts) == 5:
            name, w, exp, net, mac = parts
            good = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}|{net}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, mac):
                name, w, exp, mac = tok.rsplit("|", 3)
                net = None
        else:
            name, w, exp, mac = tok.rsplit("|", 3)
            net = None
        if net is None:
            good = hmac.new(EDITOR_SECRET.encode(), f"{name}|{w}|{exp}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, mac):
                return None
        if int(exp) < time.time():
            return None
        # Kit: the password doesn't rotate, so sessions just last _TTL.
        if net is not None:
            _check_session_net(request, name, mac, net)
        return name
    except Exception:
        return None


def _can_see_ips(request: Request) -> bool:
    """Kit: the host (anyone signed in with the editor password) sees player IPs."""
    return bool(_authed_name(request))

async def _name_exists(name: str):
    """Look the Meta name up in the accounts collection (displayName / oculusID /
    accountID, case-insensitive). Returns the matched doc or None."""
    n = (name or "").strip()
    if not n:
        return None
    esc = re.escape(n)
    return await accounts.find_one(
        {"$or": [
            {"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
            {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
            {"accountID": n},
        ]},
        {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1},
    )


async def _name_matches(name: str) -> list:
    """Accounts a typed editor name can mean: in-game name, Meta name, Discord
    username (case-insensitive) or account id. Up to 10."""
    n = (name or "").strip()
    if not n:
        return []
    esc = re.escape(n)
    rx = {"$regex": f"^{esc}$", "$options": "i"}
    cur = accounts.find({"$or": [{"displayName": rx}, {"oculusID": rx}, {"discordUsername": rx}, {"accountID": n}]},
                        {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1, "discordID": 1, "deviceKeys": 1})
    return [d async for d in cur.limit(10)]


async def _post_webhook(url: str, text: str) -> None:
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            from config import labeled
            await c.post(url, json={"content": labeled(text)[:1900]})
    except Exception as e:
        print(f"[editor] discord webhook failed: {e}")


async def _discord_notify(text: str) -> None:
    """ADMIN channel: editor login + player/data edits (+ /link via discord_router)."""
    await _post_webhook(EDITOR_DISCORD_WEBHOOK, text)


async def _ingame_notify(text: str) -> None:
    """INGAME channel: things players do in-game (e.g. in-game map saves)."""
    await _post_webhook(INGAME_DISCORD_WEBHOOK, text)


async def _maybe_notify_new_password() -> None:
    """When the password window rolls over, post the new code to the password
    webhook exactly once. Called from a background loop and lazily on page load,
    so it fires even on Render's free tier (which sleeps when idle)."""
    if not EDITOR_PW_WEBHOOK or not _PW_NOTIFY:
        return
    w = _pw_window()
    try:
        from pymongo import ReturnDocument
        prev = await globals_col.find_one_and_update(
            {"key": "editorPwWindow"}, {"$set": {"value": w}},
            upsert=True, return_document=ReturnDocument.BEFORE)
        if (prev or {}).get("value") == w:
            return  # already announced this window
        await _post_webhook(EDITOR_PW_WEBHOOK, f"New editor password: `{_editor_password(w)}`")
        print(f"[editor] rotated password, window={w}")
    except Exception as e:
        print(f"[editor] password rotate-notify failed: {e}")


def _short_val(v) -> str:
    """Compact one-line string for a field value in an audit log."""
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    s = str(s).replace("\n", " ")
    return s if len(s) <= 60 else s[:57] + "…"


def _summarize_changes(before: dict, patch: dict) -> str:
    """Readable 'field: old → new' lines for exactly what an edit changed."""
    lines = []
    for k, new in patch.items():
        old = before.get(k, "∅")
        o, n = _short_val(old), _short_val(new)
        if o == n:
            continue
        lines.append(f"• `{k}`: {o} → {n}")
    return "\n".join(lines) if lines else "• (no value changes)"


# ---- owner lock (2026-10-10, owner: "make sure it's me only") --------------
# A normal sign-in is a name + the shared rotating code, so anyone who sees the
# code could sign in AS the owner (it happened: VPN/Oslo sessions under
# the owner). The owner's accounts (EDITOR_ADMINS) instead need the owner's
# own password (OWNER_EDITOR_PW_HASH = sha256(EDITOR_SECRET|owner-pw|password))
# AND a trusted place: a network the owner's headset plays from, or a browser
# that has signed in from there before (long-lived signed device cookie).
OWNER_EDITOR_PW_HASH = os.getenv("OWNER_EDITOR_PW_HASH", "")
_OWNER_DEVICE_COOKIE = "yeeps_owner_device"
_OWNER_DEVICE_TTL = 365 * 24 * 3600


def _owner_pw_ok(pw: str) -> bool:
    if not OWNER_EDITOR_PW_HASH:
        return False
    h = hashlib.sha256(f"{EDITOR_SECRET}|owner-pw|{pw}".encode()).hexdigest()
    return hmac.compare_digest(h, OWNER_EDITOR_PW_HASH)


def _owner_device_token(acct: str) -> str:
    t = str(int(time.time()))
    mac = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{acct}|{t}".encode(), hashlib.sha256).hexdigest()
    return f"{acct}|{t}|{mac}"


def _owner_device_ok(request: Request) -> bool:
    try:
        acct, t, mac = request.cookies.get(_OWNER_DEVICE_COOKIE, "").split("|")
        good = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{acct}|{t}".encode(), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(good, mac) and acct in EDITOR_ADMINS
                and time.time() - int(t) < _OWNER_DEVICE_TTL)
    except Exception:
        return False


async def _on_owner_network(request: Request, acct) -> bool:
    from services.ip_bans import client_ip
    ip = client_ip(request)
    known = await _play_ips(acct)
    return bool(ip) and (ip in known or _ip_net(ip) in {_ip_net(x) for x in known})


# Owner names: "must sign in from home or a trusted device" rule. Off by default
# (owner, 2026-10-11) - the owner password is still required. EDITOR_OWNER_DEVICE_RULE=1 turns it on.
EDITOR_OWNER_DEVICE_RULE = os.getenv("EDITOR_OWNER_DEVICE_RULE", "0") == "1"


async def _owner_trusted(request: Request, acct) -> bool:
    if not EDITOR_OWNER_DEVICE_RULE:
        return True
    return _owner_device_ok(request) or await _on_owner_network(request, acct)


def _device_ok_for(request: Request, acct) -> bool:
    """This browser holds a trusted-device cookie for acct (the owner's accounts
    share one)."""
    try:
        cacct, t, mac = request.cookies.get(_OWNER_DEVICE_COOKIE, "").split("|")
        good = hmac.new(EDITOR_SECRET.encode(), f"owner-device|{cacct}|{t}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, mac) or time.time() - int(t) >= _OWNER_DEVICE_TTL:
            return False
        return cacct == acct or (cacct in EDITOR_ADMINS and acct in EDITOR_ADMINS)
    except Exception:
        return False


async def _trusted_for(request: Request, acct) -> bool:
    """Every editor account (2026-10-11): used from the network its headset plays
    from, or from a device that signed in there before."""
    if not acct:
        return False
    if acct in EDITOR_ADMINS:
        return await _owner_trusted(request, acct)
    return _device_ok_for(request, acct) or await _on_owner_network(request, acct)


async def _owner_account_for(name: str):
    """The owner (EDITOR_ADMINS) account a typed editor name refers to, or None."""
    n = (name or "").strip()
    if not n or not EDITOR_ADMINS:
        return None
    if n in EDITOR_ADMINS:
        return n
    esc = re.escape(n)
    doc = await accounts.find_one(
        {"accountID": {"$in": list(EDITOR_ADMINS)},
         "$or": [{"displayName": {"$regex": f"^{esc}$", "$options": "i"}},
                 {"oculusID": {"$regex": f"^{esc}$", "$options": "i"}},
                 {"discordUsername": {"$regex": f"^{esc}$", "$options": "i"}}]},
        {"_id": 0, "accountID": 1})
    return (doc or {}).get("accountID")


# Emergency switch (2026-10-10): a raider had the rotating code and signed in
# as Data Managers (NatorGator, JohnKTuff) to post an announcement and play an
# inappropriate video. While True, only the owner's accounts can use the editor.
EDITOR_OWNER_ONLY = os.getenv("EDITOR_OWNER_ONLY", "1") == "1"


# VPN/relay block for the editor. Off by default (owner, 2026-10-11); set
# EDITOR_BLOCK_VPN=1 to turn it back on. The trusted-network rule still applies.
EDITOR_BLOCK_VPN = os.getenv("EDITOR_BLOCK_VPN", "0") == "1"


def _from_vpn(request: Request) -> bool:
    if not EDITOR_BLOCK_VPN:
        return False
    from services.ip_bans import client_ip
    from services import geo
    return bool(geo.vpn_hint(client_ip(request) or ""))


# ---- editor tiers (owner, 2026-10-11) ---------------------------------------
# owner  = EDITOR_ADMINS accounts: everything.
# admin  = Discord role "hi lol": every tab (not the owner-only bits).
# editor = Data Manager role only: Players (view + edit everyday fields),
#          Online (view + kick one player) and Announce.
ADMIN_ROLE_NAME = os.getenv("EDITOR_ADMIN_ROLE_NAME", "hi lol")
# Discord role named "." (owner, 2026-10-11): counts as admin in the editor AND as
# sign-in access, so people with it can sign in without the Data Manager role.
EDITOR_EXTRA_ROLE = os.getenv("EDITOR_EXTRA_ROLE", ".")
# Discord IDs that get the admin tier in the editor as if they had the "hi lol" role
# (owner, 2026-10-11: yeeperyap). They still need the Data Manager role to sign in.
EDITOR_ADMIN_DISCORD_IDS = set(filter(None, (os.getenv("EDITOR_ADMIN_DISCORD_IDS") or
    "1044067691457167360").replace(" ", "").split(",")))
_EDITOR_TIER_ALLOWED = [
    ("GET", r"/api/me"), ("GET", r"/api/players"), ("GET", r"/api/player/[^/]+"),
    ("POST", r"/api/player/[^/]+"), ("POST", r"/api/player/[^/]+/kick"),
    ("GET", r"/api/online"), ("GET", r"/api/announce"), ("POST", r"/api/announce"),
    ("DELETE", r"/api/announce"),
]
# fields a data editor may not change on a player
_EDITOR_TIER_LOCKED_FIELDS = {"discordUsername", "lastIP", "recentIPs"}   # owner 2026-10-11: data editors edit
# every other player field; the Discord username and IP fields stay owner-only


# Owner 2026-10-12: data editors use the full site again, like admins. The owner-only
# parts stay owner-only (Photon bot tab, giving/taking the blaze role, seeing player
# IPs). DATA_EDITOR_FULL=0 brings back the limited data-editor tier.
DATA_EDITOR_FULL = os.getenv("DATA_EDITOR_FULL", "1") == "1"


async def _session_tier(request: Request) -> str:
    """Kit: one tier, the host."""
    return "owner"

def _editor_tier_allows(method: str, path: str) -> bool:
    i = path.find("/api/")
    sub = path[i:] if i >= 0 else path
    return any(method == m and re.fullmatch(rx, sub) for m, rx in _EDITOR_TIER_ALLOWED)


async def _usable_session(request: Request) -> bool:
    """Kit: signed in with the host password."""
    return bool(_authed_name(request))



async def _guard(request: Request):
    """Protect the JSON API; the UI page + /login + /logout stay open (the UI
    page itself shows the login form when unauthenticated)."""
    if "/api/" not in request.url.path:
        return
    # Refuse changes sent from another website (the editor cookie would ride along).
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = (request.headers.get("origin") or "").rstrip("/")
        host = (request.headers.get("host") or "").lower()
        if origin and origin.split("://", 1)[-1].lower() != host:
            print(f"[editor] refused cross-site {request.method} {request.url.path} from origin {origin}")
            raise HTTPException(status_code=403, detail="Cross-site request refused")
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")

router = APIRouter(dependencies=[Depends(_guard)])
editor_app = FastAPI(title="Yeeps Player Editor")

# ─── JSON API ────────────────────────────────────────────────────────────────

@router.get("/api/players")
async def list_players(q: str = "", limit: int = 50):
    """List/search player accounts."""
    filt = {}
    if q:
        rx = {"$regex": re.escape(q.strip().lstrip("@")), "$options": "i"}
        filt = {"$or": [
            {"accountID": rx},
            {"displayName": rx},
            {"discordUsername": rx},     # set by /link and /data
            {"discordID": rx},
            {"oculusID": rx},
        ]}
    cursor = accounts.find(filt, {"_id": 0, "accountID": 1, "displayName": 1, "currency": 1, "beans": 1,
                                  "discordUsername": 1}).limit(limit)
    docs = await cursor.to_list(length=limit)
    return JSONResponse(content=docs)


@router.get("/api/player/{account_id}")
async def get_player(account_id: str, request: Request):
    """Return full account document (player IPs hidden unless IP_VIEW_NAMES)."""
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    if doc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not _can_see_ips(request):
        doc.pop("lastIP", None)
        doc.pop("recentIPs", None)
    return JSONResponse(content=doc)


@router.post("/api/player/{account_id}")
async def update_player(account_id: str, request: Request):
    """Patch specific fields on an account."""
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Never allow overwriting the primary key
    patch.pop("accountID", None)
    patch.pop("_id", None)

    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Require a non-empty display name: don't let an edit clear it (keeps every
    # player identifiable in the logs and in-game instead of a bare account id).
    if "displayName" in patch and not str(patch.get("displayName") or "").strip():
        raise HTTPException(status_code=400, detail="Display name can't be empty")
    if "displayName" in patch:
        from services.names import is_visible_name
        if not is_visible_name(patch.get("displayName")):
            raise HTTPException(status_code=400, detail="Display name must have at least one visible character")

    # Read the current values of exactly the fields being changed, so the audit
    # log can show the player's name and what actually changed (old → new).
    proj = {"_id": 0, "displayName": 1}
    for k in patch:
        proj[k] = 1
    before = await accounts.find_one({"accountID": account_id}, proj)
    if before is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if (await _session_tier(request)) == "editor":
        # The players tab saves the WHOLE document, so a locked field only counts
        # when its value really changes; unchanged locked fields are dropped (2026-10-11:
        # every save of a linked or device-bound player was refused before this).
        _changed = []
        for _k in sorted(set(patch) & _EDITOR_TIER_LOCKED_FIELDS):
            if before.get(_k) != patch[_k]:
                _changed.append(_k)
            else:
                del patch[_k]
        if _changed:
            raise HTTPException(status_code=403, detail="Admins only: " + ", ".join(_changed))
        if not patch:
            return JSONResponse(content={"ok": True, "modified": 0})

    # Only the owner's account can give or take the blaze role (owner's rule,
    # 2026-10-11); the server's blaze list follows the owner's edits.
    _blaze_change = None
    if "roleKeys" in patch:
        from services import role_locks as _rl
        _had = _rl.BLAZE_ROLE in (before.get("roleKeys") or [])
        _has = _rl.BLAZE_ROLE in (patch.get("roleKeys") or [])
        if _had != _has:
            _blaze_change = _has

    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if _blaze_change is not None:
        from services import role_locks as _rl
        await _rl.set_blaze_holder(account_id, _blaze_change)
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")

    name = _authed_name(request) or "?"
    disp = before.get("displayName") or "(no name)"
    changes = _summarize_changes(before, patch)
    await _discord_notify(f"**{name}** edited **{disp}** (`{account_id}`)\n{changes}")
    return JSONResponse(content={"ok": True, "modified": result.modified_count})


@router.delete("/api/player/{account_id}")
async def delete_player(account_id: str, request: Request):
    """Permanently delete a player account. (The client re-creates a fresh one
    from the default template if that player logs in again.)"""
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    if doc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    # Safety: keep a full copy so an accidental/malicious delete is recoverable.
    try:
        from services.db import db as _db
        await _db["deleted_backups"].insert_one({
            "kind": "account", "accountID": account_id,
            "deletedBy": _authed_name(request) or "?",
            "deletedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "doc": doc})
    except Exception as e:
        print(f"[editor] delete backup failed for {account_id}: {e}")
    result = await accounts.delete_one({"accountID": account_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    # Also clear any player status row for them.
    try:
        from services.db import player_statuses
        await player_statuses.delete_one({"accountID": account_id})
    except Exception:
        pass
    name = _authed_name(request) or "?"
    dn = (doc or {}).get("displayName") or account_id
    await _discord_notify(f"{name} DELETED player {dn} ({account_id})")
    return JSONResponse(content={"ok": True, "deleted": result.deleted_count})


@router.post("/api/player/{account_id}/block")
async def block_player(account_id: str, request: Request):
    """Block / unblock a player from connecting. Blocked players are served as
    permabanned+isolated at login, so the client refuses to join any room."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    blocked = 1 if body.get("blocked", True) else 0
    reason = str(body.get("reason") or "")
    patch = {"adminBlocked": blocked}
    if reason:
        patch["blockReason"] = reason
    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    name = _authed_name(request) or "?"
    await _discord_notify(
        f"{name} {'BLOCKED' if blocked else 'unblocked'} player {account_id}"
        + (f" (reason: {reason})" if reason and blocked else ""))
    return JSONResponse(content={"ok": True, "adminBlocked": blocked})


# Which game version this deployment serves + edits. Each version runs as its
# own deployment with its own DB_NAME, so 1.19.0 and 2.4.0 data (and editors)
# stay fully separate. Set GAME_VERSION on the 2.4.0 deployment to "2.4.0".
from config import REQUIRED_CLIENT_VERSION as _REQ_VER
GAME_VERSION = os.getenv("GAME_VERSION", _REQ_VER)


@router.get("/api/me")
async def whoami(request: Request):
    """Return the logged-in Meta name + which game version/DB this editor edits."""
    name = _authed_name(request)
    if not name:
        raise HTTPException(status_code=401, detail="unauthorized")
    from config import BOT_KEY, DB_NAME
    return JSONResponse(content={"name": name, "version": GAME_VERSION, "db": DB_NAME,
                                 "photon": bool(BOT_KEY), "tier": await _session_tier(request)})


# ─── Default account editor ───────────────────────────────────────────────────
# The server's baseline for every NEW player is handlers.login.DEFAULT_ACCOUNT.
# To let it be edited from the web without a redeploy, we store an OVERRIDE in
# the globals collection (key "default_account_override") and login.py merges it
# over DEFAULT_ACCOUNT at account-creation / backfill time.
_DEFAULT_OVERRIDE_KEY = "default_account_override"


@router.get("/api/default")
async def get_default(request: Request):
    """Return the effective default account: DEFAULT_ACCOUNT merged with the
    stored override, so the editor shows the complete picture."""
    from handlers.login import DEFAULT_ACCOUNT
    from services.db import globals_col
    merged = dict(DEFAULT_ACCOUNT)
    ov = await globals_col.find_one({"key": _DEFAULT_OVERRIDE_KEY}, {"_id": 0, "value": 1})
    if ov and isinstance(ov.get("value"), dict):
        merged.update(ov["value"])
    return JSONResponse(content=merged)


@router.post("/api/default")
async def set_default(request: Request):
    """Patch the default-account override. Only the fields sent are overridden;
    everything else keeps falling back to DEFAULT_ACCOUNT."""
    from services.db import globals_col
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch.pop("_id", None)
    # blaze is never part of the default loadout (it's given one account at a time).
    if isinstance(patch.get("roleKeys"), list):
        patch["roleKeys"] = [r for r in patch["roleKeys"] if r != "blaze"]
    existing = await globals_col.find_one({"key": _DEFAULT_OVERRIDE_KEY}, {"_id": 0, "value": 1})
    value = dict((existing or {}).get("value") or {})
    value.update(patch)
    await globals_col.update_one({"key": _DEFAULT_OVERRIDE_KEY},
                                 {"$set": {"value": value}}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"{name} edited default player data")
    return JSONResponse(content={"ok": True, "fields": list(patch.keys())})


# ─── News / in-game announcement (globals key "announcementData") ─────────────
@router.get("/api/news")
async def get_news():
    """Current in-game news text (the message the game shows from G2_Globals
    announcementData)."""
    from services.db import globals_col
    d = await globals_col.find_one({"key": "announcementData"}, {"_id": 0, "data": 1})
    msg = ""
    data = (d or {}).get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        msg = data[0].get("message", "") or ""
    return JSONResponse(content={"message": msg})


@router.post("/api/news")
async def set_news(request: Request):
    """Update the in-game news. Stored as announcementData.data = [{message,
    activeTime}] — the exact shape the client reads via G2_Globals GetItem."""
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    message = str(body.get("message", ""))
    data = [{"message": message, "activeTime": "2026 01/01 00:00:00"}]
    await globals_col.update_one({"key": "announcementData"},
                                 {"$set": {"key": "announcementData", "data": data}}, upsert=True)
    name = _authed_name(request) or "?"
    await _discord_notify(f"**{name} updated the in-game news:**\n>>> {message or '(cleared)'}")
    return JSONResponse(content={"ok": True})


# ─── Map editor (import real maps in the /map API format) ─────────────────────
@router.get("/api/maps")
async def list_maps():
    """List every room that has map data — both editor-imported maps
    (imported_maps) and real in-game saves (maps collection, roomKey$current)."""
    from services.db import imported_maps, maps as maps_col
    out = {}
    async for m in imported_maps.find({}, {"_id": 0, "roomKey": 1, "blockCount": 1,
                                           "dimensions": 1, "importedAt": 1}):
        out[m["roomKey"]] = {**m, "source": "imported"}
    # The size is computed inside Mongo: pulling every saveData (~25 MB for 250
    # maps) to the self-hosted PC just for len() made this take 11 s.
    pipeline = [
        {"$match": {"mapKey": {"$regex": r"\$current$"}}},
        {"$project": {"_id": 0, "mapKey": 1, "savedBy": 1, "savedAt": 1,
                      "bytes": {"$cond": [{"$eq": [{"$type": "$saveData"}, "string"]},
                                          {"$strLenBytes": "$saveData"}, 0]}}},
    ]
    async for m in maps_col.aggregate(pipeline):
        rk = str(m.get("mapKey", "")).rsplit("$", 1)[0]
        if not rk or rk in out:
            continue
        out[rk] = {"roomKey": rk, "blockCount": None, "source": "saved",
                   "savedBy": m.get("savedBy"), "savedAt": m.get("savedAt"),
                   "bytes": m.get("bytes") or 0}
    return JSONResponse(content=sorted(out.values(), key=lambda r: r["roomKey"]))


def _fmt_of(sd: str) -> str:
    if sd.startswith("AAEAAAD"):
        return "1.19 BinaryFormatter"
    if sd.startswith("U1BBTQ"):
        return "2.x SPAM"
    return "empty" if not sd else "other"


@router.get("/api/map/{room_key}")
async def get_map(room_key: str):
    """Show a room's stored map. Prefers an editor-imported map (has parsed
    blocks); otherwise returns the real in-game save (roomKey$current) with the
    raw saveData blob summarized (it's base64 BinaryFormatter, not block JSON)."""
    from services.db import imported_maps, maps as maps_col
    doc = await imported_maps.find_one({"roomKey": room_key}, {"_id": 0, "saveData": 0})
    if doc:
        blocks = doc.get("blocks") or []
        # The full list: the page saves back what it was given, so a cut list
        # deleted every block past the cut (2026-10-12: BAM 5 Winner 5342 -> 2000).
        doc["blocks"] = blocks
        doc["blocksTruncated"] = False
        doc["source"] = "imported"
        return JSONResponse(content=doc)
    m = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0})
    if not m:
        raise HTTPException(status_code=404, detail="No map stored for that room")
    sd = m.get("saveData") or ""
    isd = m.get("saveInternalStates") or ""
    # Prefer an exact editor-saved block list; otherwise decode the saved blob.
    if isinstance(m.get("editorBlocks"), list):
        blocks = m["editorBlocks"]
    else:
        from services.mapsync_decoder import decode_map
        try:
            blocks = decode_map(sd)
        except Exception:
            blocks = []
    m.pop("saveData", None)
    m.pop("saveInternalStates", None)
    m.pop("editorBlocks", None)
    m["source"] = "saved"
    m["saveDataFormat"] = _fmt_of(sd)
    m["saveDataBytes"] = len(sd)
    m["internalStatesBytes"] = len(isd)
    m["blockCount"] = len(blocks)
    # The full list: the page saves back what it was given, so a cut list
    # deleted every block past the cut (2026-10-12: BAM 5 Winner 5342 -> 2000).
    m["blocks"] = blocks
    m["blocksTruncated"] = False
    return JSONResponse(content=m)


@router.post("/api/map/{room_key}/blocks")
async def save_map_blocks(room_key: str, request: Request):
    """Save an edited block list back to a room: re-encode to the 1.19 map
    format, write it to the room's $current save, keep the exact blocks for
    re-editing, and bump the room's edit session so clients reload."""
    from services.db import maps as maps_col, rooms as rooms_col
    from services.mapsync_serializer import serialize_blocks_spam
    from services.role_locks import DISABLED_PATTERNS
    import time as _time
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        raise HTTPException(status_code=400, detail="'blocks' must be a list")
    # normalize each block; drop DISABLED_PATTERNS (3x3x1_blazeScreen crashes
    # clients in big rooms; the desert decorations have no mesh in 1.31).
    norm = []
    skipped = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        nm = str(b.get("name", "") or "")
        if nm in DISABLED_PATTERNS:
            skipped += 1
            continue
        norm.append({
            "name": nm,
            "x": int(b.get("x", 0) or 0), "y": int(b.get("y", 0) or 0), "z": int(b.get("z", 0) or 0),
            "fwd": int(b.get("fwd", 0) or 0), "up": int(b.get("up", 0) or 0),
            "color": str(b.get("color", "") or ""), "owner": str(b.get("owner", "") or ""),
        })
    # Blocks whose owner account doesn't exist go to the server owner.
    try:
        from services.map_owners import default_block_owners
        await default_block_owners(norm)
    except Exception as e:
        print(f"[editor] owner defaulting failed for {room_key}: {e}")
    try:
        # 1.31 reads/saves the flat SPAM format. We keep the existing
        # saveInternalStates (signs/dispensers/teleporters) byte-for-byte; those
        # index into object order, so in-place edits (move/recolor) stay valid —
        # adding/removing/reordering blocks can still misalign them.
        save_data = serialize_blocks_spam(norm)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not encode map: {e}")
    # Must be an int: lastEditedSessionID is a NUMBER field on G2_Rooms; storing
    # it as a string serializes as DynamoDB type S and the client reads null for
    # it -> Int32.Parse(null) -> fatal "Failed to load game".
    session_id = int(_time.time())
    await maps_col.update_one(
        {"mapKey": f"{room_key}$current"},
        {"$set": {"mapKey": f"{room_key}$current", "roomKey": room_key,
                  "saveData": save_data, "editorBlocks": norm,
                  "mapDataVersion": 3, "savedBy": "editor",
                  "applicationVersion": "1.31.0",
                  "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")}},
        upsert=True,
    )
    await rooms_col.update_one({"roomKey": room_key},
                              {"$set": {"lastEditedSessionID": session_id, "roomDataVersion": 3}})
    name = _authed_name(request) or "?"
    _extra = f", skipped {skipped} disabled" if skipped else ""
    await _discord_notify(f"{name} edited the map for {room_key} ({len(norm)} blocks{_extra}) in the editor")
    return JSONResponse(content={"ok": True, "roomKey": room_key, "blockCount": len(norm), "skipped": skipped})


async def _ensure_world_map_entry(room_key: str, dims=None) -> dict:
    """Make an imported map reachable: the in-game world map is built from the
    room_maps "beta4" nodes, so a room with a map but no room doc / no node
    simply cannot be found in game. Create the room doc and a node next to the
    hub, and bump the map version so clients refetch it."""
    from services.db import room_maps
    if not room_key or room_key.startswith(("c_", "p_o_", "o_")):
        return {}
    made = {}
    if await rooms.find_one({"roomKey": room_key}, {"roomKey": 1}) is None:
        try:
            from services.dynamo_ops import _full_room
            doc = _full_room(room_key, room_key, dims or [80, 80, 80])
        except Exception as e:
            print(f"[editor] room doc fallback for {room_key}: {e}")
            doc = {"roomKey": room_key, "roomName": room_key, "isOfficial": 0,
                   "themeKey": "suburb", "dimensions": dims or [80, 80, 80]}
        doc.pop("_id", None)
        doc["roomKey"] = room_key
        await rooms.update_one({"roomKey": room_key}, {"$set": doc}, upsert=True)
        made["room"] = True
    rm = await room_maps.find_one({"versionKey": "beta4"})
    data = list((rm or {}).get("data") or [])
    if rm and not any(isinstance(n, dict) and n.get("roomKey") == room_key for n in data):
        occupied = {tuple(n.get("gridPosition")) for n in data
                    if isinstance(n, dict) and n.get("gridPosition")}
        spot = None
        for cx, cy in sorted(occupied, key=lambda p: abs(p[0]) + abs(p[1])):
            for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
                if (cx + dx, cy + dy) not in occupied:
                    spot = (cx + dx, cy + dy)
                    break
            if spot:
                break
        if spot:
            rd = await rooms.find_one({"roomKey": room_key}, {"themeKey": 1})
            theme = (rd or {}).get("themeKey") or "suburb"
            data.append({"roomKey": room_key, "themeKey": theme,
                         "gridPosition": list(spot)})
            await room_maps.update_one({"_id": rm["_id"]},
                                       {"$set": {"data": data,
                                                 "version": int(rm.get("version") or 1) + 1}})
            made["node"] = list(spot)
    return made


@router.post("/api/map/import")
async def import_map(request: Request):
    """Accept the output of the /map API (the full {ok,roomKey,blocks:[...]}
    envelope, a bare blocks array, or a JSON string of either), convert it to
    the MapSyncData format the game loads, and store it so that room serves the
    real blocks instead of an empty map."""
    from services.db import imported_maps
    from services.mapsync_serializer import serialize_blocks_spam, extract_blocks
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    room_key = (body.get("roomKey") or "").strip()
    raw = body.get("map")
    if not room_key:
        # allow the pasted envelope itself to carry the roomKey
        if isinstance(raw, dict) and raw.get("roomKey"):
            room_key = str(raw["roomKey"]).strip()
    if not room_key:
        raise HTTPException(status_code=400, detail="roomKey is required")
    if raw is None:
        raise HTTPException(status_code=400, detail="map data is required")
    try:
        blocks = extract_blocks(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse map: {e}")
    dims = raw.get("dimensions") if isinstance(raw, dict) else None

    # Drop blocks whose item key the 1.19 client can't render. Real Yeeps /map
    # exports are 2.x and carry item keys that don't exist in 1.19; loading them
    # makes the client spawn broken objects that NRE every frame and crash the
    # room ("bricks" the pw). Keep only known-good 1.19 map-object keys.
    total = len(blocks)
    dropped_keys = []
    from services.db import globals_col
    ov = await globals_col.find_one({"key": "valid_mapobject_keys"}, {"_id": 0, "value": 1})
    allow = set(ov["value"]) if ov and isinstance(ov.get("value"), list) else None
    if allow:
        kept = []
        seen_drop = set()
        for b in blocks:
            k = (b.get("name") or b.get("key") or "") if isinstance(b, dict) else ""
            if k in allow:
                kept.append(b)
            elif k and k not in seen_drop:
                seen_drop.add(k); dropped_keys.append(k)
        blocks = kept

    # Drop blocks with out-of-range coordinates. A corrupt coord (e.g. y=14456)
    # is outside any room's grid and makes the client throw IndexOutOfRange ->
    # the whole room bricks. No Yeeps room exceeds ~256 on any axis.
    oob = 0
    in_bounds = []
    for b in blocks:
        try:
            if all(0 <= int(b.get(ax, 0)) < 256 for ax in ("x", "y", "z")):
                in_bounds.append(b)
            else:
                oob += 1
        except (TypeError, ValueError):
            oob += 1
    blocks = in_bounds
    # Blocks whose owner account doesn't exist go to the server owner.
    try:
        from services.map_owners import default_block_owners
        await default_block_owners(blocks)
    except Exception as e:
        print(f"[editor] owner defaulting failed for {room_key}: {e}")

    try:
        save_data = serialize_blocks_spam(blocks)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not serialize map: {e}")
    doc = {
        "roomKey": room_key,
        "blocks": blocks,
        "blockCount": len(blocks),
        "dimensions": dims,
        "saveData": save_data,
        "importedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S"),
    }
    try:
        from handlers.extras import community_on as _cw_on
    except Exception:
        def _cw_on():
            return True
    if room_key.startswith("c_") and not _cw_on():
        raise HTTPException(status_code=400, detail="Community worlds are off on this server.")
    await imported_maps.update_one({"roomKey": room_key}, {"$set": doc}, upsert=True)
    try:
        from services.db import maps as maps_col
        existing = await maps_col.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0, "saveInternalStates": 1})
        isd = (existing or {}).get("saveInternalStates") or ""
        await maps_col.update_one({"mapKey": f"{room_key}$current"}, {"$set": {
            "mapKey": f"{room_key}$current", "roomKey": room_key,
            "saveData": save_data, "editorBlocks": blocks,
            "mapDataVersion": 3, "savedBy": "editor",
            "applicationVersion": "1.31.0",
            "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S"),
            "saveInternalStates": isd,
        }}, upsert=True)
        from services.db import rooms as rooms_col
        import time as _t
        await rooms_col.update_one({"roomKey": room_key},
                                  {"$set": {"lastEditedSessionID": int(_t.time()), "roomDataVersion": 3}},
                                  upsert=True)
    except Exception as e:
        print(f"[editor] imported map -> live map failed for {room_key}: {e}")
    world = await _ensure_world_map_entry(room_key, dims)
    name = _authed_name(request) or "?"
    await _discord_notify(f"{name} imported a map for {room_key} ({len(blocks)} blocks, {total - len(blocks)} dropped)")
    return JSONResponse(content={"ok": True, "roomKey": room_key, "blockCount": len(blocks),
                                 "inputBlocks": total, "dropped": total - len(blocks),
                                 "droppedBadKeys": len(dropped_keys), "droppedOutOfBounds": oob,
                                 "droppedKeysSample": sorted(dropped_keys)[:30],
                                 "worldMap": world})


@router.delete("/api/map/{room_key}")
async def delete_map(room_key: str, request: Request):
    """Fully reset a room's map to empty: remove the editor-imported map AND the
    in-game save (every $current / $map_* chunk), and bump the room's edit
    session so clients reload the now-empty room. Use this to recover a bricked
    room."""
    from services.db import imported_maps, maps as maps_col, rooms as rooms_col, db as _db
    import re as _re, time as _time
    esc = _re.escape(room_key)
    # Safety: back up every map doc before deleting so a reset is recoverable.
    try:
        victims = []
        async for d in maps_col.find({"mapKey": {"$regex": f"^{esc}\\$"}}):
            d.pop("_id", None); victims.append(d)
        async for d in imported_maps.find({"roomKey": room_key}):
            d.pop("_id", None); victims.append({"_imported": True, **d})
        if victims:
            await _db["map_backups"].insert_one({
                "roomKey": room_key, "resetBy": _authed_name(request) or "?",
                "resetAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "docs": victims})
    except Exception as e:
        print(f"[editor] map reset backup failed for {room_key}: {e}")
    r_imp = await imported_maps.delete_many({"roomKey": room_key})
    r_map = await maps_col.delete_many({"mapKey": {"$regex": f"^{esc}\\$"}})
    await rooms_col.update_one({"roomKey": room_key},
                               {"$set": {"lastEditedSessionID": int(_time.time())}})
    name = _authed_name(request) or "?"
    total = r_imp.deleted_count + r_map.deleted_count
    if total:
        await _discord_notify(f"{name} reset the map for {room_key} to empty "
                              f"({r_imp.deleted_count} imported + {r_map.deleted_count} saved removed)")
    return JSONResponse(content={"ok": True, "deleted": total,
                                 "importedRemoved": r_imp.deleted_count,
                                 "savedRemoved": r_map.deleted_count})


@router.post("/api/maps/default-owners")
async def maps_default_owners(request: Request):
    """Give every block whose owner account isn't in the database to the server
    owner (DEFAULT_MAP_OWNER), across all stored maps. Body {"apply": false}
    (default) only reports; {"apply": true} writes, snapshotting each changed
    map to its revert history first. Only the owner IDs change."""
    from services.map_owners import fix_all_maps
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await fix_all_maps(apply=bool(body.get("apply")))
    if res["applied"] and res["maps"]:
        who = _authed_name(request) or "?"
        await _discord_notify(f"{who} gave {res['blocks']} blocks owned by unknown accounts in "
                              f"{res['maps']} maps to {res['defaultOwner']}")
    return JSONResponse(content=res)


@router.get("/api/map/{room_key}/history")
async def map_history(room_key: str):
    """List saved map snapshots (newest first) available to revert to, plus
    whether an imported official copy exists."""
    from services.db import db as _db, imported_maps
    cur = _db["map_history"].find({"roomKey": room_key}).sort("ts", -1).limit(30)
    items = []
    async for d in cur:
        items.append({"id": str(d.get("_id")), "ts": d.get("ts"),
                      "savedAt": d.get("savedAt"), "savedBy": d.get("savedBy"),
                      "bytes": d.get("bytes") or len((d.get("doc") or {}).get("saveData") or "")})
    has_imported = await imported_maps.find_one({"roomKey": room_key}, {"_id": 1}) is not None
    return JSONResponse(content={"roomKey": room_key, "history": items,
                                 "hasImported": has_imported})


@router.post("/api/map/{room_key}/revert")
async def map_revert(room_key: str, request: Request):
    """Revert a room's map. Body: {"historyId": "..."} restores that snapshot, or
    {"toImported": true} drops the in-game saves so the room falls back to its
    imported official map. The current (griefed) state is snapshotted first so
    the revert itself is undoable."""
    from services.db import db as _db, maps as maps_col, rooms as rooms_col
    from bson import ObjectId
    import time as _t
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    map_key = f"{room_key}$current"
    now = int(_t.time())

    # Snapshot the current state before we change it (so you can undo the revert).
    try:
        cur = await maps_col.find_one({"mapKey": map_key}, {"_id": 0})
        if cur and cur.get("saveData"):
            await _db["map_history"].insert_one(
                {"roomKey": room_key, "ts": now, "savedBy": cur.get("savedBy"),
                 "savedAt": cur.get("savedAt"), "bytes": len(cur.get("saveData") or ""),
                 "doc": cur, "preRevert": True})
    except Exception as e:
        print(f"[editor] pre-revert snapshot failed for {room_key}: {e}")

    if body.get("toImported"):
        esc = re.escape(room_key)
        r = await maps_col.delete_many({"mapKey": {"$regex": f"^{esc}\\$"}})
        await rooms_col.update_one({"roomKey": room_key}, {"$set": {"lastEditedSessionID": now}})
        await _discord_notify(f"♻️ **{who}** reverted **{room_key}** to its imported official map "
                              f"(dropped {r.deleted_count} in-game save chunk(s))")
        return JSONResponse(content={"ok": True, "revertedTo": "imported",
                                     "savedRemoved": r.deleted_count})

    hid = str(body.get("historyId") or "")
    if not hid:
        raise HTTPException(status_code=400, detail="Give a historyId or toImported:true")
    try:
        h = await _db["map_history"].find_one({"_id": ObjectId(hid), "roomKey": room_key})
    except Exception:
        h = None
    if not h or not h.get("doc"):
        raise HTTPException(status_code=404, detail="Snapshot not found")
    doc = dict(h["doc"])
    doc.pop("_id", None)
    doc["mapKey"] = map_key
    doc["roomKey"] = room_key
    await maps_col.update_one({"mapKey": map_key}, {"$set": doc}, upsert=True)
    await rooms_col.update_one({"roomKey": room_key}, {"$set": {"lastEditedSessionID": now}})
    await _discord_notify(f"♻️ **{who}** reverted **{room_key}** to the snapshot from "
                          f"{h.get('savedAt') or h.get('ts')} ({doc.get('savedBy') or '?'}'s save)")
    return JSONResponse(content={"ok": True, "revertedTo": h.get("savedAt") or h.get("ts"),
                                 "bytes": len(doc.get("saveData") or "")})


@router.get("/api/banned")
async def banned_players(request: Request):
    """List accounts that are currently banned (admin-block / permaban / shadowban
    / active timed ban). No IPs — this tab is about players, not addresses."""
    import re as _re
    q = {"$or": [{"adminBlocked": 1}, {"isPermabanned": 1}, {"shadowBanned": 1}]}
    proj = {"_id": 0, "accountID": 1, "displayName": 1, "oculusID": 1,
            "banReason": 1, "blockReason": 1, "banExpireTimestamp": 1,
            "adminBlocked": 1, "isPermabanned": 1, "shadowBanned": 1}
    out = []
    async for d in accounts.find(q, proj).limit(1000):
        raw = d.get("displayName") or d.get("oculusID") or d.get("accountID") or ""
        name = _re.sub(r"<[^>]+>", "", str(raw)).strip() or str(d.get("accountID") or "?")
        if d.get("shadowBanned"):
            kind = "shadowban"
        elif d.get("banExpireTimestamp"):
            kind = "timed"
        else:
            kind = "permanent"
        out.append({
            "accountID": d.get("accountID"),
            "name": name,
            "reason": (d.get("banReason") or d.get("blockReason") or ""),
            "kind": kind,
            "expires": d.get("banExpireTimestamp") or "",
        })
    out.sort(key=lambda x: (x["kind"], x["name"].lower()))
    return JSONResponse(content={"banned": out, "count": len(out)})


@router.get("/api/ipbans")
async def ip_bans_list(request: Request):
    if not _can_see_ips(request):
        return JSONResponse(content={"restricted": True, "ips": []})
    from services.ip_bans import list_bans
    return JSONResponse(content=await list_bans())


@router.post("/api/ipban")
async def ip_ban_add(request: Request):
    from services.ip_bans import ban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise HTTPException(status_code=400, detail="Give an ip to ban")
    who = _authed_name(request) or "?"
    await ban_ip(ip, body.get("reason") or f"editor ban by {who}")
    await _discord_notify(f"⛔ **{who}** IP-banned `{ip}`")
    return JSONResponse(content={"ok": True, "banned": ip})


@router.post("/api/ipunban")
async def ip_ban_remove(request: Request):
    from services.ip_bans import unban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip", "")).strip()
    had = await unban_ip(ip)
    await _discord_notify(f"✅ **{_authed_name(request) or '?'}** IP-unbanned `{ip}`")
    return JSONResponse(content={"ok": True, "wasBanned": had})


def _fmt_hours(h: float) -> str:
    """Short human label for a ban duration in hours."""
    if h < 1:
        return f"{round(h * 60)} min"
    if h >= 168 and h % 168 == 0:
        return f"{int(h // 168)}w"
    if h >= 24 and h % 24 == 0:
        return f"{int(h // 24)}d"
    if h == int(h):
        return f"{int(h)}h"
    return f"{round(h, 2)}h"


@router.post("/api/player/{account_id}/ban")
async def ban_player(account_id: str, request: Request):
    """Ban a player. Body `durationHours` > 0 makes it a TIMED ban that auto-lifts
    when it expires (account block only — no permanent IP ban for a temporary
    ban); omitted / 0 = PERMANENT (account block + IP ban)."""
    from services.ip_bans import ban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1, "deviceKeys": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    try:
        dur_h = float(body.get("durationHours") or 0)
    except Exception:
        dur_h = 0.0

    if dur_h > 0:
        # TIMED ban: real expiry so login serves "suspended until X" and
        # auto-clears it when it passes. isPermabanned stays 0 so the stored
        # state reads as a timed ban (login re-derives what the client sees).
        expire = (datetime.now(timezone.utc) + timedelta(hours=dur_h)).strftime("%Y %m/%d %H:%M:%S")
        reason = f"Banned by {who} via editor ({_fmt_hours(dur_h)})"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 0,
                                            "isMutebanned": 0, "isIsolated": 0,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": expire,
                                            "shouldForceRefresh": 1}})
        # A temporary ban must NOT leave a permanent IP ban behind (IP bans don't
        # expire and hit everyone sharing that IP), so the account block is enough.
        ips = []
    else:
        # PERMANENT ban: block the account AND every IP it has logged in from.
        expire = ""
        reason = f"Banned by {who} via editor (permanent)"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 1,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": "",
                                            "shouldForceRefresh": 1}})
        ips = sorted(set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))))
        for ip in ips:
            await ban_ip(ip, f"account {account_id} ({doc.get('displayName')}) banned by {who}")
        # ...and its headset(s), so a new account on the same Quest is banned too.
        from services.device_bans import ban_devices
        await ban_devices(doc.get("deviceKeys") or [], f"account {account_id} banned by {who}")

    span = f"until {expire}" if expire else "permanent"
    await _discord_notify(f"**{who}** banned **{doc.get('displayName') or account_id}** "
                          f"(`{account_id}`) [{span}]"
                          + (f" + {len(ips)} IP(s): {', '.join(ips)}" if ips else ""))
    # Hide the actual IPs from editor users who aren't allowed to see them.
    shown = ips if _can_see_ips(request) else ["(hidden)"] * len(ips)
    return JSONResponse(content={"ok": True, "bannedIPs": shown,
                                 "until": expire, "permanent": not expire})


@router.post("/api/player/{account_id}/unban")
async def unban_player(account_id: str, request: Request):
    from services.ip_bans import unban_ip
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    await accounts.update_one({"accountID": account_id},
                              {"$set": {"adminBlocked": 0, "isPermabanned": 0,
                                        "isMutebanned": 0, "isIsolated": 0,
                                        "blockReason": "", "banReason": "",
                                        "banExpireTimestamp": "", "banCategory": "",
                                        "shadowBanned": 0, "shouldForceRefresh": 1}})
    from services.device_bans import unban_devices
    _ud = await accounts.find_one({"accountID": account_id}, {"_id": 0, "deviceKeys": 1})
    await unban_devices((_ud or {}).get("deviceKeys") or [])
    unbanned = []
    if body.get("alsoIP"):
        for ip in set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))):
            if await unban_ip(ip):
                unbanned.append(ip)
    await _discord_notify(f"✅ **{who}** unbanned **{doc.get('displayName') or account_id}** (`{account_id}`)"
                          + (f" + IPs {', '.join(unbanned)}" if unbanned else ""))
    shown = unbanned if _can_see_ips(request) else ["(hidden)"] * len(unbanned)
    return JSONResponse(content={"ok": True, "unbannedIPs": shown})


# ── Kick (boot) players ─────────────────────────────────────────────
# One-shot boots, not bans: see services/kick.py. Build UI on these.

@router.get("/api/online")
async def online_players():
    """Who is online right now: [{accountID, displayName, roomKey}]."""
    from services.kick import online_account_ids
    from services.db import player_statuses
    from services.build_gate import current_status
    ids = await online_account_ids()
    acc = {a["accountID"]: a async for a in
           accounts.find({"accountID": {"$in": ids}},
                         {"_id": 0, "accountID": 1, "displayName": 1,
                          "clientBuildTag": 1, "clientBuildStatus": 1})}
    rooms_by = {p["accountID"]: p.get("roomKey") async for p in
                player_statuses.find({"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1, "roomKey": 1})}
    return JSONResponse(content={"ok": True, "count": len(ids), "players": [
        {"accountID": a, "displayName": (acc.get(a) or {}).get("displayName") or a,
         "roomKey": rooms_by.get(a) or "",
         # APK build the player is on (services/build_gate.py): "ok",
         # "outdated" (old/unofficial APK) or "modified" (patched native libs).
         "build": (acc.get(a) or {}).get("clientBuildTag") or "",
         "buildStatus": current_status(acc.get(a) or {})}
        for a in ids]})


@router.post("/api/player/{account_id}/kick")
async def kick_player(account_id: str, request: Request):
    """Boot one player once. Body (optional): {"reason": "..."}."""
    from services.kick import kick_account
    try:
        body = await request.json()
    except Exception:
        body = {}
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
    if not doc or not await kick_account(account_id, body.get("reason", "")):
        raise HTTPException(status_code=404, detail="Account not found")
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked **{doc.get('displayName') or account_id}** (`{account_id}`)"
                          + (f": {body['reason']}" if body.get("reason") else ""))
    return JSONResponse(content={"ok": True})


@router.post("/api/player/{account_id}/device-reset")
async def reset_device_lock(account_id: str, request: Request):
    """Unbind every headset from this account (services/device_auth.py). The
    player's next login - from any IP - binds their (new) headset."""
    from services import device_auth
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    await device_auth.reset(account_id)
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** reset the headset lock for **{doc.get('displayName') or account_id}** (`{account_id}`)")
    return JSONResponse(content={"ok": True})


# ── Announcements + remote disk audio (services/remote.py) ──────────
# The 1.31 APK (131b2+) shows announcements as floating text and swaps disk
# audio from these. Build UI on these endpoints.

@router.get("/api/announce")
async def list_announcements():
    from services.remote import active_announcements
    return JSONResponse(content={"ok": True, "announcements": await active_announcements()})


@router.post("/api/announce")
async def post_announcement(request: Request):
    """Body: {"text": "...", "seconds": 10 (on screen), "activeMinutes": 10
    (players who launch within this window still get it)}."""
    from services.remote import add_announcement
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    who = _authed_name(request) or "?"
    a = await add_announcement(text, body.get("seconds", 10), body.get("activeMinutes", 10), who)
    await _discord_notify(f"**{who}** sent an announcement: {a['text']}")
    return JSONResponse(content={"ok": True, "announcement": a})


@router.delete("/api/announce")
async def delete_announcements(request: Request):
    from services.remote import clear_announcements
    await clear_announcements()
    return JSONResponse(content={"ok": True})


@router.get("/api/errors")
async def list_errors(window: str = "24h", muted: int = 0):
    from services.client_errors import summary
    return JSONResponse(content={"ok": True, "errors": await summary(window, bool(muted))})


@router.get("/api/errors/{sid}")
async def error_detail(sid: str):
    from services.client_errors import detail
    d = await detail(sid)
    if not d:
        raise HTTPException(status_code=404, detail="unknown error")
    return JSONResponse(content={"ok": True, "error": d})


@router.post("/api/errors/{sid}/mute")
async def mute_error(sid: str, request: Request):
    from services.client_errors import set_muted
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not await set_muted(sid, bool(body.get("muted", True))):
        raise HTTPException(status_code=404, detail="unknown error")
    return JSONResponse(content={"ok": True})


@router.get("/api/screens")
async def list_screens():
    from services.remote import SCREEN_SLOTS, screen_images
    have = {d["slot"]: d async for d in screen_images.find({}, {"_id": 0, "data": 0, "preview": 0})}
    out = [{"slot": k, "label": v[0], "w": v[1], "h": v[2], "image": have.get(k)}
           for k, v in SCREEN_SLOTS.items()]
    return JSONResponse(content={"ok": True, "screens": out})


@router.get("/api/screens/{slot}/preview.png")
async def screen_preview(slot: str):
    from fastapi.responses import Response as _R
    from services.remote import get_screen
    d = await get_screen(slot, preview=True)
    if not d or not d.get("preview"):
        raise HTTPException(status_code=404, detail="no picture")
    return _R(content=bytes(d["preview"]), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/api/screens/{slot}")
async def upload_screen(slot: str, request: Request):
    """Raw image file (PNG/JPG/GIF/WebP) as the request body."""
    from services.remote import SCREEN_SLOTS, set_screen
    if slot not in SCREEN_SLOTS:
        raise HTTPException(status_code=404, detail="unknown screen")
    data = await request.body()
    if len(data) < 32:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too big (max 25 MB)")
    try:
        info = await set_screen(slot, data, _authed_name(request) or "?")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read image: {e}")
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** set the {SCREEN_SLOTS[slot][0]} picture "
                          f"({info['frames']} frame(s), {info['size']//1024} KB)")
    return JSONResponse(content={"ok": True, "screen": info})


@router.delete("/api/screens/{slot}")
async def remove_screen(slot: str, request: Request):
    from services.remote import delete_screen
    ok = await delete_screen(slot)
    if ok:
        await _discord_notify(f"**{_authed_name(request) or '?'}** removed the {slot} screen picture")
    return JSONResponse(content={"ok": ok})


# ── Floating video (Announce tab): load a video to play for everyone, or stop it.
@router.get("/api/video")
async def video_status():
    from services import video
    m = await video.active_video()
    return JSONResponse(content={"ok": True, "playing": bool(m), "video": m,
                                 "gen": await video.video_gen()})


@router.post("/api/video_packed")
async def video_load_packed(request: Request):
    """Video already converted IN THE BROWSER (editor Announce tab): frames as
    BSCR segments + WAV audio, so the upload is small and the server does no
    CPU work. Rejected (409) if Stop/another Load happened since it started."""
    import mmap, tempfile
    from services import video
    who = _authed_name(request) or "?"
    # A 2-minute video is ~70 MB: stream it to an unnamed temp file and parse it
    # through mmap, so it never sits in this process's RAM (self-hosted, low RAM).
    os.makedirs(video.VIDEO_DIR, exist_ok=True)
    tf = tempfile.TemporaryFile(dir=video.VIDEO_DIR)
    mm = conv = None
    try:
        n = 0
        async for chunk in request.stream():
            n += len(chunk)
            if n > 100 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="video too big (max 100 MB)")
            tf.write(chunk)
        if n < 16:
            raise HTTPException(status_code=400, detail="empty upload")
        tf.flush()
        mm = mmap.mmap(tf.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            conv = video.parse_packed(mm)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad packed video: {e}")
        conv["meta"]["by"] = who
        try:
            meta = await video.store_and_play(conv, conv["loop"], conv["gen"])
        except video.StaleVideo:
            raise HTTPException(status_code=409, detail="stopped while converting - not played")
    finally:
        conv = None                     # drop the memoryview slices before unmapping
        if mm is not None:
            try:
                mm.close()
            except BufferError:         # a traceback still holds a slice; GC unmaps it
                pass
        tf.close()
    await _discord_notify(
        f"**{who}** started a floating video '{meta.get('name')}' "
        f"({meta['frames']} frames, {meta.get('durationMs', 0)//1000}s, {meta['size']//1024} KB, "
        f"{'with sound' if meta.get('audioFmt') else 'no sound'})")
    return JSONResponse(content={"ok": True, "video": meta})


@router.post("/api/video")
async def video_load(request: Request):
    """Raw video file (mp4/webm/mov/gif) as the body. Query: ?loop=1&name=...
    Converts to looping floating-video frames + audio and plays it for everyone."""
    import asyncio
    from services import video
    data = await request.body()
    if len(data) < 64:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > video.MAX_UPLOAD:
        raise HTTPException(status_code=400,
                            detail=f"file too big (max {video.MAX_UPLOAD // (1024*1024)} MB)")
    who = _authed_name(request) or "?"
    name = (request.query_params.get("name") or "video")[:80]
    loop = request.query_params.get("loop", "1") != "0"
    gen = request.query_params.get("gen")
    try:
        conv = await asyncio.to_thread(video.convert_video, data, name, who)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not convert video: {e}")
    if not conv["meta"].get("frames"):
        raise HTTPException(status_code=400, detail="no video frames decoded")
    try:
        meta = await video.store_and_play(conv, loop, gen)
    except video.StaleVideo:
        raise HTTPException(status_code=409, detail="stopped while converting - not played")
    dur = meta.get("durationMs", 0) // 1000
    await _discord_notify(
        f"**{who}** started a floating video '{meta.get('name')}' "
        f"({meta['frames']} frames @ {meta['fps']}fps, {dur}s, {meta['size']//1024} KB, "
        f"{'looping' if loop else 'once'})")
    return JSONResponse(content={"ok": True, "video": meta})


@router.delete("/api/video")
async def video_stop(request: Request):
    from services import video
    was = await video.stop_video()
    if was:
        await _discord_notify(f"**{_authed_name(request) or '?'}** stopped the floating video")
    return JSONResponse(content={"ok": True, "wasPlaying": was})


@router.get("/api/download")
async def download_status():
    from services import download
    return JSONResponse(content={"ok": True, **(await download.status())})


@router.post("/api/download")
async def download_upload(request: Request):
    """Body: the APK bytes. Query: ?name=Yeeps.apk. Served publicly at /download."""
    from services import download
    who = _authed_name(request) or "?"
    name = (request.query_params.get("name") or "yeeps.apk")[:120]
    try:
        meta = await download.store_stream(request.stream(), name, who)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _discord_notify(f"**{who}** uploaded the download file '{meta['filename']}' "
                          f"({meta['size'] // (1024 * 1024)} MB)")
    return JSONResponse(content={"ok": True, **(await download.status())})


@router.post("/api/download/url")
async def download_set_url(request: Request):
    """Body: {"url": "https://..."} to redirect /download elsewhere, "" for the file."""
    from services import download
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = str(body.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="link must start with http:// or https://")
    meta = await download.set_url(url)
    await _discord_notify(f"**{_authed_name(request) or '?'}** set the download link to "
                          f"{meta.get('url') or '(the uploaded file)'}")
    return JSONResponse(content={"ok": True, **(await download.status())})


@router.delete("/api/download")
async def download_clear(request: Request):
    from services import download
    was = await download.clear()
    return JSONResponse(content={"ok": True, "had": was})


@router.get("/api/settings")
async def get_settings():
    from services.db import globals_col
    fe = await globals_col.find_one({"key": "friends_everyone"}, {"_id": 0, "value": 1})
    fatal = await globals_col.find_one({"key": "fatalError"}, {"_id": 0, "data": 1})
    return JSONResponse(content={"ok": True,
                                 "friends_everyone": bool((fe or {}).get("value", False)),
                                 "maintenance": (fatal or {}).get("data") or ""})


@router.post("/api/settings")
async def set_settings(request: Request):
    """Body: {"friends_everyone": bool} and/or {"maintenance": "message" | ""}.
    A maintenance message is served as the game's global fatal error, so every
    client shows it instead of playing; "" turns it off."""
    from services.db import globals_col
    try:
        body = await request.json()
    except Exception:
        body = {}
    who = _authed_name(request) or "?"
    if "friends_everyone" in body:
        on = bool(body["friends_everyone"])
        await globals_col.update_one({"key": "friends_everyone"},
                                     {"$set": {"key": "friends_everyone", "value": on}}, upsert=True)
        await _discord_notify(f"**{who}** turned everyone-is-a-friend {'ON' if on else 'OFF'}")
    if "maintenance" in body:
        msg = str(body["maintenance"] or "").strip()[:300]
        await globals_col.update_one({"key": "fatalError"},
                                     {"$set": {"key": "fatalError", "data": msg}}, upsert=True)
        await _discord_notify(f"**{who}** " + (f"turned ON maintenance: {msg}" if msg else "turned OFF maintenance"))
    return JSONResponse(content={"ok": True})


@router.get("/api/disks")
async def list_disks():
    from services.remote import disk_audio
    out = [d async for d in disk_audio.find({}, {"_id": 0, "data": 0})]
    return JSONResponse(content={"ok": True, "disks": out})


@router.post("/api/disks/{record}")
async def upload_disk(record: str, request: Request, ext: str = "ogg"):
    """Raw audio file as the request body; ?ext=ogg|wav (16-bit). `record` is the disk
    key (record_theFinalHider) or its track name (TheFinalHider)."""
    from services.remote import AUDIO_TYPES, set_disk
    ext = (ext or "ogg").lower().lstrip(".")
    if ext not in AUDIO_TYPES:
        raise HTTPException(status_code=400, detail="ext must be ogg or wav (16-bit PCM)")
    data = await request.body()
    if len(data) < 1024:
        raise HTTPException(status_code=400, detail="empty or too-small audio file")
    who = _authed_name(request) or "?"
    clip = await set_disk(record, ext, data, who)
    await _discord_notify(f"**{who}** set disk **{clip}** to a new {ext} ({len(data)//1024} KB)")
    return JSONResponse(content={"ok": True, "clip": clip, "bytes": len(data)})


@router.delete("/api/disks/{record}")
async def remove_disk(record: str, request: Request):
    from services.remote import delete_disk
    return JSONResponse(content={"ok": await delete_disk(record)})


@router.post("/api/kick_wrong_build")
async def kick_wrong_build_players(request: Request):
    """Boot everyone online who isn't on an allowed APK build. Body (optional): {"reason": "..."}."""
    from services.kick import kick_wrong_build
    try:
        body = await request.json()
    except Exception:
        body = {}
    kicked = await kick_wrong_build(body.get("reason", ""))
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked {len(kicked)} player(s) on the wrong APK build")
    return JSONResponse(content={"ok": True, "kicked": len(kicked), "accountIDs": kicked})


@router.get("/api/require_build")
async def require_build_status():
    from services.db import globals_col
    from services.build_gate import newest_build
    d = await globals_col.find_one({"key": "requireBuild"}, {"_id": 0, "value": 1})
    return JSONResponse(content={"ok": True, "requireBuild": str((d or {}).get("value") or ""),
                                 "newest": newest_build()})


@router.post("/api/require_build")
async def set_require_build(request: Request):
    """Testing toggle. {"on": true} requires the newest build: every other client
    is served "Outdated version" and kicked (frees CCU, keeps them out while on).
    {"on": false} allows all allowlisted builds again."""
    from services.db import globals_col
    from services.build_gate import newest_build
    from services.kick import online_account_ids
    try:
        body = await request.json()
    except Exception:
        body = {}
    on = bool(body.get("on"))
    who = _authed_name(request) or "?"
    target = newest_build() if on else "*"   # "*" = allow all builds (testing)
    await globals_col.update_one({"key": "requireBuild"},
                                 {"$set": {"key": "requireBuild", "value": target}}, upsert=True)
    kicked = 0
    if on and target:
        # Force online players not on the newest build to re-pull now, so they
        # get the outdated block and leave immediately instead of after ~45 s.
        ids = await online_account_ids()
        if ids:
            wrong = [a["accountID"] async for a in accounts.find(
                {"accountID": {"$in": ids}}, {"_id": 0, "accountID": 1, "clientBuildTag": 1})
                if (a.get("clientBuildTag") or "").split(".", 1)[0] != target]
            if wrong:
                await accounts.update_many({"accountID": {"$in": wrong}},
                                           {"$set": {"shouldForceRefresh": 1}})
                kicked = len(wrong)
    await _discord_notify(
        f"**{who}** enabled require-newest-build ({target}) — kicked {kicked} old-build player(s)"
        if on else f"**{who}** disabled require-newest-build (all builds allowed again)")
    return JSONResponse(content={"ok": True, "requireBuild": target, "kicked": kicked})


@router.post("/api/kick_all")
async def kick_all_players(request: Request):
    """Boot everyone online. Body (optional): {"reason": "...", "exclude": [accountIDs]}."""
    from services.kick import kick_all
    try:
        body = await request.json()
    except Exception:
        body = {}
    kicked = await kick_all(body.get("reason", ""), body.get("exclude") or [])
    who = _authed_name(request) or "?"
    await _discord_notify(f"**{who}** kicked ALL online players ({len(kicked)})"
                          + (f": {body['reason']}" if body.get("reason") else ""))
    return JSONResponse(content={"ok": True, "kicked": len(kicked), "accountIDs": kicked})


_MEM_BASE = {"snap": None}


@router.get("/api/debug/mem")
async def debug_mem(reset: int = 0, top: int = 25, collect: int = 0, trim: int = 0):
    """Where is the game server's memory going? Object counts by type, and (when
    started with TRACEMALLOC=1) the code lines that allocated the most since the
    last ?reset=1."""
    import gc, collections, tracemalloc
    out = {}
    if collect:
        before = len(gc.get_objects())
        freed = gc.collect()
        out["gc_collect"] = {"objects_before": before, "unreachable_freed": freed,
                             "objects_after": len(gc.get_objects()), "garbage": len(gc.garbage)}
    if trim and sys.platform.startswith("linux"):
        # give freed-but-held C heap memory back to the OS (glibc; Linux only)
        import ctypes
        def rss():
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
        before = rss()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        out["malloc_trim"] = {"rss_before_mb": before, "rss_after_mb": rss()}
    counts = collections.Counter(type(o).__name__ for o in gc.get_objects())
    out["objects"] = counts.most_common(top)
    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(False, tracemalloc.__file__)])
        if reset or _MEM_BASE["snap"] is None:
            _MEM_BASE["snap"] = snap
            out["tracemalloc"] = "baseline taken"
        else:
            diff = snap.compare_to(_MEM_BASE["snap"], "traceback")
            out["growth"] = [{"kb": round(d.size_diff / 1024), "count": d.count_diff,
                              "where": [f"{f.filename.split('/')[-1]}:{f.lineno}" for f in d.traceback[-6:]]}
                             for d in diff[:top]]
        cur, peak = tracemalloc.get_traced_memory()
        out["traced_mb"] = round(cur / 1048576, 1)
    # Pending asyncio tasks grouped by where they are waiting (innermost frames)
    import asyncio
    waits = collections.Counter()
    tasks = asyncio.all_tasks()
    for t in tasks:
        st = t.get_stack(limit=40)
        mine = [f for f in st if "/vr-server/" in f.f_code.co_filename]
        where = mine[-1] if mine else (st[-1] if st else None)
        inner = st[-1] if st else None
        key = (f"{where.f_code.co_filename.split('/')[-1]}:{where.f_lineno} {where.f_code.co_name}" if where else "?") + \
              (f"  <waiting in> {inner.f_code.co_filename.split('/')[-1]}:{inner.f_lineno} {inner.f_code.co_name}" if inner else "")
        waits[key] += 1
    out["tasks"] = len(tasks)
    out["task_waits"] = waits.most_common(top)
    return JSONResponse(content=out)


@router.get("/api/debug")
async def list_debug(limit: int = 50):
    """Most recent g2_logDebug payloads the client sent, newest first."""
    cursor = debug_logs.find({}, {"_id": 0}).sort("_id", -1).limit(limit)
    return JSONResponse(content=await cursor.to_list(length=limit))


@router.delete("/api/debug")
async def clear_debug():
    result = await debug_logs.delete_many({})
    return JSONResponse(content={"ok": True, "deleted": result.deleted_count})


@router.get("/api/rooms")
async def list_rooms(q: str = "", limit: int = 200):
    """List/search rooms."""
    filt = {}
    if q:
        filt = {"$or": [
            {"roomKey": {"$regex": q, "$options": "i"}},
            {"roomName": {"$regex": q, "$options": "i"}},
        ]}
    cursor = rooms.find(
        filt,
        {"_id": 0, "roomKey": 1, "roomName": 1, "isCreative": 1,
         "isModeLocked": 1, "blockMatchmakingSegmentation": 1, "creativeSessionID": 1},
    ).limit(limit)
    docs = await cursor.to_list(length=limit)
    return JSONResponse(content=docs)


@router.get("/api/room/{room_key}")
async def get_room(room_key: str):
    """Return the full room document. For rooms not stored in the collection
    (e.g. players' private worlds, served on-the-fly) return the generated
    room data so it can still be viewed/edited; saving persists it."""
    doc = await rooms.find_one({"roomKey": room_key}, {"_id": 0})
    if doc is None:
        try:
            from services.dynamo_ops import _get_fallback_doc
            doc = await _get_fallback_doc("G2_Rooms", {"roomKey": room_key})
        except Exception:
            doc = None
        if doc is None:
            raise HTTPException(status_code=404, detail="Room not found")
        doc = {**doc, "_generated": True}
    return JSONResponse(content=doc)


@router.post("/api/room/{room_key}")
async def update_room(room_key: str, request: Request):
    """Patch fields on a room."""
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    patch.pop("roomKey", None)
    patch.pop("_id", None)
    patch.pop("_generated", None)   # UI-only marker, never store it
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    if room_key.startswith("c_"):
        raise HTTPException(status_code=400, detail="Community worlds are read live from Yeeps and are not saved here.")
    # upsert so saving a room that was served on-the-fly (players' private
    # worlds have no stored doc) creates it instead of 404-ing.
    result = await rooms.update_one(
        {"roomKey": room_key}, {"$set": {**patch, "roomKey": room_key}}, upsert=True)

    # A world-map room's biome also lives on its room-map node (the client reads
    # that for the scene). Keep both in sync so changing themeKey actually works.
    if "themeKey" in patch:
        from services.db import room_maps
        async for rm in room_maps.find({}):
            data = rm.get("data")
            if not isinstance(data, list):
                continue
            changed = False
            for node in data:
                if isinstance(node, dict) and node.get("roomKey") == room_key \
                        and node.get("themeKey") != patch["themeKey"]:
                    node["themeKey"] = patch["themeKey"]; changed = True
            if changed:
                await room_maps.update_one({"_id": rm["_id"]}, {"$set": {"data": data}})

    return JSONResponse(content={"ok": True, "modified": result.modified_count,
                                 "created": bool(result.upserted_id)})


@router.post("/api/import")
async def import_accounts(request: Request):
    """
    Import one or more full account documents (create or overwrite).
    Body can be a single object or a JSON array of objects; each must
    include an accountID field, which is used as the upsert key.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise HTTPException(status_code=400, detail="No accounts to import")

    imported, errors = [], []
    for i, doc in enumerate(items):
        if not isinstance(doc, dict):
            errors.append(f"item {i}: not an object")
            continue
        account_id = doc.get("accountID")
        if not account_id:
            errors.append(f"item {i}: missing accountID")
            continue
        doc = {k: v for k, v in doc.items() if k != "_id"}
        await accounts.update_one({"accountID": account_id}, {"$set": doc}, upsert=True)
        imported.append(account_id)

    return JSONResponse(content={"ok": True, "imported": imported, "errors": errors})


# ─── HTML UI ─────────────────────────────────────────────────────────────────

_LOGIN_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yeeps Editor — Sign in</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3}
.card{width:min(92vw,380px);background:#161b22;border:1px solid #30363d;border-radius:14px;
  padding:28px 24px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
h1{margin:0 0 4px;font-size:20px}
p.sub{margin:0 0 20px;color:#8b949e;font-size:13px}
label{display:block;font-size:12px;color:#8b949e;margin:14px 0 6px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #30363d;background:#0d1117;
  color:#e6edf3;font-size:15px}
input:focus{outline:none;border-color:#2f81f7}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;background:#238636;color:#fff;
  font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#2ea043}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:14px;color:#f85149;font-size:13px;min-height:18px}
</style></head><body>
<form class="card" id="f">
  <h1>Yeeps 1.31.0-yeeper920 Editor</h1>
  <p class="sub">Sign in with the EDITOR_PASSWORD from the server's .env file.</p>
  <label>Your name (shown in the logs)</label>
  <input id="name" autocomplete="username" placeholder="host" required>
  <label>Password</label>
  <input id="pw" type="password" autocomplete="current-password" placeholder="password" required>
  <button id="go" type="submit">Sign in</button>
  <div class="err" id="err"></div>
</form>
<script>
const f=document.getElementById('f'),err=document.getElementById('err'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{
  e.preventDefault();err.textContent='';go.disabled=true;go.textContent='Checking…';
  try{
    const r=await fetch('/editor/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('name').value,password:document.getElementById('pw').value})});
    const j=await r.json();
    if(r.ok&&j.ok){location.href='/editor/';return;}
    err.textContent=j.error||'Sign in failed.';
  }catch(_){err.textContent='Network error.';}
  go.disabled=false;go.textContent='Sign in';
});
</script></body></html>"""


# ─── Photon bot (owner only) ─────────────────────────────────────────
# The YeeperBot joins a Yeeps room as a normal Photon actor, so its room and the
# room's live message feed can be watched from the editor. All of the work lives
# in services/bot_manager.py; pyphotonrealtime is optional, so these routes just
# report why the bot is unavailable when the package is missing.
_bot_cache = {"t": 0.0, "data": None}

@router.get("/api/bot")
async def bot_status(request: Request):
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    from services.bot_manager import manager
    now = time.time()
    if _bot_cache["data"] is not None and now - _bot_cache["t"] < 0.5:
        return JSONResponse(content=_bot_cache["data"])
    data = {"ok": True, **manager.status(),
            "events": manager.events()[-80:]}
    _bot_cache["t"] = now
    _bot_cache["data"] = data
    return JSONResponse(content=data)


@router.post("/api/bot/start")
async def bot_start(request: Request):
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    from services.bot_manager import manager
    name = str((body or {}).get("name") or "")[:24]
    room = str((body or {}).get("room") or "")[:64]
    spawn = bool((body or {}).get("spawn", True))
    return JSONResponse(content={"ok": True, **manager.start(name=name, room=room, spawn=spawn)})


@router.post("/api/bot/stop")
async def bot_stop(request: Request):
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    from services.bot_manager import manager
    return JSONResponse(content={"ok": True, **manager.stop()})


@router.post("/api/bot/room")
async def bot_room(request: Request):
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    from services.bot_manager import manager
    return JSONResponse(content={"ok": True,
                                 **manager.set_room(str((body or {}).get("room") or "")[:64])})


@router.post("/api/bot/raise")
async def bot_raise(request: Request):
    """Send one in-world message (event 200, envelope {0,2,5,4}) - for testing
    a message type against the live game."""
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    from services.bot_manager import manager
    try:
        msg_type = int((body or {}).get("type"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="type (int) is required")
    return JSONResponse(content={"ok": True,
                                 **manager.raise_game(msg_type, (body or {}).get("payload") or [])})


@router.post("/api/bot/spawn")
async def bot_spawn(request: Request):
    """Announce the bot as a visible player in its room (Photon Instantiate
    202 of Photon/NetworkMessenger). The game only draws an avatar for a player
    it has received this event for, so without it the bot is invisible."""
    if not _authed_name(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    from services.bot_manager import manager
    return JSONResponse(content={"ok": True, **manager.spawn_player()})



# ─── EXTRAS-PATCH-v1: community worlds + what the server has no handler for ──
@router.get("/api/worlds")
async def worlds_get():
    from handlers import extras as x
    return JSONResponse(content={"ok": True, "enabled": x.community_on(), "worlds": await x.list_worlds()})


@router.post("/api/worlds")
async def worlds_create(request: Request):
    from handlers import extras as x
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    ok, msg, doc = await x.create_world(body.get("name"), str(body.get("adminAccountID") or "").strip(),
                                        body.get("description", ""), bool(body.get("featured")))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return JSONResponse(content={"ok": True, "world": doc})


@router.post("/api/worlds/{name}/feature")
async def worlds_feature(name: str, request: Request):
    from handlers import extras as x
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not await x.set_featured(name, bool(body.get("featured"))):
        raise HTTPException(status_code=404, detail="No such world")
    return JSONResponse(content={"ok": True})


@router.delete("/api/worlds/{name}")
async def worlds_delete(name: str):
    from handlers import extras as x
    if not await x.delete_world(name):
        raise HTTPException(status_code=404, detail="No such world")
    return JSONResponse(content={"ok": True})


@router.get("/api/extras/calls")
async def extras_calls():
    from handlers import extras as x
    unknown = [r async for r in x.extra_unknown.find({}, {"_id": 0}).sort("lastAt", -1).limit(100)]
    recent = [r async for r in x.extra_calls.find({}, {"_id": 0}).sort("_id", -1).limit(30)]
    return JSONResponse(content={"ok": True, "unknown": unknown, "recent": recent})
# ─── end EXTRAS-PATCH-v1 ─────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def editor_login_page(request: Request):
    if await _usable_session(request):
        return RedirectResponse("./", status_code=302)
    return HTMLResponse(content=_LOGIN_HTML)


@router.post("/login")
async def editor_login(request: Request):
    """Kit sign-in: any name + the host password (EDITOR_PASSWORD in .env).
    Wrong passwords count toward the IP auto-ban (services/ip_bans.py)."""
    if not KIT_EDITOR_PASSWORD:
        return JSONResponse({"ok": False, "error": "Set EDITOR_PASSWORD in the server's .env file first, "
                                                   "then restart the server."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (str(body.get("name", "")).strip() or "host")[:40]
    pw = str(body.get("password", ""))
    from services.ip_bans import client_ip, record_failed_login, clear_failures
    ip = client_ip(request) or "?"
    info = _ip_info(ip)
    if not hmac.compare_digest(pw, KIT_EDITOR_PASSWORD):
        banned_now = await record_failed_login(ip)
        await _log_editor_ip("wrong-code", name, None, info, autoBanned=bool(banned_now))
        print(f"[editor] wrong password for {name!r} from {ip}")
        if banned_now:
            return JSONResponse({"ok": False, "error": "Too many failed attempts. Your IP has been banned."},
                                status_code=403)
        return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)
    clear_failures(ip)
    token = _sign(name, ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE, token, max_age=_TTL, httponly=True, samesite="lax")
    await _log_editor_ip("login", name, None, info, sessionHash=_session_hash(token), tier="owner")
    await _discord_notify(f"**{name}** logged into the editor [{ip}] ({_where(info)})")
    print(f"[editor] login OK for {name!r} from {ip}")
    return resp


@router.get("/logout")
async def editor_logout():
    resp = RedirectResponse("login", status_code=302)
    resp.delete_cookie(_COOKIE)
    return resp


_UI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "editor_ui.html")


_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
             "Pragma": "no-cache", "Expires": "0"}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def editor_ui(request: Request):
    if not await _usable_session(request):
        resp = HTMLResponse(content=_LOGIN_HTML, headers=_NO_CACHE)
        if _authed_name(request):
            resp.delete_cookie(_COOKIE)
        return resp
    with open(_UI_PATH, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers=_NO_CACHE)


if __name__ == "__main__":
    editor_app.include_router(router)
    uvicorn.run("routers.editor_router:editor_app", host="0.0.0.0", port=8001, reload=True)
