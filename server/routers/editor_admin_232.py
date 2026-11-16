import os
import uuid
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Body, Query, UploadFile, File, Cookie, Request
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from fastapi.encoders import jsonable_encoder
from services.db import accounts, room_maps, maps, db

announcements = db["editor_announcements"]
def get_announcement_videos():
    return AsyncIOMotorGridFSBucket(
        db, bucket_name="editor_announcement_videos"
    )
MAX_VIDEO_BYTES = 100 * 1024 * 1024

router = APIRouter(tags=["editor-admin"])

def _auth(x_editor_password: str | None = None, editor_pw: str | None = None):
    expected = os.getenv("EDITOR_PASSWORD", "")
    secret = os.getenv("EDITOR_SECRET", "")
    # Dev mode: if no password is configured, allow access (private server).
    if not expected and not secret:
        return
    supplied = x_editor_password or editor_pw or ""
    allowed = {p for p in (expected, secret) if p}
    if not supplied or supplied not in allowed:
        raise HTTPException(status_code=401, detail="Invalid editor password")


def _announce_auth(x_editor_password: str | None = None, editor_pw: str | None = None):
    """Separate auth for announcement endpoints: ONLY accepts ANNOUNCE_PASSWORD."""
    announce_pw = os.getenv("ANNOUNCE_PASSWORD", "")
    # Dev mode: if no announce password configured, fall back to editor password.
    if not announce_pw:
        return _auth(x_editor_password, editor_pw)
    supplied = x_editor_password or editor_pw or ""
    if not supplied or supplied != announce_pw:
        raise HTTPException(status_code=401, detail="Invalid announce password")

def _clean(doc: Any):
    if doc is None:
        return None
    if isinstance(doc, list):
        return [_clean(x) for x in doc]
    if isinstance(doc, dict):
        return jsonable_encoder({k: (str(v) if k == "_id" else _clean(v)) for k, v in doc.items()})
    if isinstance(doc, (str, int, float, bool)) or doc is None:
        return doc
    return str(doc)

async def _find_map(room_key: str):
    for collection in (room_maps, maps):
        doc = await collection.find_one({"roomKey": room_key})
        if doc is None:
            doc = await collection.find_one({"room_key": room_key})
        if doc is None:
            doc = await collection.find_one({"_id": room_key})
        if doc is not None:
            return collection, doc
    return None, None

@router.get("/login")
async def editor_login_page(request: Request):
    from fastapi.responses import HTMLResponse, RedirectResponse
    pw = request.cookies.get("editor_pw", "")
    expected = os.getenv("EDITOR_PASSWORD", "")
    if expected and pw == expected:
        return RedirectResponse("/editor-ui", status_code=302)
    return HTMLResponse(content="""<!doctype html><html><head><title>Editor login</title>
<style>body{font-family:system-ui;background:#111;color:#ddd;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#181818;padding:24px;border-radius:8px;border:1px solid #333;min-width:280px}
input{width:100%;padding:8px;margin:6px 0;background:#0c0c0c;border:1px solid #333;color:#ddd;border-radius:6px}
button{width:100%;padding:8px;background:#7c3aed;color:#fff;border:0;border-radius:6px;cursor:pointer;margin-top:8px}</style></head>
<body><form id=f><h2>Editor login</h2><input name=name placeholder="Name (optional)" value="host"><input name=password type=password placeholder="Password" autofocus><button>Sign in</button><p id=m style="color:#e66"></p></form>
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);const r=await fetch('/editor/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:fd.get('name'),password:fd.get('password')})});const d=await r.json();if(r.ok){location.href='/editor-ui'}else{document.getElementById('m').textContent=d.detail||'Wrong password'}}</script></body></html>""")


@router.post("/login")
async def editor_login(request: Request):
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    expected = os.getenv("EDITOR_PASSWORD", "")
    secret = os.getenv("EDITOR_SECRET", "")
    pw = str(body.get("password", ""))
    allowed = {p for p in (expected, secret) if p}
    if allowed and pw not in allowed:
        raise HTTPException(status_code=401, detail="Wrong password.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("editor_pw", pw, max_age=86400, httponly=True, samesite="lax")
    return resp


@router.get("/me")
async def editor_me(x_editor_password: str | None = Header(default=None), editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    from config import DB_NAME
    return {"name": "admin", "tier": "admin", "db": DB_NAME}


@router.get("/players")
async def list_players(q: str = Query(default=""), limit: int = Query(default=100, ge=1, le=500),
                       x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    filt = {}
    if q:
        rx = {"$regex": __import__("re").escape(q.strip().lstrip("@")), "$options": "i"}
        filt = {"$or": [
            {"accountID": rx},
            {"displayName": rx},
            {"discordUsername": rx},
            {"discordID": rx},
            {"oculusID": rx},
        ]}
    cursor = accounts.find(filt, {"_id": 0, "accountID": 1, "displayName": 1, "currency": 1,
                                  "beans": 1, "adminBlocked": 1}).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_clean(d) for d in docs]


@router.get("/players/{account_id}")
async def get_player(account_id: str, x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    if doc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return _clean(doc)


@router.patch("/players/{account_id}")
async def update_player(account_id: str, payload: Any = Body(default={}),
                       x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch = dict(payload)
    patch.pop("accountID", None)
    patch.pop("_id", None)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    result = await accounts.update_one({"accountID": account_id}, {"$set": patch})
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Account not found")
    doc = await accounts.find_one({"accountID": account_id}, {"_id": 0})
    return _clean(doc)


@router.post("/players/{account_id}/ban")
async def ban_player(account_id: str, payload: dict = Body(default={}), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    from datetime import datetime, timedelta, timezone
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1, "deviceKeys": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    who = "host"
    try:
        dur_h = float(payload.get("durationHours") or 0)
    except Exception:
        dur_h = 0.0

    if dur_h > 0:
        expire = (datetime.now(timezone.utc) + timedelta(hours=dur_h)).strftime("%Y %m/%d %H:%M:%S")
        reason = f"Banned by {who} via editor ({dur_h}h)"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 0,
                                            "isMutebanned": 0, "isIsolated": 0,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": expire,
                                            "shouldForceRefresh": 1}})
        ips = []
    else:
        expire = ""
        reason = f"Banned by {who} via editor (permanent)"
        await accounts.update_one({"accountID": account_id},
                                  {"$set": {"adminBlocked": 1, "isPermabanned": 1,
                                            "blockReason": reason, "banReason": reason,
                                            "banCategory": "blocked",
                                            "banExpireTimestamp": "",
                                            "shouldForceRefresh": 1}})
        ips = sorted(set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))))
        try:
            from services.ip_bans import ban_ip
            for ip in ips:
                await ban_ip(ip, f"account {account_id} banned by {who}")
        except Exception:
            pass
        try:
            from services.device_bans import ban_devices
            await ban_devices(doc.get("deviceKeys") or [], f"account {account_id} banned by {who}")
        except Exception:
            pass

    shown = ips
    return {"ok": True, "accountID": account_id, "adminBlocked": 1,
            "bannedIPs": shown, "until": expire,
            "permanent": not expire}

@router.post("/players/{account_id}/unban")
async def unban_player(account_id: str, payload: dict = Body(default={}), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    doc = await accounts.find_one({"accountID": account_id},
                                  {"_id": 0, "displayName": 1, "lastIP": 1, "recentIPs": 1, "deviceKeys": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    await accounts.update_one({"accountID": account_id},
                              {"$set": {"adminBlocked": 0, "isPermabanned": 0,
                                        "isMutebanned": 0, "isIsolated": 0,
                                        "blockReason": "", "banReason": "",
                                        "banExpireTimestamp": "", "banCategory": "",
                                        "shadowBanned": 0, "shouldForceRefresh": 1}})
    try:
        from services.device_bans import unban_devices
        await unban_devices(doc.get("deviceKeys") or [])
    except Exception:
        pass
    unbanned = []
    if payload.get("alsoIP"):
        try:
            from services.ip_bans import unban_ip
            for ip in set(filter(None, [doc.get("lastIP")] + list(doc.get("recentIPs") or []))):
                if await unban_ip(ip):
                    unbanned.append(ip)
        except Exception:
            pass
    return {"ok": True, "accountID": account_id, "adminBlocked": 0, "unbannedIPs": unbanned}

@router.delete("/players/{account_id}")
async def delete_player(account_id: str, x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    result = await accounts.delete_one({"accountID": account_id})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True, "accountID": account_id, "deleted": True, "note": "Only the accounts collection record was deleted; related records were not automatically removed."}

@router.get("/maps")
async def list_maps(x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None), limit: int = Query(default=1000, ge=1, le=5000)):
    _auth(x_editor_password, editor_pw)
    out, seen = [], set()
    for collection, label in ((room_maps, "room_maps"), (maps, "maps")):
        cursor = collection.find({}).limit(limit)
        async for doc in cursor:
            key = str(doc.get("roomKey") or doc.get("room_key") or doc.get("_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            blocks = doc.get("blocks")
            out.append({"roomKey": key, "source": label, "blockCount": len(blocks) if isinstance(blocks, list) else doc.get("blockCount"), "bytes": doc.get("bytes")})
    out.sort(key=lambda x: x["roomKey"].lower())
    return out

@router.get("/maps/{room_key}")
async def get_map(room_key: str, x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    _, doc = await _find_map(room_key)
    if doc is None:
        raise HTTPException(status_code=404, detail="Map not found")
    return _clean(doc)

@router.put("/maps/{room_key}")
async def save_map(room_key: str, payload: Any = Body(...), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    from services.mapsync_serializer import extract_blocks, serialize_blocks_compressed
    if isinstance(payload, list):
        blocks = payload
        doc = {"roomKey": room_key, "blocks": blocks}
    elif isinstance(payload, dict):
        try:
            blocks = extract_blocks(payload)
        except Exception:
            blocks = payload.get("blocks") if isinstance(payload.get("blocks"), list) else None
        doc = dict(payload)
        doc.pop("_id", None)
        doc["roomKey"] = room_key
        if "blocks" not in doc and isinstance(doc.get("map"), list):
            doc["blocks"] = doc.pop("map")
        if blocks is None:
            blocks = doc.get("blocks") if isinstance(doc.get("blocks"), list) else []
        doc["blocks"] = blocks
    else:
        raise HTTPException(status_code=400, detail="Map must be a JSON object or blocks array")
    try:
        save_data = serialize_blocks_compressed(blocks or [])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not serialize map: {e}")
    import time as _t
    sid = int(_t.time())
    doc["saveData"] = save_data
    doc["mapDataVersion"] = 2
    doc["applicationVersion"] = "2.32.0"
    doc["savedAt"] = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    doc["savedBy"] = "editor"
    try:
        from services.db import rooms as rooms_col
        await rooms_col.update_one({"roomKey": room_key},
                                  {"$set": {"lastEditedSessionID": sid, "roomDataVersion": 3}},
                                  upsert=True)
    except Exception:
        pass
    collection, existing = await _find_map(room_key)
    if collection is None:
        collection = room_maps
        await collection.insert_one(doc)
    else:
        query = {"_id": existing["_id"]} if "_id" in existing else {"roomKey": room_key}
        await collection.replace_one(query, doc, upsert=True)
    # Also write to maps collection so the client loads it.
    try:
        from services.db import maps as maps_col
        await maps_col.update_one({"mapKey": f"{room_key}$current"}, {"$set": {
            "mapKey": f"{room_key}$current", "roomKey": room_key,
            "saveData": save_data, "editorBlocks": blocks,
            "mapDataVersion": 2, "savedBy": "editor",
            "applicationVersion": "2.32.0",
            "savedAt": datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S"),
            "saveInternalStates": "",
        }}, upsert=True)
    except Exception as e:
        print(f"[editor] map->live write failed for {room_key}: {e}")
    return {"ok": True, "roomKey": room_key, "blockCount": len(blocks) if isinstance(blocks, list) else None}

@router.delete("/maps/{room_key}")
async def reset_map(room_key: str, x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    deleted = 0
    for collection in (room_maps, maps):
        for query in ({"roomKey": room_key}, {"room_key": room_key}, {"_id": room_key}):
            result = await collection.delete_many(query)
            deleted += result.deleted_count
    return {"ok": True, "roomKey": room_key, "deleted": deleted}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()

def _announcement_public(doc: dict):
    return {
        "id": str(doc.get("id", doc.get("_id", ""))),
        "type": doc.get("type", "text"),
        "title": doc.get("title", ""),
        "message": doc.get("message", ""),
        "videoUrl": doc.get("videoUrl", ""),
        "enabled": bool(doc.get("enabled", True)),
        "priority": int(doc.get("priority", 0) or 0),
        "expiresAt": doc.get("expiresAt", ""),
        "createdAt": doc.get("createdAt", ""),
        "updatedAt": doc.get("updatedAt", ""),
    }

def _validate_announcement(payload: Any, existing: dict | None = None):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Announcement must be a JSON object")
    base = dict(existing or {})
    base.update(payload)
    kind = str(base.get("type", "text")).lower().strip()
    if kind not in ("text", "video"):
        raise HTTPException(status_code=400, detail="type must be 'text' or 'video'")
    title = str(base.get("title", "")).strip()[:160]
    message = str(base.get("message", "")).strip()[:5000]
    video_url = str(base.get("videoUrl", "")).strip()[:2000]
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if kind == "text" and not message:
        raise HTTPException(status_code=400, detail="Message is required for text announcements")
    if kind == "video":
        from urllib.parse import urlparse
        parsed = urlparse(video_url)
        uploaded_path = video_url.startswith("/editor/announcements/video/") and len(video_url.rsplit("/", 1)[-1]) == 24
        if not uploaded_path and (parsed.scheme != "https" or not parsed.netloc):
            raise HTTPException(status_code=400, detail="Video must be an uploaded file or a valid HTTPS URL")
    expires = str(base.get("expiresAt", "")).strip()[:40]
    if expires:
        try:
            datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="expiresAt must be an ISO date/time or empty")
    try:
        priority = int(base.get("priority", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="priority must be an integer")
    return {
        "type": kind, "title": title, "message": message, "videoUrl": video_url,
        "enabled": bool(base.get("enabled", True)), "priority": max(-1000, min(priority, 1000)),
        "expiresAt": expires,
    }

def _is_current(doc: dict, now: datetime):
    if not doc.get("enabled", True):
        return False
    expires = str(doc.get("expiresAt", "")).strip()
    if not expires:
        return True
    try:
        dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > now
    except ValueError:
        return False


@router.post("/announcements/upload-video")
async def upload_announcement_video(file: UploadFile = File(...), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    """Upload an actual video file into MongoDB GridFS so it survives app restarts."""
    _announce_auth(x_editor_password, editor_pw)
    from pathlib import Path
    allowed = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}
    filename = Path(file.filename or "video.mp4").name
    ext = Path(filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported video type. Upload MP4, WebM, MOV or M4V.")
    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith("video/") and content_type != "application/octet-stream":
        raise HTTPException(status_code=400, detail="The uploaded file does not look like a video.")
    stream = await announcement_videos.open_upload_stream(
        filename, metadata={"contentType": allowed[ext], "uploadedAt": _utcnow()}
    )
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise HTTPException(status_code=413, detail="Video is too large. Maximum upload size is 100 MB.")
            await stream.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The uploaded video is empty.")
        await stream.close()
    except Exception:
        try:
            await stream.abort()
        except Exception:
            pass
        raise
    finally:
        await file.close()
    return {"ok": True, "fileId": str(stream._id), "filename": filename, "size": total, "videoUrl": f"/editor/announcements/video/{stream._id}"}

@router.get("/announcements/video/{file_id}")
async def stream_announcement_video(file_id: str):
    """Public video stream used by game clients and the editor preview."""
    from bson import ObjectId
    try:
        oid = ObjectId(file_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        stream = await announcement_videos.open_download_stream(oid)
    except Exception:
        raise HTTPException(status_code=404, detail="Video not found")
    content_type = (stream.metadata or {}).get("contentType", "video/mp4")
    async def chunks():
        try:
            while True:
                chunk = await stream.readchunk()
                if not chunk:
                    break
                yield chunk
        finally:
            stream.close()
    return StreamingResponse(chunks(), media_type=content_type, headers={"Accept-Ranges": "none", "Cache-Control": "public, max-age=3600"})

@router.get("/announcements/public")
async def public_announcements():
    """Public, read-only feed intended for game clients; only published, unexpired items."""
    now = datetime.now(timezone.utc)
    out = []
    async for doc in announcements.find({}).limit(1000):
        if _is_current(doc, now):
            out.append(_announcement_public(doc))
    out.sort(key=lambda item: (-item["priority"], item["createdAt"] or ""))
    return {"announcements": out, "serverTime": now.isoformat()}

@router.get("/announcements")
async def list_announcements(x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None), limit: int = Query(default=500, ge=1, le=1000)):
    _announce_auth(x_editor_password, editor_pw)
    out = []
    async for doc in announcements.find({}).limit(limit):
        out.append(_announcement_public(doc))
    out.sort(key=lambda item: (-item["priority"], item["createdAt"] or ""))
    return out

@router.post("/announcements", status_code=201)
async def create_announcement(payload: Any = Body(...), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _announce_auth(x_editor_password, editor_pw)
    clean = _validate_announcement(payload)
    now = _utcnow()
    doc = {"id": uuid.uuid4().hex, **clean, "createdAt": now, "updatedAt": now}
    await announcements.insert_one(doc)
    return {"ok": True, "announcement": _announcement_public(doc)}

@router.put("/announcements/{announcement_id}")
async def update_announcement(announcement_id: str, payload: Any = Body(...), x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _auth(x_editor_password, editor_pw)
    existing = await announcements.find_one({"id": announcement_id})
    if existing is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    clean = _validate_announcement(payload, existing)
    clean["updatedAt"] = _utcnow()
    await announcements.update_one({"_id": existing["_id"]}, {"$set": clean})
    updated = await announcements.find_one({"_id": existing["_id"]})
    return {"ok": True, "announcement": _announcement_public(updated or {**existing, **clean})}

@router.delete("/announcements/{announcement_id}")
async def delete_announcement(announcement_id: str, x_editor_password: str | None = Header(default=None),
                       editor_pw: str | None = Cookie(default=None)):
    _announce_auth(x_editor_password, editor_pw)
    result = await announcements.delete_one({"id": announcement_id})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return {"ok": True, "id": announcement_id, "deleted": True}
