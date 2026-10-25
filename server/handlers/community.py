"""
Community world handlers:
  - g2_cwGetCanEnter
  - g2_cwTrackVisit
  - g2_cwTryPurchaseFuel
  - g2_cwEditPermissions
"""

from services.db import room_maps


async def handle_cw_get_can_enter(body: dict) -> dict:
    # Community worlds are off in the self-host kit (the owner's server mirrored
    # them live from real Yeeps). Every world name is refused.
    return {"canEnter": 1, "reason": "Community worlds are on this server."}


async def handle_cw_track_visit(body: dict) -> dict:
    # Community worlds are off in the kit; nothing to record.
    return {"success": 1}


async def handle_cw_try_purchase_fuel(body: dict) -> dict:
    return {
        "success": 1,
        "newCurrency": 1,
        "fuelExpireTimestamp": "3019 01/09 16:15:20",
        "cost": 1,
    }


async def handle_cw_edit_permissions(body: dict) -> dict:
    # Permissions are not stored: community worlds are read live from Yeeps (owner 2026-10-11).
    return {"success": 1}

# ─── EXTRAS-PATCH-v1 ────────────────────────────────────────────────────────
from handlers import extras as _x


async def handle_cw_get_can_enter(body: dict) -> dict:
    return await _x.cw_get_can_enter(body)


async def handle_cw_track_visit(body: dict) -> dict:
    return await _x.cw_track_visit(body)


async def handle_cw_try_purchase_fuel(body: dict) -> dict:
    return await _x.cw_try_purchase_fuel(body)


async def handle_cw_edit_permissions(body: dict) -> dict:
    return await _x.cw_edit_permissions(body)
