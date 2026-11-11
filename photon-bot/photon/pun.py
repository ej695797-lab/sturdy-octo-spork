"""PUN 2.45 layer on top of Photon: Unity custom types, RPCs, instantiation and
view-serialization events, decoded the way the game sends them."""
import struct
from dataclasses import dataclass

from .protocol16 import Byte, Short, register_custom

# --- PUN custom types (PhotonPeer.RegisterType in PUN's CustomTypes.cs) -------


@dataclass
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, o): return Vector3(self.x + o.x, self.y + o.y, self.z + o.z)
    def __sub__(self, o): return Vector3(self.x - o.x, self.y - o.y, self.z - o.z)
    def __mul__(self, k): return Vector3(self.x * k, self.y * k, self.z * k)
    def __repr__(self): return f"V3({self.x:.2f}, {self.y:.2f}, {self.z:.2f})"


@dataclass
class Vector2:
    x: float = 0.0
    y: float = 0.0
    def __repr__(self): return f"V2({self.x:.2f}, {self.y:.2f})"


@dataclass
class Quaternion:
    w: float = 1.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    def __repr__(self): return f"Q({self.w:.3f}, {self.x:.3f}, {self.y:.3f}, {self.z:.3f})"


@dataclass
class Player:
    actor: int
    def __repr__(self): return f"Player#{self.actor}"


register_custom(ord("V"), Vector3, lambda v: struct.pack(">fff", v.x, v.y, v.z),
                lambda b: Vector3(*struct.unpack(">fff", b)))
register_custom(ord("W"), Vector2, lambda v: struct.pack(">ff", v.x, v.y),
                lambda b: Vector2(*struct.unpack(">ff", b)))
register_custom(ord("Q"), Quaternion, lambda q: struct.pack(">ffff", q.w, q.x, q.y, q.z),
                lambda b: Quaternion(*struct.unpack(">ffff", b)))
register_custom(ord("P"), Player, lambda p: struct.pack(">i", p.actor),
                lambda b: Player(struct.unpack(">i", b)[0]))

# --- PUN event codes ----------------------------------------------------------
EV_RPC, EV_SEND_SERIALIZE, EV_INSTANTIATE, EV_CLOSE_CONNECTION = 200, 201, 202, 203
EV_DESTROY, EV_DESTROY_PLAYER, EV_SEND_SERIALIZE_RELIABLE, EV_OWNERSHIP_REQUEST = 204, 207, 206, 209
EV_OWNERSHIP_TRANSFER, EV_VACANT_VIEW_IDS, EV_OWNERSHIP_UPDATE = 210, 211, 212
# Photon (LoadBalancing) event codes
EV_JOIN, EV_LEAVE, EV_PROPS_CHANGED = 255, 254, 253

PUN_EVENT_NAMES = {200: "RPC", 201: "Serialize", 202: "Instantiate", 203: "CloseConnection",
                   204: "Destroy", 206: "SerializeReliable", 207: "DestroyPlayer",
                   209: "OwnershipRequest", 210: "OwnershipTransfer", 211: "VacantViewIds",
                   212: "OwnershipUpdate", 255: "Join", 254: "Leave", 253: "PropertiesChanged"}

# RpcList from the game's PhotonServerSettings (data.unity3d PathID 99012), in
# order: PUN sends a known RPC as its index (key 5) instead of the name (key 3).
RPC_LIST = """CantHearYou ChangeColour ClickRpc DestroyRpc HeresWhy RpcAddFootprint
RpcApplyPlayerKnockback RpcApplyStatusEffectToPlayer RpcBanAccount RpcBeginParty RpcCallVote
RpcCastVote RpcChangeFloorDepth RpcChangeMapObjectColor RpcClearStatusEffectOnPlayer
RpcCreateExplosion RpcCreateHitEffect RpcCreateItem RpcCreateProjectile RpcDestroyItem
RpcDestroyMapObject RpcDestroyProjectile RpcDoBundlePurchaseCelebration RpcDoHiderHint
RpcDoTransferAnimation RpcExecuteKickPlayer RpcItemTransformation RpcMakeSeekerForGameBalance
RpcMapAutosaveSucceeded RpcMapObjectCreated RpcMapObjectPreviewCreated RpcMapObjectPreviewDestroyed
RpcMapObjectPreviewHidden RpcMapObjectPreviewUpdated RpcMapSync RpcMutedUserSync
RpcOnPlayerCollidedWithMapObject RpcPlaySoundEffect RpcPreviewBundle RpcRefreshRoomMap
RpcSeekerCaughtHider RpcSetActiveCamera RpcSetupNewRound RpcSimpleCustomItemMessage
RpcSimpleCustomMapObjectMessage RpcTransferItemOwnership RpcUncoverBuyAllButton
RpcUpdatePrivateRoomPermissions RpcWarnAccount RpcTemporaryFriendRequest
RpcTemporaryFriendRequestResponse RpcStartSlidingSound RpcStopSlidingSound RpcAddColoredFootprint
RpcPositionCustomItemMessage RpcMultiVectorCustomItemMessage RpcSetZipliningSoundLevel
RpcStopZipliningSound RpcPlaySingletonSoundEffect RpcCreateConfettiEffect
RpcMultiVectorCustomMapObjectMessage RpcVectorCustomMapObjectMessage RpcCreateSplash
RpcSetLoopingSoundLevel RpcStopLoopingSound RpcChangeLiquid RpcChangeItemRestrictions
RpcMapObjectInternalStatesSync RpcRequestMapObjectInternalStateSync RpcDoHiderDetection
RpcModifyStaff RpcEnterStaffCreativeMode RpcWorldFuelUpdated RpcUpdateCWPermissions
RpcCreateLoseStuffingEffect RpcMobileCommunityWorldUpdated RpcMobilePatternsUpdated
RpcMobilePromoCodeUpdated RpcMobileTargetPublicWorldUpdated RpcDealHealthDamage
RpcDoGhostFlatApperance RpcRecallTeleporterDistribution RpcUpdateCustomParameters
RpcEndCurrentRound RpcPlayerEscapedWorld RpcRCToyPoseMessage RpcMobileWishlistFinalized
RpcMobileWishlistUpdated RpcBalanceTeams RpcVoteFailedToBalanceTeams RpcRoomNameChange
RpcThemeSwitch RpcLobbyDirectionChange RpcDeleteRoom RpcInitiateAdminTransfer
RpcAdminTransferOfferResponse RpcCreateProjectileSpread RpcModifyCWStaffPermissions
RpcModifyCWStaffEarnings RpcMobileWorldMapScrollEvent RpcEntityPoseMessage
RpcSimpleFloatCustomItemMessage RpcMobileCashOut RpcMapObjectScannerBoxUpdateTarget
RpcMapObjectScannerBoxHide RpcPlayerSaddleClear RpcMobileAchievementsRedeem
RpcMobileAchievementsRefresh RpcMobileAchievementsPageUpdated RpcApplyMobKnockbackFromBlockOrigin
RpcApplyMobKnockbackFromPlayerOrigin RpcApplyMobSetPose RpcApplyMobStatusEffectFromBlockOrigin
RpcApplyMobStatusEffectFromPlayerOrigin RpcCreateConfettiEffectFromBlockOrigin
RpcCreateConfettiEffectFromPlayerOrigin RpcCreateExplosionWithBlockOrigin
RpcCreateExplosionWithPlayerOrigin RpcCreateMob RpcCreateProjectileFromBlockOrigin
RpcCreateProjectileFromPlayerOrigin RpcCreateProjectileSpreadFromBlockOrigin
RpcCreateProjectileSpreadFromPlayerOrigin RpcDestroyMob RpcDoMobAction RpcDoMobHop
RpcFloatCustomMobMessage RpcReceiveStuffingToUser RpcSelectedInternalStatesDirect
RpcSimpleCustomMobMessage RpcUnownedMobSyncComplete RpcClearMobs""".split()
assert len(RPC_LIST) == 132, len(RPC_LIST)
RPC_INDEX = {n: i for i, n in enumerate(RPC_LIST)}


def set_rpc_list(names):
    """Use another game version's RpcList (the list above is 1.31's). The live
    2.31.4 list (247) differs from 1.31's at index 17 on; 2.32's (249) is the
    2.31.4 list + 2 appended, so data/rpc_list_live.json (2.32) serves both.
    Updated in place so every importer sees it."""
    RPC_LIST[:] = list(names)
    RPC_INDEX.clear()
    RPC_INDEX.update({n: i for i, n in enumerate(RPC_LIST)})

# RaiseEvent receiver groups / caching
RECV_OTHERS, RECV_ALL, RECV_MASTER = 0, 1, 2


def decode_rpc(data):
    """PUN RPC event payload (Hashtable, byte keys) -> dict."""
    if not isinstance(data, dict):
        return None
    idx = data.get(5)
    name = data.get(3) or (RPC_LIST[idx] if isinstance(idx, int) and idx < len(RPC_LIST) else f"#{idx}")
    return {"view": data.get(0), "prefix": data.get(1), "time": data.get(2),
            "method": name, "args": data.get(4) or []}


def encode_rpc(view_id, method, args, server_time):
    ts = ((int(server_time) + 2**31) % 2**32) - 2**31          # signed int32, wraps
    h = {Byte(0): int(view_id), Byte(2): ts}
    if RPC_INDEX.get(method, 999) < 255:
        h[Byte(5)] = Byte(RPC_INDEX[method])
    else:
        h[Byte(3)] = method
    if args:
        h[Byte(4)] = list(args)
    return h


def decode_instantiate(data):
    if not isinstance(data, dict):
        return None
    return {"prefab": data.get(0), "pos": data.get(1), "rot": data.get(2), "group": data.get(3),
            "views": data.get(4), "data": data.get(5), "time": data.get(6), "id": data.get(7),
            "prefix": data.get(8)}
