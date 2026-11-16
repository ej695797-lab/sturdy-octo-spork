"""Defaults for the inspected live79 client; legacy values stay selectable.

Recovered property names are documented in docs/apk-inspection. Defaults for
newer optional features are conservative first-test values, not a claim that
those features have been verified on a headset.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from config import IS_BUDDY

_ACCOUNT = {
    "customizedItemDatas": [], "currency_ciPending": 0, "completedPuzzles": [],
    "wishlistWinner_loginMessage": "", "wishlistWinner_claimMessage": "",
    "lastChallengeRedeemedTime": "1970 01/01 00:00:00",
    "longestLoginStreakData": {"length": 0, "currentTier": "", "nextTierThreshold": 0, "nextTier": ""},
    "longestLoginStreakRestoreCost": 0, "lootQuotaProgress": {},
    "collectedCardShardIDs": [], "collectedButtonRewardKeys": [],
    "featureGrants": [], "mobileLinkSessionID": "", "dataSegment": "base",
    "beanTradingCardPackRefStartTime": "2020 01/01 00:00:00",
    "challengeNonces": [],
}
_ROOM = {
    "visitVersion": 1, "teamItemRestrictions": {}, "gravityType": 0,
    "noCopy": 0, "noPaste": 0, "isFreebuild": 0, "isSolo": 0,
    "overrideMaxPlayers": 0, "mapDescription": "", "warpPortalDescription": "",
    "thumbnail_s3_uri": "", "passagewayLinks_creative": [],
    "overrideTokenUniqueID": "", "lastValidSaveOffset": 0,
    "lastEditedTerrainSessionID": 0, "previousEditedTerrainSessionID": 0,
    "terrainDataVersion": 1, "isNoTerrain": 0, "isNoFloor": 0,
    "overrideExpireDurationMinutes": 0, "overrideSpawnPosition": [0, 0, 0],
}


def account_defaults() -> dict:
    return deepcopy(_ACCOUNT) if IS_BUDDY else {}


def account_view(doc: dict) -> dict:
    out = {**account_defaults(), **doc}
    # Earlier Buddy seeds used an empty object. live79 calls AsArray on this
    # field; normalize only that empty placeholder, preserving real item data.
    if IS_BUDDY and out.get("customizedItemDatas") == {}:
        out["customizedItemDatas"] = []
    if IS_BUDDY:
        # Ownership includes gadgets, cosmetics and other patterns, not only
        # MapObject resource keys. Preserve saved/custom unlocks and filters.
        from services.role_locks import ROLE_LOCKED_PATTERNS
        owned = list(out.get("ownedPatterns") or [])
        seen = set(owned)
        for key in pattern_catalog():
            if key not in seen and key not in ROLE_LOCKED_PATTERNS:
                owned.append(key)
                seen.add(key)
        out["ownedPatterns"] = owned
    return out


def room_defaults() -> dict:
    return deepcopy(_ROOM) if IS_BUDDY else {}


def property_default(name: str):
    """(found, value), preserving the distinction between absent and empty."""
    if IS_BUDDY:
        for values in (_ACCOUNT, _ROOM):
            if name in values:
                return True, deepcopy(values[name])
    return False, None


@lru_cache(maxsize=1)
def block_catalog() -> tuple:
    path = Path(__file__).resolve().parents[1] / "seed/buddy232/mapobjects.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def pattern_catalog() -> tuple:
    """PatternManager.GetAllPatterns keys from the fingerprinted live79 APK."""
    path = Path(__file__).resolve().parents[1] / "seed/buddy232/patterns.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def seed_document(kind: str, doc: dict) -> dict:
    out = deepcopy(doc)
    if not IS_BUDDY:
        return out
    if kind == "rooms":
        return {**room_defaults(), **out}
    if kind == "globals" and out.get("key") == "valid_mapobject_keys":
        out["value"] = list(block_catalog())
    if kind == "globals" and out.get("key") == "default_account_override":
        from services.role_locks import ROLE_LOCKED_PATTERNS
        out["value"].update(account_defaults())
        out["value"]["ownedPatterns"] = [k for k in pattern_catalog() if k not in ROLE_LOCKED_PATTERNS]
        out["value"]["roleKeys"] = []
    return out
