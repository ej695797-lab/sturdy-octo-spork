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
    return {"canEnter": 0, "reason": "Community worlds are off on this server."}


async def handle_cw_track_visit(body: dict) -> dict:
    # Community worlds are off in the kit; nothing to record.
    return {"success": 1}


async def handle_cw_try_purchase_fuel(body: dict) -> dict:
    return {
        "success": 1,
        "newCurrency": 0,
        "fuelExpireTimestamp": "2099 01/01 00:00:00",
        "cost": 0,
    }


async def handle_cw_edit_permissions(body: dict) -> dict:
    # Permissions are not stored: community worlds are read live from Yeeps (owner 2026-10-11).
    return {"success": 1}
