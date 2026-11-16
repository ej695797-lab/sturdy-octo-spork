
import os
import hmac
import re
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from services.db import accounts

router = APIRouter(prefix="/editor", tags=["player-editor"])
EDITOR_PASSWORD = os.getenv("EDITOR_PASSWORD", "")
EDITOR_SECRET = os.getenv("EDITOR_SECRET", "")

EDITABLE_FIELDS = {
    "displayName",
    "activeCosmetics",
    "currency",
    "beans",
    "roleKeys",
    "ownedCosmetics",
    "anonymous_displayName",
}

def require_editor(request: Request):
    if not EDITOR_PASSWORD or not EDITOR_SECRET:
        raise HTTPException(status_code=503, detail="Editor credentials are not configured")

    supplied = request.headers.get("x-editor-password", "")
    if not hmac.compare_digest(supplied, EDITOR_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid editor password")

def safe_limit(value: int) -> int:
    return max(1, min(value, 100))

@router.get("/players")
async def list_players(request: Request, q: str = "", limit: int = 50):
    require_editor(request)
    limit = safe_limit(limit)
    query = {}
    if q.strip():
        pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
        query = {"$or": [
            {"accountID": pattern},
            {"displayName": pattern},
        ]}

    cursor = accounts.find(
        query,
        {
            "_id": 0,
            "accountID": 1,
            "displayName": 1,
            "currency": 1,
            "beans": 1,
        },
    ).limit(limit)

    return JSONResponse(content=await cursor.to_list(length=limit))

@router.get("/players/{account_id}")
async def get_player(account_id: str, request: Request):
    require_editor(request)
    doc = await accounts.find_one(
    {"accountID": account_id},
    {
        "_id": 0,
        "accountID": 1,
        "displayName": 1,
        "currency": 1,
        "beans": 1,
        "activeCosmetics": 1,
        "roleKeys": 1,
        "ownedCosmetics": 1,
        "anonymous_displayName": 1
    }
)
    if doc is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return JSONResponse(content=doc)

@router.patch("/players/{account_id}")
async def update_player(account_id: str, request: Request):
    require_editor(request)

    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="Provide player fields to update")

    if "accountID" in patch or "_id" in patch:
        raise HTTPException(status_code=400, detail="Player identifiers cannot be changed")

    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"error": "Unsupported fields", "fields": sorted(unknown)},
        )

    if "displayName" in patch:
        name = patch["displayName"]
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="displayName must be non-empty text")
        patch["displayName"] = name.strip()

    for field in ("currency", "beans"):
        if field in patch:
            value = patch[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise HTTPException(status_code=400, detail=f"{field} must be a non-negative integer")

    result = await accounts.update_one(
        {"accountID": account_id},
        {"$set": patch},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Player not found")

    return JSONResponse(content={"ok": True, "updated": sorted(patch)})
