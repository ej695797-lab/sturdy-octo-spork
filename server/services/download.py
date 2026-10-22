import os
import time

from services.db import globals_col

DIR = os.path.expanduser(os.getenv("DOWNLOAD_DIR", "data/download"))
KEY = "download_file"
MAX_BYTES = 700 * 1024 * 1024


def safe_name(name: str) -> str:
    name = os.path.basename((name or "").strip()) or "yeeps.apk"
    keep = "".join(c for c in name if c.isalnum() or c in "._- ")
    return (keep.strip() or "yeeps.apk")[:120]


def path_for(meta: dict | None) -> str | None:
    name = (meta or {}).get("filename")
    if not name:
        return None
    p = os.path.join(DIR, safe_name(name))
    return p if os.path.isfile(p) else None


async def get_meta() -> dict:
    d = await globals_col.find_one({"key": KEY}, {"_id": 0, "data": 1})
    return (d or {}).get("data") or {}


async def _save_meta(meta: dict) -> dict:
    await globals_col.update_one({"key": KEY}, {"$set": {"data": meta}}, upsert=True)
    return meta


async def store_stream(chunks, filename: str, by: str = "") -> dict:
    os.makedirs(DIR, exist_ok=True)
    name = safe_name(filename)
    tmp = os.path.join(DIR, ".upload.part")
    n = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in chunks:
                n += len(chunk)
                if n > MAX_BYTES:
                    raise ValueError(f"file too big (max {MAX_BYTES // (1024 * 1024)} MB)")
                f.write(chunk)
        if n < 1024:
            raise ValueError("empty upload")
        dst = os.path.join(DIR, name)
        os.replace(tmp, dst)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    old = await get_meta()
    oldp = path_for(old) if old.get("filename") != name else None
    if oldp and os.path.abspath(oldp) != os.path.abspath(dst):
        try:
            os.remove(oldp)
        except OSError:
            pass
    return await _save_meta({"filename": name, "size": n, "ts": int(time.time()),
                             "by": by, "url": ""})


async def set_url(url: str) -> dict:
    meta = await get_meta()
    meta["url"] = (url or "").strip()
    return await _save_meta(meta)


async def clear() -> bool:
    meta = await get_meta()
    p = path_for(meta)
    if p:
        try:
            os.remove(p)
        except OSError:
            pass
    await globals_col.delete_one({"key": KEY})
    return bool(p)


async def status() -> dict:
    meta = await get_meta()
    p = path_for(meta)
    return {"filename": meta.get("filename") or "", "size": int(meta.get("size") or 0),
            "ts": int(meta.get("ts") or 0), "by": meta.get("by") or "",
            "url": meta.get("url") or "", "present": bool(p)}
