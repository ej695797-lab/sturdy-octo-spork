"""YeeperBot's Photon command engine for the self-hosted Yeeps server.
The editor manages this process and sends only allowlisted actions.

  python3 bot.py listen [--target NAME|o_ID] [--room PHOTON_ROOM]
      join the target's room and print every RPC / spawn / join, decoded by name;
      everything is also written to logs/events-<time>.jsonl for analysis.

  python3 bot.py run [--target NAME|o_ID] [--look NAME]
      join the target's room AS A PLAYER (a Yeep others can see) and take
      commands typed into this window - type "help".
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from photon import pun
from photon.client import (NAME_SERVER, OP_AUTHENTICATE, OP_JOIN_ROOM, OP_RAISE_EVENT,
                           P_ACTOR_LIST, P_ACTOR_NR, P_ADDRESS, P_BROADCAST, P_CODE, P_DATA,
                           P_GAME_PROPS, P_PLAYER_PROPS, P_RECEIVERS, P_ROOM_NAME, P_TOKEN,
                           Connection, PhotonError, auth_params, check)
from photon.protocol16 import Byte, Custom, StringArray, TypedArray
import math
import random
import string

HERE = os.path.dirname(os.path.abspath(__file__))
VR_SERVER = os.path.abspath(os.path.join(HERE, ".."))
OWNER = ""


# A second bot for another game version (2.31.4) runs with its own env file:
#   YEEPS_BOT_ENV=~/.config/yeeps/bot-2314.env YEEPS_TALK_PORT=8792 python3 bot.py ...
# Extra keys it may set: DB_NAME (the server database to find players in),
# AUTH=userid (Photon custom auth with UserId/accountID instead of botKey),
# VOICE_APP / VOICE_VERSION / VOICE_ROOM (voice app id, its AppVersion, and the voice
# room name with {room} = the game room's name). Unset = the 1.31 values.
def load_env():
    import config
    env = {
        "APP_ID": os.getenv("PHOTON_APP_ID", ""),
        "APP_VERSION": os.getenv("PHOTON_APP_VERSION", "1.31.0_2.45"),
        "REGION": os.getenv("PHOTON_REGION", "us"),
        "BOT_KEY": os.getenv("BOT_KEY", config.BOT_KEY),
        "DB_NAME": os.getenv("DB_NAME", config.DB_NAME),
    }
    for key in ("RPC_LIST_FILE", "GAME_VERSIONS", "VOICE_APP", "VOICE_VERSION",
                "VOICE_ROOM", "VOICE_CANDIDATES"):
        if os.getenv(key) is not None:
            env[key] = os.environ[key]
    return env


_DB = None


def db():
    """One shared MongoDB client (a new client per call leaked ~10 connections and
    threads each time the room-follow check ran)."""
    global _DB
    if _DB is None:
        sys.path.insert(0, VR_SERVER)
        import pymongo, config
        _DB = pymongo.MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=15000,
                                  maxPoolSize=4)[load_env().get("DB_NAME") or config.DB_NAME]
    return _DB


def find_player(target):
    """NAME or o_ID -> (accountID, displayName, photonRoomName) of an online player."""
    d = db()
    q = {"accountID": target} if target.startswith("o_") else \
        {"displayName": {"$regex": f"^{target}$", "$options": "i"}}
    acc = d.accounts.find_one(q, {"_id": 0, "accountID": 1, "displayName": 1})
    if not acc:
        raise SystemExit(f"no account matching {target!r}")
    st = d.player_statuses.find_one({"accountID": acc["accountID"]}, {"_id": 0})
    now = datetime.now(timezone.utc).strftime("%Y %m/%d %H:%M:%S")
    if not st or (st.get("expireTimestamp") or "") < now or not st.get("photonRoomName"):
        raise SystemExit(f"{acc['displayName']} isn't online right now")
    return acc["accountID"], acc["displayName"], st["photonRoomName"], st.get("roomKey")


def typename(v):
    """Exact Photon type of a decoded value (for matching RPC signatures)."""
    from photon.protocol16 import Byte as B, Short as S, Long as L, Double as D, IntArray, StringArray as SA, TypedArray as TA
    for cls, n in ((bool, "bool"), (B, "byte"), (S, "short"), (L, "long"), (D, "double"),
                   (IntArray, "int[]"), (SA, "string[]")):
        if isinstance(v, cls):
            return n
    if isinstance(v, TA):
        return f"array<{chr(v.code)}>"
    if isinstance(v, int): return "int"
    if isinstance(v, float): return "float"
    if isinstance(v, list): return "object[]"
    if v is None: return "null"
    return type(v).__name__


def clean_name(n):
    """Display name without Unity rich-text tags (<color=..>, <b>, ...)."""
    import re
    return re.sub(r"<[^>]*>", "", str(n or "")).strip()


def find_player_soft(target):
    try:
        return find_player(target)
    except SystemExit:
        return None


def show(v, depth=0):
    """Compact readable form of decoded Photon values."""
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, Custom):
        return f"Custom({chr(v.code)},{len(v.data)}b)"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{show(k)}: {show(x, depth + 1)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        if len(v) > 12:
            return "[" + ", ".join(show(x, depth + 1) for x in v[:12]) + f", ...{len(v)}]"
        return "[" + ", ".join(show(x, depth + 1) for x in v) + "]"
    if isinstance(v, float):
        return f"{v:.3f}"
    return repr(v)


def map_sync_blob(args):
    """The block-map blob of an RpcMapSync: arg 7 on 1.31; on live 2.31.4/2.32 the
    args are reshaped (blocks = an uncompressed "SPAM\\x03" or a Brotli blob of
    one), so take the first bytes arg that is (or unpacks to) SPAM version 3."""
    cands = []
    if len(args) > 7 and isinstance(args[7], (bytes, bytearray)) and args[7]:
        cands.append(args[7])
    cands += [x for x in args if isinstance(x, (bytes, bytearray)) and x and x not in cands]
    # Prefer a NON-empty SPAM blob (a bare "SPAM"+ver+zero counts is an empty sync
    # that would wipe the map); 2.32.0_lab2 live syncs are SPAM\x06, 1.31 was \x03.
    best = None
    for x in cands:
        b = bytes(x)
        if b[:4] != b"SPAM":
            try:
                import brotli
                b = brotli.decompress(b)
            except Exception:
                continue
        if b[:4] != b"SPAM":
            continue
        if best is None:
            best = x
        if len(b) > 21:          # has more than the (empty) header + a few counts
            return x
    return best


class Bot:
    def __init__(self, name="YeeperBot", bot_id=1, log=True):
        self.env = load_env()
        self.app, self.ver, self.region = self.env["APP_ID"], self.env["APP_VERSION"], self.env["REGION"]
        if self.env.get("RPC_LIST_FILE"):                  # live builds: their own RpcList
            pun.set_rpc_list(json.load(open(os.path.join(HERE, self.env["RPC_LIST_FILE"]))))
        for k in ("VOICE_APP", "VOICE_VERSION", "VOICE_ROOM", "VOICE_CANDIDATES"):   # read by photon/voice.py
            if k in self.env:
                os.environ["YEEPS_" + k] = self.env[k]
        self.name, self.bot_id = name, bot_id
        self.game = None
        self.actor = None
        self.actors = {}           # actorNr -> props
        self.server_time_offset = 0
        self.handlers = []         # extra event callbacks (code, sender, data)
        self.poses = {}            # actorNr -> latest serialized view data (list)
        self.last_map_sync = None  # newest RpcMapSync map blob (Brotli SPAM)
        self.room_dims = (96, 96, 96)
        self.logf = None
        self.quiet_serialize = False
        # What to print: "all", "none", or a set of RPC names. Everything is
        # still written to the log file either way.
        self.show_rpcs = "all"
        self.hide_rpcs = {"RpcPlaySoundEffect"}
        self.show_spawns = True
        if log:
            os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
            self.logf = open(os.path.join(HERE, "logs", f"events-{datetime.now():%Y%m%d-%H%M%S}.jsonl"), "a")

    # --- connection ---------------------------------------------------------
    def _auth_query(self):
        if self.env.get("AUTH") == "userid":           # a server whose Photon auth takes the id as given
            acct = f"o_99000000000000{self.bot_id:02d}"
            return urlencode({"UserId": f"{acct}$VR${self.name}", "accountID": acct})
        return urlencode({"botKey": self.env["BOT_KEY"], "botId": self.bot_id, "botName": self.name})

    def _ver_for(self, room):
        """Photon AppVersion for a room: the room name ends with the client's game
        version ("..._main2.31.0", "...2.32.0_lab1"), and each game version is its
        own Photon virtual app, so join with "<that version>_<PUN>". GAME_VERSIONS
        (comma list in the env) names the versions to look for; else APP_VERSION."""
        pun_v = self.env["APP_VERSION"].rsplit("_", 1)[-1]
        for gv in sorted(filter(None, self.env.get("GAME_VERSIONS", "").split(",")), key=len, reverse=True):
            if room.endswith(gv.strip()):
                return f"{gv.strip()}_{pun_v}"
        return self.env["APP_VERSION"]

    async def join(self, room):
        self.ver = self._ver_for(room)
        os.environ["YEEPS_GAME_VER"] = self.ver.rsplit("_", 1)[0]          # for photon/voice.py {gv}
        if self.env.get("AUTH") == "userid":
            os.environ["YEEPS_VOICE_AUTH"] = self._auth_query()
        ns = Connection(NAME_SERVER, self.app, label="ns")
        await ns.open()
        try:
            p = check(await ns.op(OP_AUTHENTICATE, auth_params(
                self.app, self.ver, self.region, auth_query=self._auth_query())), "login")
        finally:
            await ns.close()
        master = Connection(p[P_ADDRESS], self.app, label="master")
        await master.open()
        try:
            check(await master.op(OP_AUTHENTICATE, auth_params(self.app, self.ver, self.region, token=p[P_TOKEN])),
                  "master login")
            g = check(await master.op(OP_JOIN_ROOM, {P_ROOM_NAME: room}), f"find room {room}")
        finally:
            await master.close()
        self.game = Connection(g[P_ADDRESS], self.app, self._on_event, label="game")
        await self.game.open()
        check(await self.game.op(OP_AUTHENTICATE, auth_params(self.app, self.ver, self.region, token=g[P_TOKEN])),
              "game login")
        r = check(await self.game.op(OP_JOIN_ROOM, {
            P_ROOM_NAME: room, P_PLAYER_PROPS: {Byte(255): self.name}, P_BROADCAST: True}), "join room")
        self.actor = r.get(P_ACTOR_NR)
        self.actors = {int(k): v for k, v in (r.get(P_PLAYER_PROPS) or {}).items()}
        self._log("joined", {"actor": self.actor, "actors": self.actors, "room": r.get(P_GAME_PROPS)})
        print(f"joined {room} as actor {self.actor}; {len(self.actors)} players here:")
        for a, props in sorted(self.actors.items()):
            print(f"   #{a}: {show(props)[:160]}")
        return r

    async def leave(self):
        if self.game:
            await self.game.close()

    # --- events -------------------------------------------------------------
    def _log(self, kind, obj):
        if self.logf:
            self.logf.write(json.dumps({"t": time.time(), "kind": kind, "v": obj}, default=repr) + "\n")
            self.logf.flush()

    def _on_event(self, code, params):
        sender = params.get(254)
        data = params.get(P_DATA)
        name = pun.PUN_EVENT_NAMES.get(code, str(code))
        if code == pun.EV_RPC:
            rpc = pun.decode_rpc(data)
            if rpc and rpc["method"] == "RpcMapSync" and map_sync_blob(rpc["args"]):
                self.last_map_sync = map_sync_blob(rpc["args"])
            if rpc and rpc.get("time"):
                self.server_time_offset = rpc["time"] - int(time.monotonic() * 1000)
            m = rpc["method"]
            if (self.show_rpcs == "all" and m not in self.hide_rpcs) or \
                    (isinstance(self.show_rpcs, set) and m in self.show_rpcs):
                print(f"RPC  #{sender} view {rpc['view']}: {m}({show(rpc['args'])[:300]})")
            self._log("rpc", {"sender": sender, **rpc, "types": [typename(x) for x in rpc["args"]]})
        elif code == pun.EV_INSTANTIATE:
            ins = pun.decode_instantiate(data)
            if self.show_spawns:
                print(f"SPAWN #{sender}: {ins['prefab']} at {ins['pos']} views={ins['views'] or ins['id']} data={show(ins['data'])[:200]}")
            self._log("instantiate", {"sender": sender, **ins})
        elif code in (pun.EV_SEND_SERIALIZE, pun.EV_SEND_SERIALIZE_RELIABLE):
            if isinstance(data, list) and data:
                if isinstance(data[0], int):
                    self.server_time_offset = data[0] - int(time.monotonic() * 1000)
                for vd in data[2:]:
                    if isinstance(vd, list) and len(vd) > 9 and vd[0] == sender * 1000 + 1:
                        self.poses[sender] = vd
            if not self.quiet_serialize:
                self._log("serialize", {"sender": sender, "code": code, "data": data})
        elif code == pun.EV_JOIN:
            self.actors[sender] = params.get(249)
            print(f"JOIN #{sender}: {(params.get(249) or {}).get(255)}")
            self._log("join", {"sender": sender, "props": params.get(249)})
        elif code == pun.EV_LEAVE:
            gone = self.actors.pop(sender, None) or {}
            print(f"LEAVE #{sender}: {gone.get(255) if isinstance(gone, dict) else ''}")
            self._log("leave", {"sender": sender})
        else:
            # every other Photon event (room/player property changes, raw RaiseEvent codes)
            # is always logged: the skeleton party start travels this way, not as an RPC
            if self.show_spawns:
                print(f"EVENT {name} #{sender}: {show(params)[:200]}")
            self._log("event", {"code": code, "sender": sender, "params": params})
        for h in self.handlers:
            h(code, sender, data)

    # --- sending ------------------------------------------------------------
    def server_time(self):
        """Photon server timestamp: a signed 32-bit millisecond counter that wraps
        (it is negative in some rooms), so always wrap ours the same way."""
        t = int(time.monotonic() * 1000) + self.server_time_offset
        return ((t + 2**31) % 2**32) - 2**31

    @property
    def view_id(self):
        return self.actor * 1000 + 1

    async def set_props(self, props):
        """OpSetProperties on our own actor (others see it as a props-changed event)."""
        await self.game.op(252, {251: props, 254: self.actor, 250: True})
        mine = self.actors.get(self.actor)
        if not isinstance(mine, dict):
            mine = self.actors[self.actor] = {}
        mine.update(props)

    async def spawn_messenger(self):
        """Instantiate our Photon/NetworkMessenger (like every real player does):
        the view others route our RPCs and pose stream through."""
        ev = {Byte(0): "Photon/NetworkMessenger", Byte(6): self.server_time(), Byte(7): self.view_id}
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_INSTANTIATE), P_DATA: ev,
                                              P_RECEIVERS: Byte(pun.RECV_OTHERS), 247: Byte(4)})

    async def respawn_avatar(self):
        """Destroy + re-instantiate our NetworkMessenger so every game rebuilds our
        avatar from our current properties (colours/cosmetics only show on spawn).
        The cached instantiate is replaced too, so late joiners get one copy."""
        vid = self.view_id
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_INSTANTIATE), P_DATA: {Byte(7): vid},
                                              247: Byte(6)})                     # RemoveFromRoomCache
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_DESTROY), P_DATA: {Byte(0): vid},
                                              P_RECEIVERS: Byte(pun.RECV_OTHERS)})
        await asyncio.sleep(0.3)
        await self.spawn_messenger()

    async def spawn_speaker(self):
        """Our Photon/PhotonSpeakerPublic (view actor*1000+2), like every player:
        the voice stream's VoiceInfo points at it, so the game plays our audio
        from our avatar."""
        if getattr(self, "_speaker", None) == self.actor:
            return
        ev = {Byte(0): "Photon/PhotonSpeakerPublic", Byte(6): self.server_time(), Byte(7): self.actor * 1000 + 2}
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_INSTANTIATE), P_DATA: ev,
                                              P_RECEIVERS: Byte(pun.RECV_OTHERS), 247: Byte(4)})
        self._speaker = self.actor

    async def send_speaker_pos(self, pos):
        """Our Photon/PhotonSpeakerPublic streams its own position (like players':
        [view, False, None, position, velocity, float]); voice plays from there."""
        vd = [self.actor * 1000 + 2, False, None, pos, pun.Vector3(), 0.0]
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_SEND_SERIALIZE),
                                              P_DATA: [self.server_time(), None, vd],
                                              P_RECEIVERS: Byte(pun.RECV_OTHERS)})

    async def send_pose(self, values):
        """One OnPhotonSerializeView packet for our messenger view: values are the
        game's pose floats etc. (body pos xyz, body rot xyzw, head/hands...)."""
        vd = [self.view_id, False, None] + list(values)
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_SEND_SERIALIZE),
                                              P_DATA: [self.server_time(), None, vd],
                                              P_RECEIVERS: Byte(pun.RECV_OTHERS)})

    def actor_by_name(self, name):
        name = name.lower()
        for a, p in self.actors.items():
            if a == self.actor or not isinstance(p, dict):
                continue
            nick = clean_name(p.get(255)).lower()
            uid = str(p.get(253) or "").lower()
            if nick == name or uid.startswith(name + "$") or (name and nick.startswith(name)):
                return a
        return None

    def pose_of(self, actor):
        """(position V3, rotation (x,y,z,w)) of an actor's body, or None."""
        vd = self.poses.get(actor)
        if not vd:
            return None
        v = vd[3:]
        return pun.Vector3(v[0], v[1], v[2]), tuple(v[3:7])

    def grid_of(self, pos):
        """World position -> map grid cell (0.5 m cells, map centred on 0 in x/z)."""
        dx, dy, dz = self.room_dims
        return (int(math.floor(pos.x * 2 + dx / 2)), int(math.floor(pos.y * 2)),
                int(math.floor(pos.z * 2 + dz / 2)))

    _sfx = None

    def _sfx_id(self, sid):
        """1.31 sound id -> the same sound's id in the room's game version. Sound ids are
        positions in the game's sound list (1.31: insertion order; 2.31.4 / 2.32:
        alphabetical, different lengths), so the 1.31 numbers played the wrong sounds on
        live. Lists: data/sfx_lists.json (from each game's data)."""
        if Bot._sfx is None:
            try:
                Bot._sfx = json.load(open(os.path.join(HERE, "data", "sfx_lists.json")))
            except Exception:
                Bot._sfx = {}
        old = Bot._sfx.get("1.31") or []
        gv = self.ver.rsplit("_", 1)[0]                 # "2.32.0_lab1_2.45" -> "2.32.0_lab1"
        new = Bot._sfx.get(gv)
        if new is None:
            # "2.32.0" rooms (the new 2.32.0 build) share the 2.32 sound list stored as
            # "2.32.0_lab1": use the list of the same base version, else the newest one.
            base = gv.split("_", 1)[0]
            same = sorted(k for k in Bot._sfx if k != "1.31" and k.split("_", 1)[0] == base)
            new = Bot._sfx.get(same[-1]) if same else Bot._sfx.get(max(
                (k for k in Bot._sfx if k != "1.31"), default="", key=lambda k: [int(x) for x in k.split("_")[0].split(".")]))
        try:
            name = old[int(sid)]
            return new.index(name) if new and name in new else int(sid)
        except Exception:
            return int(sid)

    def _live_args(self, method, args):
        """1.31-shaped RPC args -> the live 2.31.4/2.32 shapes (taken from real live
        players' RPCs in logs/, 2026-10-17). Only used with a live RpcList."""
        a = list(args)
        if method == "RpcApplyPlayerKnockback" and len(a) == 5:          # + trailing byte
            a.append(Byte(0))
        elif method == "RpcCreateConfettiEffectFromPlayerOrigin" and len(a) == 7:
            a += [Byte(0), Byte(0)]
        elif method == "RpcCreateItem" and len(a) == 11:                 # + null, int[2], bytes
            a[10] = Byte(9)                                              # live sends 9 here
            a += [None, TypedArray(ord("i"), [0, 0]), b""]
        elif method == "RpcCreateMob" and len(a) == 12:                  # null before the last bool
            a.insert(11, None)
        elif method in ("RpcCreateProjectileFromPlayerOrigin",
                        "RpcCreateProjectileSpreadFromPlayerOrigin") and len(a) in (9, 11):
            a[4:4] = [Byte(0), Byte(0)]                                  # two bytes before the owner
        elif method == "RpcMapObjectPreviewCreated" and len(a) == 4:    # (id, pattern, color, bool)
            a = [a[1], a[2], a[3], False]
        elif method in ("RpcItemTransformation", "RpcSimpleCustomItemMessage") and a:
            a[0] = None                                                  # always null on live
        elif method == "RpcPlaySoundEffect" and a:
            a[0] = type(a[0])(self._sfx_id(a[0])) if isinstance(a[0], int) else a[0]
        return a

    async def rpc(self, view_id, method, args=(), receivers=pun.RECV_ALL):
        if self.env.get("RPC_LIST_FILE"):
            args = self._live_args(method, args)
        ev = pun.encode_rpc(view_id, method, args, self.server_time())
        await self.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_RPC), P_DATA: ev,
                                              P_RECEIVERS: Byte(receivers)})


async def cmd_listen(a):
    if a.room:
        room, who = a.room, a.room
    else:
        _aid, who, room, rk = find_player(a.target)
        print(f"{who} is in {rk} (Photon room {room})")
    bot = Bot(a.name, a.bot_id)
    if a.all:
        bot.hide_rpcs = set()
    if a.only:
        bot.show_rpcs = set(a.only.split(","))
    await bot.join(room)
    print("listening - Ctrl+C to stop")
    try:
        await bot.game.closed.wait()
    finally:
        await bot.leave()


# --- math (Unity quaternions are x, y, z, w) --------------------------------

def q_forward(q):
    x, y, z, w = q
    f = (2 * (x * z + w * y), 0.0, 1 - 2 * (x * x + y * y))   # flattened to the ground
    n = math.hypot(f[0], f[2]) or 1.0
    return pun.Vector3(f[0] / n, 0.0, f[2] / n)


def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def q_rotate(q, v):
    x, y, z, w = q
    r = q_mul(q_mul(q, (v[0], v[1], v[2], 0.0)), (-x, -y, -z, w))
    return pun.Vector3(r[0], r[1], r[2])


def q_turn180(q):
    """q rotated 180 degrees about the up axis (face the opposite way)."""
    x, y, z, w = q
    return (-z, w, x, -y)


# Palette keys from the game's colour palette asset (data.unity3d PathID 383).
COLORS = ("brown pink green_dark cyan_light cyan blue_light blue_dark blue white gray gray_dark black "
          "red orange orange_light yellow green_light green blue_turquoise teal red_dark brown_dark "
          "brown_light yellow_light purple purple_light lavender blurple olive olive_light mocha "
          "mocha_light periwinkle purple_dark").split()
GLOW = "yellow_emissive green_emissive orange_emissive red_emissive cyan_emissive purple_emissive".split()


class ColSet(set):
    """A set of (x, y, z) cells that also keeps, per (x, z) column, the y's in it,
    so 'what is the top block here?' is instant even with thousands of blocks."""

    def __init__(self, *a):
        super().__init__()
        self.cols = {}
        for c in (a[0] if a else ()):
            self.add(c)

    def add(self, c):
        super().add(c)
        self.cols.setdefault((c[0], c[2]), set()).add(c[1])

    def discard(self, c):
        super().discard(c)
        col = self.cols.get((c[0], c[2]))
        if col is not None:
            col.discard(c[1])
            if not col:
                del self.cols[(c[0], c[2])]

    def remove(self, c):
        if c not in self:
            raise KeyError(c)
        self.discard(c)

    def update(self, *its):
        for it in its:
            for c in it:
                self.add(c)

    def clear(self):
        super().clear()
        self.cols.clear()


DIRS6 = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
_SIZES = None


def block_size(name):
    """Real (x, height, z) size in cells of an unrotated block: media/known_sizes.json
    (YeepsMapLoader) + the Map Maker's index (sizes from the game's own models); the
    name prefix only as a last resort ("1x2_stuffed" is 2 wide in X, not 2 deep)."""
    global _SIZES
    if _SIZES is None:
        _SIZES = {}
        for path in (os.path.join(HERE, "media", "known_sizes.json"),
                     os.path.join(VR_SERVER, "static", "mapmaker-assets", "index.json")):
            try:
                for k, v in json.load(open(path)).items():
                    s = v.get("s") if isinstance(v, dict) else v
                    if s and len(s) == 3:
                        _SIZES.setdefault(k, tuple(int(x) for x in s))
            except Exception:
                pass
    return _SIZES.get(name) or LiveMap._dims(name)


def block_ext(name, fwd=4, up=2):
    """Cells a block fills along x, y, z once turned by LookRotation(DIRS6[fwd],
    DIRS6[up]) (local X = up x forward); its grid position is the lowest corner."""
    sx, sy, sz = block_size(name)
    try:
        F, U = DIRS6[int(fwd)], DIRS6[int(up)]
    except (IndexError, TypeError, ValueError):
        F, U = DIRS6[4], DIRS6[2]
    R = (U[1] * F[2] - U[2] * F[1], U[2] * F[0] - U[0] * F[2], U[0] * F[1] - U[1] * F[0])
    if R == (0, 0, 0):                      # forward parallel to up: not a real facing
        F, U, R = DIRS6[4], DIRS6[2], (1, 0, 0)
    return tuple(abs(R[i]) * sx + abs(U[i]) * sy + abs(F[i]) * sz for i in range(3))


class LiveMap:
    """Occupied grid cells of the room, from the master's RpcMapSync (Brotli SPAM,
    decoded with the server's own decoder) kept up to date from build RPCs."""

    def __init__(self):
        self.cells = ColSet()
        self.obj = {}                        # cell -> (name, x, y, z) of the object filling it
        self.owners = {}                     # (name, x, y, z) -> account that placed it
        self.colors = {}                     # (name, x, y, z) -> colour key (for the editor's View tab)
        self.rot = {}                        # (name, x, y, z) -> (fwd, up) facing
        self.rev = 0                         # bumped on every change (View tab redraws on it)
        self.loaded = False

    def reset(self):
        self.cells.clear(); self.obj.clear(); self.owners.clear(); self.colors.clear(); self.rot.clear()
        self.loaded = False
        self.rev += 1

    @staticmethod
    def _dims(name):
        import re
        m = re.match(r"^(\d+)x(\d+)(?:x(\d+))?_", name or "")
        if not m:
            return 1, 1, 1
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        return a, c, b                      # x, height, z

    def add(self, name, x, y, z, sign=1, owner=None, color=None, fwd=None, up=None):
        key = (name, x, y, z)
        if sign > 0 and owner:
            self.owners[key] = str(owner)
        elif sign < 0:
            self.owners.pop(key, None)
        # The cells it fills depend on its facing; a removal reuses the facing it was added with.
        f0, u0 = self.rot.get(key, (4, 2))
        rot = (int(fwd) if fwd is not None else f0, int(up) if up is not None else u0)
        if sign > 0:
            self.colors[key] = str(color or self.colors.get(key) or "")
            self.rot[key] = rot
        else:
            self.colors.pop(key, None)
            self.rot.pop(key, None)
        self.rev += 1
        w, h, d = block_ext(name, *rot)
        for i in range(w):
            for j in range(h):
                for k in range(d):
                    c = (x + i, y + j, z + k)
                    if sign > 0:
                        self.cells.add(c)
                        self.obj[c] = (name, x, y, z)
                    else:
                        self.cells.discard(c)
                        self.obj.pop(c, None)

    def load_sync(self, blob):
        try:
            import brotli
            sys.path.insert(0, VR_SERVER)
            from services.mapsync_decoder import _decode_spam
            raw = bytes(blob)
            if raw[:4] != b"SPAM":           # small maps arrive uncompressed
                raw = brotli.decompress(raw)
            blocks = _decode_spam(raw)
        except Exception as e:
            # A live RpcMapSync we can't parse (e.g. a short/new-format blob) must NOT
            # drop the map: keep whatever we have (the DB map loads as the fallback).
            if not self.loaded:
                print(f"(couldn't read the room's map: {e!r} - using the server copy instead)")
            return
        # An empty sync (0 blocks) is a no-op heartbeat, not "the room is empty":
        # never let it wipe a map we already have.
        if not blocks and self.loaded:
            return
        self.reset()
        for b in blocks:
            self.add(b["name"], b["x"], b["y"], b["z"], owner=b.get("owner"), color=b.get("color"),
                     fwd=b.get("fwd"), up=b.get("up"))
        self.loaded = True

    def load_db(self, room_key):
        """The room's saved blocks from our DB (same SPAM blob the game fetches). Used
        when the live RpcMapSync can't be read (its args changed on 2.31.4/2.32).
        Private worlds (p_*) have one shared copy in the 1.31 DB (Yeeps119)."""
        try:
            import base64
            d = db()
            coll = d.client["Yeeps119"]["maps"] if str(room_key).startswith("p_") else d["maps"]
            doc = coll.find_one({"mapKey": f"{room_key}$current"}, {"_id": 0, "saveData": 1, "saveChunks": 1})
            chunks = (doc or {}).get("saveChunks") or ([doc["saveData"]] if (doc or {}).get("saveData") else [])
            if not chunks:
                print(f"(no saved blocks for {room_key} - blocks will land on the floor)")
                return False
            self.reset()
            for c in chunks:
                if not self._add_blob(base64.b64decode(c)):
                    return False
            self.loaded = True
            print(f"(room map loaded from the server: {room_key})")
            return True
        except Exception as e:
            print(f"(couldn't load {room_key} from the server: {e!r})")
            return False

    def _add_blob(self, raw):
        try:
            import brotli
            sys.path.insert(0, VR_SERVER)
            from services.mapsync_decoder import _decode_spam
            if raw[:4] != b"SPAM":
                raw = brotli.decompress(raw)
            for b in _decode_spam(raw):
                self.add(b["name"], b["x"], b["y"], b["z"], owner=b.get("owner"), color=b.get("color"),
                         fwd=b.get("fwd"), up=b.get("up"))
            return True
        except Exception as e:
            print(f"(couldn't read the room's map: {e!r})")
            return False

    def top(self, x, z):
        ys = self.cells.cols.get((x, z))
        return max(ys) + 1 if ys else 0


MENU = """
=================================  YEEPS BOT  =================================
Type a command and press Enter. NAME = a player in the room (start of the name
is enough). Leave NAME out and it means you.

  players                   who is in the room
  rain                      random coloured blocks fall all over the map
  rain NAME                 ...falling around NAME instead
  rain off                  stop the rain
  rain clean                remove every block the rain left
  fingergun [NAME]          finger gun + pull the trigger = explosive shot
  fingergun off [NAME]      take it from everyone / from one player
  fly [NAME] / fly off      hold the right trigger and point the right controller to fly
  lightning [NAME]          the finger gun shoots LIGHTNING (storm cloud, jagged bolt, blast)
  freeze [NAME]             the finger gun shoots an ICE shard: a 3x3x3 outline cage + freeze grenades for 15 s
  lightning off / freeze off   take them back (freeze off also thaws everyone)
  smite NAME / ice NAME     strike NAME with lightning / freeze NAME right now
  scare [NAME] / scare off  JUMP SCARE gun: black ball round their head, the bot (black polar bear)
                            in their face, the game's skull scream + monster roar
  jumpscare NAME            jump scare NAME right now
  thaw                      melt every ice cube
  godhand [NAME] [both]     a giant golden HAND OF GOD copies NAME's real hand: squeeze to grab
                            players, let go to throw, swing to smack, smash the ground = explosion,
                            clap your real hands = knockback blast, every 1x1 you place becomes
                            four 2x2x2 blocks ('godhand off')
  bigbuild [NAME] / bigbuild off   just the giant builder (1x1 -> four 2x2x2 blocks)
                            (the hand also rests on the ground, slams on contact and smashes pillows)
  blackhole [NAME]          a black hole in front of NAME drags everyone in, then explodes
  meteors [NAME] [N]        a meteor shower around NAME (N meteors, default 14)
  onlyup [NAME]             build the FULL Only Up map (island lobby + 5 themed sections) by NAME / you
  onlyup simple [NAME]      the small single climb instead
  onlyup clear              take the whole climb away again
  pelletfx [NAME]           pellet gun shots fly as blocks and explode
  pelletfx off
  boom [NAME]               explosion in front of NAME
  bomb basic|impulse|pumpkin|radiation|off   which real bomb the explosions use
  coins [NAME]              buttcoins orbit around NAME (coins NAME 5 = five)
  coins planet              a giant spinning outline ball in the sky with buttcoin rings
  coins off                 remove all buttcoins
  screen [small]            then place a 1x1 pillow: that block is the bottom-middle
                            of the screen, which grows up and sideways, facing into the room
  screen off                remove the screen
  pixelart                  place uploaded image as a pillow mosaic; place a 1x1 marker
  pixelart off              remove the pixel-art mosaic
  soundkey                  play uploaded audio through the bot's Photon voice connection
  soundkey off              stop SoundKey playback
  tetris [big] / snake [big]  place a 1x1 pillow for the board (bottom-middle), then
                            another for the controller; grab the coloured blocks to play
                            tetris: a row (left, rotate, right, drop)
                            snake: a plus on the ground round the pillow (arm toward the
                            board = up, toward you = down, sides = left/right)
  pong [big] [2p]           the same: board pillow, then controller pillow (green up,
                            yellow down); 2p = two players: a 3rd pillow for the right
                            player's controller; 'pong hand' = move your paddle with your hand
  tetris|snake|pong restart / game off   new game / remove it
  controls [METERS]         move the game's controller(s) that far in front of the board
                            (pong: grab the purple/orange block to switch bot <-> 2 players;
                            blue/red score bars stand left of the board)
  boss pause / boss resume  freeze the Titan (no attacks, bugs removed) / let it go
  boss [HP] / boss off      the TITAN: a giant head dead in the middle of the room; its
                            two huge hands slam/swipe/clap players fast, it spams real
                            bugs (and spits them at you), snail-spit/fire/shotgun
                            volleys, shockwaves, on a hill arena (no healing); finger gun
                            for everyone to shoot its head (default 1500 HP)
  waves / waves off         SURVIVAL: defend the glowing crystal in the middle from waves of
                            real bugs; wave number on the wall, finger gun for everyone
  map / map off             grow a random hill map (grass/dirt/stone, trees) / remove it
  potato / potato off       HOT POTATO (grows a hill map): a bomb pillow outline sticks to someone's hand - touch another
                            player to pass it before it blows; last one standing wins
  koth [MINUTES] / koth off king of the hill: grows 10-16 blocky hills of pillow cubes
                            (grass/dirt/stone, trees, flowers); hold the ring on the hilltops
  canvas [big]              pillow = canvas (bottom-middle), 2nd pillow = colour palette;
                            hit/grab a colour, then point your bat at the canvas to paint
  canvas clear / canvas off wipe it white / remove it
  apple [big|small]         play the song and its waveform on a pillow screen (or, if you
                            have none, on the wall you face); big = whole wall
  apple mute / apple FILE   no sound / another video file
  cleanup [NAME|ACCOUNT]    delete every block that player placed in this room
                            (default: the "firebox" spam bot)
  talk / talk off            talk to the players live from the editor's Talk panel (your
                            voice plays at the hooked player's face; you hear them back)
  applesign                 the first sign you place plays Bad Apple as text art
                            with the song; applesign off stops playback and keeps the sign
  apple off                 stop
  apple off                 stop and remove the screen
  mimic NAME [METERS]       a copy of NAME beside them (2 m) that walks, moves and looks like
                            them, holds/throws/uses the same items and places the same blocks
  mirror NAME               the same, but like a reflection
  stop                      stop mimic/mirror (bot hides under the map again)
  look NAME                 the bot copies NAME's colours and cosmetics
  outline [NAME]            a block outline floats in front of NAME and follows
  outline off
  mirrorbuild [NAME]        copies what NAME builds 5 blocks further ahead
  mirrorbuild off
  block PATTERN [COLOR] [WxHxD] [NAME]   e.g. block 1x1_stuffed red 3x3x1
  menu                      show this list again
  quit                      leave

  extra: rain 200 10  (200 blocks, 10 a second)   mimic NAME 3  (3 m away)
         watch all / watch off / rpc METHOD [json]  (debugging)
===============================================================================
"""
HELP = MENU


def mirror_values(v):
    """Reflection: swap left/right hand blocks and flip their x / rotation
    (layout after body pos+rot: head pos3+rot4, hand A pos3+rot4+3, hand B pos3+rot4+3)."""
    v = list(v)
    try:
        def flip_pos(p): return [-p[0], p[1], p[2]]
        def flip_rot(r): return [r[0], -r[1], -r[2], r[3]]
        head_p, head_r = v[7:10], v[10:14]
        a, b = v[14:24], v[24:34]
        v[7:10], v[10:14] = flip_pos(head_p), flip_rot(head_r)
        v[14:24] = flip_pos(b[0:3]) + flip_rot(b[3:7]) + b[7:10]
        v[24:34] = flip_pos(a[0:3]) + flip_rot(a[3:7]) + a[7:10]
    except Exception:
        pass
    return v


def room_dims_for(rk):
    """The room size the game uses: the saved one, else the same defaults the server
    gives the game (pw 32x80x32 sandbox / 80x80x80, any other room 128x96x128).
    Live-mirror rooms (wilds, hub...) have no saved size, and the bot used to guess
    96x96x96 (or keep the last room's size), so boss/KOTH hills landed off-centre."""
    try:
        r = db().rooms.find_one({"roomKey": rk}, {"_id": 0, "dimensions": 1}) or {}
        dims = r.get("dimensions")
        if isinstance(dims, str):
            dims = json.loads(dims)
        if isinstance(dims, list) and len(dims) == 3 and all(0 < int(v) <= 1024 for v in dims):
            return tuple(int(v) for v in dims)
    except Exception as e:
        print(f"(room size read failed for {rk}: {e!r})")
    rk = str(rk or "")
    if rk.startswith(("p_", "o_", "c_")):
        return (32, 80, 32) if rk.endswith("_sandbox") else (80, 80, 80)
    return (128, 96, 128)


async def cmd_run(a):
    aid, who, room, rk = find_player(a.target)
    d = db()
    r = d.rooms.find_one({"roomKey": rk}, {"_id": 0, "dimensions": 1}) or {}
    print(f"{who} is in {rk} (Photon room {room})")
    bot = Bot(a.name, a.bot_id)
    bot.quiet_serialize = True
    bot.show_rpcs, bot.show_spawns = "none", False     # quiet: commands stay readable
    bot.room_dims = room_dims_for(rk)
    print(f"(room size {bot.room_dims})")
    account = f"o_99000000000000{a.bot_id:02d}"
    # YEEPS_LOOK_ACCT (the View app: the owner's own account): wear that account's outfit +
    # colors. Copied onto the bot's account BEFORE joining, since games load a player's
    # look from the server when they join.
    look_acct = (os.environ.get("YEEPS_LOOK_ACCT") or "").strip()
    if look_acct:
        try:
            lk = db().accounts.find_one({"accountID": look_acct}, {"_id": 0, "skinColor": 1, "eyeColor": 1,
                                                                   "activeCosmetics": 1, "displayName": 1}) or {}
            if isinstance(lk.get("activeCosmetics"), list):
                lk["activeCosmetics"] = [c for c in lk["activeCosmetics"] if not str(c).startswith("roleBadge")]
            if lk:
                db().accounts.update_one({"accountID": account}, {"$set": lk})
                print(f"wearing {look_acct}'s outfit")
        except Exception as e:
            print(f"(couldn't copy {look_acct}'s outfit: {e!r})")
    await bot.join(room)
    state = {"mimic": None, "mirror": False, "dist": 1.6, "last": None, "copy": False, "override": None,
             "b": None, "tp": None, "kv": pun.Vector3(0, 0, 0), "ky": 0.0, "feet": 0.0, "hstand": None}

    # mimic = its own player that copies them: it spawns facing them and every
    # step they take it takes too (turned round, as if facing them), but it moves
    # itself - walls stop it, it stands on blocks, falls off edges and gets
    # knocked back. state["b"] = its body position, state["tp"] = theirs.
    def mx_pos(v):
        b, tp = state["b"], state["tp"]
        if not isinstance(v, pun.Vector3) or b is None or tp is None:
            return v
        return pun.Vector3(b.x - (v.x - tp.x), b.y + (v.y - tp.y), b.z - (v.z - tp.z))

    def mx_dir(v):
        return pun.Vector3(-v.x, v.y, -v.z) if isinstance(v, pun.Vector3) else v

    def mx_rot(q):
        return pun.Quaternion(-q.y, q.z, q.w, -q.x) if isinstance(q, pun.Quaternion) else q
    cur = {"room": room, "rk": rk}

    # Effects remember WHO they follow by account, so they keep working when that
    # player rejoins (new actor number) or the bot follows them to another room.
    TRACKED = []

    def acct_of(actor):
        props = bot.actors.get(actor)
        if not isinstance(props, dict):
            return None
        return str(props.get(253) or "").split("$")[0] or None

    def track(d, key):
        TRACKED.append((d, key))

    def remember(d, key):
        d[key + "_acct"] = acct_of(d[key]) if d.get(key) is not None else None

    def reresolve():
        for d, key in TRACKED:
            acct = d.get(key + "_acct")
            if acct:
                d[key] = next((x for x, pr in bot.actors.items() if x != bot.actor and isinstance(pr, dict)
                               and str(pr.get(253) or "").startswith(acct + "$")), None)

    bot.handlers.append(lambda code, sender, data: reresolve() if code in (pun.EV_JOIN, pun.EV_LEAVE) else None)
    track(state, "mimic")

    async def apply_look(name):
        t = bot.actor_by_name(name)
        if t is None:
            print(f"no player called {name!r} here"); return
        props = {k: v for k, v in (bot.actors.get(t) or {}).items() if isinstance(k, str)}
        if "roleKeys" in props:
            rk = props["roleKeys"]
            props["roleKeys"] = TypedArray(rk.code, []) if isinstance(rk, TypedArray) else StringArray()
        await bot.set_props(props)
        print(f"bot now looks like {name}")

    def _rgba_v3(c):
        u = int(c or 0) & 0xFFFFFFFF
        return pun.Vector3(((u >> 24) & 255) / 255.0, ((u >> 16) & 255) / 255.0, ((u >> 8) & 255) / 255.0)

    async def apply_owner_look():
        """Wear the YEEPS_LOOK_ACCT account's colors + cosmetics the way games read them: our
        Photon properties (skinColor/eyeColor Vector3, activeCosmeticDatas), shaped like a real
        player's (copied from one in the room). Must happen before our avatar spawns."""
        tmpl = next((pr for a_, pr in bot.actors.items() if a_ != bot.actor and isinstance(pr, dict)
                     and "activeCosmeticDatas" in pr), None)
        if tmpl is None:
            print("(no player here to copy the property layout from - default look)"); return
        lk = await asyncio.to_thread(lambda: db().accounts.find_one(
            {"accountID": look_acct}, {"_id": 0, "skinColor": 1, "eyeColor": 1, "activeCosmetics": 1}) or {})
        props = {k: v for k, v in tmpl.items() if isinstance(k, str)}
        rk = props.get("roleKeys")
        props["roleKeys"] = TypedArray(rk.code, []) if isinstance(rk, TypedArray) else StringArray()
        props.update(teamKey="", isGhost=False, isSleeping=False, statusEffect=0, statusEffectExpireTime=0.0)
        if lk.get("skinColor") is not None:
            props["skinColor"] = _rgba_v3(lk["skinColor"])
        if lk.get("eyeColor") is not None:
            props["eyeColor"] = _rgba_v3(lk["eyeColor"])
        cos = [str(c) for c in (lk.get("activeCosmetics") or []) if not str(c).startswith("roleBadge")]
        ac = tmpl["activeCosmeticDatas"]
        props["activeCosmeticDatas"] = TypedArray(ac.code, cos) if isinstance(ac, TypedArray) else cos
        await bot.set_props(props)
        print(f"wearing {look_acct}'s look ({len(cos)} cosmetics)")

    if look_acct:
        await apply_owner_look()
    else:
        await apply_look(clean_name(a.look or who))
    await bot.spawn_messenger()
    print(f"bot is in the room as {a.name} (view {bot.view_id}). type help")

    BODY_H = 1.0                                   # a Yeep is about 2 cells tall

    def _col(x, z):
        DX, _, DZ = bot.room_dims
        return live.cells.cols.get((int(math.floor(x * 2 + DX / 2)), int(math.floor(z * 2 + DZ / 2)))) or ()

    def surface_below(x, z, y_lim):
        """Height (m) of the highest block top at (x, z) that is at or below y_lim; 0 = floor."""
        best = 0.0
        for y in _col(x, z):
            top = (y + 1) * 0.5
            if best < top <= y_lim + 0.01:
                best = top
        return best

    def wall_at(x, z, climb, head):
        """A block between the highest step it can climb and the top of its head."""
        for ox, oz in ((0, 0), (0.2, 0.2), (0.2, -0.2), (-0.2, 0.2), (-0.2, -0.2)):
            for y in _col(x + ox, z + oz):
                if (y + 1) * 0.5 > climb + 0.01 and y * 0.5 < head:
                    return True
        return False

    def clone_step(p):
        """One 0.1 s tick of the clone's own movement from the target's latest move."""
        st = state
        if st["b"] is None or st["tp"] is None:
            # never drawn on top of them: start it in front, as its own player
            vd = bot.poses.get(st["mimic"]) if st["mimic"] is not None else None
            clone_spawn(p, tuple(vd[6:10]) if vd and len(vd) > 9 else (0.0, 0.0, 0.0, 1.0), st.get("dist") or 2.0)
            return st["b"]
        d = p + st["tp"] * -1
        st["tp"] = p
        if math.hypot(d.x, d.z) > 2.5 or abs(d.y) > 2.5:            # they respawned / teleported
            d = pun.Vector3(0, 0, 0)
        h = p.y - surface_below(p.x, p.z, p.y)                      # their height over their ground
        if st["hstand"] is None or h < st["hstand"]:
            st["hstand"] = h
        else:                                    # drift back up so one odd reading can't stick
            st["hstand"] += (h - st["hstand"]) * 0.02
        jump = max(0.0, h - st["hstand"])
        kv, b, feet = st["kv"], st["b"], st["feet"]
        climb = feet + 0.5 + jump + st["ky"]                        # one step up, more mid-jump
        head = feet + jump + st["ky"] + BODY_H
        nx = b.x - d.x + kv.x * 0.1                                  # their move, turned round
        nz = b.z - d.z + kv.z * 0.1
        DX, _, DZ = bot.room_dims
        lim_x, lim_z = DX / 4 - 0.3, DZ / 4 - 0.3                    # room walls
        if abs(nx) > lim_x:
            nx = math.copysign(lim_x, nx); kv.x = 0
        if abs(nz) > lim_z:
            nz = math.copysign(lim_z, nz); kv.z = 0
        if wall_at(nx, b.z, climb, head):                            # slide along walls
            nx = b.x; kv.x = -kv.x * 0.3
        if wall_at(nx, nz, climb, head):
            nz = b.z; kv.z = -kv.z * 0.3
        ground = surface_below(nx, nz, climb)
        feet = ground if ground >= feet else max(ground, feet - 0.6)  # step up, or fall
        st["ky"] = max(0.0, st["ky"] + kv.y * 0.1)                   # knocked into the air
        kv.y = kv.y - 0.98 if st["ky"] > 0 else max(0.0, kv.y)
        damp = 0.97 if st["ky"] > 0 else 0.82
        kv.x *= damp; kv.z *= damp
        st["feet"] = feet
        st["b"] = pun.Vector3(nx, feet + h + st["ky"], nz)
        return st["b"]

    def clone_spawn(p, rot, dist):
        """Start the clone `dist` m in front of p, standing on whatever is there."""
        f = q_forward(rot)
        n_ = math.hypot(f.x, f.z) or 1.0
        sx, sz = p.x + f.x / n_ * dist, p.z + f.z / n_ * dist
        tg = surface_below(p.x, p.z, p.y)
        h = p.y - tg
        feet = surface_below(sx, sz, tg + 0.5)
        state.update(b=pun.Vector3(sx, feet + h, sz), tp=p, kv=pun.Vector3(0, 0, 0), ky=0.0,
                     feet=feet, hstand=h)

    def clone_push(force, scale=1.0):
        kv = state["kv"]
        state["kv"] = pun.Vector3(kv.x + force.x * scale, kv.y + force.y * scale, kv.z + force.z * scale)

    # Editor View tab: while the owner flies the camera there, the bot's avatar (and the
    # talk voice) sit where the camera is, head turned the way it looks. state["fly"] =
    # {"t", "g": grid xyz, "d": look direction (game axes)}, sent over the talk socket.
    FLY_STALE = 3.0

    def fly_world():
        fl = state.get("fly")
        if not fl or time.monotonic() - fl["t"] > FLY_STALE:
            return None
        DX, DY, DZ = bot.room_dims
        gx, gy, gz = fl["g"]
        # the View's camera often starts far above / outside the map: keep the avatar inside it
        gx, gy, gz = min(max(gx, 1), DX - 1), min(max(gy, 1), DY - 2), min(max(gz, 1), DZ - 1)
        dx, dy, dz = fl["d"]
        return pun.Vector3((gx - DX / 2) / 2, gy / 2, (gz - DZ / 2) / 2), (dx, dy, dz)

    def fly_pose():
        fw = fly_world()
        if not fw:
            return None
        if time.monotonic() - state.get("fly_said", 0) > 15:
            state["fly_said"] = time.monotonic()
            print(f"view: avatar at {fw[0].x:.1f},{fw[0].y:.1f},{fw[0].z:.1f} m"
                  + ("" if state.get("last") or bot.poses else " - no pose to copy yet"))
        tmpl = state.get("last")
        if not tmpl or len(tmpl) < 14:
            t0 = bot.actor_by_name(who)
            src = bot.poses.get(t0) if t0 is not None else None
            src = src or next((vd for act, vd in bot.poses.items() if act != bot.actor and vd and len(vd) >= 17), None)
            if not src:
                return None
            tmpl = list(src[3:])
        tmpl = apply_limbs(list(tmpl))                   # so the head offset below is the posed one
        cam, (dx, dy, dz) = fw
        yaw = math.atan2(dx, dz) if math.hypot(dx, dz) > 1e-4 else 0.0
        pitch = -math.asin(max(-1.0, min(1.0, dy)))
        q = (0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2))
        hl = [float(c) for c in tmpl[7:10]]
        body = cam - q_rotate(q, hl)                      # head right where the camera is
        out = list(tmpl)
        out[0:3] = [body.x, body.y, body.z]
        out[3:7] = list(q)
        out[10:14] = [math.sin(pitch / 2), 0.0, 0.0, math.cos(pitch / 2)]
        return out

    # View app: head / hands dragged in the little pose picture (body-local metres).
    # LIMB_REST = the game's own idle pose (hands behind the back), seen on idle players:
    # head pos; hand pos + rot + finger grips. The bot starts in it, Reset pose goes back to it.
    LIMB_AT = {"head": 7, "right": 14, "left": 24}
    LIMB_REST = {"head": [0.0, 0.238, 0.0],
                 "right": [0.072, -0.219, -0.194, 0.0788, -0.747, -0.0492, 0.658, 1.0, 1.0, 1.0],
                 "left": [-0.072, -0.219, -0.194, 0.0788, 0.747, 0.0492, 0.658, 1.0, 1.0, 1.0]}
    state["limbs"] = {k: list(v) for k, v in LIMB_REST.items()}

    def apply_limbs(v):
        for part, vals in (state.get("limbs") or {}).items():
            i = LIMB_AT[part]
            if len(v) >= i + len(vals):
                v[i:i + len(vals)] = list(vals)
        return v

    async def pose_loop():
        while True:
            await asyncio.sleep(0.1)
            if state.get("speaker") is not None:
                await bot.send_speaker_pos(state["speaker"])
            if state.get("override") is not None:            # jump scare in progress
                await bot.send_pose(state["override"])
                continue
            fp = fly_pose()
            if fp is not None:
                state["last"] = fp
                if not state.get("hidden"):
                    await bot.send_pose(fp)
                continue
            if state.get("hidden"):
                continue
            t = state["mimic"]
            if t is None:
                if state["last"] is not None and int(time.monotonic() * 10) % 10 == 0:
                    await bot.send_pose(state["last"])      # keep the frozen pose alive
                continue
            vd = bot.poses.get(t)
            if not vd:
                continue
            v = list(vd[3:])
            pos = pun.Vector3(v[0], v[1], v[2])
            rot = tuple(v[3:7])
            if state["copy"]:
                b_ = clone_step(pos)
                v[0:3] = [b_.x, b_.y, b_.z]
                v[3:7] = list(q_turn180(rot))
            else:
                npos = pos + q_forward(rot) * state["dist"]
                v[0:3] = [npos.x, npos.y, npos.z]
                v[3:7] = list(q_turn180(rot))
                if state["mirror"]:
                    v = mirror_values(v)
            state["last"] = v
            await bot.send_pose(v)

    loop_task = asyncio.create_task(pose_loop())

    live = LiveMap()

    def on_map(code, sender, data):
        if code != pun.EV_RPC:
            return
        r = pun.decode_rpc(data)
        if not r:
            return
        m, args = r["method"], r["args"]
        try:
            if m == "RpcMapSync" and map_sync_blob(args):
                live.load_sync(map_sync_blob(args))
            elif m == "RpcMapObjectCreated":
                live.add(args[0], int(args[1]), int(args[2]), int(args[3]),
                         owner=args[7] if len(args) > 7 else None,
                         color=args[6] if len(args) > 6 else None,
                         fwd=args[4] if len(args) > 5 else None, up=args[5] if len(args) > 5 else None)
            elif m == "RpcDestroyMapObject":
                live.add(args[0], int(args[1]), int(args[2]), int(args[3]), sign=-1)
        except Exception:
            pass

    bot.handlers.append(on_map)
    if bot.last_map_sync:
        live.load_sync(bot.last_map_sync)
    if not live.loaded and rk:
        live.load_db(rk)

    HIDDEN_SPOT = pun.Vector3(0.0, -1.0, 0.0)      # just under the ground (owner's choice, 2026-10-10)

    def ensure_avatar(t, where=None):
        """Previews only show while the bot has an avatar: give it a pose (a copy
        of t's, moved to `where`) if it has none. pose_loop keeps it alive."""
        if state["mimic"] is not None or state["last"] is not None:
            return
        vd = bot.poses.get(t)
        if not vd:
            return
        v = list(vd[3:])
        pos = pun.Vector3(v[0], v[1], v[2])
        rot = tuple(v[3:7])
        p2 = where or HIDDEN_SPOT
        v[0:3] = [p2.x, p2.y, p2.z]
        v[3:7] = list(q_turn180(rot))
        state["last"] = v

    async def hide_soon():
        for _ in range(40):                 # as soon as we know a pose shape, park under the map
            tt = bot.actor_by_name(who)
            if tt is not None and bot.poses.get(tt):
                ensure_avatar(tt); return
            await asyncio.sleep(0.1)

    asyncio.create_task(hide_soon())

    # --- outline: a build preview (ghost block) ---------------------------
    ol = {"id": None, "actor": None, "follow": False, "pos": None, "rot": pun.Quaternion(1, 0, 0, 0),
          "sent": 0.0}
    track(ol, "actor")
    user_id = f"{account}$VR${a.name}"

    def cell_center(pos, dist):
        g = list(bot.grid_of(pos))
        DX, DY, DZ = bot.room_dims
        for i, lim in enumerate((DX, DY, DZ)):          # keep it inside the build area
            g[i] = min(max(g[i], 0), lim - 1)
        return pun.Vector3((g[0] - DX / 2) * 0.5 + 0.25, g[1] * 0.5 + 0.25, (g[2] - DZ / 2) * 0.5 + 0.25)

    def outline_spot(actor):
        pose = bot.pose_of(actor)
        if not pose:
            return None
        pos, rot = pose
        return cell_center(pos + q_forward(rot) * 1.0, 1.0)

    async def outline_update(force=False):
        if ol["id"] is None:
            return
        if ol["follow"]:
            spot = outline_spot(ol["actor"])
            if spot and (spot != ol["pos"]):
                ol["pos"] = spot; force = True
        if force or time.monotonic() - ol["sent"] > 1.0:     # keep-alive
            await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated", [ol["id"], ol["pos"], ol["rot"], True],
                          receivers=pun.RECV_OTHERS)
            ol["sent"] = time.monotonic()

    async def outline_off():
        if ol["id"] is None:
            return False
        for m in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed"):
            await bot.rpc(bot.view_id, m, [ol["id"]], receivers=pun.RECV_OTHERS)
        ol["id"] = None
        return True

    async def outline_on(pattern, color, follow, name):
        t = bot.actor_by_name(name)
        if t is None:
            print(f"no player called {name!r} here"); return
        spot = None
        for _ in range(30):
            spot = outline_spot(t)
            if spot:
                break
            await asyncio.sleep(0.1)
        if not spot:
            print(f"can't see where {name} is yet - try again in a second"); return
        ensure_avatar(t)
        await asyncio.sleep(0.3)
        await outline_off()
        ol.update(id="".join(random.choice(string.ascii_letters + string.digits) for _ in range(4)),
                  actor=t, follow=follow, pos=spot)
        remember(ol, "actor")
        await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated", [user_id, ol["id"], pattern, color],
                      receivers=pun.RECV_OTHERS)
        await outline_update(force=True)
        me = bot.pose_of(t)[0]
        print(f"({bot.actors[t].get(255)} is at {me}, grid {bot.grid_of(me)} of {bot.room_dims})")
        print(f"outline of {pattern} ({color}) in front of {bot.actors[t].get(255)} at {spot}"
              + (" - following them" if follow else "") + ". 'outline off' to remove")

    async def outline_loop():
        while True:
            await asyncio.sleep(0.1)
            try:
                await outline_update()
            except Exception as e:
                print(f"outline error: {e!r}")

    outline_task = asyncio.create_task(outline_loop())

    # --- rain: spinning blocks fall and land as real blocks ---------------
    rain = {"on": False, "left": 0, "rate": 15.0, "radius": 8.0, "actor": None, "falling": [],
            "next": 0.0, "placed": 0, "whole_map": True, "cells": []}
    track(rain, "actor")
    # rain: straight down at a steady speed (no tumbling) from ~30 m up, lots at once
    G, DROP_CELLS, MAX_AIR, TICK, MAX_RATE = 25.0, 60, 40, 0.1, 30.0
    RAIN_SPEED = 22.0                                   # m/s, constant like real rain

    def rand_id():
        return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(4))

    def spin_q(axis, angle):
        h = angle / 2
        return pun.Quaternion(math.cos(h), axis[0] * math.sin(h), axis[1] * math.sin(h), axis[2] * math.sin(h))

    async def rain_spawn():
        DX, DY, DZ = bot.room_dims
        if rain["whole_map"]:
            # anywhere in the build area, at a random (off-grid) spot inside the cell
            gx, gz = random.randint(1, DX - 2), random.randint(1, DZ - 2)
            x = (gx - DX / 2) * 0.5 + random.uniform(0.05, 0.45)
            z = (gz - DZ / 2) * 0.5 + random.uniform(0.05, 0.45)
        else:
            pose = bot.pose_of(rain["actor"])
            if not pose:
                return
            c = pose[0]
            for _ in range(12):                 # pick a spot near them, inside the build area
                ang, r = random.uniform(0, 2 * math.pi), rain["radius"] * math.sqrt(random.random())
                x, z = c.x + r * math.cos(ang), c.z + r * math.sin(ang)
                gx, _, gz = bot.grid_of(pun.Vector3(x, 0, z))
                if 1 <= gx < DX - 1 and 1 <= gz < DZ - 1:
                    break
            else:
                return
        gy = live.top(gx, gz)
        if gy + 1 >= DY:
            return
        live.cells.add((gx, gy, gz))         # reserve so the next one stacks on top
        land_y = gy * 0.5 + 0.25
        top_y = DY * 0.5 - 1.0                              # just under the ceiling
        ax = [random.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(a * a for a in ax)) or 1
        b = {"id": rand_id(), "color": random.choice(COLORS + GLOW), "x": x, "z": z,
             "cell": (gx, gy, gz), "land": land_y, "start": min(land_y + DROP_CELLS * 0.5, top_y),
             "axis": [a / n for a in ax], "w": random.uniform(4, 9), "t0": time.monotonic()}
        await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated", [user_id, b["id"], "1x1_stuffed", b["color"]],
                      receivers=pun.RECV_OTHERS)
        rain["falling"].append(b)

    async def rain_tick():
        now = time.monotonic()
        for b in list(rain["falling"]):
            t = now - b["t0"]
            y = b["start"] - RAIN_SPEED * t
            if y <= b["land"]:
                rain["falling"].remove(b)
                for m in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed"):
                    await bot.rpc(bot.view_id, m, [b["id"]], receivers=pun.RECV_OTHERS)
                gx, gy, gz = b["cell"]
                await bot.rpc(bot.view_id, "RpcMapObjectCreated",
                              ["1x1_stuffed", gx, gy, gz, 1, 2, b["color"], account, False],
                              receivers=pun.RECV_OTHERS)
                rain["placed"] += 1
                rain["cells"].append((gx, gy, gz))
            else:
                await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated",
                              [b["id"], pun.Vector3(b["x"], y, b["z"]), pun.Quaternion(1, 0, 0, 0), True],
                              receivers=pun.RECV_OTHERS)
        while rain["on"] and len(rain["falling"]) < MAX_AIR and now >= rain["next"]:
            if rain["left"] == 0:
                rain["on"] = False
                rain["done_msg"] = True
                break
            await rain_spawn()
            rain["left"] -= 1
            rain["next"] = max(rain["next"], now - TICK) + 1.0 / max(rain["rate"], 0.1)

    async def rain_loop():
        while True:
            await asyncio.sleep(TICK)
            if rain.get("done_msg") and not rain["falling"]:
                rain["done_msg"] = False
                print(f"rain finished ({rain['placed']} blocks placed)")
            if rain["on"] or rain["falling"]:
                try:
                    await rain_tick()
                except Exception as e:
                    print(f"rain error: {e!r}")

    rain_task = asyncio.create_task(rain_loop())

    # --- effects engine: moving/spinning outlines with gravity ----------------
    fx = {"objs": [], "pellet_actor": None, "shots": 0}
    track(fx, "pellet_actor")
    FX_TICK, FX_MAX_SHOTS = 0.1, 10**9      # no limit: shots never run out

    def q_dir(q):
        """Unity forward (0,0,1) rotated by a PUN Quaternion (w,x,y,z)."""
        w, x, y, z = q.w, q.x, q.y, q.z
        f = pun.Vector3(2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y))
        n = math.sqrt(f.x * f.x + f.y * f.y + f.z * f.z) or 1.0
        return f * (1 / n)

    def solid(p):
        """Is world point p inside a wall/block/floor of the build area?"""
        DX, DY, DZ = bot.room_dims
        gx, gy, gz = int(math.floor(p.x * 2 + DX / 2)), int(math.floor(p.y * 2)), int(math.floor(p.z * 2 + DZ / 2))
        if p.y < 0 or not (0 <= gx < DX and 0 <= gz < DZ) or gy >= DY:
            return True
        return (gx, gy, gz) in live.cells

    def raycast(start, d, max_dist=60.0, step=0.15):
        p = start
        travelled = 0.0
        while travelled < max_dist:
            q = p + d * step
            if solid(q):
                return p, travelled
            p, travelled = q, travelled + step
        return p, travelled

    async def fx_add(pos, color, vel=None, gravity=0.0, life=1.0, spin=None, on_end=None, pattern="1x1_stuffed"):
        ax = [random.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(a * a for a in ax)) or 1
        o = {"id": rand_id(), "p0": pos, "v": vel or pun.Vector3(), "g": gravity, "life": life,
             "axis": [a / n for a in ax], "w": spin if spin is not None else random.uniform(5, 12),
             "t0": time.monotonic(), "on_end": on_end}
        await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated", [user_id, o["id"], pattern, color],
                      receivers=pun.RECV_OTHERS)
        fx["objs"].append(o)

    async def fx_tick():
        now = time.monotonic()
        for o in list(fx["objs"]):
            t = now - o["t0"]
            if t >= o["life"]:
                fx["objs"].remove(o)
                for m in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed"):
                    await bot.rpc(bot.view_id, m, [o["id"]], receivers=pun.RECV_OTHERS)
                if o["on_end"]:
                    await o["on_end"]()
                continue
            p = o["p0"] + o["v"] * t + pun.Vector3(0, -0.5 * o["g"] * t * t, 0)
            await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated",
                          [o["id"], p, spin_q(o["axis"], o["w"] * t), True], receivers=pun.RECV_OTHERS)

    async def fx_loop():
        while True:
            await asyncio.sleep(FX_TICK)
            if fx["objs"]:
                try:
                    await fx_tick()
                except Exception as e:
                    print(f"fx error: {e!r}")

    fx_task = asyncio.create_task(fx_loop())

    BOMBS = {"basic": "throwingBasicBomb", "impulse": "throwingImpulseBomb",
             "pumpkin": "throwingPumpkinBomb", "radiation": "throwingRadiationBomb"}
    bomb = {"kind": "basic"}

    async def bomb_at(pos):
        """A real game bomb blowing up at pos: create the item there, pin it, tell
        everyone it exploded, remove it (the same RPCs a thrown bomb uses)."""
        if bomb["kind"] == "off":
            return
        iid = rand_id()
        zero, ident = pun.Vector3(), pun.Quaternion(1, 0, 0, 0)
        await bot.rpc(bot.view_id, "RpcCreateItem",
                      [iid, BOMBS[bomb["kind"]], 0, None, pos, ident, zero, zero,
                       TypedArray(ord("s"), []), Byte(1), Byte(1)], receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcItemTransformation", [user_id, iid, 1, None, pos, ident, zero, zero],
                      receivers=pun.RECV_OTHERS)
        await asyncio.sleep(0.05)
        await bot.rpc(bot.view_id, "RpcSimpleCustomItemMessage", [user_id, iid, "explode"],
                      receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcDestroyItem", [iid], receivers=pun.RECV_OTHERS)

    async def explode(pos, normal=None, land=True, power=5.0, radius=6.0):
        """Real bomb explosion + coloured block debris + confetti + impact puffs.
        land=False: debris just flies and vanishes (no blocks left behind)."""
        normal = normal or pun.Vector3(0, 1, 0)
        asyncio.get_running_loop().create_task(bomb_at(pos + normal * 0.2))
        palette = random.sample(GLOW + ["white", "pink", "blurple", "teal", "yellow"], 4)
        for _ in range(3):
            await bot.rpc(bot.view_id, "RpcCreateHitEffect", [pos, normal, 0], receivers=pun.RECV_OTHERS)
        for i in range(3):
            d = pun.Vector3(random.uniform(-1, 1), random.uniform(0.3, 1.2), random.uniform(-1, 1))
            await bot.rpc(bot.view_id, "RpcCreateConfettiEffectFromPlayerOrigin",
                          [pos, d, pun.Vector3(), TypedArray(ord("s"), random.sample(palette, 2)), 1, None, user_id],
                          receivers=pun.RECV_OTHERS)
        await knockback(pos, radius=radius, power=power)
        start = pos + normal * 0.3
        for i in range(6):
            v = pun.Vector3(random.uniform(-1, 1), random.uniform(0.4, 1.4), random.uniform(-1, 1))
            n = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z) or 1
            v = v * (random.uniform(3.5, 6.5) / n) + normal * 1.5
            color = palette[i % len(palette)]
            spot = landing(start, v, 14.0) if land else None
            if spot is None:                       # no landing wanted / out of the map: vanish
                await fx_add(start, color, vel=v, gravity=14.0, life=1.0)
                continue
            t_land, cell = spot
            live.cells.add(cell)                   # reserve so debris stacks
            await fx_add(start, color, vel=v, gravity=14.0, life=t_land,
                         on_end=place_block(cell, color))

    def landing(p0, v, g, max_t=3.0, dt=0.02):
        """Time and grid cell where a thrown block comes to rest (first solid hit)."""
        DX, DY, DZ = bot.room_dims
        t, prev = 0.0, p0
        while t < max_t:
            t += dt
            p = p0 + v * t + pun.Vector3(0, -0.5 * g * t * t, 0)
            if solid(p):
                gx = int(math.floor(prev.x * 2 + DX / 2)); gz = int(math.floor(prev.z * 2 + DZ / 2))
                gy = max(int(math.floor(prev.y * 2)), 0)
                if not (0 <= gx < DX and 0 <= gz < DZ and gy < DY) or (gx, gy, gz) in live.cells:
                    gy = live.top(gx, gz) if (0 <= gx < DX and 0 <= gz < DZ) else -1
                    if gy < 0 or gy >= DY:
                        return None
                return t, (gx, gy, gz)
            prev = p
        return None

    def place_block(cell, color):
        async def go():
            gx, gy, gz = cell
            await bot.rpc(bot.view_id, "RpcMapObjectCreated",
                          ["1x1_stuffed", gx, gy, gz, 1, 2, color, account, False], receivers=pun.RECV_OTHERS)
        return go

    async def knockback(pos, radius=6.0, power=5.0):
        """Throw every player near the blast away from it (and up)."""
        hit = 0
        for act, props in list(bot.actors.items()):
            if act == bot.actor or not isinstance(props, dict) or not props.get(253):
                continue
            pose = bot.pose_of(act)
            if not pose:
                continue
            d = pose[0] - pos
            dist = math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z)
            if dist > radius:
                continue
            flat = math.hypot(d.x, d.z) or 1.0
            k = power * (1.2 - dist / radius)             # closer = harder
            force = pun.Vector3(d.x / flat * k, k * 0.8 + 4.0, d.z / flat * k)
            await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback", [props[253], force, 3, None, -1],
                          receivers=pun.RECV_OTHERS)
            hit += 1
        return hit

    fx_booms = []                                   # (time, where, shooter account, name) of recent shots

    async def pellet_shot(muzzle, rot, shooter=None):
        await shoot(muzzle, q_dir(rot), shooter)

    async def shoot(muzzle, d, shooter=None):
        if fx["shots"] >= FX_MAX_SHOTS:
            return
        hit, dist = raycast(muzzle + d * 0.4, d)
        bd = boss_ray(muzzle + d * 0.4, d) if boss.get("on") else None
        boss_hit = bd is not None and bd < dist
        if boss_hit:                                     # the boss is in the way: hit it
            dist = bd
            hit = muzzle + d * (0.4 + bd)
        # face normal: which axis did we cross into the solid cell on
        nxt = hit + d * 0.15
        DX, _, DZ = bot.room_dims
        a = (int(math.floor(hit.x * 2)), int(math.floor(hit.y * 2)), int(math.floor(hit.z * 2)))
        b = (int(math.floor(nxt.x * 2)), int(math.floor(nxt.y * 2)), int(math.floor(nxt.z * 2)))
        normal = pun.Vector3(-(b[0] - a[0]), -(b[1] - a[1]), -(b[2] - a[2]))
        if (normal.x, normal.y, normal.z) == (0, 0, 0):
            normal = d * -1
        speed = 16.0
        fx["shots"] += 1

        async def boom_here():
            fx["shots"] -= 1
            if shooter is not None:
                fx_booms.append((time.monotonic(), hit, acct_of(shooter), pname(shooter)))
                del fx_booms[:-30]
            if boss_hit:
                await boss_damage(shooter, 3, hit)
                await explode(hit, d * -1, land=False, power=0.0, radius=0.1)
            else:
                await explode(hit, normal, land=not (boss.get("on") or koth.get("on")))

        await fx_add(muzzle + d * 0.4, random.choice(GLOW), vel=d * speed, gravity=0.0,
                     life=max(dist / speed, 0.15), spin=14.0, on_end=boom_here)

    def on_pellet(code, sender, data):
        if code != pun.EV_RPC or fx["pellet_actor"] is None or sender != fx["pellet_actor"]:
            return
        r = pun.decode_rpc(data)
        if r and r["method"] == "RpcCreateProjectileFromPlayerOrigin" and len(r["args"]) > 2 \
                and r["args"][0] == "pellet" and isinstance(r["args"][1], pun.Vector3):
            asyncio.get_running_loop().create_task(pellet_shot(r["args"][1], r["args"][2], sender))

    bot.handlers.append(on_pellet)

    # --- buttcoins: real game items, orbiting a player / a planet in the sky ----
    co = {"items": {}, "orbit": None, "orbit_n": 3, "orbit_r": 1.3, "planet": None, "t0": time.monotonic(),
          "tick": 0}
    track(co, "orbit")
    ZERO, IDENT = pun.Vector3(), pun.Quaternion(1, 0, 0, 0)

    def yaw_q(a):
        return pun.Quaternion(math.cos(a / 2), 0.0, math.sin(a / 2), 0.0)

    async def item_create(key, pos, kind):
        iid = rand_id()
        await bot.rpc(bot.view_id, "RpcCreateItem",
                      [iid, key, 0, None, pos, IDENT, ZERO, ZERO, TypedArray(ord("s"), []), Byte(1), Byte(1)],
                      receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcItemTransformation", [user_id, iid, 1, None, pos, IDENT, ZERO, ZERO],
                      receivers=pun.RECV_OTHERS)
        co["items"][iid] = {"key": key, "pos": pos, "kind": kind}
        return iid

    async def item_move(iid, pos, rot):
        await bot.rpc(bot.view_id, "RpcItemTransformation", [user_id, iid, 1, None, pos, rot, ZERO, ZERO],
                      receivers=pun.RECV_OTHERS)
        co["items"][iid]["pos"] = pos

    async def items_clear(kind=None):
        for iid, it in list(co["items"].items()):
            if kind is None or it["kind"] == kind:
                await bot.rpc(bot.view_id, "RpcDestroyItem", [iid], receivers=pun.RECV_OTHERS)
                co["items"].pop(iid, None)

    def on_item_gone(code, sender, data):
        # someone picked up / destroyed one of our coins: forget it, the loop re-makes it
        if code == pun.EV_RPC and sender != bot.actor:
            r = pun.decode_rpc(data)
            if r and r["method"] == "RpcDestroyItem" and r["args"] and r["args"][0] in co["items"]:
                co["items"].pop(r["args"][0], None)

    bot.handlers.append(on_item_gone)

    def by_kind(kind):
        return [iid for iid, it in co["items"].items() if it["kind"] == kind]

    def tumble_axis():
        ax = [random.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in ax)) or 1
        return [x / n for x in ax]

    BALL_COLORS = ["yellow_emissive", "orange_emissive", "yellow", "orange_light", "yellow_light"]

    async def ball_clear():
        pl = co["planet"]
        if not pl:
            return
        for sl in pl["slots"]:
            if sl.get("pid"):
                for m in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed"):
                    await bot.rpc(bot.view_id, m, [sl["pid"]], receivers=pun.RECV_OTHERS)
                sl["pid"] = None

    async def planet_build(center, ball_r=3.2, n_ball=160):
        """A spinning ball of buttcoins (Fibonacci sphere, every coin tumbling) with
        two tilted rings orbiting it. Positions are recomputed each update."""
        await ball_clear(); await items_clear("ball"); await items_clear("ring")
        golden = math.pi * (3 - math.sqrt(5))
        slots = []
        for i in range(n_ball):
            y = 1 - 2 * (i + 0.5) / n_ball
            r = math.sqrt(1 - y * y)
            th = golden * i
            slots.append({"base": (math.cos(th) * r, y, math.sin(th) * r), "axis": tumble_axis(),
                          "w": random.uniform(2.0, 5.0), "phase": random.uniform(0, 6.3), "iid": None})
        rings = [{"rad": 5.2, "tilt": 0.35, "n": 16, "w": 0.45}, {"rad": 6.8, "tilt": -0.25, "n": 16, "w": -0.3}]
        ring_slots = [{"ring": ri, "k": k, "iid": None} for ri, rg in enumerate(rings) for k in range(rg["n"])]
        co["planet"] = {"c": center, "r": ball_r, "spin": 0.35, "slots": slots, "rings": rings,
                        "ring_slots": ring_slots, "group": 0}
        t = time.monotonic() - co["t0"]
        for i, sl in enumerate(slots):
            sl["pid"] = rand_id()
            await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated",
                          [user_id, sl["pid"], "1x1_stuffed", random.choice(BALL_COLORS)], receivers=pun.RECV_OTHERS)
            await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated",
                          [sl["pid"], ball_pos(sl, t), spin_q(sl["axis"], sl["phase"]), True], receivers=pun.RECV_OTHERS)
            if i % 8 == 7:
                await asyncio.sleep(0.02)
        for rs in ring_slots:
            rs["iid"] = await item_create("buttcoin", ring_pos(rs, t), "ring")

    def ball_pos(sl, t):
        pl = co["planet"]
        a = pl["spin"] * t                       # whole ball turning about the up axis
        bx, by, bz = sl["base"]
        x, z = bx * math.cos(a) - bz * math.sin(a), bx * math.sin(a) + bz * math.cos(a)
        return pl["c"] + pun.Vector3(x, by, z) * pl["r"]

    def ring_pos(rs, t):
        pl = co["planet"]
        rg = pl["rings"][rs["ring"]]
        a = rg["w"] * t + 2 * math.pi * rs["k"] / rg["n"]
        x, z = math.cos(a) * rg["rad"], math.sin(a) * rg["rad"]
        return pl["c"] + pun.Vector3(x, z * math.sin(rg["tilt"]), z * math.cos(rg["tilt"]))

    async def coin_tick():
        co["tick"] += 1
        t = time.monotonic() - co["t0"]
        # orbit coins around a player: 8 updates a second
        if co["orbit"] is not None:
            pose = bot.pose_of(co["orbit"])
            mine = by_kind("orbit")
            while len(mine) < co["orbit_n"] and pose:
                mine.append(await item_create("buttcoin", pose[0], "orbit"))
            if pose:
                c = pose[0] + pun.Vector3(0, 0.55, 0)
                for k, iid in enumerate(mine[:co["orbit_n"]]):
                    a = 1.8 * t + 2 * math.pi * k / max(len(mine), 1)
                    p = c + pun.Vector3(math.cos(a) * co["orbit_r"], 0.25 * math.sin(2 * a), math.sin(a) * co["orbit_r"])
                    await item_move(iid, p, yaw_q(6 * t + k))
        pl = co["planet"]
        if not pl:
            return
        # ball: a quarter of the coins per tick (each coin moves twice a second),
        # every coin tumbling on its own axis while the ball turns
        g = pl["group"] = (pl["group"] + 1) % 4
        for i in range(g, len(pl["slots"]), 4):
            sl = pl["slots"][i]
            if sl.get("pid"):
                await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated",
                              [sl["pid"], ball_pos(sl, t), spin_q(sl["axis"], sl["w"] * t + sl["phase"]), True],
                              receivers=pun.RECV_OTHERS)
        # rings: about 3 updates a second
        if co["tick"] % 3 == 0:
            for rs in pl["ring_slots"]:
                pos = ring_pos(rs, t)
                if rs["iid"] not in co["items"]:
                    rs["iid"] = await item_create("buttcoin", pos, "ring"); continue
                await item_move(rs["iid"], pos, yaw_q(3 * t + rs["k"]))

    async def coin_loop():
        while True:
            await asyncio.sleep(0.125)
            if co["orbit"] is not None or co["planet"]:
                try:
                    await coin_tick()
                except Exception as e:
                    print(f"coin error: {e!r}")

    coin_task = asyncio.create_task(coin_loop())

    # --- apple + screen: a video on a screen of blocks, with the song -------------
    ap = {"task": None, "stop": False, "screen": None, "on": set(), "pick": None, "voice": None}

    def find_video(arg):
        if arg and os.path.isfile(os.path.expanduser(arg)):
            return os.path.expanduser(arg)
        uploaded = os.getenv("YEEPS_BAD_APPLE_MEDIA", "")
        if not arg and uploaded and os.path.isfile(uploaded) and os.path.getsize(uploaded) > 0:
            return uploaded
        bundled = os.path.join(HERE, "media", "bad_apple.mp4")   # archive.org/details/TouhouBadApple
        if not arg and os.path.isfile(bundled):
            return bundled
        import glob
        for pat in ("~/Downloads/*[Bb]ad*[Aa]pple*", "~/Videos/*[Bb]ad*[Aa]pple*", "~/Desktop/*[Bb]ad*[Aa]pple*"):
            hits = sorted(glob.glob(os.path.expanduser(pat)))
            if hits:
                return hits[0]
        return None

    def decode_frames(path, w, h, fps):
        """Video or audio waveform -> frames of lit pixels."""
        import subprocess, imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if path.lower().endswith((".mp3", ".wav", ".ogg")):
            graph = f"[0:a]showwaves=s={w}x{h}:mode=cline:rate={fps}:colors=white,format=gray[v]"
            cmd = [ffmpeg, "-v", "error", "-i", path, "-filter_complex", graph,
                   "-map", "[v]", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        else:
            cmd = [ffmpeg, "-v", "error", "-i", path,
                   "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        n = len(raw) // (w * h)
        frames = []
        for i in range(n):
            fr = raw[i * w * h:(i + 1) * w * h]
            # pure black/white (Bad Apple) -> cut at 128; any other video -> this
            # frame's own average, so dark or bright footage still shows shapes
            extreme = sum(1 for px in fr if px < 40 or px > 215)
            cut = 128 if extreme > 0.85 * len(fr) else max(sum(fr) / len(fr), 1)
            frames.append({(k % w, k // w) for k, px in enumerate(fr) if px >= cut})
        return frames

    def size_for(name):
        DX, DY, DZ = bot.room_dims
        if name == "small":
            return 1, 24, 18, 6
        sc = 2
        w = min((min(DX, DZ) - 2) // sc, 46)
        return sc, w, min(int(w * 3 / 4), (DY - 4) // sc), 5

    def cell_world(c, sc=1):
        DX, DY, DZ = bot.room_dims
        return pun.Vector3((c[0] + sc / 2 - DX / 2) * 0.5, (c[1] + sc / 2) * 0.5, (c[2] + sc / 2 - DZ / 2) * 0.5)

    def make_layout(axis, s, cu, y0, plane, size):
        """Screen geometry. axis 'z'|'x' = the axis the viewer looks along, s = +1/-1
        which way along it; cu = centre cell across the screen; y0 = bottom cell;
        plane = cell of the pixel layer (the black board sits one block further away)."""
        sc, w, h, fps = size
        DX, DY, DZ = bot.room_dims
        half = (w * sc) // 2
        y0 = max(0, min(y0, DY - h * sc))
        right, along, lim = (s, DX, DZ) if axis == "z" else (-s, DZ, DX)
        cu = min(max(cu, half + 1), along - half - 1)
        u0 = cu - half if right > 0 else cu + half - sc
        lo, hi = (0, lim - 2 * sc) if s > 0 else (sc, lim - sc)
        plane = min(max(plane, lo), hi)
        board = plane + s * sc

        def cell(c, r, depth):
            u, y = u0 + right * c * sc, y0 + (h - 1 - r) * sc
            return (u, y, depth) if axis == "z" else (depth, y, u)

        return {"pix": {(c, r): cell(c, r, plane) for r in range(h) for c in range(w)},
                "brd": {(c, r): cell(c, r, board) for r in range(h) for c in range(w)},
                "mid": cell(w // 2, h // 2, board + s * sc), "w": w, "h": h, "sc": sc, "fps": fps,
                "pattern": "2x2x2_stuffed" if sc == 2 else "1x1_stuffed", "built": set()}

    def facing(t):
        f = q_forward(bot.pose_of(t)[1])
        return ("z", 1 if f.z > 0 else -1) if abs(f.z) >= abs(f.x) else ("x", 1 if f.x > 0 else -1)

    def wall_layout(t, size):
        """No screen picked: the boundary wall the player faces, centred on them, 1 m up."""
        axis, s = facing(t)
        DX, DY, DZ = bot.room_dims
        sc = size[0]
        lim = DZ if axis == "z" else DX
        gx, _, gz = bot.grid_of(bot.pose_of(t)[0])
        plane = (lim - 1 - 2 * sc) if s > 0 else (1 + sc)
        return make_layout(axis, s, gx if axis == "z" else gz, 2, plane, size)

    async def put(cell, color, pattern):
        x, y, z = cell
        await bot.rpc(bot.view_id, "RpcMapObjectCreated", [pattern, x, y, z, 1, 2, color, account, False],
                      receivers=pun.RECV_OTHERS)

    async def take(cell, pattern):
        x, y, z = cell
        await bot.rpc(bot.view_id, "RpcDestroyMapObject", [pattern, x, y, z, account, 0, False],
                      receivers=pun.RECV_OTHERS)

    async def build_board(lay):
        n = 0
        for cell in lay["brd"].values():
            if cell not in live.cells and cell not in lay["built"]:
                await put(cell, "black", lay["pattern"]); lay["built"].add(cell); n += 1
                if n % 40 == 0:
                    await asyncio.sleep(0.15)

    async def clear_pixels(lay):
        for c in list(ap["on"]):
            await take(c, lay["pattern"]); ap["on"].discard(c)
            await asyncio.sleep(0.004)

    async def remove_screen(lay):
        await clear_pixels(lay)
        for c in list(lay["built"]):
            await take(c, lay["pattern"]); lay["built"].discard(c)
            await asyncio.sleep(0.004)

    def park_avatar(world):
        """Put the bot's avatar at `world` (inside a backboard block) folded up -
        head and hands pulled into the body - so nobody sees it but its voice
        plays from the screen."""
        tmpl = next((v for v in bot.poses.values() if v), None)
        if not tmpl:
            return
        v = list(tmpl[3:])
        v[0:3] = [world.x, world.y - 0.25, world.z]
        v[3:7] = [0.0, 0.0, 0.0, 1.0]
        v[7:10] = [0.0, 0.05, 0.0]                  # head
        v[14:17] = [0.0, 0.0, 0.0]                  # right hand
        v[24:27] = [0.0, 0.0, 0.0]                  # left hand
        state["mimic"] = None
        state["last"] = v

    async def strip_cosmetics():
        mine = bot.actors.get(bot.actor) or {}
        cur_c = mine.get("activeCosmeticDatas")
        if cur_c is None:
            return
        empty = TypedArray(cur_c.code, []) if isinstance(cur_c, TypedArray) else type(cur_c)()
        await bot.set_props({"activeCosmeticDatas": empty})

    async def apple_play(path, lay, fps, temporary, audio):
        name = os.path.basename(path)
        w, h = lay["w"], lay["h"]
        print(f"apple: converting {name} to {w}x{h} at {fps} fps...")
        try:
            frames = await asyncio.to_thread(decode_frames, path, w, h, fps)
        except Exception as e:
            print(f"apple: couldn't decode {name} ({e!r})")
            ap["task"] = None
            return
        if not frames:
            print("apple: media produced no visual frames")
            ap["task"] = None
            return
        packets = []
        if audio:
            try:
                from photon.voice import opus_packets
                packets = await asyncio.to_thread(opus_packets, path, os.path.join(HERE, "media"))
            except Exception as e:
                print(f"apple: no audio ({e!r})")
        if not lay["built"]:
            print("apple: building the screen...")
            try:
                await build_board(lay)
            except Exception as e:
                print(f"apple: screen construction failed ({e!r})")
                ap["task"] = None
                return
        voice = None
        if packets:
            try:
                from photon.voice import VoiceSender
                await bot.spawn_speaker()
                await strip_cosmetics()
                hide_in = lay["brd"].get((w // 2, h // 2))          # a black board block mid-screen
                spot = cell_world(hide_in, lay["sc"]) if hide_in else cell_world(lay["mid"], lay["sc"])
                park_avatar(spot)
                state["speaker"] = spot                            # the song comes from the screen
                voice = VoiceSender(cur["room"], bot.actor * 1000 + 2, a.name)
                await voice.connect()
                ap["voice"] = voice
                await asyncio.sleep(1.0)              # let everyone link the voice to our speaker
            except Exception as e:
                print(f"apple: voice failed ({e!r}) - playing without sound")
                voice = None
        print(f"apple: playing {len(frames) / fps:.0f} s{' with sound' if voice else ''} - 'apple off' to stop")
        cells = lay["pix"]
        cap = 150 if lay["sc"] == 2 else max(60, w * h // 6)
        start = time.monotonic() + 0.3

        async def video():
            backlog_add, backlog_del = set(), set()
            for i, lit in enumerate(frames):
                if ap["stop"]:
                    break
                want = {cells[p] for p in lit if p in cells}
                backlog_add = (backlog_add | (want - ap["on"])) & want
                backlog_del = (backlog_del | (ap["on"] - want)) - want
                budget = cap
                for c in list(backlog_del)[:budget]:
                    await take(c, lay["pattern"]); ap["on"].discard(c); backlog_del.discard(c); budget -= 1
                for c in list(backlog_add)[:max(budget, 0)]:
                    await put(c, "white", lay["pattern"]); ap["on"].add(c); backlog_add.discard(c)
                delay = start + (i + 1) / fps - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        try:
            jobs = [video()]
            if voice:
                jobs.append(voice.play(packets, start, stop=lambda: ap["stop"]))
            await asyncio.gather(*jobs)
            await asyncio.sleep(1.5)
        finally:
            if voice:
                await voice.close(); ap["voice"] = None
            state["speaker"] = None
            state["last"] = None
            t0 = bot.actor_by_name(who)
            if t0 is not None:
                ensure_avatar(t0)
            print("apple: finished - clearing")
            if temporary:
                await remove_screen(lay)
            else:
                await clear_pixels(lay)
            ap["task"] = None

    def on_screen_pick(code, sender, data):
        """'screen' is waiting: the owner's next placed 1x1 block marks the spot."""
        pk = ap["pick"]
        if not pk or code != pun.EV_RPC or sender != pk["actor"]:
            return
        r = pun.decode_rpc(data)
        if not r or r["method"] != "RpcMapObjectCreated" or not str(r["args"][0]).startswith("1x1_"):
            return
        ap["pick"] = None
        m = (int(r["args"][1]), int(r["args"][2]), int(r["args"][3]))
        asyncio.get_running_loop().create_task(screen_at(m, str(r["args"][0]), pk))

    bot.handlers.append(on_screen_pick)

    async def screen_at(marker, marker_pattern, pk):
        await asyncio.sleep(0.3)
        if marker_pattern:
            await take(marker, marker_pattern)        # remove the marker pillow
        live.cells.discard(marker)
        if ap["screen"]:
            await remove_screen(ap["screen"])
        # always face into the room: parallel to the nearest wall, viewer looking at it
        DX, DY, DZ = bot.room_dims
        mx, my, mz = marker
        walls = [(DZ - 1 - mz, "z", 1), (mz, "z", -1), (DX - 1 - mx, "x", 1), (mx, "x", -1)]
        _, axis, s = min(walls)
        cu = mx if axis == "z" else mz
        plane = mz if axis == "z" else mx
        sc, w, h, _fps = pk["size"]
        lay = make_layout(axis, s, cu, my, plane, pk["size"])   # marker = bottom-middle
        ap["screen"] = lay
        print(f"\nscreen: building {lay['w']}x{lay['h']} ({lay['w'] * lay['sc'] / 2:.0f} x "
              f"{lay['h'] * lay['sc'] / 2:.0f} m) at your marker...")
        try:
            await build_board(lay)
        except Exception as e:
            print(f"screen: construction failed ({e!r}); removing partial screen")
            try:
                await remove_screen(lay)
            finally:
                ap["screen"] = None
            return
        print("screen ready - type apple to play on it, 'screen off' to remove it\nbot> ", end="", flush=True)

    pixel_art = {"pick": None, "lay": None, "shown": set()}
    pixel_palette = {
        "black": (10, 10, 10), "gray_dark": (65, 65, 65), "gray": (135, 135, 135),
        "white": (245, 245, 245), "red_dark": (125, 35, 40), "red": (220, 55, 60),
        "orange": (245, 125, 40), "yellow": (245, 220, 55), "green_dark": (45, 105, 55),
        "green": (55, 170, 75), "cyan": (45, 175, 190), "blue_dark": (45, 70, 145),
        "blue": (65, 115, 220), "purple": (135, 70, 190), "pink": (230, 115, 175),
        "brown": (130, 85, 55),
    }

    def marker_layout(marker, size):
        DX, DY, DZ = bot.room_dims
        mx, my, mz = marker
        walls = [(DZ - 1 - mz, "z", 1), (mz, "z", -1), (DX - 1 - mx, "x", 1), (mx, "x", -1)]
        _, axis, s = min(walls)
        cu = mx if axis == "z" else mz
        plane = mz if axis == "z" else mx
        return make_layout(axis, s, cu, my, plane, size)

    def pixel_art_colors(path, max_w, max_h):
        from PIL import Image
        with Image.open(path) as source:
            source = source.convert("RGB")
            scale = min(max_w / source.width, max_h / source.height, 1.0)
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            source = source.resize((width, height), Image.Resampling.LANCZOS)
            pixels = list(source.getdata())
        palette = list(pixel_palette.items())
        return width, height, [
            min(palette, key=lambda entry: sum((pixel[i] - entry[1][i]) ** 2 for i in range(3)))[0]
            for pixel in pixels
        ]

    async def remove_pixel_art():
        lay = pixel_art["lay"]
        if not lay:
            pixel_art["shown"].clear()
            return
        for cell in list(pixel_art["shown"]):
            await take(cell, lay["pattern"])
            pixel_art["shown"].discard(cell)
            await asyncio.sleep(0.004)
        await remove_screen(lay)
        pixel_art["lay"] = None

    async def pixel_art_at(marker, marker_pattern):
        await asyncio.sleep(0.3)
        if marker_pattern:
            await take(marker, marker_pattern)
        live.cells.discard(marker)
        if pixel_art["lay"]:
            await remove_pixel_art()
        DX, DY, DZ = bot.room_dims
        path = os.getenv("YEEPS_PIXEL_ART_IMAGE", "")
        if not path or not os.path.isfile(path):
            print("pixel art: uploaded image is no longer available")
            return
        max_w = max(1, min(32, DX - 4, DZ - 4))
        max_h = max(1, min(24, DY - 4))
        try:
            width, height, colors = pixel_art_colors(path, max_w, max_h)
        except Exception as exc:
            print(f"pixel art: couldn't read the uploaded image ({exc!r})")
            return
        lay = marker_layout(marker, (1, width, height, 0))
        pixel_art["lay"] = lay
        print(f"\npixel art: building {width}x{height} pillow image...")
        try:
            await build_board(lay)
            cells = lay["pix"]
            for index, color in enumerate(colors):
                if color == "black":
                    continue
                cell = cells[(index % width, index // width)]
                await put(cell, color, lay["pattern"])
                pixel_art["shown"].add(cell)
                if len(pixel_art["shown"]) % 40 == 0:
                    await asyncio.sleep(0.15)
        except Exception as exc:
            print(f"pixel art: construction failed ({exc!r}); removing partial image")
            await remove_pixel_art()
            return
        print("pixel art ready - use 'pixelart off' to remove it")

    def on_pixel_art_pick(code, sender, data):
        pk = pixel_art["pick"]
        if not pk or code != pun.EV_RPC or sender != pk["actor"]:
            return
        r = pun.decode_rpc(data)
        if not r or r["method"] != "RpcMapObjectCreated" or len(r["args"]) < 4 \
                or not str(r["args"][0]).startswith("1x1_"):
            return
        pixel_art["pick"] = None
        marker = (int(r["args"][1]), int(r["args"][2]), int(r["args"][3]))
        asyncio.get_running_loop().create_task(pixel_art_at(marker, str(r["args"][0])))

    bot.handlers.append(on_pixel_art_pick)

    soundkey = {"task": None, "stop": False, "voice": None}

    async def soundkey_play(target):
        voice = None
        try:
            path = os.getenv("YEEPS_SOUNDKEY_AUDIO", "")
            if not path or not os.path.isfile(path) or not os.path.getsize(path):
                print("soundkey: uploaded audio is not available")
                return
            print("soundkey: preparing uploaded audio...")
            from photon.voice import opus_packets, VoiceSender
            packets = await asyncio.to_thread(opus_packets, path, os.path.join(HERE, "media"))
            if not packets:
                print("soundkey: audio contains no playable packets")
                return
            await bot.spawn_speaker()
            await strip_cosmetics()
            pose = bot.pose_of(target)
            spot = pose[0] + pun.Vector3(0, 0.6, 0) if pose else pun.Vector3(0, -2, 0)
            park_avatar(spot)
            state["speaker"] = spot
            voice = VoiceSender(cur["room"], bot.actor * 1000 + 2, a.name)
            soundkey["voice"] = voice
            await voice.connect()
            await asyncio.sleep(1.0)
            print("soundkey: playing uploaded audio")
            await voice.play(packets, time.monotonic() + 0.3, stop=lambda: soundkey["stop"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"soundkey: playback failed ({exc!r})")
        finally:
            if voice:
                try:
                    await voice.close()
                except Exception as exc:
                    print(f"soundkey: voice disconnect failed ({exc!r})")
            soundkey["voice"] = None
            soundkey["task"] = None
            soundkey["stop"] = False
            state["speaker"] = None
            state["last"] = None
            tracked = bot.actor_by_name(who)
            if tracked is not None:
                ensure_avatar(tracked)

    async def soundkey_stop():
        soundkey["stop"] = True
        task = soundkey["task"]
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        soundkey["task"] = None
        soundkey["stop"] = False

    # --- Bad Apple text animation on the first sign the followed player places ---
    # The sign drops messages longer than about 60 characters. Keep the 5-line
    # frame and rich-text size tags within that limit.
    asg = {"on": False, "actor": None, "signs": {}, "task": None, "stop": False, "frames": {},
           "voice": None, "w": 8, "h": 5, "fps": 8, "plain": True}

    def decode_sign_frames(path, w, h, fps):
        """Video or audio waveform -> frames of lit pixels."""
        import subprocess, imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if path.lower().endswith((".mp3", ".wav", ".ogg")):
            graph = f"[0:a]showwaves=s={w}x{h}:mode=cline:rate={fps}:colors=white,format=gray[v]"
            cmd = [ffmpeg, "-v", "error", "-i", path, "-filter_complex", graph,
                   "-map", "[v]", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        else:
            cmd = [ffmpeg, "-v", "error", "-i", path, "-vf",
                   f"fps={fps},crop='min(iw,ih)':'min(iw,ih)',scale={w}:{h}:flags=area,format=gray",
                   "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        return [{(k % w, k // w) for k, px in enumerate(raw[i * w * h:(i + 1) * w * h]) if px >= 128}
                for i in range(len(raw) // (w * h))]
    SIGN = "1x1x2_textSign"

    def sign_text(lit, w, h, plain):
        """One compact rich-text frame that fits the sign's message limit."""
        pixels = "\n".join("".join("_" if (x, y) in lit else "#" for x in range(w)) for y in range(h))
        return f"<size=10>{pixels}</size>"

    async def sign_say(cell, text):
        x, y, z = cell
        await bot.rpc(bot.view_id, "RpcSimpleCustomMapObjectMessage", [SIGN, x, y, z, f"{account}|{text}"],
                      receivers=pun.RECV_OTHERS)

    def on_sign_pick(code, sender, data):
        if code != pun.EV_RPC:
            return
        r = pun.decode_rpc(data)
        if not r:
            return
        a_ = r["args"] or []
        if r["method"] == "RpcDestroyMapObject" and len(a_) >= 4 and str(a_[0]) == SIGN:
            try:
                cell = (int(a_[1]), int(a_[2]), int(a_[3]))
            except (TypeError, ValueError):
                return
            asg["signs"].pop(cell, None)
            if not asg["signs"] and asg["task"]:
                asg["stop"] = True
            return
        if r["method"] == "RpcSimpleCustomMapObjectMessage" and len(a_) >= 5 \
                and str(a_[0]) == SIGN and sender != bot.actor:
            try:
                cell = (int(a_[1]), int(a_[2]), int(a_[3]))
            except (TypeError, ValueError):
                return
            entry = asg["signs"].get(cell)
            message = str(a_[4])
            if entry is not None and "|" in message:
                entry["original_text"] = message.split("|", 1)[1]
            return
        if not asg["on"] or sender != asg["actor"] or r["method"] != "RpcMapObjectCreated" \
                or not a_ or str(a_[0]) != SIGN or len(a_) < 5:
            return
        try:
            cell = (int(a_[1]), int(a_[2]), int(a_[3]))
        except (TypeError, ValueError):
            return
        if asg["signs"]:
            return
        asg["signs"][cell] = {"original_text": ""}
        asyncio.get_running_loop().create_task(start_sign_play())

    bot.handlers.append(on_sign_pick)

    async def start_sign_play():
        await asyncio.sleep(0.5)
        if asg["on"] and asg["signs"] and not asg["task"]:
            asg["task"] = asyncio.get_running_loop().create_task(sign_play())
            print("\napplesign: animating the first sign you placed\nbot> ", end="", flush=True)

    async def sign_play():
        w, h, fps = asg["w"], asg["h"], asg["fps"]
        try:
            path = find_video(None)
            key = (path, w, h, fps)
            if key not in asg["frames"]:
                print("\napplesign: converting the video...")
                asg["frames"][key] = await asyncio.to_thread(decode_sign_frames, path, w, h, fps)
            frames = asg["frames"][key]
            voice = None
            try:
                from photon.voice import opus_packets, VoiceSender
                packets = await asyncio.to_thread(opus_packets, path, os.path.join(HERE, "media"))
                first = next(iter(asg["signs"]), None)
                if packets and first is not None:
                    await bot.spawn_speaker()
                    await strip_cosmetics()
                    sw = cell_world(first)
                    spot = pun.Vector3(sw.x, first[1] * 0.5 - 1.0, sw.z)   # under the ground below the sign
                    park_avatar(spot)
                    state["speaker"] = spot
                    voice = VoiceSender(cur["room"], bot.actor * 1000 + 2, a.name)
                    await voice.connect()
                    asg["voice"] = voice
                    await asyncio.sleep(1.0)
            except Exception as e:
                print(f"applesign: playing without sound ({e!r})")
                packets, voice = [], None
            start = time.monotonic() + 0.3

            async def video():
                for i, lit in enumerate(frames):
                    if asg["stop"] or not asg["signs"]:
                        break
                    txt = sign_text(lit, w, h, asg["plain"])
                    for c in list(asg["signs"]):
                        await sign_say(c, txt)
                    delay = start + (i + 1) / fps - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)

            jobs = [video()]
            if voice:
                jobs.append(voice.play(packets, start, stop=lambda: asg["stop"] or not asg["signs"]))
            await asyncio.gather(*jobs)
        except Exception as e:
            print(f"applesign error: {e!r}")
        finally:
            if asg["voice"]:
                try:
                    await asg["voice"].close()
                except Exception as e:
                    print(f"applesign: couldn't close voice ({e!r})")
                asg["voice"] = None
            state["speaker"] = None
            state["last"] = None
            t0 = bot.actor_by_name(who)
            if t0 is not None:
                ensure_avatar(t0)
            for c, entry in list(asg["signs"].items()):
                try:
                    await sign_say(c, entry["original_text"])
                except Exception as e:
                    print(f"applesign: couldn't restore sign at {c} ({e!r})")
            asg["task"] = None
            asg["stop"] = False

    async def signs_off():
        asg["on"] = False
        asg["stop"] = True
        task = asg["task"]
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asg["signs"].clear()
        asg["stop"] = False

    # --- arcade: tetris + snake on boards of blocks; grab control blocks to play --
    TET_SHAPES = {
        "I": ([(0, 0), (1, 0), (2, 0), (3, 0)], "cyan"),
        "O": ([(0, 0), (1, 0), (0, 1), (1, 1)], "yellow"),
        "T": ([(0, 0), (1, 0), (2, 0), (1, 1)], "purple"),
        "S": ([(0, 0), (1, 0), (1, 1), (2, 1)], "green"),
        "Z": ([(1, 0), (2, 0), (0, 1), (1, 1)], "red"),
        "J": ([(0, 0), (1, 0), (2, 0), (0, 1)], "blue"),
        "L": ([(0, 0), (1, 0), (2, 0), (2, 1)], "orange"),
    }
    GAMES = {
        # board (cols, rows) inside the frame, frame on top too?, controls left->right
        "tetris": {"size": (10, 20), "top": False,
                   "ctrl": [("left", "blue"), ("rotate", "purple"), ("right", "red"), ("drop", "green")]},
        "pong": {"size": (24, 14), "top": True,
                 "ctrl": [("upx3", "blue"), ("up", "blue"), ("down", "blue"), ("downx3", "blue")]},
        "snake": {"size": (20, 14), "top": True, "plus": True,
                  "ctrl": [("left", "blue"), ("up", "green"), ("down", "yellow"), ("right", "red")]},
        "flappy": {"size": (16, 12), "top": True, "ctrl": [("flap", "yellow")]},
        "minesweeper": {"size": (8, 8), "top": True,
                        "ctrl": [("reveal-mode", "cyan"), ("flag-mode", "yellow")]},
        "tictactoe": {"size": (3, 3), "top": True, "board_only": True},
        "connectfour": {"size": (7, 6), "top": True, "board_only": True},
    }
    P2_CTRL = [("upx3_2", "red"), ("up2", "red"), ("down2", "red"), ("downx3_2", "red")]
    gm = {"kind": None, "lay": None, "shown": {}, "controls": {}, "gone": {}, "queue": [], "task": None,
          "pick": None, "over": False, "score": 0, "lines": 0, "locked": {}, "piece": None,
          "body": [], "dir": (1, 0), "food": None, "next_dir": None, "interact_cells": {},
          "end_message": ""}

    def g_cell(gc, gr):
        """game column/row (row 0 = bottom) -> block cell, inside the 1-wide frame"""
        lay = gm["lay"]
        return lay["pix"][(gc + 1, lay["h"] - 2 - gr)]

    def g_frame():
        lay, want = gm["lay"], {}
        for c in range(lay["w"]):
            want[lay["pix"][(c, lay["h"] - 1)]] = "gray"
            if GAMES[gm["kind"]]["top"]:
                want[lay["pix"][(c, 0)]] = "gray"
        for r in range(lay["h"]):
            want[lay["pix"][(0, r)]] = "gray"; want[lay["pix"][(lay["w"] - 1, r)]] = "gray"
        return want

    async def g_render(want):
        pat = gm["lay"]["pattern"]
        for cell, col in list(gm["shown"].items()):
            if want.get(cell) != col:
                await take(cell, pat); del gm["shown"][cell]
        for cell, col in want.items():
            if cell not in gm["shown"]:
                await put(cell, col, pat); gm["shown"][cell] = col

    def g_say(msg):
        print(f"\n{msg}\nbot> ", end="", flush=True)

    # tetris rules
    def t_rotate(cells):
        r = [(c[1], -c[0]) for c in cells]
        mx, my = min(x for x, _ in r), min(y for _, y in r)
        return [(x - mx, y - my) for x, y in r]

    def t_fits(cells, ox, oy):
        TW, TH = GAMES["tetris"]["size"]
        return all(0 <= ox + cx < TW and oy + cy >= 0 and (ox + cx, oy + cy) not in gm["locked"]
                   for cx, cy in cells)

    def t_spawn():
        TW, TH = GAMES["tetris"]["size"]
        cells, color = TET_SHAPES[random.choice(list(TET_SHAPES))]
        p = {"cells": list(cells), "color": color, "x": TW // 2 - 2, "y": TH - 2}
        return p if t_fits(p["cells"], p["x"], p["y"]) else None

    def t_lock():
        TW, TH = GAMES["tetris"]["size"]
        pc = gm["piece"]
        for cx, cy in pc["cells"]:
            gm["locked"][(pc["x"] + cx, pc["y"] + cy)] = pc["color"]
        full = [y for y in range(TH) if all((x, y) in gm["locked"] for x in range(TW))]
        if full:
            gm["locked"] = {(x, y - sum(1 for f in full if f < y)): col
                            for (x, y), col in gm["locked"].items() if y not in full}
            gm["lines"] += len(full)
            gm["score"] += {1: 100, 2: 300, 3: 500, 4: 800}[len(full)]
            g_say(f"tetris: {len(full)} line{'s' if len(full) > 1 else ''}! score {gm['score']}, lines {gm['lines']}")
        gm["piece"] = t_spawn()

    def t_act(action):
        pc = gm["piece"]
        if not pc:
            return
        if action == "left" and t_fits(pc["cells"], pc["x"] - 1, pc["y"]):
            pc["x"] -= 1
        elif action == "right" and t_fits(pc["cells"], pc["x"] + 1, pc["y"]):
            pc["x"] += 1
        elif action == "rotate":
            rc = t_rotate(pc["cells"])
            for kick in (0, -1, 1, -2, 2):
                if t_fits(rc, pc["x"] + kick, pc["y"]):
                    pc["cells"], pc["x"] = rc, pc["x"] + kick
                    break
        elif action == "down" and t_fits(pc["cells"], pc["x"], pc["y"] - 1):
            pc["y"] -= 1
        elif action == "drop":
            while t_fits(pc["cells"], pc["x"], pc["y"] - 1):
                pc["y"] -= 1
            t_lock()

    def t_step():
        pc = gm["piece"]
        if pc and t_fits(pc["cells"], pc["x"], pc["y"] - 1):
            pc["y"] -= 1
        elif pc:
            t_lock()
        if gm["piece"] is None:
            gm["over"] = True

    def t_want():
        TW, TH = GAMES["tetris"]["size"]
        want = g_frame()
        for (x, y), col in gm["locked"].items():
            if y < TH:
                want[g_cell(x, y)] = col
        pc = gm["piece"]
        if pc:
            for cx, cy in pc["cells"]:
                if pc["y"] + cy < TH:
                    want[g_cell(pc["x"] + cx, pc["y"] + cy)] = "red_emissive" if gm["over"] else pc["color"]
        return want

    def t_speed():
        return max(0.15, 0.8 - 0.05 * (gm["lines"] // 5))

    # snake rules
    def s_new():
        SW, SH = GAMES["snake"]["size"]
        cx, cy = SW // 2, SH // 2
        gm.update(body=[(cx, cy), (cx - 1, cy), (cx - 2, cy)], dir=(1, 0), next_dir=None)
        s_food()

    def s_food():
        SW, SH = GAMES["snake"]["size"]
        free = [(x, y) for x in range(SW) for y in range(SH) if (x, y) not in gm["body"]]
        gm["food"] = random.choice(free) if free else None

    def s_act(action):
        d = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}.get(action)
        cur_d = gm["dir"]
        if d and (d[0] != -cur_d[0] or d[1] != -cur_d[1]):     # no instant reverse
            gm["next_dir"] = d

    def s_step():
        SW, SH = GAMES["snake"]["size"]
        if gm["next_dir"]:
            gm["dir"], gm["next_dir"] = gm["next_dir"], None
        hx, hy = gm["body"][0]
        nh = (hx + gm["dir"][0], hy + gm["dir"][1])
        if not (0 <= nh[0] < SW and 0 <= nh[1] < SH) or nh in gm["body"][:-1]:
            gm["over"] = True
            return
        gm["body"].insert(0, nh)
        if nh == gm["food"]:
            gm["score"] += 10
            s_food()
            g_say(f"snake: yum! length {len(gm['body'])}, score {gm['score']}")
        else:
            gm["body"].pop()

    def s_want():
        want = g_frame()
        if gm["food"]:
            want[g_cell(*gm["food"])] = "red"
        for i, (x, y) in enumerate(gm["body"]):
            want[g_cell(x, y)] = "red_emissive" if gm["over"] else ("green_light" if i == 0 else "green")
        return want

    def s_speed():
        return max(0.14, 0.4 - 0.01 * (len(gm["body"]) - 3))

    # pong rules: you = left paddle (blue), bot = right paddle (red), first to 5
    PADDLE = 4

    def p_new():
        PW, PH = GAMES["pong"]["size"]
        gm.update(pl=PH // 2 - PADDLE // 2, cpu=PH // 2 - PADDLE // 2, pts=[0, 0], hits=0)
        p_serve(1)

    def p_serve(direction):
        PW, PH = GAMES["pong"]["size"]
        gm["ball"] = [PW // 2, random.randint(3, PH - 4)]
        gm["vel"] = [direction, random.choice((-1, 1))]
        gm["hits"] = 0
        gm["serve_wait"] = time.monotonic() + 1.0

    def p_act(action):
        PW, PH = GAMES["pong"]["size"]
        side = "cpu" if action.endswith("2") else "pl"            # ...2 = right paddle
        n = 3 if "x3" in action else 1                           # one block per grab, x3 blocks 3
        if action.startswith("up"):
            gm[side] = min(gm[side] + n, PH - PADDLE)
        elif action.startswith("down"):
            gm[side] = max(gm[side] - n, 0)

    def p_hand():
        """pong hand: right-hand height (relative to your body) moves your paddle."""
        PW, PH = GAMES["pong"]["size"]
        t0 = bot.actor_by_name(who)
        vd = bot.poses.get(t0) if t0 is not None else None
        if not vd or len(vd) < 3 + 24:
            return
        hy = float(vd[3 + 15])                                # right hand y, body-local
        f = min(max((hy + 0.25) / 0.6, 0.0), 1.0)
        gm["pl"] = int(round(f * (PH - PADDLE)))

    def p_step():
        PW, PH = GAMES["pong"]["size"]
        if gm.get("hand"):
            p_hand()
        bx, by = gm["ball"]
        # bot paddle (1 player only): follows the ball, a little slow so you can win
        target = by - PADDLE // 2
        if not gm.get("twop") and random.random() < 0.8:
            gm["cpu"] += (target > gm["cpu"]) - (target < gm["cpu"])
        gm["cpu"] = min(max(gm["cpu"], 0), PH - PADDLE)
        if time.monotonic() < gm.get("serve_wait", 0):
            return
        vx, vy = gm["vel"]
        nx, ny = bx + vx, by + vy
        if ny < 0 or ny >= PH:                                  # top/bottom bounce
            vy = -vy; ny = by + vy
        for side, px, paddle in ((-1, 0, gm["pl"]), (1, PW - 1, gm["cpu"])):
            if vx == side and nx == px and paddle <= ny < paddle + PADDLE:
                vx = -vx
                off = ny - (paddle + PADDLE / 2 - 0.5)          # edge hits angle off
                vy = 1 if off > 0 else -1
                nx = bx + vx
                gm["hits"] += 1
        gm["vel"] = [vx, vy]
        if nx < 0 or nx >= PW:                                  # a point
            who_scored = 1 if nx < 0 else 0
            gm["pts"][who_scored] += 1
            names = ("blue", "red") if gm.get("twop") else ("you", "bot")
            g_say(f"pong: {names[who_scored]} scored - {names[0]} {gm['pts'][0]} : {gm['pts'][1]} {names[1]}")
            if max(gm["pts"]) >= 5:
                gm["over"] = True
                g_say(f"pong: {names[0] if gm['pts'][0] >= 5 else names[1]} wins!")
                return
            p_serve(1 if who_scored else -1)
            return
        gm["ball"] = [nx, ny]
        gm["score"] = gm["pts"][0]

    def p_want():
        PW, PH = GAMES["pong"]["size"]
        want = g_frame()
        for r in range(0, PH, 2):                               # dotted net
            want[g_cell(PW // 2, r)] = "gray"
        for i in range(PADDLE):
            want[g_cell(0, gm["pl"] + i)] = "blue"
            want[g_cell(PW - 1, gm["cpu"] + i)] = "red"
        bx, by = gm["ball"]
        if 0 <= bx < PW and 0 <= by < PH:
            want[g_cell(bx, by)] = "red_emissive" if gm["over"] else "yellow_emissive"
        p_scoreboard(want)
        return want

    def p_scoreboard(want):
        """Scoreboard LEFT of the board (as you face it): a blue bar and a red bar, 2 wide,
        standing on the board's bottom row, growing 2 blocks up per point (5 = win).
        Goes on the right side instead if the left side is outside the room."""
        lay = gm["lay"]
        DX, DY, DZ = bot.room_dims
        w, h, sc = lay["w"], lay["h"], lay["sc"]
        a0, a1 = lay["pix"][(0, h - 1)], lay["pix"][(1, h - 1)]
        step = (a1[0] - a0[0], a1[2] - a0[2])                  # one board column to the right

        def at(c, k):
            return (a0[0] + step[0] * c, a0[1] + sc * k, a0[2] + step[1] * c)

        def inside(cell):
            return 0 <= cell[0] < DX and 0 <= cell[1] < DY and 0 <= cell[2] < DZ

        cols = ((-7, -6), (-4, -3))                            # blue, red (1 gap, 2 from the board)
        if not inside(at(-7, 0)):
            cols = ((w + 2, w + 3), (w + 5, w + 6))
        for team, colour, cc in ((0, "blue", cols[0]), (1, "red", cols[1])):
            for k in range(2 * min(gm["pts"][team], 5)):
                for c in cc:
                    cell = at(c, k)
                    if inside(cell) and (cell in gm["shown"] or cell not in live.cells):
                        want[cell] = colour

    def p_speed():
        return max(0.07, 0.16 - 0.006 * gm.get("hits", 0))

    # Flappy Bird: grab the controller block to flap, avoid the scrolling pipes.
    def f_new():
        fw, fh = GAMES["flappy"]["size"]
        gm.update(bird=(3, fh // 2), pipes=[{"x": fw - 4, "gap": fh // 2}],
                  pipe_countdown=7, score=0)

    def f_act(action):
        if action == "flap" and not gm["over"]:
            x, y = gm["bird"]
            gm["bird"] = (x, min(GAMES["flappy"]["size"][1] - 1, y + 3))

    def f_step():
        fw, fh = GAMES["flappy"]["size"]
        x, y = gm["bird"]
        gm["bird"] = (x, y - 1)
        if y - 1 < 0:
            gm.update(over=True, end_message=f"flappy: crashed - score {gm['score']}")
            return
        pipes = gm["pipes"]
        for pipe in pipes:
            pipe["x"] -= 1
            if pipe["x"] + 1 == x:
                gm["score"] += 1
            if pipe["x"] <= x < pipe["x"] + 2 and not pipe["gap"] - 2 <= y - 1 <= pipe["gap"] + 2:
                gm.update(over=True, end_message=f"flappy: hit a pipe - score {gm['score']}")
                return
        gm["pipes"] = [pipe for pipe in pipes if pipe["x"] + 2 > 0]
        gm["pipe_countdown"] -= 1
        if gm["pipe_countdown"] <= 0:
            gm["pipes"].append({"x": fw - 1, "gap": random.randint(3, fh - 4)})
            gm["pipe_countdown"] = 7
        if y - 1 >= fh:
            gm.update(over=True, end_message=f"flappy: flew too high - score {gm['score']}")

    def f_want():
        fw, fh = GAMES["flappy"]["size"]
        want = g_frame()
        for pipe in gm["pipes"]:
            for x in range(pipe["x"], pipe["x"] + 2):
                if 0 <= x < fw:
                    for y in range(fh):
                        if not pipe["gap"] - 2 <= y <= pipe["gap"] + 2:
                            want[g_cell(x, y)] = "green"
        bird_x, bird_y = gm["bird"]
        if 0 <= bird_y < fh:
            want[g_cell(bird_x, bird_y)] = "yellow_emissive" if not gm["over"] else "red_emissive"
        return want

    def f_speed():
        return 0.3

    # Minesweeper uses a reveal/flag mode controller and clickable board tiles.
    def m_new():
        mw, mh = GAMES["minesweeper"]["size"]
        gm.update(mines=set(), revealed=set(), flags=set(), mine_started=False,
                  mine_mode="reveal", score=0)

    def m_neighbors(x, y):
        mw, mh = GAMES["minesweeper"]["size"]
        return [(nx, ny) for nx in range(max(0, x - 1), min(mw, x + 2))
                for ny in range(max(0, y - 1), min(mh, y + 2)) if (nx, ny) != (x, y)]

    def m_reveal(x, y):
        if (x, y) in gm["revealed"] or (x, y) in gm["flags"] or gm["over"]:
            return
        mw, mh = GAMES["minesweeper"]["size"]
        if not gm["mine_started"]:
            available = [(nx, ny) for nx in range(mw) for ny in range(mh) if (nx, ny) != (x, y)]
            gm["mines"] = set(random.sample(available, 10))
            gm["mine_started"] = True
        if (x, y) in gm["mines"]:
            gm.update(over=True, end_message="minesweeper: mine hit")
            return
        pending = [(x, y)]
        while pending:
            cell = pending.pop()
            if cell in gm["revealed"] or cell in gm["flags"] or cell in gm["mines"]:
                continue
            gm["revealed"].add(cell)
            around = m_neighbors(*cell)
            if not any(n in gm["mines"] for n in around):
                pending.extend(n for n in around if n not in gm["revealed"])
        if len(gm["revealed"]) == mw * mh - len(gm["mines"]):
            gm.update(over=True, end_message="minesweeper: cleared")

    def m_act(action):
        if action == "reveal-mode":
            gm["mine_mode"] = "reveal"
            g_say("minesweeper: reveal mode - grab a covered tile")
        elif action == "flag-mode":
            gm["mine_mode"] = "flag"
            g_say("minesweeper: flag mode - grab a covered tile to mark or unmark it")

    def m_click(x, y):
        cell = (x, y)
        if gm["mine_mode"] == "flag":
            if cell not in gm["revealed"] and not gm["over"]:
                if cell in gm["flags"]:
                    gm["flags"].remove(cell)
                elif len(gm["flags"]) < 10:
                    gm["flags"].add(cell)
        else:
            m_reveal(x, y)

    def m_want():
        mw, mh = GAMES["minesweeper"]["size"]
        want = g_frame()
        number_colors = ["white", "cyan", "green_light", "orange", "blue_light",
                         "purple", "cyan_light", "red", "red_emissive"]
        for x in range(mw):
            for y in range(mh):
                cell = (x, y)
                if cell in gm["flags"]:
                    color = "yellow_emissive"
                elif cell in gm["revealed"]:
                    count = sum(n in gm["mines"] for n in m_neighbors(x, y))
                    color = number_colors[count]
                elif gm["over"] and cell in gm["mines"]:
                    color = "red_emissive"
                else:
                    color = "blue"
                want[g_cell(x, y)] = color
        return want

    def m_speed():
        return 3600.0

    # Two-player Tic-Tac-Toe; players take turns by grabbing an empty tile.
    def tt_new():
        gm.update(marks={}, turn=0, winning=set(), score=0)

    def tt_act(x, y):
        if (x, y) in gm["marks"] or gm["over"]:
            return
        gm["marks"][(x, y)] = gm["turn"]
        lines = [[(x, y) for x in range(3)] for y in range(3)]
        lines += [[(x, y) for y in range(3)] for x in range(3)]
        lines += [[(i, i) for i in range(3)], [(i, 2 - i) for i in range(3)]]
        for line in lines:
            if all(cell in gm["marks"] and gm["marks"][cell] == gm["turn"] for cell in line):
                gm.update(over=True, winning=set(line),
                          end_message=f"tic-tac-toe: {'blue' if gm['turn'] == 0 else 'red'} wins")
                return
        if len(gm["marks"]) == 9:
            gm.update(over=True, end_message="tic-tac-toe: draw")
        else:
            gm["turn"] = 1 - gm["turn"]

    def tt_want():
        want = g_frame()
        for x in range(3):
            for y in range(3):
                cell = (x, y)
                color = "white"
                if cell in gm["marks"]:
                    color = "blue" if gm["marks"][cell] == 0 else "red"
                if cell in gm["winning"]:
                    color = "yellow_emissive"
                want[g_cell(x, y)] = color
        return want

    def tt_speed():
        return 3600.0

    # Two-player Connect Four; any board tile selects its column.
    def c4_new():
        gm.update(grid={}, turn=0, score=0)

    def c4_act(x, _y):
        cw, ch = GAMES["connectfour"]["size"]
        if gm["over"] or not 0 <= x < cw:
            return
        y = next((row for row in range(ch) if (x, row) not in gm["grid"]), None)
        if y is None:
            return
        player = gm["turn"]
        gm["grid"][(x, y)] = player
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            chain = [(x, y)]
            for direction in (1, -1):
                nx, ny = x + dx * direction, y + dy * direction
                while gm["grid"].get((nx, ny)) == player:
                    chain.append((nx, ny))
                    nx, ny = nx + dx * direction, ny + dy * direction
            if len(chain) >= 4:
                gm.update(over=True,
                          end_message=f"connect four: {'blue' if player == 0 else 'red'} wins")
                return
        if len(gm["grid"]) == cw * ch:
            gm.update(over=True, end_message="connect four: draw")
        else:
            gm["turn"] = 1 - player

    def c4_want():
        want = g_frame()
        for x in range(7):
            for y in range(6):
                player = gm["grid"].get((x, y))
                want[g_cell(x, y)] = "white" if player is None else ("blue" if player == 0 else "red")
        return want

    def c4_speed():
        return 3600.0

    RULES = {"tetris": (t_act, t_step, t_want, t_speed), "snake": (s_act, s_step, s_want, s_speed),
             "pong": (p_act, p_step, p_want, p_speed),
             "flappy": (f_act, f_step, f_want, f_speed),
             "minesweeper": (m_act, lambda: None, m_want, m_speed),
             "tictactoe": (lambda _a: None, lambda: None, tt_want, tt_speed),
             "connectfour": (lambda _a: None, lambda: None, c4_want, c4_speed)}
    BOARD_ACTIONS = {"minesweeper": m_click, "tictactoe": tt_act, "connectfour": c4_act}

    def g_new_game():
        gm.update(over=False, score=0, lines=0, locked={}, queue=[], end_message="")
        if gm["kind"] == "tetris":
            gm["piece"] = t_spawn()
        elif gm["kind"] == "pong":
            p_new()
        elif gm["kind"] == "snake":
            s_new()
        elif gm["kind"] == "flappy":
            f_new()
        elif gm["kind"] == "minesweeper":
            m_new()
        elif gm["kind"] == "tictactoe":
            tt_new()
        elif gm["kind"] == "connectfour":
            c4_new()

    async def g_loop():
        act, step, want_fn, speed = RULES[gm["kind"]]
        last, dirty, announced, over_at = time.monotonic(), True, False, 0.0
        while True:
            await asyncio.sleep(0.05)
            while gm["queue"]:
                a_ = gm["queue"].pop(0)
                if isinstance(a_, tuple) and a_[0] == "board":
                    if not gm["over"]:
                        board_act = BOARD_ACTIONS.get(gm["kind"])
                        if board_act:
                            board_act(a_[1], a_[2])
                    dirty = True
                    continue
                if a_ == "mode":
                    if time.monotonic() - gm.get("mode_t", 0) > 1.5:
                        gm["mode_t"] = time.monotonic()
                        await p_toggle_mode()
                        gm["queue"].clear()
                        last, dirty, announced = time.monotonic(), True, False
                    break
                if not gm["over"]:
                    act(a_); dirty = True
            if not gm["over"] and time.monotonic() - last >= speed():
                last = time.monotonic()
                step(); dirty = True
            if gm["over"] and not announced:
                announced = True
                over_at = time.monotonic()
                message = gm.get("end_message") or f"{gm['kind']}: GAME OVER - score {gm['score']}"
                g_say(f"{message}. new game in 4 s")
            if gm["over"] and announced and time.monotonic() - over_at >= 4.0:
                g_new_game()                                    # auto reset after a fail
                gm["queue"].clear()
                last, dirty = time.monotonic(), True
                g_say(f"{gm['kind']}: new game!")
            if not gm["over"]:
                announced = False
            for cell, t_gone in list(gm["gone"].items()):      # put grabbed controls back
                if time.monotonic() - t_gone > 0.1:              # fast: grab again right away
                    await put(cell, gm["controls"][cell][1], "1x1_stuffed"); del gm["gone"][cell]
            if dirty:
                await g_render(want_fn()); dirty = False

    def on_game_control(code, sender, data):
        if code != pun.EV_RPC or sender == bot.actor:
            return
        r = pun.decode_rpc(data)
        if not r or len(r["args"]) < 4:
            return
        try:
            cell = (int(r["args"][1]), int(r["args"][2]), int(r["args"][3]))
        except (TypeError, ValueError):
            return
        if cell in gm["controls"]:
            if r["method"] == "RpcDestroyMapObject":            # control grabbed -> do it
                gm["queue"].append(gm["controls"][cell][0])
                gm["gone"][cell] = time.monotonic()
            elif r["method"] == "RpcMapObjectCreated":         # player put it back
                gm["gone"].pop(cell, None)
        elif r["method"] == "RpcDestroyMapObject" and cell in gm["interact_cells"]:
            gm["shown"].pop(cell, None)
            gm["queue"].append(("board", *gm["interact_cells"][cell]))

    bot.handlers.append(on_game_control)

    async def g_build_board(kind, marker, size_name, marker_pattern):
        if marker_pattern:
            await take(marker, marker_pattern); live.cells.discard(marker)
        await g_off()
        DX, DY, DZ = bot.room_dims
        mx, my, mz = marker
        _, axis, s = min([(DZ - 1 - mz, "z", 1), (mz, "z", -1), (DX - 1 - mx, "x", 1), (mx, "x", -1)])
        sc = 2 if size_name == "big" else 1
        gw, gh = GAMES[kind]["size"]
        lay = make_layout(axis, s, mx if axis == "z" else mz, my, mz if axis == "z" else mx,
                          (sc, gw + 2, gh + (2 if GAMES[kind]["top"] else 1), 0))
        lay.update(axis=axis, s=s)
        gm.update(kind=kind, lay=lay, shown={}, controls={}, gone={})
        gm["interact_cells"] = {}
        g_say(f"{kind}: building the board ({lay['w'] * sc / 2:.0f} x {lay['h'] * sc / 2:.0f} m)...")
        await build_board(lay)
        return lay

    async def g_place_controls(spot, spot_pattern, ctrl_override=None, keep=False):
        """A row of control blocks centred on `spot`, left->right as you face the board."""
        if spot_pattern:
            await take(spot, spot_pattern); live.cells.discard(spot)
        lay = gm["lay"]
        axis, s = lay["axis"], lay["s"]
        right = s if axis == "z" else -s
        ctrl = ctrl_override or GAMES[gm["kind"]]["ctrl"]
        if not keep:
            for cell in list(gm["controls"]) + list(gm.get("deco", [])):
                await take(cell, "1x1_stuffed")
            gm["controls"], gm["deco"] = {}, []
        x, y, z = spot

        def at(du, dy):                                   # du = sideways (right +), dy = up
            return (x + right * du, y + dy, z) if axis == "z" else (x, y + dy, z + right * du)

        if GAMES[gm["kind"]].get("plus") and not ctrl_override:
            # a plus lying flat on the ground around the pillow (gray centre):
            # up = the arm toward the board, down = toward you, left/right = sides
            def flat(du, dfwd):
                return (x + right * du, y, z + s * dfwd) if axis == "z" else (x + s * dfwd, y, z + right * du)
            spots = {"left": flat(-1, 0), "right": flat(1, 0), "up": flat(0, 1), "down": flat(0, -1)}
            cells = [(spots[a], (a, c)) for a, c in ctrl]
            centre = at(0, 0)
            gm["deco"] = [centre]
            await put(centre, "gray", "1x1_stuffed")
        else:
            offs = [2 * i - (len(ctrl) - 1) for i in range(len(ctrl))]   # -3,-1,1,3 in a row
            if any("x3" in a_ for a_, _ in ctrl):
                offs = [-2, -1, 1, 2]           # pong: the x3 blocks touch the main up / down pair
            cells = [(at(k, 0), ac) for ac, k in zip(ctrl, offs)]
        for cell, (action, color) in cells:
            gm["controls"][cell] = (action, color)
            await put(cell, color, "1x1_stuffed")
        labels = {"flap": "flap", "reveal-mode": "reveal mode", "flag-mode": "flag mode"}
        names = ", ".join(f"{c} = {labels.get(a, a.replace('_2', '').rstrip('2').replace('x3', ' x3'))}"
                          for a, c in ctrl)
        if gm["kind"] == "pong" and len({c for _, c in ctrl}) == 1:
            names = f"all {ctrl[0][1]}: up 3, up 1, (gap), down 1, down 3"
        how = "the plus sign" if GAMES[gm["kind"]].get("plus") and not ctrl_override else "the row, left to right"
        who_for = " (right / red paddle)" if ctrl_override else (" (left / blue paddle)" if gm.get("twop") else "")
        g_say(f"{gm['kind']}: controller{who_for} ready - {how}: {names}")

    def default_controls_spot(front=None):
        lay = gm["lay"]
        sc = lay["sc"]
        base = lay["pix"][(lay["w"] // 2, lay["h"] - 1)]
        front = front if front is not None else 3 * sc + 2
        x, y, z = base
        return (x, y, z - lay["s"] * front) if lay["axis"] == "z" else (x - lay["s"] * front, y, z)

    async def g_move_controls(front_cells):
        """Re-place the controller(s) `front_cells` in front of the board (game keeps going)."""
        lay = gm["lay"]
        gm["front"] = front_cells
        base = default_controls_spot(front_cells)
        gm["gone"] = {}
        if gm.get("twop"):
            right = lay["s"] if lay["axis"] == "z" else -lay["s"]
            sh = lambda c, k: (c[0] + right * k, c[1], c[2]) if lay["axis"] == "z" else (c[0], c[1], c[2] + right * k)
            await g_place_controls(sh(base, -4), None)
            await g_place_controls(sh(base, 4), None, ctrl_override=P2_CTRL, keep=True)
        else:
            await g_place_controls(base, None)
        if gm["kind"] == "pong":
            await p_mode_block(p_mode_spot(base))

    def p_mode_spot(*_):
        """where the pong mode block goes: the middle of the screen's bottom edge, one block out front"""
        return default_controls_spot(gm["lay"]["sc"])

    async def p_mode_block(cell):
        """grab it to switch pong between playing the bot (purple) and 2 players (orange)"""
        colour = "orange" if gm.get("twop") else "purple"
        gm["controls"][cell] = ("mode", colour)
        await put(cell, colour, "1x1_stuffed")
        g_say(f"pong: {colour} mode block - grab it to switch to "
              + ("playing the bot" if gm.get("twop") else "2 players"))

    async def p_toggle_mode():
        gm["twop"] = not gm.get("twop")
        await g_move_controls(gm.get("front") or (3 * gm["lay"]["sc"] + 2))
        g_new_game()
        g_say("pong: 2 PLAYERS - blue (left) vs red (right), one controller each" if gm["twop"]
              else "pong: VS THE BOT - you are blue, the bot plays red")

    async def g_start(spot=None, spot_pattern=None, spot2=None, spot2_pattern=None):
        lay = gm["lay"]
        gm["interact_cells"] = {
            g_cell(x, y): (x, y)
            for x in range(GAMES[gm["kind"]]["size"][0])
            for y in range(GAMES[gm["kind"]]["size"][1])
        } if gm["kind"] in BOARD_ACTIONS else {}
        if gm.get("twop") and not spot and not GAMES[gm["kind"]].get("board_only"):
            base = default_controls_spot()          # test layout: p1 left of centre, p2 right
            right = lay["s"] if lay["axis"] == "z" else -lay["s"]
            sh = lambda c, k: (c[0] + right * k, c[1], c[2]) if lay["axis"] == "z" else (c[0], c[1], c[2] + right * k)
            spot, spot2 = sh(base, -4), sh(base, 4)
        if not GAMES[gm["kind"]].get("board_only"):
            await g_place_controls(spot or default_controls_spot(), spot_pattern)
        if gm.get("twop") and spot2:
            await g_place_controls(spot2, spot2_pattern, ctrl_override=P2_CTRL, keep=True)
        if gm["kind"] == "pong":
            await p_mode_block(p_mode_spot(spot or default_controls_spot(), spot2 if gm.get("twop") else None))
        g_new_game()
        gm["task"] = asyncio.create_task(g_loop())
        if gm["kind"] == "flappy":
            g_say("flappy: grab the flap control to fly through the gaps; avoid the green pipes")
        elif gm["kind"] == "minesweeper":
            g_say("minesweeper: grab reveal or flag mode, then grab a board tile; first reveal is safe. "
                  "Colors: white 0, cyan 1, green 2, orange 3, blue 4, purple 5, light-cyan 6, red 7, bright-red 8")

    async def g_off():
        if gm["task"]:
            gm["task"].cancel(); gm["task"] = None
        lay = gm["lay"]
        if not lay:
            gm.update(kind=None, interact_cells={}, queue=[])
            gm["pick"] = None
            return
        for cell in list(gm["shown"]):
            await take(cell, lay["pattern"]); await asyncio.sleep(0.003)
        gm["shown"] = {}
        for cell in list(gm["controls"]) + list(gm.get("deco", [])):
            await take(cell, "1x1_stuffed")
        gm["controls"], gm["deco"] = {}, []
        for c in list(lay["built"]):
            await take(c, lay["pattern"]); lay["built"].discard(c); await asyncio.sleep(0.003)
        gm.update(lay=None, kind=None, interact_cells={}, queue=[])

    def on_game_pick(code, sender, data):
        """Waiting for pillow #1 (board) then pillow #2 (controller)."""
        pk = gm["pick"]
        if not pk or code != pun.EV_RPC or sender != pk["actor"]:
            return
        r = pun.decode_rpc(data)
        if not r or r["method"] != "RpcMapObjectCreated" or not str(r["args"][0]).startswith("1x1_"):
            return
        m = (int(r["args"][1]), int(r["args"][2]), int(r["args"][3]))
        loop = asyncio.get_running_loop()
        if pk["stage"] == "board":
            pk["stage"] = "busy"

            async def board_then_ask():
                await g_build_board(pk["kind"], m, pk["size"], str(r["args"][0]))
                if GAMES[pk["kind"]].get("board_only"):
                    gm["pick"] = None
                    await g_start()
                    g_say(f"{pk['kind']}: board ready - grab a tile to play; 'game off' removes it")
                else:
                    pk["stage"] = "controls"
                    g_say(f"{pk['kind']}: now place another 1x1 pillow where the CONTROLLER should go")
            loop.create_task(board_then_ask())
        elif pk["stage"] == "controls":
            if gm.get("twop"):
                pk["stage"] = "controls2"; pk["spot1"] = (m, str(r["args"][0]))
                g_say("pong: now place a 3rd 1x1 pillow for the RIGHT (red) player's controller")
            else:
                gm["pick"] = None
                loop.create_task(g_start(m, str(r["args"][0])))
        elif pk["stage"] == "controls2":
            gm["pick"] = None
            s1, p1 = pk["spot1"]
            loop.create_task(g_start(s1, p1, m, str(r["args"][0])))

    bot.handlers.append(on_game_pick)

    # --- canvas: a wall you paint by pointing a held item (bat) at it ------------
    PALETTE = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
               "brown", "black", "gray", "white"]
    cv = {"lay": None, "shown": {}, "inv": {}, "palette": {}, "gone": {}, "painters": {}, "last": {},
          "pick": None, "task": None}

    async def canvas_build(marker, size_name, marker_pattern):
        if marker_pattern:
            await take(marker, marker_pattern); live.cells.discard(marker)
        await canvas_off()
        DX, DY, DZ = bot.room_dims
        mx, my, mz = marker
        _, axis, s = min([(DZ - 1 - mz, "z", 1), (mz, "z", -1), (DX - 1 - mx, "x", 1), (mx, "x", -1)])
        w, h = (48, 32) if size_name == "big" else (32, 24)
        lay = make_layout(axis, s, mx if axis == "z" else mz, my, mz if axis == "z" else mx, (1, w, h, 0))
        lay.update(axis=axis, s=s)
        cv.update(lay=lay, shown={}, inv={cell: cr for cr, cell in lay["pix"].items()}, painters={}, last={})
        g_say(f"canvas: building a {w}x{h} canvas ({w / 2:.0f} x {h / 2:.0f} m)...")
        n = 0
        for cell in lay["pix"].values():
            if cell not in live.cells:
                await put(cell, "white", "1x1_stuffed"); cv["shown"][cell] = "white"; n += 1
                if n % 40 == 0:
                    await asyncio.sleep(0.15)
        g_say("canvas: now place another 1x1 pillow where the COLOUR PALETTE should go")

    async def canvas_palette(spot, spot_pattern):
        if spot_pattern:
            await take(spot, spot_pattern); live.cells.discard(spot)
        lay = cv["lay"]
        axis, s = lay["axis"], lay["s"]
        right = s if axis == "z" else -s
        x, y, z = spot
        for cell in list(cv["palette"]):
            await take(cell, "1x1_stuffed")
        cv["palette"] = {}
        for i, color in enumerate(PALETTE):
            k = i - len(PALETTE) // 2
            cell = (x + right * k, y, z) if axis == "z" else (x, y, z + right * k)
            cv["palette"][cell] = color
            await put(cell, color, "1x1_stuffed")
        if not cv["task"]:
            cv["task"] = asyncio.create_task(canvas_loop())
        g_say("canvas: hit (or grab) a colour on the palette, then hold your bat and point it at "
              "the canvas to paint. white = eraser")

    def canvas_select(actor, color):
        cv["painters"][actor] = color
        g_say(f"canvas: {clean_name((bot.actors.get(actor) or {}).get(255))} picked {color}")

    def on_canvas(code, sender, data):
        if code != pun.EV_RPC or not cv["palette"] or sender == bot.actor:
            return
        r = pun.decode_rpc(data)
        if not r:
            return
        m, a_ = r["method"], r["args"]
        if m == "RpcCreateHitEffect" and a_ and isinstance(a_[0], pun.Vector3):
            p = a_[0]
            for cell, color in cv["palette"].items():           # bat hit near a palette block
                c = cell_world(cell)
                if (p.x - c.x) ** 2 + (p.y - c.y) ** 2 + (p.z - c.z) ** 2 < 0.6 ** 2:
                    canvas_select(sender, color); break
        elif m == "RpcDestroyMapObject" and len(a_) >= 4:
            try:
                cell = (int(a_[1]), int(a_[2]), int(a_[3]))
            except (TypeError, ValueError):
                return
            if cell in cv["palette"]:                          # grabbed a palette block
                canvas_select(sender, cv["palette"][cell])
                cv["gone"][cell] = time.monotonic()

    bot.handlers.append(on_canvas)

    def canvas_aim(actor):
        """Where does `actor`'s right hand (holding something) point on the canvas?"""
        vd = bot.poses.get(actor)
        if not vd or len(vd) < 3 + 34:
            return None
        v = vd[3:]
        grip, thumb = float(v[22]), float(v[23])
        if grip < 0.85 or thumb < 0.5:                         # not holding an item
            return None
        body_p, body_q = pun.Vector3(v[0], v[1], v[2]), tuple(v[3:7])
        o = body_p + q_rotate(body_q, v[14:17])
        d = q_rotate(q_mul(body_q, tuple(v[17:21])), (0.0, 0.0, 1.0))
        lay = cv["lay"]
        DX, DY, DZ = bot.room_dims
        some = next(iter(lay["pix"].values()))
        if lay["axis"] == "z":
            plane_w = (some[2] + 0.5 - DZ / 2) * 0.5
            if abs(d.z) < 1e-3:
                return None
            t = (plane_w - o.z) / d.z
        else:
            plane_w = (some[0] + 0.5 - DX / 2) * 0.5
            if abs(d.x) < 1e-3:
                return None
            t = (plane_w - o.x) / d.x
        if t <= 0 or t > 40:
            return None
        hit = o + d * t
        cell = bot.grid_of(hit)
        cell = (some[0], cell[1], cell[2]) if lay["axis"] == "x" else (cell[0], cell[1], some[2])
        return cv["inv"].get(cell)

    async def canvas_paint(cr, color, radius=1.6):
        lay = cv["lay"]
        c0, r0 = cr
        rr = int(math.ceil(radius))
        for dc in range(-rr, rr + 1):
            for dr in range(-rr, rr + 1):
                if dc * dc + dr * dr > radius * radius:
                    continue
                cell = lay["pix"].get((c0 + dc, r0 + dr))
                if cell and cv["shown"].get(cell) != color:
                    if cell in cv["shown"]:
                        await take(cell, "1x1_stuffed")
                    await put(cell, color, "1x1_stuffed"); cv["shown"][cell] = color

    async def canvas_loop():
        while True:
            await asyncio.sleep(0.12)
            try:
                for actor, color in list(cv["painters"].items()):
                    cr = canvas_aim(actor)
                    if cr and cr != cv["last"].get(actor):
                        cv["last"][actor] = cr
                        await canvas_paint(cr, color)
                for cell, t_gone in list(cv["gone"].items()):
                    if time.monotonic() - t_gone > 0.4:
                        await put(cell, cv["palette"][cell], "1x1_stuffed"); del cv["gone"][cell]
            except Exception as e:
                print(f"canvas error: {e!r}")

    async def canvas_off():
        if cv["task"]:
            cv["task"].cancel(); cv["task"] = None
        for cell in list(cv["palette"]):
            await take(cell, "1x1_stuffed")
        cv["palette"] = {}
        for cell in list(cv["shown"]):
            await take(cell, "1x1_stuffed"); await asyncio.sleep(0.003)
        cv["shown"] = {}
        cv["lay"] = None

    def on_canvas_pick(code, sender, data):
        pk = cv["pick"]
        if not pk or code != pun.EV_RPC or sender != pk["actor"]:
            return
        r = pun.decode_rpc(data)
        if not r or r["method"] != "RpcMapObjectCreated" or not str(r["args"][0]).startswith("1x1_"):
            return
        m = (int(r["args"][1]), int(r["args"][2]), int(r["args"][3]))
        loop = asyncio.get_running_loop()
        if pk["stage"] == "canvas":
            pk["stage"] = "busy"

            async def go():
                await canvas_build(m, pk["size"], str(r["args"][0]))
                pk["stage"] = "palette"
            loop.create_task(go())
        elif pk["stage"] == "palette":
            cv["pick"] = None
            loop.create_task(canvas_palette(m, str(r["args"][0])))

    bot.handlers.append(on_canvas_pick)


    # --- BOSS FIGHT -----------------------------------------------------------------
    boss = {"on": False}

    def room_centre():
        return pun.Vector3(0.0, 0.0, 0.0)

    def player_actors():
        return [a_ for a_, pr in bot.actors.items() if a_ != bot.actor and isinstance(pr, dict)
                and pr.get(253) and bot.pose_of(a_)]

    def boss_ray(o, d):
        """distance along the ray to the boss sphere, or None"""
        if not boss.get("on"):
            return None
        c, r = boss["c"], boss["r"] + 0.4
        oc = o - c
        b = oc.x * d.x + oc.y * d.y + oc.z * d.z
        cc = oc.x * oc.x + oc.y * oc.y + oc.z * oc.z - r * r
        disc = b * b - cc
        if disc < 0:
            return None
        t = -b - math.sqrt(disc)
        return t if t > 0 else None

    # The TITAN: a giant tumbling head locked in the middle of the room (eyes, angry
    # brows, frown, HP bar), two huge pillow hands that slam / swipe / clap players,
    # real game mobs as minions (RpcCreateMob, run by a player's game), eye orbs and
    # a ground shockwave. 3 phases: >60% HP, ANGRY <=60%, BERSERK <=25%.
    # Grows a hill arena (same terrain as king of the hill) first. No regeneration.
    PHASE_SPEED = {1: 1.7, 2: 2.1, 3: 2.6}
    MOB_KINDS = {1: ["insectExploder", "insectExploder", "spittingSnail"],
                 2: ["insectExploder", "insectExploder", "spittingSnail", "insectSoldier"],
                 3: ["insectExploder", "insectSoldier", "insectSoldier", "spittingSnail", "insectTank"]}

    def qm(a, b):
        r = q_mul((a.x, a.y, a.z, a.w), (b.x, b.y, b.z, b.w))
        return pun.Quaternion(r[3], r[0], r[1], r[2])

    def pitch_q(a):
        return pun.Quaternion(math.cos(a / 2), math.sin(a / 2), 0.0, 0.0)

    def roll_q(a):
        return pun.Quaternion(math.cos(a / 2), 0.0, 0.0, math.sin(a / 2))

    def lerp(a, b, f):
        return a + (b - a) * f

    def ease(f):
        return f * f * (3 - 2 * f)

    def flat_dist(a, b):
        return math.hypot(a.x - b.x, a.z - b.z)

    def boss_phase():
        f = boss["hp"] / boss["max"]
        return 1 if f > 0.6 else 2 if f > 0.25 else 3

    async def boss_damage(shooter, amount, where):
        if not boss.get("on") or boss.get("dying") or boss.get("paused"):
            return
        boss["hp"] = max(0, boss["hp"] - amount)
        boss["last_hit"] = time.monotonic()
        acct = acct_of(shooter) if shooter is not None else "?"
        boss["dmg"][acct] = boss["dmg"].get(acct, 0) + amount
        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [where, pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)
        ph = boss_phase()
        if ph > boss["phase"]:
            boss["phase"] = ph
            boss["enraged"] = True
            if ph == 2:
                g_say("boss: THE TITAN IS ANGRY! Both hands now, and it sweeps the floor!")
            else:
                g_say("boss: THE TITAN GOES BERSERK! Red hands, clap attacks, tanks!")
                asyncio.get_running_loop().create_task(hands_recolour("red_emissive"))
        if boss["hp"] <= 0:
            asyncio.get_running_loop().create_task(boss_die())

    async def boss_preview(pid, color, pattern="1x1_stuffed"):
        await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated", [user_id, pid, pattern, color],
                      receivers=pun.RECV_OTHERS)

    async def boss_move(pid, pos, rot):
        await bot.rpc(bot.view_id, "RpcMapObjectPreviewUpdated", [pid, pos, rot, True], receivers=pun.RECV_OTHERS)

    async def boss_gone(pid):
        for m_ in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed"):
            await bot.rpc(bot.view_id, m_, [pid], receivers=pun.RECV_OTHERS)

    def boss_centre():
        """dead in the middle of the room, high enough to stand under"""
        DX, DY, DZ = bot.room_dims
        g = ground_y(0.0, 0.0)
        return pun.Vector3(0.0, max(g + 4.5, min(g + 7.5, DY * 0.5 - 5.0)), 0.0)

    # --- hands: a 2x2 m pillow palm, four 2 m fingers and a thumb (pivots are centres;
    # the long side of 1x4 / 1x2 pillows is their local X) ---
    def hand_shape(k):
        sx = 1 if k == 0 else -1                     # thumbs on the inside, palms down
        return ([("4x4_stuffed", (0.0, 0.0, 0.0), IDENT, "purple_dark")] +
                [("1x4_stuffed", (x, 0.0, 2.0), yaw_q(math.pi / 2), "purple") for x in (-0.78, -0.26, 0.26, 0.78)] +
                [("1x2_stuffed", (sx * 1.35, 0.0, 0.45), yaw_q(-sx * 0.6), "purple")])

    async def hand_create(h, colour=None):
        for pid, (pat, _, _, col) in zip(h["pids"], h["shape"]):
            await boss_preview(pid, colour or col, pat)

    async def hands_recolour(colour):
        boss["hand_col"] = colour                       # kept so a reload keeps the colour
        for h in boss.get("hands", []):
            old = h["pids"]
            h["pids"] = [rand_id() for _ in old]
            await hand_create(h, colour)
            for pid in old:
                await boss_gone(pid)

    async def hand_draw(h, pos, yaw, pitch=0.0, roll=0.0):
        q = qm(qm(yaw_q(yaw), pitch_q(pitch)), roll_q(roll))
        qt = (q.x, q.y, q.z, q.w)
        for pid, (_, off, rel, _) in zip(h["pids"], h["shape"]):
            await boss_move(pid, pos + q_rotate(qt, off), qm(q, rel))
        h["pos"] = pos

    def hand_rest(k):
        c, f, side = boss["c"], boss["f"], boss["side"]
        return c + side * (-5.2 if k == 0 else 5.2) + f * 1.5 + pun.Vector3(0, -2.4, 0)

    def hand_aim(h):
        pose = bot.pose_of(h["tgt"])
        if pose:
            p = pose[0]
            h["aim"] = pun.Vector3(p.x, ground_y(p.x, p.z), p.z)

    def radial(p):
        c = boss["c"]
        dx, dz = p.x - c.x, p.z - c.z
        n_ = math.hypot(dx, dz) or 1.0
        return dx / n_, dz / n_

    async def smack(actor, force, where):
        props = bot.actors.get(actor) or {}
        if not props.get(253):
            return
        await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback", [props[253], force, 3, None, -1],
                      receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [where, pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)

    async def hand_contact(p0, p1, push_dir):
        """everyone the hand sweeps through (segment p0 -> p1) gets smacked"""
        now = time.monotonic()
        sx_, sz_ = p1.x - p0.x, p1.z - p0.z
        L2 = sx_ * sx_ + sz_ * sz_ or 1e-6
        for a_ in player_actors():
            pose = bot.pose_of(a_)
            if not pose or now - boss["hit_cd"].get(a_, 0) < 0.6:
                continue
            p = pose[0]
            t_ = max(0.0, min(1.0, ((p.x - p0.x) * sx_ + (p.z - p0.z) * sz_) / L2))
            q_ = pun.Vector3(p0.x + sx_ * t_, p0.y + (p1.y - p0.y) * t_, p0.z + sz_ * t_)
            if flat_dist(p, q_) < 2.2 and -2.0 < p.y - q_.y < 2.5:
                boss["hit_cd"][a_] = now
                pw = 13.0 + 3.0 * boss["phase"]
                await smack(a_, pun.Vector3(push_dir[0] * pw, 7.0 + boss["phase"], push_dir[1] * pw),
                            p + pun.Vector3(0, 1.0, 0))

    def hand_begin(h, tgt, now, mode=None):
        sp = PHASE_SPEED[boss["phase"]]
        if mode is None:
            mode = "swipe" if random.random() < (0.3 if boss["phase"] == 1 else 0.5) else "slam"
        h.update(mode=mode, tgt=tgt, stage="wind", t0=now, from_=h["pos"],
                 dur={"slam": 1.3, "swipe": 1.0, "clap": 1.1}[mode] / sp)
        hand_aim(h)
        if "aim" not in h:
            h["aim"] = hand_rest(h["k"])

    def wind_goal(h):
        """where the hand winds up to: (pos, yaw, pitch, roll)"""
        a_, c, k = h["aim"], boss["c"], h["k"]
        rx, rz = radial(a_)
        if h["mode"] == "slam":
            return a_ + pun.Vector3(0, 5.5, 0), math.atan2(rx, rz), -0.6, 0.0
        if h["mode"] == "swipe":
            s = 1 if k == 1 else -1
            R = max(4.0, flat_dist(a_, c))
            th = math.atan2(rz, rx) - s * 1.1
            p = pun.Vector3(c.x + math.cos(th) * R, 0, c.z + math.sin(th) * R)
            p.y = ground_y(p.x, p.z) + 1.0
            h["arc"] = (R, th, math.atan2(rz, rx) + s * 1.1, s)
            return p, math.atan2(math.cos(th), math.sin(th)), 0.0, 0.0
        # clap: the hands open wide on both sides of the player, palms facing each other
        px, pz = rz, -rx
        sd = -1 if k == 0 else 1
        return (a_ + pun.Vector3(px * 3.4 * sd, 1.3, pz * 3.4 * sd), math.atan2(rx, rz), 0.0,
                (math.pi / 2) * -sd)

    async def hand_tick(h, now, targets, tick, may_start):
        k = h["k"]
        st = h["stage"]
        if st == "idle":
            face = math.atan2(boss["f"].x, boss["f"].z)
            if targets and may_start and now >= h["next"]:
                hand_begin(h, random.choice(targets), now)
            elif tick % 3 == k:
                await hand_draw(h, hand_rest(k) + pun.Vector3(0, 0.35 * math.sin(now * 1.7 + 2 * k), 0), face)
            return
        sp = PHASE_SPEED[boss["phase"]]
        el = now - h["t0"]
        if st == "wind":
            if el < h["dur"] * 0.7:                     # follows the player, then locks: dodge it!
                hand_aim(h)
            f = min(el / h["dur"], 1.0)
            dst, yaw, pitch, roll = wind_goal(h)
            await hand_draw(h, lerp(h["from_"], dst, ease(f)), yaw, pitch * f, roll * f)
            h["pose"] = (yaw, pitch, roll)
            if f >= 1.0:
                h.update(stage="strike", t0=now, from_=h["pos"], last=h["pos"])
        elif st == "strike":
            yaw, pitch, roll = h["pose"]
            if h["mode"] == "slam":
                f = min(el / 0.15, 1.0)
                await hand_draw(h, lerp(h["from_"], h["aim"] + pun.Vector3(0, 0.45, 0), f * f), yaw, pitch * (1 - f))
                if f >= 1.0:
                    asyncio.get_running_loop().create_task(slam_impact(h["aim"]))
                    h.update(stage="hold", t0=now, hold=0.3)
            elif h["mode"] == "swipe":
                R, th0, th1, s = h["arc"]
                f = min(el / (0.65 / sp), 1.0)
                th = th0 + (th1 - th0) * f
                c = boss["c"]
                p = pun.Vector3(c.x + math.cos(th) * R, 0, c.z + math.sin(th) * R)
                p.y = ground_y(p.x, p.z) + 1.0
                await hand_draw(h, p, math.atan2(math.cos(th), math.sin(th)))
                await hand_contact(h["last"], p, (-math.sin(th) * s, math.cos(th) * s))
                h["last"] = p
                if f >= 1.0:
                    h.update(stage="hold", t0=now, hold=0.15)
            else:                                        # clap
                rx, rz = radial(h["aim"])
                sd = -1 if k == 0 else 1
                f = min(el / 0.18, 1.0)
                goal = h["aim"] + pun.Vector3(rz * 0.75 * sd, 1.3, -rx * 0.75 * sd)
                await hand_draw(h, lerp(h["from_"], goal, f * f), yaw, 0.0, roll)
                if f >= 1.0:
                    if k == 0:
                        asyncio.get_running_loop().create_task(
                            explode(h["aim"] + pun.Vector3(0, 1.2, 0), land=False, power=14.0, radius=3.6))
                    h.update(stage="hold", t0=now, hold=0.3)
        elif st == "hold":
            if el > h["hold"]:
                h.update(stage="back", t0=now, from_=h["pos"])
        elif st == "back":
            f = min(el / (0.8 / sp), 1.0)
            face = math.atan2(boss["f"].x, boss["f"].z)
            await hand_draw(h, lerp(h["from_"], hand_rest(k), ease(f)), face)
            if f >= 1.0:
                gap = {1: (0.4, 0.9), 2: (0.2, 0.5), 3: (0.05, 0.25)}[boss["phase"]]
                h.update(stage="idle", mode=None, next=now + random.uniform(*gap))

    def look_q(d):
        """rotation whose forward (+Z) points along direction d"""
        n_ = math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) or 1.0
        return qm(yaw_q(math.atan2(d.x, d.z)), pitch_q(-math.asin(max(-1.0, min(1.0, d.y / n_)))))

    def mouth():
        return boss["c"] + boss["f"] * (boss["r"] + 0.4) + pun.Vector3(0, -1.0, 0)

    def chest(a_):
        pose = bot.pose_of(a_)
        return pose[0] + pun.Vector3(random.uniform(-0.4, 0.4), 0.8, random.uniform(-0.4, 0.4)) if pose else None

    async def mob_blast(pos, radius=3.0):
        """the exploder bug's own explosion (RpcCreateExplosionWithBlockOrigin type 9, origin 'm' = mob)"""
        await bot.rpc(bot.view_id, "RpcCreateExplosionWithBlockOrigin",
                      [9, pos, float(radius), random.randint(1, 99999), None, "m", -1, -1, -1, None],
                      receivers=pun.RECV_OTHERS)

    async def slam_impact(at):
        await mob_blast(at + pun.Vector3(0, 0.3, 0), 3.0)
        await explode(at + pun.Vector3(0, 0.3, 0), land=False, power=8.0 + 2 * boss["phase"], radius=4.2)
        if boss["phase"] >= 2 and len(boss["mobs"]) < MOB_CAP[boss["phase"]]:
            await boss_spawn_mob("insectExploder", at=at + pun.Vector3(random.uniform(-1, 1), 0.3, random.uniform(-1, 1)))

    async def spit(src, dst, speed=12.0):
        """a real snail spit (the spitting snail's projectile, mob origin), lobbed a bit"""
        d = dst - src
        t_ = math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) / speed
        d = d + pun.Vector3(0, 0.5 * 9.81 * t_ * t_ * 0.6, 0)
        await bot.rpc(bot.view_id, "RpcCreateProjectileFromBlockOrigin",
                      ["snailSpit", src, look_q(d), float(speed), -1, -1, -1, "m", None, rand_id(), 0],
                      receivers=pun.RECV_OTHERS)

    async def fireburst(src, dst):
        await bot.rpc(bot.view_id, "RpcCreateProjectileFromPlayerOrigin",
                      ["fireburst", src, look_q(dst - src), 25.0, user_id, None, None, rand_id(), 0],
                      receivers=pun.RECV_OTHERS)

    async def shotgun(src, dst, n=7, spread=8.0):
        await bot.rpc(bot.view_id, "RpcCreateProjectileSpreadFromPlayerOrigin",
                      ["shotgunPellet", src, look_q(dst - src), 25.0, user_id, None, None, n, float(spread),
                       random.randint(1, 999999), rand_id()], receivers=pun.RECV_OTHERS)

    async def boss_volley(targets):
        """spit from the mouth at several players (+ fire from the eyes, shotgun when berserk)"""
        ph = boss["phase"]
        m_ = mouth()
        for tgt in random.sample(targets, min(2 + ph, len(targets))):
            dst = chest(tgt)
            if dst is None:
                continue
            for _ in range(ph):
                await spit(m_ + pun.Vector3(random.uniform(-0.6, 0.6), 0, random.uniform(-0.6, 0.6)), dst,
                           random.uniform(11.0, 14.0))
        tgt = random.choice(targets)
        dst = chest(tgt)
        if dst is not None and ph >= 2:
            for sd in (-1, 1):
                await fireburst(boss["c"] + boss["f"] * (boss["r"] + 0.5) + boss["side"] * (1.15 * sd)
                                + pun.Vector3(0, 0.6, 0), dst)
        if dst is not None and ph >= 3:
            await shotgun(m_, dst, n=9, spread=10.0)

    # --- minions: real game mobs, simulated by a player's game (the owner) ---
    MOB_CAP = {1: 14, 2: 20, 3: 28}

    async def boss_spawn_mob(kind, at=None, throw_at=None):
        players = player_actors()
        if not players:
            return
        owner = (bot.actors.get(random.choice(players)) or {}).get(253)
        c = boss["c"]
        vel = pun.Vector3()
        if throw_at is not None:                         # spat out of its mouth toward a player
            pos = mouth()
            d = throw_at - pos
            T = 1.1
            vel = pun.Vector3(d.x / T, d.y / T + 0.5 * 9.81 * T, d.z / T)
        elif at is not None:
            pos = at
        else:
            ang, rr = random.uniform(0, 2 * math.pi), random.uniform(3.5, 9.0)
            x, z = c.x + math.cos(ang) * rr, c.z + math.sin(ang) * rr
            pos = pun.Vector3(x, ground_y(x, z) + 0.3, z)
        mid = rand_id()
        await bot.rpc(bot.view_id, "RpcCreateMob",
                      [mid, kind, -1, -1, -1, pos, yaw_q(random.uniform(0, 2 * math.pi)),
                       vel, pun.Vector3(), owner, 0, False], receivers=pun.RECV_OTHERS)
        if throw_at is None:
            await bot.rpc(bot.view_id, "RpcCreateConfettiEffectFromPlayerOrigin",
                          [pos, pun.Vector3(0, 1, 0), pun.Vector3(),
                           TypedArray(ord("s"), ["purple_emissive", "red_emissive"]), 1, None, user_id],
                          receivers=pun.RECV_OTHERS)
        boss["mobs"][mid] = time.monotonic()

    def on_boss_mob_gone(code, sender, data):
        if code == pun.EV_RPC and boss.get("on"):
            r = pun.decode_rpc(data)
            if r and r["method"] == "RpcDestroyMob" and r["args"]:
                boss.get("mobs", {}).pop(r["args"][0], None)

    bot.handlers.append(on_boss_mob_gone)

    async def boss_start(max_hp):
        if boss.get("on"):
            await boss_stop()
        golden = math.pi * (3 - math.sqrt(5))
        n = 44
        parts = []
        for i in range(n):
            y = 1 - 2 * (i + 0.5) / n
            r = math.sqrt(1 - y * y)
            parts.append({"pid": rand_id(), "base": (math.cos(golden * i) * r, y, math.sin(golden * i) * r),
                          "axis": tumble_axis(), "w": random.uniform(1.0, 2.5),
                          "col": random.choice(["purple", "purple_dark", "blurple", "purple_emissive"])})
        now = time.monotonic()
        boss.update(on=True, dying=False, paused=False, hp=max_hp, max=max_hp, r=3.2, t0=now, parts=parts,
                    face=[rand_id() for _ in range(6)], bar=[rand_id() for _ in range(10)],
                    bar_bg=[rand_id() for _ in range(10)], dmg={}, hand_col=None, reload_due=False,
                    enraged=False, phase=1, group=0, shots=[], mobs={}, hit_cd={}, last_hit=now,
                    next_orb=now + 3, next_wave=now + 3, next_shock=now + 12, next_clap=now + 6,
                    face_yaw=None, bar_lit=None, f=pun.Vector3(0, 0, 1), side=pun.Vector3(1, 0, 0),
                    c=pun.Vector3(0, -500, 0))                 # not hittable while the arena grows
        ensure_avatar(bot.actor_by_name(who) or next(iter(player_actors()), None))
        if not any_terrain():                            # same hills as king of the hill = the arena
            await koth_terrain(store=boss, label="boss", clear_r=9)
            if not boss.get("on"):
                return
        now = time.monotonic()
        boss.update(t0=now, last_hit=now, next_orb=now + 3, next_wave=now + 3, next_shock=now + 12,
                    next_clap=now + 6)
        boss["c"] = boss_centre()
        boss["hands"] = [{"k": k, "shape": hand_shape(k), "pids": [rand_id() for _ in range(6)], "stage": "idle",
                          "mode": None, "next": now + 2 + 0.7 * k, "pos": hand_rest(k)} for k in range(2)]
        for p_ in parts:
            await boss_preview(p_["pid"], p_["col"], "2x2x2_stuffed")
        fp = boss["face"]
        for e in fp[:2]:
            await boss_preview(e, "red_emissive", "2x2x2_stuffed")       # eyes
        for e in fp[2:]:
            await boss_preview(e, "black", "1x4_stuffed")                # brows + frown
        for b_ in boss["bar"]:
            await boss_preview(b_, "green_emissive")
        for b_ in boss["bar_bg"]:                                      # lost HP shows black
            await boss_preview(b_, "black")
        for h in boss["hands"]:
            await hand_create(h)
            await hand_draw(h, h["pos"], 0.0)
        # everyone gets the finger gun while the boss is up
        boss["gave"] = []
        for a_ in player_actors():
            ac = acct_of(a_)
            if ac and ac not in fg["accts"]:
                fg["accts"][ac] = fg_state(); boss["gave"].append(ac)
        boss["task"] = asyncio.create_task(boss_loop())
        g_say(f"boss: THE TITAN AWAKENS ({max_hp} HP)! Everyone has the finger gun - shoot its head! "
              "Watch out for its hands and its bugs.")

    async def boss_pause(on):
        """freeze the Titan where it is (no attacks, no bugs, can't be hurt) or let it go again"""
        if not boss.get("on") or boss.get("dying"):
            print("no boss running"); return
        if on == bool(boss.get("paused")):
            print("boss already " + ("paused" if on else "running")); return
        boss["paused"] = on
        now = time.monotonic()
        if on:
            for mid in list(boss.get("mobs", {})):      # its bugs go away while paused
                await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
            boss["mobs"] = {}
            for sh in boss.get("shots", []):
                await boss_gone(sh["pid"])
            boss["shots"] = []
            for h in boss.get("hands", []):              # hands back to their resting spot
                h.update(stage="idle", mode=None)
                await hand_draw(h, hand_rest(h["k"]), math.atan2(boss["f"].x, boss["f"].z))
            boss["paused_at"] = now
            g_say(f"boss: PAUSED ({math.ceil(boss['hp'])}/{boss['max']} HP) - 'boss resume' to continue")
        else:
            gap = now - boss.get("paused_at", now)
            boss["t0"] += gap
            boss.update(next_orb=now + 3, next_wave=now + 3, next_shock=now + 10, next_clap=now + 6)
            for h in boss.get("hands", []):
                h["next"] = now + 2 + 0.7 * h["k"]
            g_say("boss: THE TITAN WAKES UP AGAIN!")

    async def boss_stop():
        boss["on"] = False
        t_ = boss.get("task")
        if t_ and t_ is not asyncio.current_task():
            t_.cancel()
        for p_ in boss.get("parts", []):
            await boss_gone(p_["pid"])
        for e in boss.get("face", []) + boss.get("bar", []) + boss.get("bar_bg", []):
            await boss_gone(e)
        for h in boss.get("hands", []):
            for pid in h["pids"]:
                await boss_gone(pid)
        for sh in boss.get("shots", []):
            await boss_gone(sh["pid"])
        for mid in list(boss.get("mobs", {})):
            await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
        for ac in boss.get("gave", []):
            fg["accts"].pop(ac, None)
        boss.update(parts=[], face=[], bar=[], bar_bg=[], hands=[], shots=[], mobs={})
        await clear_terrain(boss, "boss")

    async def boss_reload():
        """Send the whole Titan to everyone in the room again. A player only gets the
        previews that exist when they join, so the outline is re-created under new ids
        (placed straight away), then the old pieces are removed."""
        if not boss.get("on") or boss.get("dying") or not boss.get("hands"):
            return
        old = ([p_["pid"] for p_ in boss["parts"]] + list(boss["face"]) + list(boss["bar"])
               + list(boss["bar_bg"]) + [pid for h in boss["hands"] for pid in h["pids"]])
        c = boss["c"]
        now = time.monotonic()
        for p_ in boss["parts"]:
            p_["pid"] = rand_id()
            await boss_preview(p_["pid"], p_["col"], "2x2x2_stuffed")
            await head_move(p_, now - boss["t0"], c)
        boss["face"] = [rand_id() for _ in range(6)]
        for e in boss["face"][:2]:
            await boss_preview(e, "red_emissive", "2x2x2_stuffed")       # eyes
        for e in boss["face"][2:]:
            await boss_preview(e, "black", "1x4_stuffed")                # brows + frown
        boss["bar"] = [rand_id() for _ in range(10)]
        boss["bar_bg"] = [rand_id() for _ in range(10)]
        for b_ in boss["bar"]:
            await boss_preview(b_, "green_emissive")
        for b_ in boss["bar_bg"]:
            await boss_preview(b_, "black")
        yaw = math.atan2(boss["f"].x, boss["f"].z)
        for h in boss["hands"]:
            h["pids"] = [rand_id() for _ in range(6)]
            await hand_create(h, boss.get("hand_col"))
            await hand_draw(h, h["pos"], yaw)
        await boss_face(c, boss["f"], boss["side"], yaw)
        boss.update(face_yaw=yaw, bar_lit=None)                         # HP bar redraws next tick
        for pid in old:
            await boss_gone(pid)
        print(f"boss: outline reloaded for everyone ({len(old)} pieces replaced)")

    async def boss_reload_soon(delay=5.0):
        """A join schedules one reload, 5 s later, so the newcomer has loaded the room."""
        await asyncio.sleep(delay)
        boss["reload_due"] = False
        await boss_reload()

    def boss_on_join(code, sender, data):
        if code == pun.EV_JOIN and boss.get("on") and not boss.get("reload_due") and sender != bot.actor:
            boss["reload_due"] = True
            print("boss: a player joined - the Titan reloads its outline in 5 s")
            asyncio.get_running_loop().create_task(boss_reload_soon())

    bot.handlers.append(boss_on_join)

    async def boss_die():
        boss["dying"] = True
        c = boss["c"]
        g_say("boss: THE TITAN IS DEFEATED!!!")
        for mid in list(boss.get("mobs", {})):            # its bugs die with it
            await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
        boss["mobs"] = {}
        for i in range(10):
            off = pun.Vector3(random.uniform(-3, 3), random.uniform(-2.5, 2.5), random.uniform(-3, 3))
            await explode(c + off, land=False, power=0.0, radius=0.1)
            await asyncio.sleep(0.2)
        for _ in range(40):                              # buttcoin shower
            v = pun.Vector3(random.uniform(-7, 7), random.uniform(2, 8), random.uniform(-7, 7))
            await bot.rpc(bot.view_id, "RpcCreateItem",
                          [rand_id(), "buttcoin", 0, None, c, pun.Quaternion(1, 0, 0, 0), v, pun.Vector3(),
                           TypedArray(ord("s"), []), Byte(1), Byte(1)], receivers=pun.RECV_OTHERS)
        ranking = sorted(boss["dmg"].items(), key=lambda kv: -kv[1])
        names = []
        for ac, dmg in ranking[:5]:
            a_ = next((k for k, pr in bot.actors.items() if isinstance(pr, dict)
                       and str(pr.get(253, "")).startswith(ac + "$")), None)
            names.append(f"{pname(a_) if a_ is not None else ac}: {dmg}")
        g_say("boss: damage dealt - " + (", ".join(names) or "nobody?"))
        await boss_stop()

    async def boss_fire(target_actor):
        pose = bot.pose_of(target_actor)
        if not pose:
            return
        dst = pose[0] + pun.Vector3(0, 0.5, 0)
        src = boss["c"] + boss["f"] * (boss["r"] + 0.6) + pun.Vector3(0, 0.6, 0)
        d = dst - src
        dist = math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) or 1
        boss["shots"].append({"pid": rand_id(), "src": src, "dir": d * (1 / dist), "dist": dist,
                              "t0": time.monotonic(), "speed": 10.0 + 2.0 * boss["phase"]})
        await boss_preview(boss["shots"][-1]["pid"], "red_emissive", "2x2x2_stuffed")

    async def boss_slam():
        """ground shockwave ring around the Titan"""
        c = boss["c"]
        ground = pun.Vector3(c.x, ground_y(c.x, c.z) + 0.3, c.z)
        ring = [rand_id() for _ in range(20)]
        for pid in ring:
            await boss_preview(pid, "orange_emissive")
        for step in range(7):
            rad = 1.5 + step * 1.7
            for k, pid in enumerate(ring):
                a_ = 2 * math.pi * k / len(ring)
                await boss_move(pid, ground + pun.Vector3(math.cos(a_) * rad, 0, math.sin(a_) * rad),
                                pun.Quaternion(1, 0, 0, 0))
            await asyncio.sleep(0.1)
        await knockback(ground, radius=13.0, power=10.0)
        for pid in ring:
            await boss_gone(pid)

    async def boss_face(c, f, side, yaw):
        """eyes, angry brows and a frown on the side facing the nearest player"""
        R = boss["r"]
        base = yaw_q(yaw)
        fp = boss["face"]
        up = pun.Vector3(0, 1, 0)
        for k, sd in enumerate((-1, 1)):
            await boss_move(fp[k], c + f * (R + 0.3) + side * (1.15 * sd) + up * 0.6, base)
            await boss_move(fp[2 + k], c + f * (R + 0.35) + side * (1.15 * sd) + up * 1.55,
                            qm(base, roll_q(0.4 * sd)))
            await boss_move(fp[4 + k], c + f * (R + 0.25) + side * (0.85 * sd) + up * -1.15,
                            qm(base, roll_q(-0.3 * sd)))

    async def head_move(p_, t, c):
        """one tumbling head cube at time t (the loop and a reload place cubes this way)"""
        spin = 0.25 * t
        bx, by, bz = p_["base"]
        x, z = bx * math.cos(spin) - bz * math.sin(spin), bx * math.sin(spin) + bz * math.cos(spin)
        await boss_move(p_["pid"], c + pun.Vector3(x, by, z) * boss["r"], spin_q(p_["axis"], p_["w"] * t))

    async def boss_loop():
        tick = 0
        try:
            while boss.get("on"):
                try:
                    await asyncio.sleep(0.125)
                    if boss.get("dying") or boss.get("paused"):
                        continue
                    tick += 1
                    now = time.monotonic()
                    t = now - boss["t0"]
                    c = boss["c"]
                    ph = boss["phase"]
                    targets = player_actors()
                    # face the nearest player
                    if targets:
                        near = min(targets, key=lambda a_: flat_dist(bot.pose_of(a_)[0], c))
                        rx, rz = radial(bot.pose_of(near)[0])
                        boss["f"], boss["side"] = pun.Vector3(rx, 0, rz), pun.Vector3(rz, 0, -rx)
                    f, side = boss["f"], boss["side"]
                    yaw = math.atan2(f.x, f.z)
                    turned = boss["face_yaw"] is None or abs(math.remainder(yaw - boss["face_yaw"], 2 * math.pi)) > 0.12
                    if turned or tick % 16 == 0:
                        boss["face_yaw"] = yaw
                        await boss_face(c, f, side, yaw)
                    # head: a fifth of the tumbling cubes per tick
                    g_ = boss["group"] = (boss["group"] + 1) % 5
                    for i in range(g_, len(boss["parts"]), 5):
                        await head_move(boss["parts"][i], t, c)
                    # HP bar above it (only redrawn when it changes)
                    lit = math.ceil(10 * boss["hp"] / boss["max"])
                    if turned or lit != boss["bar_lit"] or tick % 16 == 8:
                        boss["bar_lit"] = lit
                        hide = pun.Vector3(0, -200, 0)              # under the map
                        for k, (b_, bg) in enumerate(zip(boss["bar"], boss["bar_bg"])):
                            pos = c + side * ((k - 4.5) * 0.55) + pun.Vector3(0, boss["r"] + 1.6, 0)
                            await boss_move(b_, pos if k < lit else pos + hide, yaw_q(yaw))
                            await boss_move(bg, pos + hide if k < lit else pos, yaw_q(yaw))
                    # hands (phase 1: one at a time)
                    for h in boss["hands"]:
                        other = boss["hands"][1 - h["k"]]
                        await hand_tick(h, now, targets, tick, True)
                    # berserk: clap a player between both hands
                    if ph >= 2 and targets and now >= boss["next_clap"] and all(h["stage"] == "idle" for h in boss["hands"]):
                        tgt = random.choice(targets)
                        for h in boss["hands"]:
                            hand_begin(h, tgt, now, "clap")
                        boss["next_clap"] = now + (random.uniform(4, 6) if ph == 2 else random.uniform(2.5, 4))
                    # eye orbs
                    for sh in list(boss["shots"]):
                        tr = (now - sh["t0"]) * sh["speed"]
                        if tr >= sh["dist"]:
                            boss["shots"].remove(sh)
                            await boss_gone(sh["pid"])
                            asyncio.get_running_loop().create_task(
                                explode(sh["src"] + sh["dir"] * sh["dist"], land=False, power=7.0, radius=4.0))
                        else:
                            await boss_move(sh["pid"], sh["src"] + sh["dir"] * tr, spin_q([0, 1, 0], 10 * tr))
                    if targets and now >= boss["next_orb"]:
                        await boss_volley(targets)
                        boss["next_orb"] = now + {1: 1.0, 2: 0.6, 3: 0.35}[ph]
                    # ground shockwave
                    if now >= boss["next_shock"]:
                        boss["next_shock"] = now + {1: 14, 2: 9, 3: 6}[ph]
                        asyncio.get_running_loop().create_task(boss_slam())
                    # minion waves (forget mobs nobody reported dead after 2 minutes)
                    for mid, t_made in list(boss["mobs"].items()):
                        if now - t_made > 120:
                            del boss["mobs"][mid]
                    cap = MOB_CAP[ph]
                    if targets and now >= boss["next_wave"]:
                        boss["next_wave"] = now + {1: 3.0, 2: 2.2, 3: 1.5}[ph]
                        for i in range(min(1 + ph, cap - len(boss["mobs"]))):
                            if i % 2:                            # half are spat out at players
                                await boss_spawn_mob(random.choice(MOB_KINDS[ph]),
                                                     throw_at=chest(random.choice(targets)))
                            else:
                                await boss_spawn_mob(random.choice(MOB_KINDS[ph]))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"boss tick error: {e!r}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"boss error: {e!r}")

    def on_boss_pellet(code, sender, data):
        """Any player's pellet gun shot that passes through the boss hurts it."""
        if code != pun.EV_RPC or not boss.get("on"):
            return
        r = pun.decode_rpc(data)
        if r and r["method"] == "RpcCreateProjectileFromPlayerOrigin" and len(r["args"]) > 2 \
                and isinstance(r["args"][1], pun.Vector3):
            d = q_dir(r["args"][2])
            bd = boss_ray(r["args"][1], d)
            if bd is not None and bd < 40:
                asyncio.get_running_loop().create_task(boss_damage(sender, 2, r["args"][1] + d * bd))

    bot.handlers.append(on_boss_pellet)

    # --- KING OF THE HILL -------------------------------------------------------------
    koth = {"on": False}
    PAL_RGB = {"red": (1, 0, 0), "orange": (1, .5, 0), "yellow": (1, 1, 0), "green": (0, .7, 0),
               "cyan": (0, 1, 1), "blue": (0, .3, 1), "purple": (.5, 0, 1), "pink": (1, .4, .7),
               "white": (1, 1, 1), "black": (.1, .1, .1), "brown": (.5, .3, .1), "gray": (.5, .5, .5)}

    def player_colour(a_):
        sk = (bot.actors.get(a_) or {}).get("skinColor")
        if not isinstance(sk, pun.Vector3):
            return random.choice(list(PAL_RGB))
        return min(PAL_RGB, key=lambda k: sum((x - y) ** 2 for x, y in zip(PAL_RGB[k], (sk.x, sk.y, sk.z))))

    def koth_colour(ac, a_):
        """This player's KOTH colour: their score bar, their BAM block and the crown all
        use it. The palette colour closest to their skin that nobody else has yet, so
        no two players share a colour (owner 2026-10-13: "so you can tell who is who")."""
        cols = koth.setdefault("colours", {})
        if ac in cols:
            return cols[ac]
        used = set(cols.values())
        free = [k for k in PAL_RGB if k not in used] or list(PAL_RGB)
        sk = (bot.actors.get(a_) or {}).get("skinColor") if a_ is not None else None
        if isinstance(sk, pun.Vector3):
            pick = min(free, key=lambda k: sum((x - y) ** 2 for x, y in zip(PAL_RGB[k], (sk.x, sk.y, sk.z))))
        else:
            pick = random.choice(free)
        cols[ac] = pick
        return pick

    def koth_spot():
        """the next hilltop for the zone (or a random spot if there's no terrain)"""
        peaks = koth.get("peaks") or []
        DX, DY, DZ = bot.room_dims
        if peaks:
            choices = [pk for pk in peaks if pk != koth.get("peak")] or peaks
            pk = random.choice(choices)
            koth["peak"] = pk
            gx, gz, top = pk
            return pun.Vector3((gx + 0.5 - DX / 2) * 0.5, top * 0.5 + 0.1, (gz + 0.5 - DZ / 2) * 0.5)
        mx, mz = DX * 0.25 - 4, DZ * 0.25 - 4
        return pun.Vector3(random.uniform(-mx, mx), 0.15, random.uniform(-mz, mz))

    async def koth_terrain(store=None, label="koth", clear_r=0):
        """Blocky hills all over the room, built from 2x2x2 pillow cubes (1 m voxels):
        stepped/boxy shapes with flat tops and cliffs, grass on top, a dirt layer,
        stone below - only the blocks you can see. Plus cube trees and flowers.
        store = the game that owns the hills (koth or boss); clear_r = keep an open
        flat circle of that many metres in the middle of the room."""
        store = koth if store is None else store
        DX, DY, DZ = bot.room_dims
        V = 2                                             # voxel = 2 cells = 1 m
        GX, GZ, GY = DX // V, DZ // V, DY // V
        n_hills = max(10, min(16, GX * GZ // 170))
        hills = []
        for _ in range(n_hills):
            for _try in range(40):
                hx, hz = random.randint(4, GX - 5), random.randint(4, GZ - 5)
                if all((hx - a_) ** 2 + (hz - b_) ** 2 > 6 ** 2 for a_, b_, *_ in hills):
                    break
            r = random.uniform(2.5, 6.0)
            hills.append((hx, hz, random.randint(2, 7), r, random.uniform(0.6, 1.6), random.uniform(0.6, 1.6),
                          random.choice([1.3, 2.0, 3.5, 5.0])))          # bigger = flatter top, cliffier
        Hm = {}
        for hx, hz, hh, r, sx, sz, steep in hills:
            for x in range(int(hx - r * sx) - 1, int(hx + r * sx) + 2):
                for z in range(int(hz - r * sz) - 1, int(hz + r * sz) + 2):
                    dx, dz = abs(x - hx) / sx, abs(z - hz) / sz
                    d = (0.65 * max(dx, dz) + 0.35 * math.hypot(dx, dz)) / r     # boxy, not round
                    if d < 1 and 3 <= x < GX - 3 and 3 <= z < GZ - 3:
                        h = int(math.ceil(hh * (1 - d ** steep)))              # terraces / cliffs
                        if random.random() < 0.08:
                            h += 1                                              # a few bumps
                        if h >= 1 and not (clear_r and math.hypot(x + 0.5 - GX / 2, z + 0.5 - GZ / 2) < clear_r):
                            Hm[(x, z)] = min(max(Hm.get((x, z), 0), h), GY - 3)
        vox = {}
        for (x, z), h in Hm.items():
            low = min(Hm.get((x + 1, z), 0), Hm.get((x - 1, z), 0), Hm.get((x, z + 1), 0), Hm.get((x, z - 1), 0))
            for y in range(0, h):                                               # solid hills
                depth = h - 1 - y
                exposed = y >= min(low, h - 1)
                if depth == 0:
                    col = "green"                                               # grass top
                elif depth == 1:
                    col = "brown"                                               # dirt layer
                elif exposed:
                    col = "gray"                                                # stone on cliffs
                else:
                    col = "brown_dark"                                          # filled inside
                vox[(x, y, z)] = col
        peaks = []
        for hx, hz, hh, *_ in hills:
            top = Hm.get((hx, hz), 0)
            if top >= 1:
                peaks.append((hx * V + 1, hz * V + 1, top * V + (1 if str(cur.get("rk") or "").startswith("wild") else 0)))
        trees = 0
        cols = list(Hm.items())
        random.shuffle(cols)
        for (x, z), h in cols:
            if trees >= max(6, len(hills) // 2):
                break
            if 1 <= h <= 3 and all(math.hypot(x * V - a_, z * V - b_) > 6 for a_, b_, _ in peaks) \
                    and h + 5 < GY:
                for y in range(h, h + 3):                                       # trunk
                    vox[(x, y, z)] = "brown_dark"
                for dx in range(-1, 2):                                         # leaves
                    for dz in range(-1, 2):
                        for dy in range(0, 2):
                            if not (abs(dx) == 1 and abs(dz) == 1 and dy == 1):
                                vox.setdefault((x + dx, h + 3 + dy, z + dz), "green_dark")
                vox.setdefault((x, h + 5, z), "green_dark")
                trees += 1
        flowers = []
        for (x, z), h in cols[:30]:
            if (x, h, z) not in vox:
                flowers.append(((x * V, h * V, z * V), random.choice(["pink", "yellow", "red", "purple_light"])))
        # Wilds rooms have their own ground and refused blocks at y=0: start half a metre up.
        BASE = 1 if str(cur.get("rk") or "").startswith("wild") else 0
        trunk = {k for k, c_ in vox.items() if c_ == "brown_dark" and Hm.get((k[0], k[2]), 0) <= k[1]}

        def solid(x, y, z):
            return y < 0 or (x, y, z) in vox

        place = []
        for (x, y, z), col in vox.items():
            # a cube with something on all six sides can't be seen: skip it (far fewer blocks)
            if all(solid(x + a_, y + b_, z + c_) for a_, b_, c_ in
                   ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))):
                continue
            pat, fwd = "2x2x2_stuffed", 1
            h = Hm.get((x, z), 0)
            if y == h - 1 and (x, y, z) not in trunk and (x, y + 1, z) not in vox:
                # grass top on a straight edge: a slope rising toward the higher side
                # (up=2 slopes rise toward DIRS[fwd]: 0 +X, 1 -X, 4 +Z, 5 -Z)
                lo = [(Hm.get((x + 1, z), 0) < h, 1), (Hm.get((x - 1, z), 0) < h, 0),
                      (Hm.get((x, z + 1), 0) < h, 5), (Hm.get((x, z - 1), 0) < h, 4)]
                low = [f_ for is_low, f_ in lo if is_low]
                if len(low) == 1:
                    pat, fwd = "2x2x2_slope_stuffed", low[0]
            place.append(((x * V, y * V + BASE, z * V), col, pat, fwd))
        g_say(f"{label}: growing {len(hills)} hills ({len(place)} blocks, {trees} trees)...")
        store["terrain"] = []
        n = 0
        for cell, col, pat, fwd in place:
            occ = [(cell[0] + i, cell[1] + j, cell[2] + k) for i in range(V) for j in range(V) for k in range(V)]
            if any(c_ in live.cells for c_ in occ):
                continue
            await bot.rpc(bot.view_id, "RpcMapObjectCreated", [pat, *cell, fwd, 2, col, account, False],
                          receivers=pun.RECV_OTHERS)
            store["terrain"].append((cell, pat))
            live.cells.update(occ)
            n += 1
            if n % 40 == 0:
                await asyncio.sleep(0.15)
                if not store.get("on"):
                    return
        for cell, col in flowers:
            cell = (cell[0], cell[1] + BASE, cell[2])
            if cell not in live.cells:
                await put(cell, col, "1x1_stuffed")
                store["terrain"].append((cell, "1x1_stuffed")); live.cells.add(cell)
        store["peaks"] = peaks
        store["surf"] = {}
        for (x, z), h in Hm.items():                      # surface height per cell column
            for i in range(V):
                for k in range(V):
                    store["surf"][(x * V + i, z * V + k)] = h * V + BASE
        if label == "koth":
            g_say(f"koth: {len(hills)} hills ready - get to the top!")

    async def clear_terrain(store, label):
        """take down the hills a game grew"""
        terrain = store.get("terrain") or []
        store["terrain"], store["peaks"] = [], []
        if terrain:
            g_say(f"{label}: removing the hills ({len(terrain)} blocks)...")
        for cell, pat in terrain:
            await take(cell, pat)
            n_ = 2 if pat.startswith("2x2x2") else 1
            for i in range(n_):
                for j in range(n_):
                    for k in range(n_):
                        live.cells.discard((cell[0] + i, cell[1] + j, cell[2] + k))
            await asyncio.sleep(0.003)

    def ground_y(px, pz):
        """world y of the top of whatever blocks are REALLY there now (players can
        break the hills) under world point (px, pz); 0 = the floor"""
        DX, DY, DZ = bot.room_dims
        gx, gz = int(math.floor(px * 2 + DX / 2)), int(math.floor(pz * 2 + DZ / 2))
        return live.top(gx, gz) * 0.5

    CROWN_BLOCK = "2x2x2_stuffedTrophie_BAM2"

    async def koth_crown(holder):
        """the BAM trophy block's outline floating + spinning above `holder`'s head"""
        want_col = koth_colour(acct_of(holder), holder) if holder is not None else "yellow"
        if koth.get("crown") and koth.get("crown_col") != want_col:
            await boss_gone(koth["crown"][0]); koth["crown"] = []        # new colour: recreate
        if not koth.get("crown"):
            koth["crown"], koth["crown_col"] = [rand_id()], want_col
            await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated",
                          [user_id, koth["crown"][0], CROWN_BLOCK, want_col], receivers=pun.RECV_OTHERS)
        pose = bot.pose_of(holder) if holder is not None else None
        pid = koth["crown"][0]
        if not pose:
            await boss_move(pid, pun.Vector3(0, -200, 0), pun.Quaternion(1, 0, 0, 0))
            return
        await boss_move(pid, pose[0] + pun.Vector3(0, 1.5, 0), spin_q([0, 1, 0], time.monotonic() * 1.5))

    async def koth_tags(holder):
        """Every player with points wears the BAM block in their score colour over their
        head, so you can tell whose bar is whose. The king has the spinning crown instead."""
        tags = koth.setdefault("tags", {})
        for ac in list(koth["scores"]):
            a_ = next((k for k, pr in bot.actors.items() if isinstance(pr, dict)
                       and str(pr.get(253, "")).startswith(ac + "$")), None)
            pose = bot.pose_of(a_) if a_ is not None and a_ != holder else None
            col = koth_colour(ac, a_)
            if ac not in tags or tags[ac][1] != col:
                if ac in tags:
                    await boss_gone(tags[ac][0])
                tags[ac] = (rand_id(), col)
                await bot.rpc(bot.view_id, "RpcMapObjectPreviewCreated",
                              [user_id, tags[ac][0], CROWN_BLOCK, col], receivers=pun.RECV_OTHERS)
            if pose:
                await boss_move(tags[ac][0], pose[0] + pun.Vector3(0, 1.5, 0), pun.Quaternion(1, 0, 0, 0))
            else:                                       # left the room, or wearing the crown now
                await boss_move(tags[ac][0], pun.Vector3(0, -200, 0), pun.Quaternion(1, 0, 0, 0))

    async def koth_start(round_s):
        if koth.get("on"):
            await koth_stop()
        koth.update(on=True, r=2.5, ring=[rand_id() for _ in range(16)], c=pun.Vector3(0, 0.15, 0), target=None,
                    scores={}, bars={}, colours={}, tags={}, round_s=round_s, round_end=time.monotonic() + round_s,
                    next_move=time.monotonic() + 25, next_bomb=time.monotonic() + 5, bombs=[], tick=0,
                    state=None)
        ensure_avatar(bot.actor_by_name(who) or next(iter(player_actors()), None))
        if not koth.get("terrain"):
            await koth_terrain()
        koth["c"] = koth_spot()
        koth["round_end"] = time.monotonic() + round_s
        koth["next_move"] = time.monotonic() + 25
        for pid in koth["ring"]:
            await boss_preview(pid, "white")
        koth["task"] = asyncio.create_task(koth_loop())
        g_say(f"koth: KING OF THE HILL! stand in the glowing ring to score - {round_s // 60} min rounds")

    async def koth_stop():
        koth["on"] = False
        t_ = koth.get("task")
        if t_ and t_ is not asyncio.current_task():
            t_.cancel()
        for pid in koth.get("ring", []):
            await boss_gone(pid)
        for pids in koth.get("bars", {}).values():
            for pid in pids:
                await boss_gone(pid)
        for b_ in koth.get("bombs", []):
            await boss_gone(b_["pid"])
        for pid in koth.get("crown") or []:
            await boss_gone(pid)
        for pid, _col in (koth.get("tags") or {}).values():
            await boss_gone(pid)
        koth["tags"] = {}
        koth["crown"], koth["holder"], koth["since"], koth["seen"] = [], None, {}, {}
        koth["ring"], koth["bars"], koth["bombs"] = [], {}, []
        await clear_terrain(koth, "koth")

    async def koth_ring_colour(colour):
        """recolour = recreate the ring outlines in a new colour"""
        if koth["state"] == colour:
            return
        koth["state"] = colour
        for pid in koth["ring"]:
            await boss_gone(pid)
        koth["ring"] = [rand_id() for _ in range(16)]
        for pid in koth["ring"]:
            await boss_preview(pid, colour)

    async def koth_bar(ac, actor):
        """scoreboard bar for one player, floating above the middle of the room"""
        if ac not in koth["bars"]:
            koth["bars"][ac] = []
            koth_colour(ac, actor)
            cols_ = koth.setdefault("cols", {})
            cols_[ac] = len(cols_)                                      # fixed column, never moves
        want = min(24, koth["scores"][ac] // 4)
        while len(koth["bars"][ac]) < want:
            pid = rand_id()
            koth["bars"][ac].append(pid)
            await boss_preview(pid, koth["colours"][ac])
            x = (koth["cols"][ac] - 3.5) * 0.8                         # columns side by side
            await boss_move(pid, pun.Vector3(x, 9.0 + 0.5 * len(koth["bars"][ac]), 0), pun.Quaternion(1, 0, 0, 0))

    async def koth_loop():
        try:
            while koth.get("on"):
                try:
                    await asyncio.sleep(0.125)
                    koth["tick"] += 1
                    now = time.monotonic()
                    # zone slides to a new spot every 25 s
                    if now >= koth["next_move"]:
                        koth["target"], koth["next_move"] = koth_spot(), now + 25
                        g_say("koth: the zone jumped to another hill!" if koth.get("peaks") else "koth: the hill is moving!")
                    if koth["target"]:
                        c, tg = koth["c"], koth["target"]
                        dv = tg - c
                        dist = math.hypot(dv.x, dv.z)
                        if dist < 0.3 or koth.get("peaks"):
                            koth["c"], koth["target"] = tg, None              # hill to hill: jump
                        else:
                            koth["c"] = c + dv * (min(0.5, dist) / dist)
                    # the zone always rests on the surface under its centre
                    koth["c"] = pun.Vector3(koth["c"].x, ground_y(koth["c"].x, koth["c"].z) + 0.1, koth["c"].z)
                    # who is in the zone
                    inside = []
                    for a_ in player_actors():
                        p = bot.pose_of(a_)[0]
                        if math.hypot(p.x - koth["c"].x, p.z - koth["c"].z) <= koth["r"] and \
                                ground_y(p.x, p.z) - 1.0 < p.y < ground_y(p.x, p.z) + 3:
                            inside.append(a_)
                    await koth_ring_colour("white" if not inside else ("green_emissive" if len(inside) == 1 else "yellow_emissive"))
                    since = koth.setdefault("since", {})
                    seen = koth.setdefault("seen", {})
                    inside_ac = {acct_of(a_): a_ for a_ in inside}
                    for ac in inside_ac:
                        since.setdefault(ac, now)
                        seen[ac] = now
                    for ac in list(since):
                        if ac not in inside_ac and now - seen.get(ac, 0) > 2.0:
                            del since[ac]                       # out for 2 s+: timer resets
                    live_ac = {ac: a_ for ac, a_ in ((ac, next((k for k, pr in bot.actors.items() if isinstance(pr, dict)
                               and str(pr.get(253, "")).startswith(ac + "$")), None)) for ac in since) if a_ is not None}
                    holder = live_ac[min(live_ac, key=since.get)] if live_ac else None
                    if holder != koth.get("holder"):
                        koth["holder"], koth["holder_at"], koth["announced"] = holder, now, False
                    if holder is not None and not koth.get("announced") and now - koth.get("holder_at", now) >= 3:
                        koth["announced"] = True
                        g_say(f"koth: {pname(holder)} wears the crown!")
                    if koth["tick"] % 2 == 1:
                        await koth_crown(holder)
                        await koth_tags(holder)
                    # ring (4 updates a second)
                    if koth["tick"] % 2 == 0:
                        rot = now * 0.6
                        c0 = koth["c"]
                        for k, pid in enumerate(koth["ring"]):
                            a_ = rot + 2 * math.pi * k / len(koth["ring"])
                            px, pz = c0.x + math.cos(a_) * koth["r"], c0.z + math.sin(a_) * koth["r"]
                            await boss_move(pid, pun.Vector3(px, ground_y(px, pz) + 0.25, pz),   # sits on the blocks
                                            pun.Quaternion(1, 0, 0, 0))
                    # scoring twice a second
                    if koth["tick"] % 4 == 0:
                        for a_ in inside:
                            ac = acct_of(a_)
                            koth["scores"][ac] = koth["scores"].get(ac, 0) + (2 if len(inside) == 1 else 1)
                            await koth_bar(ac, a_)
                    # bombs drop into the zone
                    if now >= koth["next_bomb"]:
                        koth["next_bomb"] = now + random.uniform(2.5, 4.5)
                        ang, rr = random.uniform(0, 6.3), random.uniform(0, koth["r"])
                        lx, lz = koth["c"].x + math.cos(ang) * rr, koth["c"].z + math.sin(ang) * rr
                        land_p = pun.Vector3(lx, ground_y(lx, lz) + 0.25, lz)
                        koth["bombs"].append({"pid": rand_id(), "p": land_p, "t0": now})
                        await boss_preview(koth["bombs"][-1]["pid"], "red_emissive")
                    for b_ in list(koth["bombs"]):
                        t = now - b_["t0"]
                        if t >= 1.0:
                            koth["bombs"].remove(b_)
                            await boss_gone(b_["pid"])
                            await explode(b_["p"], land=False, power=6.0, radius=3.5)
                        else:
                            await boss_move(b_["pid"], b_["p"] + pun.Vector3(0, 12 * (1 - t), 0), spin_q([1, 0, 0], 8 * t))
                    # round over
                    if now >= koth["round_end"]:
                        ranking = sorted(koth["scores"].items(), key=lambda kv: -kv[1])
                        if ranking:
                            win_ac = ranking[0][0]
                            win_a = next((k for k, pr in bot.actors.items() if isinstance(pr, dict)
                                          and str(pr.get(253, "")).startswith(win_ac + "$")), None)
                            g_say(f"koth: {pname(win_a) if win_a is not None else win_ac} WINS with {ranking[0][1]} points!")
                            if win_a is not None and bot.pose_of(win_a):
                                wp = bot.pose_of(win_a)[0]
                                for _ in range(4):
                                    await explode(wp + pun.Vector3(random.uniform(-2, 2), 3, random.uniform(-2, 2)),
                                                  land=False, power=0.0, radius=0.1)
                                    await asyncio.sleep(0.3)
                        else:
                            g_say("koth: round over - nobody scored")
                        for pids in koth["bars"].values():
                            for pid in pids:
                                await boss_gone(pid)
                        for pid, _col in (koth.get("tags") or {}).values():
                            await boss_gone(pid)
                        koth.update(scores={}, bars={}, cols={}, tags={}, round_end=time.monotonic() + koth["round_s"] + 8)
                        await asyncio.sleep(8)
                        g_say("koth: new round!")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"koth tick error: {e!r}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"koth error: {e!r}")

    # --- finger gun: grip squeezed + thumb up, pull the trigger to shoot -------
    # Each hand in the pose stream: pos3 (body-local), rot4 (body-local, x y z w),
    # then index (0.5 rest .. 1.0 trigger pulled), grip (0.5 .. 1.0), thumb (0 up, 1 down).
    # Hand A (offset 14) = right hand, hand B (offset 24) = left (found 2026-10-08).
    # several players can have the finger gun: account -> per-hand state
    fg = {"accts": {}}

    def fg_state(mode="gun"):
        """mode = what the finger gun fires: gun (explosive shot), lightning, freeze."""
        return {"armed": {14: True, 24: True}, "aimed": {14: False, 24: False}, "last": {14: 0.0, 24: 0.0},
                "mode": mode, "cd": 0.0}

    def on_finger(code, sender, data):
        """Fire when a hand that was AIMING (grip squeezed, thumb up, index not pulled)
        pulls the trigger. Tuned on a recording of the owner: catches every deliberate
        pull; a plain grab (everything squeezed at once) has no aim step, so it doesn't."""
        if code not in (pun.EV_SEND_SERIALIZE, pun.EV_SEND_SERIALIZE_RELIABLE) or not fg["accts"]:
            return
        st = fg["accts"].get(acct_of(sender))
        if st is None:
            return
        vd = bot.poses.get(sender)
        if not vd or len(vd) < 3 + 34:
            return
        v = vd[3:]
        for off in (14, 24):
            try:
                index, grip, thumb = float(v[off + 7]), float(v[off + 8]), float(v[off + 9])
            except (TypeError, ValueError, IndexError):
                continue
            fire = (st["aimed"][off] and grip >= 0.85 and thumb <= 0.3 and index >= 0.9
                    and st["armed"][off] and time.monotonic() - st["last"][off] > 0.15)
            if index < 0.75:
                st["armed"][off] = True                 # trigger released: ready again
            if grip < 0.7 or thumb > 0.5:
                st["aimed"][off] = False
            elif grip >= 0.85 and thumb <= 0.3 and index <= 0.75:
                st["aimed"][off] = True                 # counts for the next packet
            if fire:
                st["armed"][off] = False
                st["last"][off] = time.monotonic()
                body_p = pun.Vector3(v[0], v[1], v[2])
                body_q = tuple(v[3:7])
                hand_p = body_p + q_rotate(body_q, v[off:off + 3])
                d = q_rotate(q_mul(body_q, tuple(v[off + 3:off + 7])), (0.0, 0.0, 1.0))
                n = math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) or 1.0
                d = d * (1 / n)
                mode = st.get("mode", "gun")
                if mode in ("lightning", "freeze", "scare"):
                    if time.monotonic() < st["cd"]:
                        continue                           # still recharging
                    st["cd"] = time.monotonic() + {"lightning": 1.2, "freeze": 1.6, "scare": 3.0}[mode]
                    cast = {"lightning": lightning_cast, "freeze": freeze_shot, "scare": scare_cast}[mode]
                    asyncio.get_running_loop().create_task(cast(hand_p + d * 0.15, d, sender))
                else:
                    asyncio.get_running_loop().create_task(shoot(hand_p + d * 0.15, d, sender))

    bot.handlers.append(on_finger)

    # Point the followed player's right controller and hold the trigger to fly.
    tracked_flight = {"actor": None, "enabled": False, "last_boost": 0.0}
    track(tracked_flight, "actor")

    def on_tracked_flight(code, sender, data):
        if code not in (pun.EV_SEND_SERIALIZE, pun.EV_SEND_SERIALIZE_RELIABLE) \
                or not tracked_flight["enabled"] or sender != tracked_flight["actor"]:
            return
        pose = bot.poses.get(sender)
        if not pose or len(pose) < 3 + 14 + 8:
            return
        try:
            values = pose[3:]
            right_trigger = float(pose[3 + 14 + 7])
            body_rotation = tuple(values[3:7])
            hand_rotation = q_mul(body_rotation, tuple(values[17:21]))
        except (TypeError, ValueError, IndexError):
            return
        now = time.monotonic()
        if right_trigger < 0.9 or now - tracked_flight["last_boost"] < 0.12:
            return
        props = bot.actors.get(sender) or {}
        account_id = props.get(253)
        if not account_id:
            return
        direction = q_rotate(hand_rotation, (0.0, 0.0, 1.0))
        magnitude = math.sqrt(direction.x ** 2 + direction.y ** 2 + direction.z ** 2) or 1.0
        force = direction * (16.0 / magnitude) + pun.Vector3(0, 4.0, 0)
        tracked_flight["last_boost"] = now
        asyncio.get_running_loop().create_task(bot.rpc(
            bot.view_id, "RpcApplyPlayerKnockback",
            [account_id, force, 3, None, -1], receivers=pun.RECV_OTHERS))

    bot.handlers.append(on_tracked_flight)

    # --- POWERS on the finger-gun gesture: LIGHTNING and FREEZE -----------------
    # Built from block outlines (previews), real explosions/knockback and, for the
    # ice, real glass blocks that actually hold the player for a few seconds.
    BOLT_COLS = ["yellow_emissive", "orange_light", "yellow_emissive", "orange_light"]   # yellow + light orange
    ICE_COLS = ["cyan_light", "blue_light", "white", "cyan_light", "cyan"]
    frozen = {}                                   # victim account -> {"cells": [...]}

    def rnd_axis():
        a = [random.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in a)) or 1.0
        return [x / n for x in a]

    def aim_target(origin, d, caster):
        """LOCK-ON: the player nearest to where they point (smallest angle, within
        ~25 degrees, up to 60 m), walls ignored. None if nobody is near the aim."""
        best, best_cos = None, math.cos(math.radians(25))
        for act in player_actors():
            if act == caster:
                continue
            w = bot.pose_of(act)[0] + pun.Vector3(0, 0.3, 0) - origin
            dist = math.sqrt(w.x ** 2 + w.y ** 2 + w.z ** 2)
            if dist < 0.5 or dist > 60:
                continue
            cos_a = (w.x * d.x + w.y * d.y + w.z * d.z) / dist
            if cos_a > best_cos:
                best, best_cos = act, cos_a
        return best

    async def lock_marker(victim, color, secs=0.3):
        """A little spinning ring over their head: 'locked on'."""
        ids = [rand_id() for _ in range(6)]
        for pid in ids:
            await boss_preview(pid, color)
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < secs:
            pose = bot.pose_of(victim)
            if pose:
                top = pose[0] + pun.Vector3(0, 1.4, 0)
                for i, pid in enumerate(ids):
                    ang = el * 12 + i * 2 * math.pi / len(ids)
                    r = 0.9 - el * 1.2
                    await boss_move(pid, top + pun.Vector3(math.cos(ang) * r, 0, math.sin(ang) * r), yaw_q(ang))
            await asyncio.sleep(0.05)
        for pid in ids:
            await boss_gone(pid)

    def feet_of(act):
        p = bot.pose_of(act)[0]
        return pun.Vector3(p.x, surface_below(p.x, p.z, p.y), p.z)

    async def strike(pos, victim=None):
        """LIGHTNING: a storm cloud gathers and a charge ring closes in, then a
        jagged bolt cracks down top to bottom, flickers, and the impact blasts a
        shockwave ring + sparks (real explosion; the victim is thrown up)."""
        if victim is not None and bot.pose_of(victim):
            pos = feet_of(victim)
        sky = pos + pun.Vector3(0, 15.0, 0)
        cloud = [rand_id() for _ in range(8)]
        ring = [rand_id() for _ in range(10)]
        bolt = [rand_id() for _ in range(26)]
        forks = [[rand_id() for _ in range(5)] for _ in range(3)]      # side branches
        for i, pid in enumerate(cloud):
            await boss_preview(pid, ("gray_dark", "black", "gray", "purple_dark")[i % 4], "2x2x2_stuffed")
        for pid in ring:
            await boss_preview(pid, "yellow_emissive")
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < 0.5:                 # gather (follows them)
            if victim is not None and bot.pose_of(victim):
                pos = feet_of(victim)
                sky = pos + pun.Vector3(0, 15.0, 0)
            for i, pid in enumerate(cloud):
                ang = el * 2.5 + i * 2 * math.pi / len(cloud)
                r = 2.6 - el * 1.6
                await boss_move(pid, sky + pun.Vector3(math.cos(ang) * r, math.sin(el * 7 + i) * 0.4,
                                                       math.sin(ang) * r), yaw_q(ang))
            for i, pid in enumerate(ring):
                ang = -el * 9 + i * 2 * math.pi / len(ring)
                r = 1.8 - el * 2.6
                await boss_move(pid, pos + pun.Vector3(math.cos(ang) * r, 0.15, math.sin(ang) * r), yaw_q(ang))
            await asyncio.sleep(0.05)

        def jag():
            pts, off, n = [], pun.Vector3(0, 0, 0), len(bolt)
            for i in range(n):
                f = i / (n - 1)
                if 0 < i < n - 1:
                    off = pun.Vector3(off.x * 0.45 + random.uniform(-0.75, 0.75), 0,
                                      off.z * 0.45 + random.uniform(-0.75, 0.75))
                else:
                    off = pun.Vector3(0, 0, 0)
                pts.append(sky + (pos - sky) * f + off)
            return pts

        def fork_pts(pts):
            out = []
            for fk in forks:
                start = pts[random.randint(4, len(pts) - 8)]
                ang = random.uniform(0, 6.3)
                step = pun.Vector3(math.cos(ang) * 0.55, -0.6, math.sin(ang) * 0.55)
                out.append([start + step * (j + 1) + pun.Vector3(random.uniform(-0.2, 0.2), 0, random.uniform(-0.2, 0.2))
                            for j in range(len(fk))])
            return out

        pts = jag()
        fpts = fork_pts(pts)
        for i, pid in enumerate(bolt):                              # crack down
            await boss_preview(pid, BOLT_COLS[i % len(BOLT_COLS)])
            await boss_move(pid, pts[i], spin_q(rnd_axis(), random.uniform(0, 6.3)))
            if i % 3 == 2:
                await asyncio.sleep(0.01)
        for fk, fp in zip(forks, fpts):                             # branches crackle off it
            for pid, q in zip(fk, fp):
                await boss_preview(pid, random.choice(BOLT_COLS))
                await boss_move(pid, q, spin_q(rnd_axis(), random.uniform(0, 6.3)))
        for pid in ring:
            await boss_gone(pid)
        if victim is not None:                                      # thrown high into the air
            uid = (bot.actors.get(victim) or {}).get(253)
            if uid:
                ang = random.uniform(0, 6.3)
                await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback",
                              [uid, pun.Vector3(math.cos(ang) * 7.0, 22.0, math.sin(ang) * 7.0), 3, None, -1],
                              receivers=pun.RECV_OTHERS)
        asyncio.get_running_loop().create_task(explode(pos, land=False, power=9.0, radius=5.5))
        for i in range(18):                                         # shockwave ring
            ang = i * 2 * math.pi / 18
            await fx_add(pos + pun.Vector3(0, 0.2, 0), BOLT_COLS[i % len(BOLT_COLS)],
                         vel=pun.Vector3(math.cos(ang) * 9.0, 0.0, math.sin(ang) * 9.0), life=0.6, spin=10.0)
        for _ in range(12):                                         # sparks
            await fx_add(pos + pun.Vector3(0, 0.4, 0), random.choice(BOLT_COLS),
                         vel=pun.Vector3(random.uniform(-3.5, 3.5), random.uniform(5, 10), random.uniform(-3.5, 3.5)),
                         gravity=14.0, life=0.9, spin=16.0)
        for _ in range(3):                                          # flicker
            await asyncio.sleep(0.07)
            pts = jag()
            fpts = fork_pts(pts)
            for i, pid in enumerate(bolt):
                await boss_move(pid, pts[i], spin_q(rnd_axis(), random.uniform(0, 6.3)))
            for fk, fp in zip(forks, fpts):
                for pid, q in zip(fk, fp):
                    await boss_move(pid, q, spin_q(rnd_axis(), random.uniform(0, 6.3)))
        await asyncio.sleep(0.12)
        for pid in bolt + cloud + [x for fk in forks for x in fk]:
            await boss_gone(pid)

    async def lightning_cast(origin, d, caster):
        ensure_avatar(caster)
        victim = aim_target(origin, d, caster)
        if victim is not None:
            await lock_marker(victim, "yellow_emissive", 0.25)
            await strike(feet_of(victim), victim)
        else:
            hit, _ = raycast(origin + d * 0.3, d, max_dist=45.0)
            await strike(hit)

    async def ice_burst(pos, n):
        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [pos, pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)
        for i in range(n):
            await fx_add(pos, random.choice(ICE_COLS + ["cyan_emissive", "white"]),
                         vel=pun.Vector3(random.uniform(-4, 4), random.uniform(2, 7), random.uniform(-4, 4)),
                         gravity=12.0, life=0.9, spin=14.0)

    async def freeze_grenade_at(pos):
        """A real freezeGrenade going off at pos (create, pin, "x" = detonate, remove)."""
        iid = rand_id()
        zero, ident = pun.Vector3(), pun.Quaternion(1, 0, 0, 0)
        await bot.rpc(bot.view_id, "RpcCreateItem",
                      [iid, "freezeGrenade", 0, None, pos, ident, zero, zero, TypedArray(ord("s"), []), Byte(1), Byte(1)],
                      receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcItemTransformation", [user_id, iid, 1, None, pos, ident, zero, zero],
                      receivers=pun.RECV_OTHERS)
        await asyncio.sleep(0.05)
        await bot.rpc(bot.view_id, "RpcSimpleCustomItemMessage", [user_id, iid, "x"], receivers=pun.RECV_OTHERS)
        await asyncio.sleep(0.1)
        await bot.rpc(bot.view_id, "RpcDestroyItem", [iid], receivers=pun.RECV_OTHERS)

    CAGE = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (0, 1, 2) for dz in (-1, 0, 1) if (dx, dy, dz) != (0, 1, 0)]
    CAGE_COLS = ["cyan_emissive", "cyan_light", "white", "blue_light"]

    async def encase(victim, hold=15.0):
        """FREEZE: a 3x3x3 pillow-outline cage flies together round them, then a
        real freeze grenade goes off on them every 1.5 s for `hold` seconds (so
        they stay frozen), twinkling; at the end the cage bursts into shards."""
        ac = acct_of(victim)
        if not ac or ac in frozen or not bot.pose_of(victim):
            return
        cage = [rand_id() for _ in CAGE]
        frozen[ac] = {"stop": False}
        DX, _, DZ = bot.room_dims

        def base():
            pose = bot.pose_of(victim)
            if pose:
                p = pose[0]
                gx, gz = math.floor(p.x * 2 + DX / 2), math.floor(p.z * 2 + DZ / 2)
                base.last = pun.Vector3((gx - DX / 2) * 0.5 + 0.25, surface_below(p.x, p.z, p.y) + 0.25,
                                        (gz - DZ / 2) * 0.5 + 0.25)
            return base.last
        base.last = None
        c = base()
        for i, pid in enumerate(cage):
            await boss_preview(pid, CAGE_COLS[i % len(CAGE_COLS)])
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < 0.5:                 # the cage flies together
            f = 1 - (1 - el / 0.5) ** 3
            for (dx, dy, dz), pid in zip(CAGE, cage):
                to = c + pun.Vector3(dx * 0.5, dy * 0.5, dz * 0.5)
                out = pun.Vector3(dx, dy - 1, dz)
                n_ = math.sqrt(out.x ** 2 + out.y ** 2 + out.z ** 2) or 1.0
                frm = to + out * (4.0 / n_)
                await boss_move(pid, frm + (to - frm) * f, spin_q([0.0, 1.0, 0.0], (1 - f) * 6))
            await asyncio.sleep(0.05)
        end, next_nade, tick = time.monotonic() + hold, 0.0, 0
        ident = pun.Quaternion(1, 0, 0, 0)
        while time.monotonic() < end and not frozen.get(ac, {}).get("stop", True):
            c = base()
            for (dx, dy, dz), pid in zip(CAGE, cage):                # stays round them
                await boss_move(pid, c + pun.Vector3(dx * 0.5, dy * 0.5, dz * 0.5), ident)
            if time.monotonic() >= next_nade:                       # keep them frozen
                next_nade = time.monotonic() + 1.5
                pose = bot.pose_of(victim)
                asyncio.get_running_loop().create_task(freeze_grenade_at(pose[0] if pose else c))
            tick += 1
            if tick % 2 == 0:
                await fx_add(c + pun.Vector3(random.uniform(-0.8, 0.8), random.uniform(0.0, 1.4),
                                             random.uniform(-0.8, 0.8)), "white", vel=pun.Vector3(0, 0.4, 0),
                             life=0.4, spin=20.0)
            await asyncio.sleep(0.2)
        for pid in cage:
            await boss_gone(pid)
        frozen.pop(ac, None)
        await ice_burst(c + pun.Vector3(0, 0.5, 0), 18)

    async def thaw(ac, c=None):
        st = frozen.get(ac)
        if st:
            st["stop"] = True                     # encase() ends: removes the cage + bursts

    async def freeze_shot(origin, d, caster):
        """A spinning ice shard flies where they point, leaving a frosty trail;
        it freezes the first player it reaches, or bursts on a wall."""
        ensure_avatar(caster)
        victim = aim_target(origin, d, caster)
        sid = rand_id()
        if victim is not None:
            # LOCKED ON: the shard homes in on them (curves after them, round walls)
            asyncio.get_running_loop().create_task(lock_marker(victim, "cyan_emissive", 0.3))
            await boss_preview(sid, "cyan_emissive")
            p, vel, t0 = origin, d * 14.0, time.monotonic()
            while time.monotonic() - t0 < 4.0:
                pose = bot.pose_of(victim)
                if not pose:
                    break
                goal = pose[0] + pun.Vector3(0, 0.4, 0)
                to = goal - p
                gd = math.sqrt(to.x ** 2 + to.y ** 2 + to.z ** 2)
                if gd < 0.7:
                    break
                want = to * (24.0 / gd)
                vel = vel + (want - vel) * 0.35                     # steer toward them
                p = p + vel * 0.05
                await boss_move(sid, p, spin_q([0.27, 0.93, 0.25], (time.monotonic() - t0) * 9))
                if random.random() < 0.7:
                    await fx_add(p, random.choice(["white", "cyan_light"]),
                                 vel=pun.Vector3(random.uniform(-0.6, 0.6), random.uniform(-0.2, 0.6),
                                                 random.uniform(-0.6, 0.6)), gravity=1.0, life=0.35, spin=12.0)
                await asyncio.sleep(0.05)
            await boss_gone(sid)
            if bot.pose_of(victim):
                await encase(victim)
            return
        hit, dist = raycast(origin + d * 0.3, d, max_dist=40.0)
        end_t = dist
        await boss_preview(sid, "cyan_emissive")
        t = 0.0
        while t < end_t:
            t = min(end_t, t + 22.0 * 0.05)
            p = origin + d * t
            await boss_move(sid, p, spin_q([0.27, 0.93, 0.25], t * 3))
            if random.random() < 0.7:
                await fx_add(p, random.choice(["white", "cyan_light"]),
                             vel=pun.Vector3(random.uniform(-0.6, 0.6), random.uniform(-0.2, 0.6), random.uniform(-0.6, 0.6)),
                             gravity=1.0, life=0.35, spin=12.0)
            await asyncio.sleep(0.05)
        await boss_gone(sid)
        if victim is not None:
            await encase(victim)
        else:
            await ice_burst(origin + d * end_t, 8)

    # --- JUMP SCARE gun: point at someone and the bot appears right in their face
    # (a black polar bear: skin + eye colour 0) with a loud scream from its mouth.
    # colour 0 reads as "not set" (default colours), so near-black: RGB 5,5,5 packed
    SCARE_LOOK = {"skinColor": 84215295, "eyeColor": 84215295,
                  "activeCosmetics": ["polarBearHead", "polarBearBody", "polarBearArms", "polarBearLegs",
                                      "polarBearTail"]}
    sc = {"ready": False, "voice": None, "room": None, "packets": None, "busy": False}

    async def scare_voice():
        from photon.voice import VoiceSender
        if sc["voice"]:
            try:
                await sc["voice"].close()
            except Exception:
                pass
        await bot.spawn_speaker()
        v_ = VoiceSender(cur["room"], bot.actor * 1000 + 2, a.name)
        await v_.connect()
        sc["voice"], sc["room"] = v_, cur["room"]

    async def scare_prepare():
        """Dress the bot for scaring and open its voice (it rejoins once so
        everyone loads the look)."""
        if sc["packets"] is None:
            from photon.voice import opus_packets
            sc["packets"] = await asyncio.to_thread(opus_packets, os.path.join(HERE, "media", "scare.wav"),
                                                    None, "64k")
        if not sc["ready"]:
            await asyncio.to_thread(_db_set_look, SCARE_LOOK)
            await rejoin_same_room(None, {"skinColor": pun.Vector3(0.02, 0.02, 0.02),
                                          "eyeColor": pun.Vector3(0.02, 0.02, 0.02)})
            sc["ready"] = True
            sc["room"] = None                                   # actor changed: new voice link
        print("jump scare ready (black polar bear)")

    # the game's own sound list (data.unity3d SoundEffect database, index = id)
    SFX = {"skullScream": 374, "monsterReveal": 375, "monsterRoar": 377}

    async def play_sfx(sid, pos, volume=1.0, pitch=1.0):
        """RpcPlaySoundEffect(id, x, y, z in 0.1 m, volume x1000, pitch x1000, seed)."""
        # The game's RPC takes six shorts + an int (what real clients send); seven
        # ints don't match it and every client logs an error (a lag spike) instead.
        from photon.protocol16 import Short as _Sh
        sh = lambda v: _Sh(max(-32768, min(32767, int(v))))
        await bot.rpc(bot.view_id, "RpcPlaySoundEffect",
                      [sh(sid), sh(round(pos.x * 10)), sh(round(pos.y * 10)), sh(round(pos.z * 10)),
                       sh(volume * 1000), sh(pitch * 1000), random.randint(0, 9999)], receivers=pun.RECV_OTHERS)

    def ball_cells(center, r_in=2.6, r_out=3.6):
        """Grid cells of a hollow ball (in cells) round a world point."""
        DX, DY, DZ = bot.room_dims
        cx, cy, cz = center.x * 2 + DX / 2, center.y * 2, center.z * 2 + DZ / 2
        out = []
        R = int(math.ceil(r_out))
        for x in range(int(cx) - R, int(cx) + R + 1):
            for y in range(int(cy) - R, int(cy) + R + 1):
                for z in range(int(cz) - R, int(cz) + R + 1):
                    d = math.sqrt((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 + (z + 0.5 - cz) ** 2)
                    if r_in <= d <= r_out and 0 <= x < DX and 0 <= z < DZ and 0 <= y < DY \
                            and (x, y, z) not in live.cells:
                        out.append((x, y, z))
        return out

    def scare_pose(v):
        """The bot's pose with its face right in front of the victim's eyes,
        facing them, claws up by its face, shaking. Returns (pose, mouth point)."""
        bp, bq = pun.Vector3(v[0], v[1], v[2]), tuple(v[3:7])
        hl, hq = list(v[7:10]), tuple(v[10:14])
        head = bp + q_rotate(bq, hl)
        view = q_rotate(q_mul(bq, hq), (0.0, 0.0, 1.0))
        n_ = math.hypot(view.x, view.z)
        if n_ < 0.2:
            view = q_forward(bq); n_ = math.hypot(view.x, view.z) or 1.0
        view = pun.Vector3(view.x / n_, 0.0, view.z / n_)
        face = head + view * 0.32 + pun.Vector3(random.uniform(-0.05, 0.05), 0.12 + random.uniform(-0.05, 0.05),
                                                random.uniform(-0.05, 0.05))     # a little higher, shaking
        yaw = math.atan2(-view.x, -view.z)
        q = (0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2))
        body = face - q_rotate(q, hl)
        out = list(v)
        out[0:3] = [body.x, body.y, body.z]
        out[3:7] = list(q)
        # the head jerks and twitches round (yaw/pitch/roll up to ~35 degrees)
        tt = time.monotonic()
        hy = math.sin(tt * 17) * 0.35 + random.uniform(-0.15, 0.15)
        hp = math.sin(tt * 11 + 1.3) * 0.3 + random.uniform(-0.12, 0.12)
        hr = math.sin(tt * 23 + 0.7) * 0.4 + random.uniform(-0.15, 0.15)
        cy_, sy_ = math.cos(hy / 2), math.sin(hy / 2)
        cp_, sp_ = math.cos(hp / 2), math.sin(hp / 2)
        cr_, sr_ = math.cos(hr / 2), math.sin(hr / 2)
        out[10:14] = [cy_ * sp_ * cr_ + sy_ * cp_ * sr_, sy_ * cp_ * cr_ - cy_ * sp_ * sr_,
                      cy_ * cp_ * sr_ - sy_ * sp_ * cr_, cy_ * cp_ * cr_ + sy_ * sp_ * sr_]
        for off, side in ((14, 1.0), (24, -1.0)):
            out[off:off + 3] = [hl[0] + side * 0.2, hl[1] + 0.02, hl[2] + 0.18]
            out[off + 3:off + 7] = [0.0, 0.0, 0.0, 1.0]
            out[off + 7:off + 10] = [1.0, 1.0, 1.0]
        return out, face

    # Trick-or-Treat door events (owner 2026-10-18: "the dancing skellys ... spawn around the
    # player"). The big Halloween door (8x10x4_halloweenDoor, BigHalloweenDoorMapObject) runs
    # its events from RpcSimpleCustomMapObjectMessage "s|<event>|<seed>|<player key>|1" (seen
    # in a recording of a knock); every client then plays the event locally around that
    # player. Event list of the door (game data): 0 skeleton dance party, 1 horror mob,
    # 2 fire storm, 3 sleep, 4 float, 5 ice storm. Uses a door already in the room, else
    # places a temporary one out of the way and removes it once the event is over.
    DOOR_KEY = "8x10x4_halloweenDoor"
    DOOR_EVENTS = {"skelly": 0, "skeleton": 0, "skeletons": 0, "dance": 0, "party": 0,
                   "horror": 1, "mob": 1, "fire": 2, "firestorm": 2, "sleep": 3,
                   "float": 4, "ice": 5, "icestorm": 5}
    DOOR_EVENT_NAMES = ["skeleton dance party", "horror mob", "fire storm", "sleep", "float", "ice storm"]

    def door_free_spot(near):
        """Lowest free 8x10x4 spot (grid) for a temporary door, as far from the player as fits."""
        DX, DY, DZ = bot.room_dims
        w, h, d = live._dims(DOOR_KEY)
        best = None
        for x in range(0, max(1, DX - w), 4):
            for z in range(0, max(1, DZ - d), 4):
                y = 0
                while y + h <= DY and any((x + i, y + j, z + k) in live.cells
                                          for i in range(w) for j in range(h) for k in range(d)):
                    y += 1
                if y + h > DY:
                    continue
                dist = (x - near[0]) ** 2 + (z - near[2]) ** 2
                if best is None or dist > best[0]:
                    best = (dist, x, y, z)
        return best[1:] if best else None

    async def door_event(victim, ev, at=None):
        uid = str((bot.actors.get(victim) or {}).get(253) or (bot.actors.get(victim) or {}).get("253") or "")
        if not uid:
            print("can't see that player's account yet"); return
        doors = [] if at else [k for k in live.owners if k[0] == DOOR_KEY]
        temp = None
        if at:
            # the party gathers round the door, so put a temporary door right where asked
            DX, DY, DZ = bot.room_dims
            w, h, d = live._dims(DOOR_KEY)
            x = max(0, min(DX - w, at[0] - w // 2)); y = max(0, min(DY - h, at[1])); z = max(0, min(DZ - d, at[2] - d // 2))
            await bot.rpc(bot.view_id, "RpcMapObjectCreated", [DOOR_KEY, x, y, z, 1, 2, "purple_dark", account, False],
                          receivers=pun.RECV_OTHERS)
            temp = (x, y, z)
            await asyncio.sleep(1.5)
        elif doors:
            _, x, y, z = doors[0]
        else:
            p_ = bot.pose_of(victim)
            spot = door_free_spot(bot.grid_of(p_[0]) if p_ else (0, 0, 0))
            if not spot:
                print("no room for a Halloween door in this map"); return
            x, y, z = spot
            await bot.rpc(bot.view_id, "RpcMapObjectCreated", [DOOR_KEY, x, y, z, 1, 2, "purple_dark", account, False],
                          receivers=pun.RECV_OTHERS)
            # not added to the live map: in a room in PLAY mode the game refuses it ("cannot
            # create creative map object in play mode"), and later presses must not aim at it
            temp = (x, y, z)
            print("(no Halloween door in this room: placed a temporary one - only works while the room is in CREATIVE mode)")
            await asyncio.sleep(1.5)
        seed = random.randint(-2 ** 31, 2 ** 31 - 1)
        await bot.rpc(bot.view_id, "RpcSimpleCustomMapObjectMessage", [DOOR_KEY, x, y, z, f"s|{ev}|{seed}|{uid}|1"],
                      receivers=pun.RECV_OTHERS)
        print(f"door event: {DOOR_EVENT_NAMES[ev]} on {pname(victim)} (door at {x},{y},{z}{', temporary' if temp else ''})")
        if temp:
            await asyncio.sleep(35)
            await bot.rpc(bot.view_id, "RpcDestroyMapObject", [DOOR_KEY, *temp, account, 0, False],
                          receivers=pun.RECV_OTHERS)

    # "skellyblock" (owner 2026-10-18: "i place a block pick it up and a skelly spawns"):
    # the hooked player places any block and picks it back up within a minute -> a skeleton
    # dance party right there (temporary door at that spot; the room must be in creative).
    skb = {"actor": None, "placed": {}}

    def on_skelly_block(code, sender, data):
        if skb["actor"] is None or code != pun.EV_RPC or sender != skb["actor"]:
            return
        r = pun.decode_rpc(data)
        if not r or len(r["args"]) < 4:
            return
        a_ = r["args"]
        key = (str(a_[0]), a_[1], a_[2], a_[3])
        if r["method"] == "RpcMapObjectCreated":
            skb["placed"][key] = time.time()
            if len(skb["placed"]) > 200:
                skb["placed"].pop(next(iter(skb["placed"])))
        elif r["method"] == "RpcDestroyMapObject":
            t0 = skb["placed"].pop(key, None)
            if t0 and time.time() - t0 < 60 and str(a_[0]) != DOOR_KEY:
                asyncio.get_running_loop().create_task(door_event(skb["actor"], 0, at=(a_[1], a_[2], a_[3])))
    bot.handlers.append(on_skelly_block)

    # Single dancing skeletons (live59+ clients; owner 2026-10-18 "spawn skelly"): queued on
    # our server for this Photon room, every live59 player there spawns the game's own
    # DancingSkeleton locally. 1 = in front of the player, N = a ring round them; all face them.
    # Bounce (4) removed by the owner 2026-10-18. Each skeleton stays for 15 loops of its dance
    # (salsa clip 2.04 s; fast x2, slow x0.45; Gangnam Style = 8 beats at 132 BPM)
    SKEL_STYLES = {"salsa": 0, "normal": 0, "fast": 1, "slow": 2, "slowmo": 2, "spin": 3,
                   "guangdong": 5, "gangnam": 5}
    SKEL_LOOP_S = {0: 2.0417, 1: 2.0417 / 2.0, 2: 2.0417 / 0.45, 3: 2.0417, 5: 8 * 60.0 / 132.0}
    SKEL_LOOPS = 15
    DANCE_DIR = os.path.join(os.path.dirname(HERE), "yeeps-proxy-240", "vr-server", "data", "dances")   # Mixamo dances (style 7)

    def dance_list():
        try:
            return json.load(open(os.path.join(DANCE_DIR, "index.json")))
        except Exception:
            return {}

    async def skel_spawn(victim, n=1, ttl=20.0, style=0, dance_slug=""):
        pose = bot.pose_of(victim)
        if not pose:
            print("can't see that player yet"); return
        p_, r_ = pose
        f_ = q_forward(r_)
        fl = math.hypot(f_.x, f_.z) or 1.0
        base = math.atan2(f_.x / fl, f_.z / fl)
        spots = []
        for i in range(max(1, n)):
            ang = base + (2 * math.pi * i / max(1, n))
            rad = 2.0 if n == 1 else 2.5
            sx, sz = p_.x + math.sin(ang) * rad, p_.z + math.cos(ang) * rad
            g = surface_below(sx, sz, p_.y + 0.5)
            if g < p_.y - 2.5:          # standing on terrain the block map doesn't know about
                g = p_.y - 1.2
            yaw = math.degrees(math.atan2(p_.x - sx, p_.z - sz))     # turned to face the player
            dances = dance_list()
            st = (7 if dances else random.choice([0, 1, 2, 3, 5])) if style < 0 else style   # -1 = random
            slug, loop_s = "", SKEL_LOOP_S.get(st, 2.0417)
            if st == 7:
                if not dances:
                    st = 0
                else:
                    slug = dance_slug if (dance_slug in dances) else random.choice(list(dances))
                    loop_s = float(dances[slug].get("seconds") or 4.0)
            spots.append({"x": round(sx, 2), "y": round(g, 2), "z": round(sz, 2), "yaw": round(yaw, 1),
                          "ttl": round(min(loop_s * SKEL_LOOPS, 240.0) + 1.0, 1), "style": st, "slug": slug,
                          "seed": random.randint(1, 2 ** 31)})
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:8240/bot/skel",
                                         data=json.dumps({"room": cur["room"], "spawns": spots}).encode(),
                                         headers={"Content-Type": "application/json"})
            await asyncio.get_running_loop().run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5).read())
            print(f"skelly: {len(spots)} dancing skeleton(s) at {pname(victim)} (live59+ players see it)")
        except Exception as e:
            print(f"skelly failed: {e!r}")

    async def jumpscare(victim):
        if sc["busy"]:
            return
        sc["busy"] = True
        acct = acct_of(victim)
        try:
            await scare_prepare()
            victim = next((x for x, pr in bot.actors.items() if x != bot.actor and isinstance(pr, dict)
                           and str(pr.get(253) or "").startswith(f"{acct}$")), None)
            for _ in range(40):
                if victim is not None and bot.poses.get(victim):
                    break
                await asyncio.sleep(0.05)
            if victim is None or not bot.poses.get(victim):
                print("jump scare: can't see them"); return
            # a ball of black blocks round their head: all they see is the bot
            vd = bot.poses.get(victim)
            v0 = list(vd[3:])
            head = pun.Vector3(v0[0], v0[1], v0[2]) + q_rotate(tuple(v0[3:7]), v0[7:10])
            ball = ball_cells(head)
            for (x, y, z) in ball:
                await bot.rpc(bot.view_id, "RpcMapObjectCreated", ["1x1_stuffed", x, y, z, 1, 2, "black", account, False],
                              receivers=pun.RECV_OTHERS)
                live.cells.add((x, y, z))
            sc["ball"] = ball
            start = time.monotonic()
            sounds = [(0.0, "skullScream", 2.5, 1.0), (0.0, "monsterRoar", 2.5, 0.9), (0.05, "monsterReveal", 2.5, 1.0),
                      (0.7, "skullScream", 2.5, 1.1), (1.2, "monsterRoar", 2.5, 1.0)]
            while time.monotonic() < start + 1.9:
                vd = bot.poses.get(victim)
                if vd:
                    pose_, mouth = scare_pose(list(vd[3:]))
                    state["override"] = pose_
                    await bot.send_pose(pose_)
                    el = time.monotonic() - start
                    while sounds and sounds[0][0] <= el:
                        _, key, vol, pit = sounds.pop(0)
                        await play_sfx(SFX[key], mouth, vol, pit)
                await asyncio.sleep(0.05)
            print(f"jump scared {pname(victim)}")
        except Exception as e:
            print(f"jump scare failed: {e!r}")
        finally:
            for (x, y, z) in sc.pop("ball", []) or []:              # take the ball away
                await bot.rpc(bot.view_id, "RpcDestroyMapObject", ["1x1_stuffed", x, y, z, account, 0, False],
                              receivers=pun.RECV_OTHERS)
                live.cells.discard((x, y, z))
            state["override"] = None
            state["speaker"] = None
            if state["mimic"] is None:
                state["last"] = None
                ensure_avatar(victim if victim is not None else next(iter(player_actors()), None))
            sc["busy"] = False

    async def scare_cast(origin, d, caster):
        victim = aim_target(origin, d, caster)
        if victim is not None:
            await jumpscare(victim)

    # --- mirror build: copy a player's block placements/deletes, offset ---
    mb = {"actor": None, "off": (0, 0, 0), "count": 0}
    track(mb, "actor")

    FACE_TURN = {0: 1, 1: 0, 2: 2, 3: 3, 4: 5, 5: 4}     # facing index after a 180 degree turn

    async def copy_block(method, args):
        dx, _, dz = mb["off"]
        DX, DY, DZ = bot.room_dims
        a2 = list(args)
        if mb.get("by_mimic"):
            # the same spot relative to the clone (turned round, as it faces them)
            src = (str(a2[0]), int(a2[1]), int(a2[2]), int(a2[3]))
            if method == "RpcDestroyMapObject":
                dst = mb.setdefault("cells", {}).pop(src, None)
                if dst is None:
                    return                       # not one it copied: never touch others' builds
                a2[1], a2[2], a2[3] = dst
            else:
                c = pun.Vector3((src[1] - DX / 2) * 0.5 + 0.25, src[2] * 0.5 + 0.25, (src[3] - DZ / 2) * 0.5 + 0.25)
                c2 = mx_pos(c)
                a2[1] = int(math.floor(c2.x * 2 + DX / 2))
                a2[2] = int(math.floor(c2.y * 2))
                a2[3] = int(math.floor(c2.z * 2 + DZ / 2))
                if a2[2] < 0:
                    return
                if len(a2) > 5:
                    a2[4] = FACE_TURN.get(int(a2[4]), a2[4])
                    a2[5] = FACE_TURN.get(int(a2[5]), a2[5])
                mb.setdefault("cells", {})[src] = (a2[1], a2[2], a2[3])
        else:
            a2[1], a2[3] = int(a2[1]) + dx, int(a2[3]) + dz
        if not (0 <= a2[1] < DX and 0 <= a2[3] < DZ):
            print(f"(copy of {a2[0]} would be outside the build area - skipped)"); return
        if method == "RpcMapObjectCreated":
            a2[7] = account
        else:
            a2[4] = account
        await bot.rpc(bot.view_id, method, a2, receivers=pun.RECV_OTHERS)
        mb["count"] += 1

    def on_build(code, sender, data):
        if code != pun.EV_RPC or mb["actor"] is None or sender != mb["actor"]:
            return
        r = pun.decode_rpc(data)
        if r and r["method"] in ("RpcMapObjectCreated", "RpcDestroyMapObject") and len(r["args"]) >= 5:
            asyncio.get_running_loop().create_task(copy_block(r["method"], r["args"]))

    bot.handlers.append(on_build)

    # --- mimic copies items too: pulling one out, holding it, throwing it,
    # using it (custom item messages) and putting it away. Each of the target's
    # items gets a twin owned by the bot, held in the bot's hand.
    mi = {"map": {}, "known": {}}          # their item id -> our twin id; id -> RpcCreateItem args
    ITEM_MSGS = ("RpcSimpleCustomItemMessage", "RpcSimpleFloatCustomItemMessage",
                 "RpcMultiVectorCustomItemMessage", "RpcPositionCustomItemMessage")


    async def mimic_items_clear():
        for bid in list(mi["map"].values()):
            await bot.rpc(bot.view_id, "RpcDestroyItem", [bid], receivers=pun.RECV_OTHERS)
        mi["map"].clear()

    async def twin_create(src_args, tuid):
        bid = rand_id()
        c = list(src_args)
        c[0] = bid
        if len(c) > 7:
            c[4], c[5], c[6], c[7] = mx_pos(c[4]), mx_rot(c[5]), mx_dir(c[6]), mx_dir(c[7])
        if len(c) > 8 and isinstance(c[8], list):
            c[8] = TypedArray(getattr(c[8], "code", ord("s")),
                              [user_id if x == tuid else x for x in c[8]])
        await bot.rpc(bot.view_id, "RpcCreateItem", c, receivers=pun.RECV_OTHERS)
        return bid

    async def copy_item_rpc(m, args, tuid):
        tacct = tuid.split("$")[0]
        if m == "RpcCreateItem" and args:
            mi["map"][args[0]] = await twin_create(args, tuid)
        elif m == "RpcItemTransformation" and len(args) >= 5:
            tid = args[1]
            if tid not in mi["map"]:
                src = mi["known"].get(tid)
                if not src:
                    return
                mi["map"][tid] = await twin_create(src, tuid)
            a2 = list(args)
            a2[0], a2[1] = user_id, mi["map"][tid]
            if a2[3] == tuid:
                a2[3] = user_id                  # held: in OUR hand
            elif a2[3] is None and len(a2) >= 8:   # loose in the world: the clone's spot + turn
                a2[4], a2[5], a2[6], a2[7] = mx_pos(a2[4]), mx_rot(a2[5]), mx_dir(a2[6]), mx_dir(a2[7])
            await bot.rpc(bot.view_id, m, a2, receivers=pun.RECV_OTHERS)
        elif m in ITEM_MSGS and len(args) >= 2 and args[1] in mi["map"]:
            a2 = list(args)
            a2[0], a2[1] = user_id, mi["map"][args[1]]
            for i in range(2, len(a2)):
                x = a2[i]
                if x == tacct:
                    a2[i] = account
                elif isinstance(x, pun.Vector3):
                    a2[i] = mx_pos(x)
                elif isinstance(x, list) and x and isinstance(x[0], pun.Vector3):
                    # [origin, direction, ...]: origin to the clone's spot, the rest turned.
                    # Edit in place: a decoded Vector3[] keeps its custom_code for re-sending.
                    for j in range(len(x)):
                        x[j] = mx_pos(x[j]) if j == 0 else mx_dir(x[j])
            await bot.rpc(bot.view_id, m, a2, receivers=pun.RECV_OTHERS)

    def on_mimic_items(code, sender, data):
        if code != pun.EV_RPC:
            return
        r = pun.decode_rpc(data)
        if not r:
            return
        m, args = r["method"], list(r["args"] or [])
        loop_ = asyncio.get_running_loop()
        if m == "RpcCreateItem" and args:
            mi["known"][args[0]] = args
            if len(mi["known"]) > 3000:
                mi["known"].pop(next(iter(mi["known"])))
        if state["copy"] and state["b"] is not None:
            if m == "RpcApplyPlayerKnockback" and len(args) > 1 and args[0] == user_id \
                    and isinstance(args[1], pun.Vector3):
                clone_push(args[1])                                   # someone hit the clone
            elif m.startswith("RpcCreateExplosion"):
                at = next((x for x in args if isinstance(x, pun.Vector3)), None)
                rad = next((float(x) for x in args if isinstance(x, float)), 2.0)
                if at is not None:
                    b = state["b"]
                    dv = pun.Vector3(b.x - at.x, b.y - at.y, b.z - at.z)
                    dd = math.sqrt(dv.x ** 2 + dv.y ** 2 + dv.z ** 2) or 0.01
                    reach = rad * 2.5
                    if dd < reach:
                        k = 9.0 * (1 - dd / reach) / dd
                        clone_push(pun.Vector3(dv.x * k, abs(dv.y * k) + 3.0, dv.z * k))
        if m == "RpcDestroyItem" and args and args[0] in mi["map"]:
            bid = mi["map"].pop(args[0])
            loop_.create_task(bot.rpc(bot.view_id, "RpcDestroyItem", [bid], receivers=pun.RECV_OTHERS))
            return
        t = state["mimic"]
        if not state["copy"] or t is None or sender != t:
            return
        tuid = str((bot.actors.get(t) or {}).get(253) or "")
        if tuid and (m in ("RpcCreateItem", "RpcItemTransformation") or m in ITEM_MSGS):
            loop_.create_task(copy_item_rpc(m, args, tuid))

    bot.handlers.append(on_mimic_items)

    async def start_mirrorbuild(name, n):
        t = bot.actor_by_name(name)
        if t is None:
            print(f"no player called {name!r} here"); return
        for _ in range(30):                       # wait up to 3 s for their pose
            if bot.pose_of(t):
                break
            await asyncio.sleep(0.1)
        pose = bot.pose_of(t)
        if not pose:
            print(f"can't see which way {name} is facing yet - try again in a second"); return
        f = q_forward(pose[1])
        if abs(f.x) >= abs(f.z):
            off, way = (n if f.x > 0 else -n, 0, 0), ("+X" if f.x > 0 else "-X")
        else:
            off, way = (0, 0, n if f.z > 0 else -n), ("+Z" if f.z > 0 else "-Z")
        mb.update(actor=t, off=off, count=0); remember(mb, "actor")
        print(f"mirror build ON: every block {bot.actors[t].get(255)} places or deletes is copied "
              f"{n} blocks ahead ({way}, the way they're facing now). 'mirrorbuild off' to stop")

    if a.mirrorbuild:
        await start_mirrorbuild(who, a.mirrorbuild)

    # --- ONLY UP: a real climbing course of game blocks spiralling up the room --
    # Wood platforms + jump stones, spring pads, trampolines, vertical handhold-pole
    # climbs, checkpoints (button under an item dispenser = boost bomb, a lamp lit
    # by an alwaysPowering block, signs) and a summit with a party button. Facings:
    # rotation = LookRotation(DIRS[fwd], DIRS[up]) with DIRS = +X -X +Y -Y +Z -Z and
    # the grid position = the rotated block's lowest corner (Blazeisodd/YeepsMapLoader);
    # button-under-dispenser pairs and lamp-on-alwaysPowering come from real builds. Only empty cells are used and
    # everything placed is recorded so 'onlyup clear' takes it all back.
    OU_FILE = os.path.join(HERE, "logs", "onlyup_{}.json")
    ou = {"placed": [], "busy": False}
    WOOD = ["brown_light", "orange_light", "mocha_light", "brown", "yellow_light"]

    def ou_load():
        try:
            ou["placed"] = [tuple(x) for x in json.load(open(OU_FILE.format(cur["rk"])))]
        except Exception:
            ou["placed"] = []

    def ou_save():
        try:
            json.dump(ou["placed"], open(OU_FILE.format(cur["rk"]), "w"))
        except Exception as e:
            print(f"(couldn't save the only-up block list: {e!r})")

    async def ou_put(pattern, x, y, z, fwd=1, up=2, color="white", dims=(1, 1, 1)):
        DX, DY, DZ = bot.room_dims
        w, h, d = dims
        cells = [(x + i, y + j, z + k) for i in range(w) for j in range(h) for k in range(d)]
        if any(not (0 <= cx < DX and 0 <= cy < DY and 0 <= cz < DZ) or (cx, cy, cz) in live.cells
               for cx, cy, cz in cells):
            return False
        await bot.rpc(bot.view_id, "RpcMapObjectCreated", [pattern, x, y, z, fwd, up, color, account, False],
                      receivers=pun.RECV_OTHERS)
        for c in cells:
            live.cells.add(c)
        ou["placed"].append((pattern, x, y, z))
        await asyncio.sleep(0.05 if len(ou["placed"]) % 25 == 0 else 0.004)
        return True

    async def ou_msg(pattern, x, y, z, text):
        await bot.rpc(bot.view_id, "RpcSimpleCustomMapObjectMessage", [pattern, x, y, z, text],
                      receivers=pun.RECV_OTHERS)

    async def ou_pad(cx, y, cz, size, color):
        x0, z0 = cx - size // 2, cz - size // 2
        for i in range(size):
            for k in range(size):
                await ou_put("1x1_wooden", x0 + i, y, z0 + k, color=color)

    async def ou_signpost(x, y, z, text):
        """A 2-high wood post with a text sign on every side (readable from anywhere)."""
        await ou_put("1x1_wooden", x, y, z, color="brown")
        await ou_put("1x1_wooden", x, y + 1, z, color="brown")
        for dx, dz, fwd in ((1, 0, 0), (-1, 0, 1), (0, 1, 4), (0, -1, 5)):
            if await ou_put("1x1x2_textSign", x + dx, y, z + dz, fwd=fwd, up=2, color="white", dims=(1, 2, 1)):
                await ou_msg("1x1x2_textSign", x + dx, y, z + dz, f"{account}|{text}")

    async def ou_lamp(x, y, z):
        """Lamps are off: the owner asked for no light blocks in builds (2026-10-10)."""
        return

    async def ou_dispenser(x, y, z, item):
        """pulseButton_small with an itemDispenser right on top (how official maps wire them)."""
        if await ou_put("pulseButton_small", x, y, z, fwd=0, up=4, color="red"):
            if await ou_put("itemDispenser", x, y + 1, z, fwd=1, up=2, color="blue"):
                await ou_msg("itemDispenser", x, y + 1, z, f"set_{item}")

    async def ou_stones(a, b):
        """Jump stones from pad a to pad b ((x, z, top-layer y) each)."""
        ax, az, ay = a
        bx, bz, by = b
        dist = math.hypot(bx - ax, bz - az)
        n = max(0, int(dist / 2.6))
        for k in range(1, n + 1):
            f = k / (n + 1)
            await ou_put("1x1_wooden", int(round(ax + (bx - ax) * f)), ay + int(round((by - ay) * f)),
                         int(round(az + (bz - az) * f)), color=random.choice(WOOD))

    async def onlyup_build(t):
        if ou["busy"]:
            print("only up: already building"); return
        ou["busy"] = True
        try:
            pose = bot.pose_of(t)
            DX, DY, DZ = bot.room_dims
            p, rot = pose
            f = q_forward(rot)
            fn = math.hypot(f.x, f.z) or 1.0
            R = 9
            ux, uz = p.x * 2 + DX / 2, p.z * 2 + DZ / 2
            uy = int(round(surface_below(p.x, p.z, p.y) * 2))
            cx = min(max(ux + f.x / fn * (R + 3), R + 5), DX - R - 6)
            cz = min(max(uz + f.z / fn * (R + 3), R + 5), DZ - R - 6)
            th0 = math.atan2(uz - cz, ux - cx)
            top_y = min(DY - 10, uy + 60)
            ou_load()
            start_n = len(ou["placed"])
            plan = ["steps", "steps", "spring", "steps", "pole", "check", "steps", "tramp",
                    "steps", "spring", "steps", "pole", "check"]
            g_say(f"only up: building a {(top_y - uy) // 2} m climb in front of {pname(t)}...")
            prev, prev_kind, y, i, checks = None, "start", uy, 0, 0
            while True:
                kind = "start" if prev is None else ("summit" if y >= top_y else plan[(i - 1) % len(plan)])
                th = th0 + i * (2 * math.pi / 10)
                sx, sz = int(round(cx + R * math.cos(th))), int(round(cz + R * math.sin(th)))
                if prev is not None:
                    px, pz, py = prev
                    if prev_kind == "pole":                         # pad right next to the pole top
                        sx, sz = ou_pole_top[0], ou_pole_top[1]
                    elif prev_kind not in ("spring", "tramp"):
                        await ou_stones(prev, (sx, sz, y))
                if prev is not None and prev_kind in ("spring", "tramp"):
                    # launched straight up: land on a big pad close by
                    sx, sz = (prev[0] + sx) // 2, (prev[1] + sz) // 2
                size = {"start": 3, "steps": 2, "spring": 3, "pole": 2, "tramp": 3, "check": 4, "summit": 5}[kind]
                if prev_kind in ("spring", "tramp"):
                    size = max(size, 4)
                color = {"check": "green_light", "summit": "yellow", "start": "white"}.get(kind, random.choice(WOOD))
                await ou_pad(sx, y, sz, size, color)
                rise = 2
                if kind == "start":
                    await ou_signpost(sx + 2, y + 1, sz + 2, "ONLY UP\nClimb to the top!\nDon't look down")
                    await ou_put("1x1x2_arrowSign_up", sx - 1, y + 1, sz + 1, fwd=0, up=2, color="yellow", dims=(1, 2, 1))
                elif kind == "spring":
                    await ou_put("2x2_springPad", sx - 1, y + 1, sz - 1, fwd=4, up=2, color="green", dims=(2, 1, 2))
                    rise = 7
                elif kind == "tramp":
                    await ou_put("2x2_trampoline", sx - 1, y + 1, sz - 1, fwd=0, up=2, color="blue_light", dims=(2, 1, 2))
                    rise = 5
                elif kind == "pole":
                    th2 = th0 + (i + 1) * (2 * math.pi / 10)
                    nx, nz = cx + R * math.cos(th2), cz + R * math.sin(th2)
                    dx, dz = nx - sx, nz - sz
                    dn = math.hypot(dx, dz) or 1.0
                    qx, qz = int(round(sx + dx / dn * 2)), int(round(sz + dz / dn * 2))
                    for j in range(2):                              # 2 poles = 8 cells (4 m) up
                        # rotation = LookRotation(DIRS[fwd], DIRS[up]), DIRS = +X -X +Y -Y +Z -Z;
                        # a pole is 4 long on its local X (KNOWN_SIZES 4x1x1) and local X =
                        # up x forward, so fwd -Z / up -X points it straight up
                        await ou_put("1x4_handhold_pole", qx, y + 1 + 4 * j, qz, fwd=5, up=1, color="gray",
                                     dims=(1, 4, 1))
                    await ou_put("1x1x2_arrowSign_up", sx, y + 1, sz, fwd=0, up=2, color="yellow", dims=(1, 2, 1))
                    ou_pole_top = (int(round(qx + dx / dn * 2)), int(round(qz + dz / dn * 2)))
                    rise = 8
                elif kind == "check":
                    checks += 1
                    await ou_dispenser(sx, y + 1, sz, "throwingImpulseBomb")
                    await ou_lamp(sx + 1, y + 1, sz + 1)
                    await ou_lamp(sx - 2, y + 1, sz - 2)
                    await ou_signpost(sx - 2, y + 1, sz + 1, f"CHECKPOINT {checks}\nPress the red button\nfor a boost bomb")
                elif kind == "summit":
                    await ou_put("1x1_partyButton", sx, y + 1, sz, fwd=0, up=2, color="pink")
                    await ou_dispenser(sx + 2, y + 1, sz, "wristPropellor")
                    for lx, lz in ((-2, -2), (2, 2), (-2, 2), (2, -2)):
                        await ou_lamp(sx + lx, y + 1, sz + lz)
                    await ou_signpost(sx - 1, y + 1, sz + 2,
                                      "YOU MADE IT!\nPress the party button\nGrab a propeller\nto fly down")
                    break
                prev, prev_kind = (sx, sz, y), kind
                y += rise
                i += 1
            ou_save()
            g_say(f"only up: done - {len(ou['placed']) - start_n} blocks, {checks} checkpoints, "
                  f"top at {(y - uy) // 2} m. 'onlyup clear' removes it")
        except Exception as e:
            ou_save()
            print(f"only up failed: {e!r}")
        finally:
            ou["busy"] = False

    async def onlyup_clear():
        ou_load()
        n = 0
        for pattern, x, y, z in reversed(ou["placed"]):
            await take((x, y, z), pattern)
            live.add(pattern, x, y, z, sign=-1)
            n += 1
            await asyncio.sleep(0.004)
        ou["placed"] = []
        ou_save()
        g_say(f"only up: removed {n} blocks")

    ou_pole_top = (0, 0)

    # --- ONLY UP (full map): a floating grass island lobby (cosmetics store,
    # tech web, toy store, vending machines, flowers, bushes, gnomes, lamps,
    # signs) and a spiral climb through 5 themed sections - grass, snow, beach,
    # lab, sky - each with its own rocks/decorations and a checkpoint, up to a
    # summit. Footprints use real object sizes (media/known_sizes.json, from the
    # user's YeepsMapLoader) turned by the facing: local X = up x forward.
    try:
        KS = json.load(open(os.path.join(HERE, "media", "known_sizes.json")))
    except Exception:
        KS = {}
    DIRS6 = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]

    def ext(pattern, fwd, up):
        sx, sy, sz = KS.get(pattern) or LiveMap._dims(pattern)
        F, U = DIRS6[fwd], DIRS6[up]
        R = (U[1] * F[2] - U[2] * F[1], U[2] * F[0] - U[0] * F[2], U[0] * F[1] - U[1] * F[0])
        return tuple(abs(R[i]) * sx + abs(U[i]) * sy + abs(F[i]) * sz for i in range(3))

    async def fput(pattern, x, y, z, color, fwd=4, up=2):
        return await ou_put(pattern, int(x), int(y), int(z), fwd=fwd, up=up, color=color,
                            dims=ext(pattern, fwd, up))

    def face_to(dx, dz):
        """fwd index pointing along (dx, dz) (horizontal)."""
        return (0 if dx > 0 else 1) if abs(dx) >= abs(dz) else (4 if dz > 0 else 5)

    def face_away(dx, dz):
        """Stores/tech web: their FRONT is local -Z (official maps), so to show the
        front toward (dx, dz) their forward must point the other way."""
        return face_to(-dx, -dz)

    # Only blocks proven to exist in 1.31 (seen in 1.31 player saves or accepted
    # live): media/seen_in_131_saves.json. No sand, ice, vending machine, lab barrels.
    SECTIONS = [
        {"name": "GRASS", "stone": ("2x2x1_grassRock", "blue_turquoise"), "pad": ("4x4x4_grassRock", "blue_turquoise"),
         "big": ("8x8x4_grassRock", "blue_turquoise"),
         "deco": [("decoration_suburb_flower0", "red"), ("decoration_suburb_flower1", "yellow"),
                  ("decoration_suburb_flower2", "pink"), ("decoration_suburb_mushroom0", "red"),
                  ("decoration_suburb_gnome0", "blue"), ("2x2x2_bush", "green_dark")], "trick": "spring"},
        {"name": "SNOW", "stone": ("2x2x1_snowRock", "white"), "pad": ("2x2x2_snowRock", "white"),
         "big": ("8x8x4_snowRock", "white"),
         "deco": [("decoration_snow_snowman0", "blue"), ("decoration_snow_icicle0", "cyan_light"),
                  ("decoration_snow_pillar0", "white"), ("decoration_snow_icicle2", "cyan_light")], "trick": "pole"},
        {"name": "BEACH", "stone": ("2x2x1_beachRock", "yellow_light"), "pad": ("4x4_wooden", "yellow_light"),
         "big": ("8x8_wooden", "yellow_light"),
         "deco": [("decoration_beach_coral0", "purple_light"), ("decoration_beach_coral1", "pink"),
                  ("decoration_beach_crab0", "red"), ("decoration_beach_starfish0", "orange"),
                  ("decoration_beach_clam0", "pink"), ("2x2x2_bush", "green")], "trick": "tramp"},
        {"name": "LAB", "stone": ("2x2_slab2_wooden", "gray"), "pad": ("4x4_wooden", "gray"),
         "big": ("8x8_wooden", "gray_dark"),
         "deco": [("decoration_researchFacility_hazmat", "orange"), ("decoration_researchFacility_hazmat", "yellow")],
         "trick": "spring_pole", "lamps": True},
        {"name": "SKY", "stone": ("2x2x1_fantasyRock", "lavender"), "pad": ("4x4x4_fantasyRock", "lavender"),
         "big": ("8x8x4_fantasyRock", "lavender"),
         "deco": [("2x2x2_stuffed", "purple_emissive"), ("2x2x2_stuffed", "cyan_emissive"),
                  ("decoration_fantasy_unicorn0", "white"), ("decoration_fantasy_mushroom0", "purple"),
                  ("decoration_fantasy_gnome0", "pink")], "trick": "tramp"},
    ]

    async def put_top(pattern, x, top, z, color, fwd=4, up=2):
        """Place so the block's top surface is at layer `top` (players stand at top)."""
        h = ext(pattern, fwd, up)[1]
        return await fput(pattern, x, top - int(h), z, color, fwd=fwd, up=up)

    async def deco_on(pad_x, top, pad_z, size, sec, n=2):
        """A few decorations standing on a pad whose surface is at `top`."""
        for _ in range(n):
            pat, col = random.choice(sec["deco"])
            await fput(pat, pad_x + random.randint(0, max(0, size - 2)), top,
                       pad_z + random.randint(0, max(0, size - 2)), col, fwd=random.choice((0, 1, 4, 5)))

    async def onlyup_full(t):
        if ou["busy"]:
            print("only up: already building"); return
        ou["busy"] = True
        try:
            DX, DY, DZ = bot.room_dims
            p, rot = bot.pose_of(t)
            f = q_forward(rot)
            fn = math.hypot(f.x, f.z) or 1.0
            fx, fz = f.x / fn, f.z / fn
            ux, uz = p.x * 2 + DX / 2, p.z * 2 + DZ / 2
            uy = max(0, int(round((p.y - 0.45) * 2)))             # their feet (the ground is terrain)
            R = 20
            cx = int(min(max(ux + fx * 20, R + 5), DX - R - 6))
            cz = int(min(max(uz + fz * 20, R + 5), DZ - R - 6))
            top_l = min(uy + 8, DY - 70)                          # island surface
            ou["placed"] = []
            g_say(f"only up: building the full map next to {pname(t)} (island lobby + 5 worlds)...")

            # 1) floating grass island: 3 x 3 big grass rocks (24 x 24 cells)
            for i in (-12, -4, 4):
                for k in (-12, -4, 4):
                    await put_top("8x8x4_grassRock", cx + i, top_l, cz + k, "blue_turquoise")
            # grass steps from where they stand up onto the island
            ex, ez = cx - fx * 13, cz - fz * 13
            n_st = max(3, int(math.hypot(ex - ux, ez - uz) / 2))
            for k in range(1, n_st + 1):
                fr = k / (n_st + 1)
                await put_top("2x2x1_grassRock", ux + (ex - ux) * fr - 1, uy + 1 + int(round((top_l - 1 - uy) * fr)),
                              uz + (ez - uz) * fr - 1, "blue_turquoise")
            # 2) lobby: shops round the sides, fronts to the middle
            await fput("cosmeticsStore", cx - 11, top_l, cz - 4, "gray", fwd=face_away(1, 0))
            await fput("techWeb", cx + 3, top_l, cz - 4, "gray", fwd=face_away(-1, 0))
            await fput("petStore", cx - 3, top_l, cz + 4, "gray", fwd=face_away(0, -1))
            await fput("toyStore", cx - 11, top_l, cz + 6, "gray", fwd=face_away(1, 0))
            await fput("merchStore", cx + 7, top_l, cz + 6, "gray", fwd=face_away(-1, 0))
            for lx, lz in ((-11, -11), (9, -11), (-11, 10), (9, 10)):
                await fput("2x2x2_bush", cx + lx, top_l, cz + lz, "green_dark")
            for lx, lz in ((-9, -10), (8, -10), (-3, -7), (2, -7)):
                await ou_lamp(cx + lx, top_l, cz + lz)
            for _ in range(16):
                pat, col = random.choice(SECTIONS[0]["deco"][:5])
                await fput(pat, cx + random.randint(-9, 8), top_l, cz + random.randint(-11, -6), col,
                           fwd=random.choice((0, 1, 4, 5)))
            await ou_signpost(cx - 1, top_l, cz - 2, "ONLY UP\\nClimb all 5 worlds\\nto reach the top!")

            # 3) the climb: spiral round the island through 5 worlds
            th = math.atan2(-fz, -fx) + math.pi / 2
            y = top_l
            sx_, sz_ = cx + R * math.cos(th), cz + R * math.sin(th)
            await fput("decoration_suburb_arch0", sx_ - 2, y, sz_ - 1, "white", fwd=face_to(math.cos(th), math.sin(th)))
            sec_h = max(8, (DY - 7 - top_l) // len(SECTIONS))
            step = 0.21                                            # ~4 cells between hops (2 m)
            checks = 0
            for si, sec in enumerate(SECTIONS):
                stone_p, stone_c = sec["stone"]
                pad_p, pad_c = sec["pad"]
                trick = sec["trick"]
                trick_rise = {"spring": 6, "pole": 8, "tramp": 4, "spring_pole": 8}[trick]
                goal = y + max(5, sec_h - trick_rise)
                hop, last_rise = 0, 1
                while y < goal or hop < 7:
                    th += step
                    rise = random.choice((0, 1, 1, 1, 2)) if last_rise else random.choice((1, 1, 2))
                    last_rise = rise
                    y += rise
                    if y > DY - 8:
                        break
                    nx, nz = cx + R * math.cos(th), cz + R * math.sin(th)
                    hop += 1
                    if hop % 4 == 0:                                   # a rest pad with decorations
                        psz = int(ext(pad_p, 4, 2)[0])
                        await put_top(pad_p, nx - psz // 2, y + 1, nz - psz // 2, pad_c)
                        await deco_on(int(nx - psz // 2), y + 1, int(nz - psz // 2), psz, sec, n=1)
                        if sec.get("lamps"):
                            await ou_lamp(int(nx + psz // 2 - 1), y + 1, int(nz + psz // 2 - 1))
                    elif sec["name"] == "SKY" and hop % 2 == 0:
                        pat, col = random.choice(sec["deco"][:2])        # glowing pillow cubes
                        await put_top(pat, nx - 1, y + 1, nz - 1, col)
                    else:
                        await put_top(stone_p, nx - 1, y + 1, nz - 1, stone_c)
                # the section's trick to jump a level, from a pad
                th += step
                nx, nz = cx + R * math.cos(th), cz + R * math.sin(th)
                psz = int(ext(pad_p, 4, 2)[0])
                await put_top(pad_p, nx - psz // 2, y + 1, nz - psz // 2, pad_c)
                if trick in ("spring", "spring_pole"):
                    await fput("2x2_springPad", nx - 1, y + 1, nz - 1, "green")
                elif trick == "tramp":
                    await fput("2x2_trampoline", nx - 1, y + 1, nz - 1, "blue_light")
                if trick in ("pole", "spring_pole"):
                    qx, qz = cx + (R - 3) * math.cos(th + 0.12), cz + (R - 3) * math.sin(th + 0.12)
                    for j in range(2):                                 # vertical: local X -> -Y
                        await fput("1x4_handhold_pole", qx, y + 1 + 4 * j, qz, "gray", fwd=5, up=1)
                    await ou_put("1x1x2_arrowSign_up", int(nx), y + 1, int(nz + 1), fwd=0, up=2,
                                 color="yellow", dims=(1, 2, 1))
                y += trick_rise
                th += step * 1.4
                nx, nz = cx + (R - 1) * math.cos(th), cz + (R - 1) * math.sin(th)
                # checkpoint: a big platform with button-under-dispenser, lamp and a sign
                checks += 1
                last = si == len(SECTIONS) - 1
                big_p, big_c = sec["big"]
                bsz = int(ext(big_p, 4, 2)[0])
                ok = False
                for out in (0, 4, 8, -4):                      # nudge outward if the spiral is in the way
                    ox, oz = nx + math.cos(th) * out, nz + math.sin(th) * out
                    bx, bz = int(ox - bsz // 2), int(oz - bsz // 2)
                    if await put_top(big_p, bx, y + 1, bz, big_c):
                        ok = True
                        break
                if not ok:                                      # smaller pad instead
                    big_p, big_c = sec["pad"]
                    bsz = int(ext(big_p, 4, 2)[0])
                    bx, bz = int(nx - bsz // 2), int(nz - bsz // 2)
                    await put_top(big_p, bx, y + 1, bz, big_c)
                if last:
                    await ou_put("1x1_partyButton", bx + 3, y + 1, bz + 3, fwd=0, up=2, color="pink")
                    await ou_dispenser(bx + 5, y + 1, bz + 3, "wristPropellor")
                    for lx, lz in ((0, 0), (bsz - 1, bsz - 1), (0, bsz - 1), (bsz - 1, 0)):
                        await ou_lamp(bx + lx, y + 1, bz + lz)
                    await ou_signpost(bx + 2, y + 1, bz + 5,
                                      "YOU MADE IT TO THE TOP!\\nParty button = party\\nGrab a propeller\\nto fly down")
                    await deco_on(bx + 1, y + 1, bz + 1, bsz - 2, sec, n=3)
                else:
                    await ou_dispenser(bx + 1, y + 1, bz + 1, "throwingImpulseBomb")
                    await ou_lamp(bx + 3, y + 1, bz + 3)
                    nxt = SECTIONS[si + 1]["name"]
                    await ou_signpost(bx + bsz - 2, y + 1, bz + 1,
                                      f"CHECKPOINT {checks}\\n{sec['name']} done!\\nNext: {nxt}\\nRed button = boost bomb")
                    await deco_on(bx + 1, y + 1, bz + 2, bsz - 2, sec, n=2)
            ou_save()
            g_say(f"only up: full map done - {len(ou['placed'])} blocks, island lobby + {checks} worlds, "
                  f"summit {(y - uy) // 2} m up. 'onlyup clear' removes it all")
        except Exception as e:
            ou_save()
            print(f"only up (full) failed: {e!r}")
        finally:
            ou["busy"] = False

    # --- TEST HOUSE: checks rotations. A 16x16 grass block, white wooden walls
    # with glass windows and a doorway, a gable roof of 1x1 slopes (a slope's
    # tall side is its FORWARD - from real roofs: the solid block next to a
    # slope is always on its forward side), a chimney, a little garden, and a
    # row of 4 slopes (fwd 0/1/4/5) with labels to check against.
    async def build_house(t):
        if ou["busy"]:
            print("busy"); return
        ou["busy"] = True
        try:
            DX, DY, DZ = bot.room_dims
            p, rot = bot.pose_of(t)
            f = q_forward(rot)
            fn = math.hypot(f.x, f.z) or 1.0
            fx, fz = f.x / fn, f.z / fn
            ux, uz = p.x * 2 + DX / 2, p.z * 2 + DZ / 2
            uy = max(0, int(round((p.y - 0.45) * 2)))
            top = uy + 5                                       # grass surface (floating a bit)

            def free(ax, az):
                return all((x, y, z) not in live.cells for x in range(ax, ax + 16) for z in range(az, az + 17)
                           for y in range(top - 4, top + 14))
            spot = None
            for dist in (12, 20, 28, 36):                      # in front of them, else beside
                for side in (0, 18, -18):
                    ax = int(min(max(ux + fx * dist - fz * side - 8, 2), DX - 18))
                    az = int(min(max(uz + fz * dist + fx * side - 8, 2), DZ - 19))
                    if free(ax, az):
                        spot = (ax, az); break
                if spot:
                    break
            if not spot:
                print("house: no free 16x16 space near you - move somewhere emptier"); return
            gx, gz = spot
            ou_load()
            g_say(f"house: building the test house in front of {pname(t)}...")
            for dx_, dz_ in ((0, 0), (8, 0), (0, 8), (8, 8)):   # 4 big grass blocks (a 16x16 one can hit terrain)
                await put_top("8x8x4_grassRock", gx + dx_, top, gz + dz_, "blue_turquoise")
            # steps up from them
            for k in range(1, 5):
                fr = k / 5
                await put_top("2x2x1_grassRock", ux + (gx + 8 - ux) * fr * 0.6 - 1, uy + k, uz + (gz + 8 - uz) * fr * 0.6 - 1,
                              "blue_turquoise")
            x0, z0 = gx + 4, gz + 4                            # 8 x 8 house
            H = 4
            for yy in range(top, top + H):
                for i in range(8):
                    for (x, z) in ((x0 + i, z0), (x0 + i, z0 + 7), (x0, z0 + i), (x0 + 7, z0 + i)):
                        door = z == z0 and x in (x0 + 3, x0 + 4) and yy < top + 3
                        if door:
                            continue
                        window = yy in (top + 1, top + 2) and (
                            (z in (z0, z0 + 7) and x in (x0 + 1, x0 + 6)) or (x in (x0, x0 + 7) and z in (z0 + 2, z0 + 5)))
                        if window:
                            await ou_put("1x1_glass", x, yy, z, fwd=4, up=2, color="cyan_light")
                        else:
                            await ou_put("1x1_wooden", x, yy, z, fwd=4, up=2, color="white")
            # gable roof: ridge along X; south side rises toward +Z (fwd 4), north toward -Z (fwd 5)
            ry = top + H
            for k in range(5):
                for x in range(x0 - 1, x0 + 9):
                    await ou_put("1x1_slope_wooden", x, ry + k, z0 - 1 + k, fwd=4, up=2, color="brown")
                    await ou_put("1x1_slope_wooden", x, ry + k, z0 + 8 - k, fwd=5, up=2, color="brown")
                # fill the triangular gable ends under the roof
                for z in range(z0 + k, z0 + 8 - k):
                    if k < 4:
                        await ou_put("1x1_wooden", x0, ry + k, z, fwd=4, up=2, color="white")
                        await ou_put("1x1_wooden", x0 + 7, ry + k, z, fwd=4, up=2, color="white")
            for yy in range(ry + 2, ry + 6):                    # chimney through the north slope
                await ou_put("1x1_wooden", x0 + 5, yy, z0 + 6, fwd=4, up=2, color="red_dark")
            # garden: flowers, bushes, a lamp
            for (x, z, pat, col) in ((gx + 1, gz + 1, "2x2x2_bush", "green_dark"), (gx + 13, gz + 1, "2x2x2_bush", "green_dark"),
                                     (gx + 2, gz + 13, "decoration_suburb_flower0", "red"),
                                     (gx + 3, gz + 13, "decoration_suburb_flower1", "yellow"),
                                     (gx + 12, gz + 13, "decoration_suburb_flower2", "pink"),
                                     (gx + 13, gz + 13, "decoration_suburb_gnome0", "blue")):
                await fput(pat, x, top, z, col, fwd=5)
            await ou_lamp(gx + 2, top, gz + 3)
            # rotation test row: one slope per facing, with a label post each
            for n_, (fw, lbl) in enumerate(((0, "fwd 0 (+X)"), (1, "fwd 1 (-X)"), (4, "fwd 4 (+Z)"), (5, "fwd 5 (-Z)"))):
                x = gx + 2 + n_ * 3
                await ou_put("1x1_slope_wooden", x, top, gz + 15, fwd=fw, up=2, color="orange")
                await ou_put("1x1x2_textSign", x, top, gz + 14, fwd=5, up=2, color="white", dims=(1, 2, 1))
                await ou_msg("1x1x2_textSign", x, top, gz + 14, f"{account}|slope {lbl}\ntall side should\npoint {lbl[-3:-1]}")
            ou_save()
            g_say(f"house: done ({len(ou['placed'])} blocks recorded). 'onlyup clear' removes everything I built")
        except Exception as e:
            ou_save()
            print(f"house failed: {e!r}")
        finally:
            ou["busy"] = False

    def fit_facing(pattern, want):
        """A (fwd, up) that turns `pattern` into exactly `want` = (x, y, z) cells
        (e.g. a 2x4 plank standing up as a 2-wide, 4-tall, 1-thick wall)."""
        for f_ in range(6):
            for u_ in range(6):
                if f_ // 2 == u_ // 2:
                    continue
                if tuple(int(v) for v in ext(pattern, f_, u_)) == tuple(want):
                    return f_, u_
        return None

    async def piece(pattern, x, y, z, want, color):
        fu = fit_facing(pattern, want)
        if fu is None:
            print(f"(no facing makes {pattern} {want})"); return False
        return await ou_put(pattern, int(x), int(y), int(z), fwd=fu[0], up=fu[1], color=color, dims=want)

    # --- TEST HOUSE 2: same house, darker, built from the biggest pieces that
    # fit - 2x4 wall planks, 1x2 sill/lintel bars, 2x2 glass windows, an 8x8
    # floor, 2x2x2 roof slopes (tall side = forward) - in a room corner.
    async def build_house2(t, corner="far-left"):
        if ou["busy"]:
            print("busy"); return
        ou["busy"] = True
        try:
            DX, DY, DZ = bot.room_dims
            p, rot = bot.pose_of(t)
            f = q_forward(rot)
            fn = math.hypot(f.x, f.z) or 1.0
            fx, fz = f.x / fn, f.z / fn
            lx_, lz_ = -fz, fx                                   # their left
            ux, uz = p.x * 2 + DX / 2, p.z * 2 + DZ / 2
            uy = max(0, int(round((p.y - 0.45) * 2)))
            top = uy + 5
            corners = [(2, 2), (DX - 18, 2), (2, DZ - 18), (DX - 18, DZ - 18)]
            def score(c):
                dx, dz = c[0] + 8 - ux, c[1] + 8 - uz
                return dx * fx + dz * fz + dx * lx_ + dz * lz_     # far ahead + to the left
            gx, gz = max(corners, key=score)
            ou_load()
            n0 = len(ou["placed"])
            g_say(f"house 2: building in the far-left corner (as {pname(t)} is facing)...")
            for dx_, dz_ in ((0, 0), (8, 0), (0, 8), (8, 8)):
                await put_top("8x8x4_grassRock", gx + dx_, top, gz + dz_, "blue_turquoise")
            x0, z0 = gx + 4, gz + 4
            WALL, TRIM, GLASS, ROOF, FLOOR = "brown_dark", "mocha", "blue_dark", "gray_dark", "brown"
            await piece("8x8_wooden", x0, top, z0, (8, 1, 8), FLOOR)
            y1 = top + 1                                          # walls: rows y1 .. y1+3

            async def column(kind, x, z, along_x):
                w = (2, 1) if along_x else (1, 2)                  # (x, z) size of a 2-wide, 1-thick slice
                if kind == "wall":
                    await piece("2x4_wooden", x, y1, z, (w[0], 4, w[1]), WALL)
                elif kind == "win":
                    await piece("1x2_wooden", x, y1, z, (w[0], 1, w[1]), TRIM)
                    await piece("2x2_glass", x, y1 + 1, z, (w[0], 2, w[1]), GLASS)
                    await piece("1x2_wooden", x, y1 + 3, z, (w[0], 1, w[1]), TRIM)
                elif kind == "door":
                    await piece("1x2_wooden", x, y1 + 3, z, (w[0], 1, w[1]), TRIM)

            for i, kind in enumerate(("win", "door", "wall", "win")):          # south wall (faces them)
                await column(kind, x0 + 2 * i, z0, True)
            for i, kind in enumerate(("win", "wall", "wall", "win")):          # north wall
                await column(kind, x0 + 2 * i, z0 + 7, True)
            for i, kind in enumerate(("win", "wall", "win")):                  # west / east walls (z0+1..z0+6)
                await column(kind, x0, z0 + 1 + 2 * i, False)
                await column(kind, x0 + 7, z0 + 1 + 2 * i, False)
            # roof: 2x2x2 slopes, south rows rise toward +Z (fwd 4), north toward -Z (fwd 5)
            ry = y1 + 4
            for i in range(4):
                x = x0 + 2 * i
                await ou_put("2x2x2_slope_wooden", x, ry, z0, fwd=4, up=2, color=ROOF, dims=(2, 2, 2))
                await ou_put("2x2x2_slope_wooden", x, ry + 2, z0 + 2, fwd=4, up=2, color=ROOF, dims=(2, 2, 2))
                await ou_put("2x2x2_slope_wooden", x, ry, z0 + 6, fwd=5, up=2, color=ROOF, dims=(2, 2, 2))
                await ou_put("2x2x2_slope_wooden", x, ry + 2, z0 + 4, fwd=5, up=2, color=ROOF, dims=(2, 2, 2))
            for xx in (x0, x0 + 7):                                            # gable ends under the top rows
                await piece("2x2_wooden", xx, ry, z0 + 2, (1, 2, 2), WALL)
                await piece("2x2_wooden", xx, ry, z0 + 4, (1, 2, 2), WALL)
            for k in range(2):                                                  # chimney
                await ou_put("2x2x2_wooden", x0 + 5, ry + 2 + 2 * k, z0 + 6, fwd=4, up=2, color="red_dark", dims=(2, 2, 2))
            # garden
            await piece("2x4_wooden", x0 + 2, top - 0, z0 - 4, (2, 1, 4), "mocha")             # path to the door
            for (x, z, pat, col) in ((gx + 1, gz + 1, "2x2x2_bush", "green_dark"), (gx + 13, gz + 1, "2x2x2_bush", "green_dark"),
                                     (gx + 1, gz + 13, "2x2x2_bush", "green_dark"),
                                     (gx + 3, gz + 1, "decoration_suburb_flower0", "red"),
                                     (gx + 11, gz + 2, "decoration_suburb_flower2", "purple"),
                                     (gx + 13, gz + 13, "decoration_suburb_mushroom0", "red")):
                await fput(pat, x, top, z, col, fwd=5)
            ou_save()
            g_say(f"house 2: done ({len(ou['placed']) - n0} pieces) at the corner {gx},{gz}")
        except Exception as e:
            ou_save()
            print(f"house 2 failed: {e!r}")
        finally:
            ou["busy"] = False

    # ======================= HAND OF GOD / BLACK HOLE / METEORS =======================
    def from_to(u, v):
        """Quaternion (pun) turning unit vector u onto unit vector v."""
        cx_ = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        w_ = 1.0 + u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
        if w_ < 1e-6:                                            # opposite: half turn about any perpendicular
            return pun.Quaternion(0.0, 0.0, 1.0, 0.0) if abs(u[1]) < 0.9 else pun.Quaternion(0.0, 1.0, 0.0, 0.0)
        n_ = math.sqrt(w_ * w_ + cx_[0] ** 2 + cx_[1] ** 2 + cx_[2] ** 2)
        return pun.Quaternion(w_ / n_, cx_[0] / n_, cx_[1] / n_, cx_[2] / n_)

    def unit(x, y, z):
        n_ = math.sqrt(x * x + y * y + z * z) or 1.0
        return (x / n_, y / n_, z / n_)

    def clamp01(v):
        return max(0.0, min(1.0, v))

    # --- HAND OF GOD: a giant golden hand that copies a player's real hand ---
    # Fingers along the hand's +Z, palm facing inward (-X right, +X left), thumb on
    # top. Fingers curl with the grip, the index with the trigger, the thumb with
    # the thumb. Squeeze near someone = grab them (held at the palm by steady
    # knockback pulls), open = throw with the hand's speed; swing fast = smack.
    gh = {"on": False, "actor": None, "both": False, "hands": {}, "task": None}
    track(gh, "actor")
    GH_S, GH_UP, GH_FWD = 6.0, 0.6, 2.5
    GH_REST = (math.sin(0.19), 0.0, 0.0, math.cos(0.19))       # fingers angled ~22 deg down at rest
    BAR_X = (1.0, 0.0, 0.0)

    def gh_parts(side, ci, cg, ct):
        """A Yeep-style 3-finger hand (~4 m): palm, index finger (trigger), one
        finger for the grip, and a thumb."""
        parts = [("4x4_stuffed", (0.0, 0.0, 0.0), from_to((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)), "yellow_emissive")]
        for fy, c in ((0.5, ci), (-0.5, cg)):                    # index (top), grip finger (bottom)
            a1, a2 = c * 1.3, c * 1.4
            knuckle = (0.0, fy, 1.0)
            d1 = (-side * math.sin(a1), 0.0, math.cos(a1))
            d2 = (-side * math.sin(a1 + a2), 0.0, math.cos(a1 + a2))
            d3 = (-side * math.sin(a1 + a2 + c * 0.6), 0.0, math.cos(a1 + a2 + c * 0.6))
            mid = tuple(knuckle[i] + d1[i] * 1.0 for i in range(3))
            end2 = tuple(mid[i] + d2[i] * 1.0 for i in range(3))
            parts.append(("1x2_stuffed", tuple(knuckle[i] + d1[i] * 0.5 for i in range(3)), from_to(BAR_X, d1), "orange_emissive"))
            parts.append(("1x2_stuffed", tuple(mid[i] + d2[i] * 0.5 for i in range(3)), from_to(BAR_X, d2), "yellow_emissive"))
            parts.append(("1x1_stuffed", tuple(end2[i] + d3[i] * 0.25 for i in range(3)), from_to(BAR_X, d3), "orange_emissive"))
        # thumb: like the real Yeep hand (SimpleHand mesh) - a long thumb standing
        # straight UP off the top of the palm, at right angles to the fingers;
        # pressing the thumb folds it down toward the fingers.
        at = ct * 1.25
        t1 = unit(-side * 0.12, math.cos(at), math.sin(at))
        t2 = unit(-side * 0.12, math.cos(at * 1.35), math.sin(at * 1.35))
        base = (-side * 0.2, 1.0, 0.2)
        tmid = tuple(base[i] + t1[i] * 1.0 for i in range(3))
        parts.append(("1x2_stuffed", tuple(base[i] + t1[i] * 0.5 for i in range(3)), from_to(BAR_X, t1), "orange_emissive"))
        parts.append(("1x1_stuffed", tuple(tmid[i] + t2[i] * 0.25 for i in range(3)), from_to(BAR_X, t2), "yellow_emissive"))
        return parts

    def gh_track_pose(actor, off):
        """(giant hand position, rotation tuple, index, grip, thumb, user fwd) or None."""
        vd = bot.poses.get(actor)
        if not vd or len(vd) < 3 + 34:
            return None
        v = vd[3:]
        bp, bq = pun.Vector3(v[0], v[1], v[2]), tuple(v[3:7])
        head = bp + q_rotate(bq, v[7:10])
        chest = head + pun.Vector3(0, -0.25, 0)
        hand = bp + q_rotate(bq, v[off:off + 3])
        rq = q_mul(bq, tuple(v[off + 3:off + 7]))
        f_ = q_forward(bq)
        fn_ = math.hypot(f_.x, f_.z) or 1.0
        fwd = pun.Vector3(f_.x / fn_, 0.0, f_.z / fn_)
        P = chest + pun.Vector3(0, GH_UP, 0) + fwd * GH_FWD + (hand - chest) * GH_S
        try:
            return P, rq, float(v[off + 7]), float(v[off + 8]), float(v[off + 9]), fwd
        except (TypeError, ValueError):
            return None

    async def gh_start(actor, both):
        await gh_stop()
        ensure_avatar(actor)
        gh.update(on=True, actor=actor, both=both, hands={})
        remember(gh, "actor")
        for off, side in ((14, 1.0), (24, -1.0)) if both else ((14, 1.0),):
            ids = [rand_id() for _ in gh_parts(side, 0, 0, 0)]
            for pid, (pat, _, _, col) in zip(ids, gh_parts(side, 0, 0, 0)):
                await boss_preview(pid, col, pat)
            gh["hands"][off] = {"ids": ids, "side": side, "prev": None, "held": None, "open": True,
                                "hpos": {}, "hit_cd": {}, "swish": 0.0, "slam_cd": 0.0}
        gh.update(clap_d=None, clap_cd=0.0)
        gh["task"] = asyncio.create_task(gh_loop())
        print(f"HAND OF GOD on for {pname(actor)} ({'both hands' if both else 'right hand'}): move your hand, "
              f"squeeze to grab, let go to throw, swing to smack")

    async def gh_stop():
        gh["on"] = False
        t_ = gh.get("task")
        if t_:
            t_.cancel()
        for h in gh["hands"].values():
            for pid in h["ids"]:
                await boss_gone(pid)
        gh["hands"] = {}
        for pid in (gh.get("ghost") or {}).get("ids", []):
            await boss_gone(pid)
        gh["ghost"] = None
        gh["build_origin"] = None

    async def gh_loop():
        dt = 0.05
        try:
            while gh["on"]:
                await asyncio.sleep(dt)
                a_ = gh["actor"]
                if a_ is None:
                    continue
                now = time.monotonic()
                upose = bot.pose_of(a_)
                feet = upose[0].y - 0.45 if upose else 0.0
                # CLAP: their real hands snap together -> blast everyone close away
                vd0 = bot.poses.get(a_)
                if vd0 and len(vd0) >= 3 + 34:
                    v0 = vd0[3:]
                    bp0, bq0 = pun.Vector3(v0[0], v0[1], v0[2]), tuple(v0[3:7])
                    rh, lh = bp0 + q_rotate(bq0, v0[14:17]), bp0 + q_rotate(bq0, v0[24:27])
                    dd = math.dist((rh.x, rh.y, rh.z), (lh.x, lh.y, lh.z))
                    prev_d, prev_t = gh.get("clap_d"), gh.get("clap_t", now)
                    if vd0 is not gh.get("clap_vd"):                     # only on a fresh pose
                        gh["clap_vd"], gh["clap_d"], gh["clap_t"] = vd0, dd, now
                    else:
                        prev_d = None
                    if prev_d is not None and dd < 0.2 and now - prev_t > 0.01 and \
                            (prev_d - dd) / (now - prev_t) > 1.2 and now > gh["clap_cd"]:
                        gh["clap_cd"] = now + 1.0
                        mids = [o["prev"] for o in gh["hands"].values() if o.get("prev") is not None]
                        cpos = (mids[0] + mids[1]) * 0.5 if len(mids) == 2 else (mids[0] if mids else bp0)
                        asyncio.get_running_loop().create_task(gh_clap(cpos, a_))
                vd_now = bot.poses.get(a_)
                fresh = vd_now is not None and vd_now is not gh.get("last_vd")
                if fresh:
                    gh["last_vd"] = vd_now
                for off, h in gh["hands"].items():
                    if fresh or "meas" not in h:
                        tp = gh_track_pose(a_, off)
                        if not tp:
                            continue
                        Pm = tp[0]
                        pm = h.get("meas")
                        if pm is not None and now - pm[1] > 0.02:
                            h["mvel"] = (Pm - pm[0]) * (1 / (now - pm[1]))
                        h["meas"] = (Pm, now, tp)
                    Pm, tm, tp = h["meas"]
                    mv = h.get("mvel") or pun.Vector3()
                    msp = math.sqrt(mv.x ** 2 + mv.y ** 2 + mv.z ** 2)
                    fast = msp > 4.0
                    # predict between packets, plus a little lead so the hand doesn't trail
                    ahead = min(now - tm, 0.12) + (0.0 if fast else 0.08)
                    P = Pm + mv * ahead
                    _, rq, idx, grip, thb, fwd = tp
                    rq = q_mul(rq, GH_REST)
                    ci, cg, ct = clamp01((idx - 0.55) / 0.4), clamp01((grip - 0.55) / 0.4), clamp01(thb)
                    R = pun.Quaternion(rq[3], rq[0], rq[1], rq[2])
                    # Previews glide toward each new spot, except the first move after
                    # being hidden (MapObjectPreview.Hide clears its "shown" flag, then
                    # OnReceiveDoPreview places it instantly). So a finger that bends or
                    # straightens is hidden + moved in one go: it snaps instead of easing in.
                    lc = h.get("curl") or (ci, cg, ct)
                    snap = set()
                    if abs(ci - lc[0]) > 0.2:
                        snap |= {1, 2, 3}                          # index: 3 segments
                    if abs(cg - lc[1]) > 0.2:
                        snap |= {4, 5, 6}                          # grip finger: 3 segments
                    if abs(ct - lc[2]) > 0.2:
                        snap |= {7, 8}                             # thumb
                    h["curl"] = (ci, cg, ct)
                    parts_ = gh_parts(h["side"], ci, cg, ct)
                    wpos = [P + q_rotate(rq, loc) for (_, loc, _, _) in parts_]
                    low = min(w_.y for w_ in wpos) - 0.3
                    ground = max(feet, surface_below(P.x, P.z, P.y + 1.0))
                    touching = low <= ground + 0.15
                    if low < ground:                               # sits ON the ground, never in it
                        lift = pun.Vector3(0, ground - low, 0)
                        P = P + lift
                        wpos = [w_ + lift for w_ in wpos]
                    # Fast swing: every piece jumps straight to the new spot (hide + move),
                    # because gliding previews can't keep up with a 6x-scaled swing.
                    for n_, (pid, (pat, loc, lrot, _)) in enumerate(zip(h["ids"], parts_)):
                        if fast or n_ in snap:
                            await bot.rpc(bot.view_id, "RpcMapObjectPreviewHidden", [pid], receivers=pun.RECV_OTHERS)
                        await boss_move(pid, wpos[n_], qm(R, lrot))
                    palm = P + q_rotate(rq, (-h["side"] * 0.6, 0.0, 0.8))
                    vel = h.get("mvel") or pun.Vector3()
                    h["prev"] = P
                    speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
                    closed = grip >= 0.8
                    # grab: the moment the fist closes, take the nearest player near the palm
                    if closed and h["open"] and h["held"] is None:
                        best, bd = None, 2.6
                        for b_ in player_actors():
                            if b_ == a_ or (gh["both"] and any(o["held"] == b_ for o in gh["hands"].values())):
                                continue
                            d_ = bot.pose_of(b_)[0] - palm
                            dist = math.sqrt(d_.x ** 2 + d_.y ** 2 + d_.z ** 2)
                            if dist < bd:
                                best, bd = b_, dist
                        if best is not None:
                            h["held"] = best
                            h["hpos"] = {}
                            await play_sfx(50, palm, 2.0, 0.7)
                    h["open"] = not closed
                    held = h["held"]
                    if held is not None:
                        pose = bot.pose_of(held)
                        uid = (bot.actors.get(held) or {}).get(253)
                        if not pose or not uid:
                            h["held"] = None
                        elif closed and bot.poses.get(held) is not h["hpos"].get("vd"):   # keep them in the palm
                            pos = pose[0]
                            last, lt = h["hpos"].get("p"), h["hpos"].get("t", now)
                            pv = (pos - last) * (1 / max(now - lt, 0.05)) if last is not None else pun.Vector3()
                            h["hpos"].update(p=pos, t=now, vd=bot.poses.get(held))
                            fvec = (palm - pos) * 5.0 - pv * 0.6 + pun.Vector3(0, 2.5, 0)
                            m_ = math.sqrt(fvec.x ** 2 + fvec.y ** 2 + fvec.z ** 2)
                            if m_ > 30:
                                fvec = fvec * (30 / m_)
                            await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback", [uid, fvec, 3, None, -1],
                                          receivers=pun.RECV_OTHERS)
                        elif not closed:                                 # let go: throw
                            fvec = vel * 0.8 + pun.Vector3(0, 4, 0)
                            m_ = math.sqrt(fvec.x ** 2 + fvec.y ** 2 + fvec.z ** 2)
                            if m_ > 28:
                                fvec = fvec * (28 / m_)
                            await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback", [uid, fvec, 3, None, -1],
                                          receivers=pun.RECV_OTHERS)
                            await play_sfx(4, palm, 2.0, 0.6)
                            h["held"] = None
                    # SLAM: the hand comes down onto the ground -> explosion
                    fist = grip >= 0.8 and idx >= 0.8                # grip AND trigger closed
                    if fist and vel.y < -3.0 and touching and not h.get("was_touching") and now > h["slam_cd"]:
                        h["slam_cd"] = now + 0.8
                        hit_at = pun.Vector3(palm.x, ground + 0.1, palm.z)
                        asyncio.get_running_loop().create_task(gh_slam(hit_at, a_))
                    h["was_touching"] = touching
                    # SMASH: pillows the moving hand runs into are destroyed
                    if speed > 3.0 and now > h.get("smash_cd", 0.0):
                        h["smash_cd"] = now + 0.15
                        tip_ = P + q_rotate(rq, (0.0, 0.0, 2.6))
                        n_sm = await smash_pillows(palm, 3.0, cap=6) + await smash_pillows(tip_, 2.0, cap=4)
                        if n_sm:
                            h["smashed"] = h.get("smashed", 0) + n_sm
                    # smack: a fast swing hits whoever the hand passes through
                    if speed > 6.0:
                        if now > h["swish"]:                 # no swing sound (owner 2026-11-01: sounded like a chainsaw)
                            h["swish"] = now + 0.45
                        tip = P + q_rotate(rq, (0.0, 0.0, 3.0))
                        await fx_add(tip, random.choice(["yellow_emissive", "white", "orange_emissive"]),
                                     vel=pun.Vector3(random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(-1, 1)),
                                     life=0.3, spin=15.0)
                        vd_ = unit(vel.x, vel.y, vel.z)
                        for b_ in player_actors():
                            if b_ in (a_, h["held"]) or now < h["hit_cd"].get(b_, 0):
                                continue
                            q_ = bot.pose_of(b_)[0]
                            near = min(math.dist((q_.x, q_.y, q_.z), (c_.x, c_.y, c_.z)) for c_ in (palm, tip, P))
                            if near < 2.0:
                                h["hit_cd"][b_] = now + 0.7
                                k = min(speed * 0.85, 24.0)
                                await smack(b_, pun.Vector3(vd_[0] * k, vd_[1] * k + 5.0, vd_[2] * k), q_)
                                await play_sfx(226, q_, 2.5, 0.8)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"(hand of god stopped: {e!r})")

    # GIANT BUILDER (on with the hand of god, or 'bigbuild NAME'): every 1x1 block
    # they place becomes four 2x2x2 blocks of the same kind + colour round that spot.
    try:
        BIG_OK = set(json.load(open(os.path.join(HERE, "media", "seen_in_131_saves.json"))))
    except Exception:
        BIG_OK = {"2x2x2_wooden", "2x2x2_stuffed"}
    bigb = {"actor": None}
    track(bigb, "actor")

    def big_of(pat):
        big = "2x2x2_" + str(pat)[4:]
        return big if big in BIG_OK else "2x2x2_stuffed"

    def giant_origin(world):
        """Grid-locked 4x4x4 spot (cells, multiples of 4) holding a world point."""
        DX, DY, DZ = bot.room_dims
        ox = int(math.floor((world.x * 2 + DX / 2) / 4)) * 4
        oy = int(math.floor((world.y * 2) / 4)) * 4
        oz = int(math.floor((world.z * 2 + DZ / 2) / 4)) * 4
        return (min(max(ox, 0), DX - 4), min(max(oy, 0), DY - 4), min(max(oz, 0), DZ - 4))

    CUBE8 = [(i, j, k) for i in (0, 2) for j in (0, 2) for k in (0, 2)]
    own_blocks = set()          # giant-builder blocks: swipes don't smash them, a fist slam does

    async def giant_block(args):
        """They placed a 1x1: take it away and build a 4x4x4 cube of eight 2x2x2
        blocks (same kind + colour) where the hand of god is - or, without the
        hand, where the 1x1 was."""
        pat, x, y, z = str(args[0]), int(args[1]), int(args[2]), int(args[3])
        col = args[6] if len(args) > 6 else "white"
        big = big_of(pat)
        await asyncio.sleep(0.15)
        await bot.rpc(bot.view_id, "RpcDestroyMapObject", [pat, x, y, z, account, 0, False], receivers=pun.RECV_OTHERS)
        live.add(pat, x, y, z, sign=-1)
        o_ = gh.get("build_origin") if gh.get("on") else None
        lo = gh.get("last_origin")
        if o_ is None and gh.get("on") and lo and time.monotonic() - lo[1] < 1.5:
            o_ = lo[0]                                   # the outline was just there
        if o_ is None:
            o_ = (x - 1, y, z - 1)
        DX, DY, DZ = bot.room_dims
        for dx, dy, dz in CUBE8:
            bx, by, bz = o_[0] + dx, o_[1] + dy, o_[2] + dz
            cells = [(bx + i, by + j, bz + k) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
            if any(not (0 <= c[0] < DX and 0 <= c[1] < DY and 0 <= c[2] < DZ) or c in live.cells for c in cells):
                continue
            await bot.rpc(bot.view_id, "RpcMapObjectCreated", [big, bx, by, bz, 4, 2, col, account, False],
                          receivers=pun.RECV_OTHERS)
            live.add(big, bx, by, bz)
            own_blocks.add((big, bx, by, bz))
        await play_sfx(15, pun.Vector3((o_[0] + 2 - DX / 2) * 0.5, (o_[1] + 2) * 0.5, (o_[2] + 2 - DZ / 2) * 0.5), 2.0, 0.7)

    # what they're holding: their own build preview (RpcMapObjectPreviewCreated/
    # Updated/Hidden/Destroyed from them) tells us it's a 1x1 and its colour
    gh_hold = {"id": None, "pattern": None, "color": None, "visible": False}

    def on_giant_place(code, sender, data):
        if code != pun.EV_RPC:
            return
        who_ = bigb["actor"] if bigb["actor"] is not None else (gh["actor"] if gh.get("on") else None)
        if who_ is None or sender != who_:
            return
        r = pun.decode_rpc(data)
        if not r:
            return
        m, a_ = r["method"], r["args"] or []
        if m == "RpcMapObjectCreated" and len(a_) >= 7 and str(a_[0]).startswith("1x1_"):
            asyncio.get_running_loop().create_task(giant_block(list(a_)))
        elif m == "RpcMapObjectPreviewCreated" and len(a_) >= 4:
            gh_hold.update(id=a_[1], pattern=str(a_[2]), color=a_[3], visible=False)
        elif m == "RpcMapObjectPreviewUpdated" and a_ and a_[0] == gh_hold["id"]:
            gh_hold["visible"] = True
        elif m in ("RpcMapObjectPreviewHidden", "RpcMapObjectPreviewDestroyed") and a_ and a_[0] == gh_hold["id"]:
            gh_hold["visible"] = False
            if m == "RpcMapObjectPreviewDestroyed":
                gh_hold["id"] = None

    async def gh_ghost(P, rq):
        """The 4x4x4 build outline at the hand, grid-locked, while they hold a 1x1."""
        g = gh.get("ghost")
        if not g:
            g = gh["ghost"] = {"ids": [], "key": None, "origin": None, "shown": False}
        holding = (gh_hold["visible"] and gh_hold["pattern"] and gh_hold["pattern"].startswith("1x1_"))
        if not holding:
            gh["build_origin"] = None
            if g["shown"]:
                for pid in g["ids"]:
                    await bot.rpc(bot.view_id, "RpcMapObjectPreviewHidden", [pid], receivers=pun.RECV_OTHERS)
                g["shown"] = False
            return
        key = (big_of(gh_hold["pattern"]), gh_hold["color"])
        if key != g["key"]:                                     # (re)make the 8 outline pieces
            for pid in g["ids"]:
                await boss_gone(pid)
            g.update(ids=[rand_id() for _ in CUBE8], key=key, origin=None, shown=False)
            for pid in g["ids"]:
                await boss_preview(pid, key[1] or "white", key[0])
        o_ = giant_origin(P + q_rotate(rq, (0.0, 0.0, 2.2)))
        gh["build_origin"] = o_
        gh["last_origin"] = (o_, time.monotonic())
        if o_ != g["origin"] or not g["shown"]:
            DX, DY, DZ = bot.room_dims
            for pid, (dx, dy, dz) in zip(g["ids"], CUBE8):
                c_ = pun.Vector3((o_[0] + dx + 1 - DX / 2) * 0.5, (o_[1] + dy + 1) * 0.5, (o_[2] + dz + 1 - DZ / 2) * 0.5)
                await bot.rpc(bot.view_id, "RpcMapObjectPreviewHidden", [pid], receivers=pun.RECV_OTHERS)
                await boss_move(pid, c_, IDENT)                   # hidden + move = it jumps cell to cell
            g["origin"], g["shown"] = o_, True

    async def smash_pillows(center, radius_cells, cap=8, include_own=False):
        """Destroy pillow blocks (any '...stuffed...' object) near a world point.
        Blocks the giant builder made are skipped unless include_own (fist slams)."""
        DX, DY, DZ = bot.room_dims
        cx_, cy_, cz_ = center.x * 2 + DX / 2, center.y * 2, center.z * 2 + DZ / 2
        R_ = int(math.ceil(radius_cells))
        hit = {}
        for x in range(int(cx_) - R_, int(cx_) + R_ + 1):
            for y in range(max(0, int(cy_) - R_), int(cy_) + R_ + 1):
                for z in range(int(cz_) - R_, int(cz_) + R_ + 1):
                    o_ = live.obj.get((x, y, z))
                    if o_ and "stuffed" in o_[0].lower() and (include_own or o_ not in own_blocks) and \
                            (x + 0.5 - cx_) ** 2 + (y + 0.5 - cy_) ** 2 + (z + 0.5 - cz_) ** 2 <= radius_cells ** 2:
                        hit[o_] = True
                        if len(hit) >= cap:
                            break
        for (name, x, y, z) in hit:
            await bot.rpc(bot.view_id, "RpcDestroyMapObject", [name, x, y, z, account, 0, False], receivers=pun.RECV_OTHERS)
            live.add(name, x, y, z, sign=-1)
            own_blocks.discard((name, x, y, z))
            wp = pun.Vector3((x - DX / 2) * 0.5 + 0.25, y * 0.5 + 0.25, (z - DZ / 2) * 0.5 + 0.25)
            for _ in range(2):
                await fx_add(wp, random.choice(["pink", "white", "yellow_light"]),
                             vel=pun.Vector3(random.uniform(-3, 3), random.uniform(2, 5), random.uniform(-3, 3)),
                             gravity=12.0, life=0.6, spin=14.0)
        if hit:
            await play_sfx(63, center, 2.0, 0.8)
        return len(hit)

    async def gh_slam(at, owner):
        """Ground slam: a real explosion, a dust/shock ring along the ground and debris."""
        await play_sfx(320, at, 2.5, 0.7)
        await play_sfx(97, at, 2.0, 0.9)
        await smash_pillows(at + pun.Vector3(0, 0.5, 0), 5.0, cap=20, include_own=True)
        await explode(at, land=False, power=0.0, radius=0.1)        # the blast itself, no bot knockback
        for a_ in player_actors():                                   # knockback for others, not the owner
            if a_ == owner:
                continue
            p_ = bot.pose_of(a_)[0]
            d_ = p_ - at
            dist = math.sqrt(d_.x ** 2 + d_.y ** 2 + d_.z ** 2)
            if dist < 5.5:
                flat = math.hypot(d_.x, d_.z) or 1.0
                k = 7.0 * (1.15 - dist / 5.5)
                await smack(a_, pun.Vector3(d_.x / flat * k, 5.0 + k * 0.5, d_.z / flat * k), p_)
        for j in range(16):
            a_ = j * 2 * math.pi / 16
            await fx_add(at + pun.Vector3(0, 0.15, 0), ("yellow_emissive", "orange_emissive", "brown")[j % 3],
                         vel=pun.Vector3(math.cos(a_) * 9, 0.0, math.sin(a_) * 9), life=0.5, spin=10.0)
        for _ in range(8):
            await fx_add(at, random.choice(["brown", "gray", "brown_dark"]),
                         vel=pun.Vector3(random.uniform(-3, 3), random.uniform(4, 8), random.uniform(-3, 3)),
                         gravity=14.0, life=0.9, spin=14.0)

    async def gh_clap(at, owner):
        """Clap: everyone close is thrown away from it, with a bright burst ring."""
        await play_sfx(623, at, 2.5, 0.8)
        await play_sfx(99, at, 2.0, 1.0)
        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [at, pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)
        for a_ in player_actors():
            if a_ == owner:
                continue
            p_ = bot.pose_of(a_)[0]
            d_ = p_ - at
            dist = math.sqrt(d_.x ** 2 + d_.y ** 2 + d_.z ** 2)
            if dist > 7.0:
                continue
            flat = math.hypot(d_.x, d_.z) or 1.0
            k = 9.0 * (1.15 - dist / 7.0)
            await smack(a_, pun.Vector3(d_.x / flat * k, 4.0 + k * 0.4, d_.z / flat * k), p_)
        for ring_ in range(2):                                  # two bright rings (flat + upright)
            for j in range(14):
                a_ = j * 2 * math.pi / 14
                v_ = (pun.Vector3(math.cos(a_) * 10, 0.0, math.sin(a_) * 10) if ring_ == 0
                      else pun.Vector3(math.cos(a_) * 10, math.sin(a_) * 10, 0.0))
                await fx_add(at, ("white", "yellow_emissive")[j % 2], vel=v_, life=0.45, spin=14.0)

    # --- BLACK HOLE: a swirling vortex that drags everyone in, then collapses into a blast ---
    async def black_hole(center, secs=10.0):
        g_say("blackhole: a black hole opens...")
        core = [(rand_id(), unit(random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1))) for _ in range(10)]
        rings = []
        for r_i, col in enumerate(("purple_emissive", "cyan_emissive", "white")):
            for j in range(16):
                rings.append({"id": rand_id(), "ring": r_i, "a": j * 2 * math.pi / 16, "r": 3.0 + r_i * 2.0 + random.uniform(0, 1.5),
                              "col": col, "tilt": (r_i - 1) * 0.35})
        for pid, _ in core:
            await boss_preview(pid, random.choice(["black", "purple_dark", "black"]), "2x2x2_stuffed")
        for rg in rings:
            await boss_preview(rg["id"], rg["col"])
        t0 = time.monotonic()
        next_pull = next_snd = 0.0
        while (el := time.monotonic() - t0) < secs:
            grow = min(1.0, el / 1.5)
            for pid, d_ in core:                                    # tumbling dark core
                rr = 1.3 * grow + 0.15 * math.sin(el * 9 + d_[0] * 5)
                await boss_move(pid, center + pun.Vector3(d_[0] * rr, d_[1] * rr, d_[2] * rr), spin_q(d_, el * 4))
            for rg in rings:                                        # accretion rings spiral in, then respawn outside
                rg["a"] += (0.35 + 1.4 / max(rg["r"], 0.6)) * (1 if rg["ring"] != 1 else -1)
                rg["r"] -= 0.12 + 0.02 * rg["ring"]
                if rg["r"] < 0.9:
                    rg["r"] = 7.0 + random.uniform(0, 2)
                x_, z_ = math.cos(rg["a"]) * rg["r"], math.sin(rg["a"]) * rg["r"]
                y_ = z_ * math.sin(rg["tilt"])
                await boss_move(rg["id"], center + pun.Vector3(x_ * grow, y_ * grow, z_ * math.cos(rg["tilt"]) * grow),
                                yaw_q(-rg["a"]))
            if el > next_snd:
                next_snd = el + 1.8
                await play_sfx(207, center, 3.0, 0.6)
                await play_sfx(164, center, 2.5, 0.7)
            if el > next_pull and el > 1.0:                         # gravity: pull + swirl
                next_pull = el + 0.3
                for a_ in player_actors():
                    pos = bot.pose_of(a_)[0]
                    d_ = center - pos
                    dist = math.sqrt(d_.x ** 2 + d_.y ** 2 + d_.z ** 2)
                    if dist > 22 or dist < 0.2:
                        continue
                    k = 9.0 * (1.0 - dist / 22) + 2.0
                    swirl = pun.Vector3(-d_.z, 0, d_.x) * (1.5 / dist)
                    fvec = pun.Vector3(d_.x / dist * k, d_.y / dist * k + 1.2, d_.z / dist * k) + swirl
                    uid = (bot.actors.get(a_) or {}).get(253)
                    if uid:
                        await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback", [uid, fvec, 3, None, -1],
                                      receivers=pun.RECV_OTHERS)
            await asyncio.sleep(0.1)
        # collapse: everything rushes into the middle, then a huge blast
        for step_ in range(6):
            f_ = 1 - (step_ + 1) / 6
            for rg in rings:
                rg["a"] += 0.8
                await boss_move(rg["id"], center + pun.Vector3(math.cos(rg["a"]) * rg["r"] * f_, 0,
                                                               math.sin(rg["a"]) * rg["r"] * f_), IDENT)
            await asyncio.sleep(0.06)
        for pid, _ in core:
            await boss_gone(pid)
        for rg in rings:
            await boss_gone(rg["id"])
        await play_sfx(104, center, 3.0, 0.7)
        await play_sfx(97, center, 3.0, 0.8)
        asyncio.get_running_loop().create_task(explode(center, land=False, power=12.0, radius=10.0))
        for j in range(24):                                         # shockwave
            ang = j * 2 * math.pi / 24
            await fx_add(center, ("purple_emissive", "white", "cyan_emissive")[j % 3],
                         vel=pun.Vector3(math.cos(ang) * 14, 0.0, math.sin(ang) * 14), life=0.7, spin=12.0)
        g_say("blackhole: ...and it collapses!")

    # --- METEOR SHOWER: glowing meteors streak in with fiery trails and blow up ---
    async def meteor(target):
        ang = random.uniform(0, 2 * math.pi)
        start = target + pun.Vector3(math.cos(ang) * 16, 30.0, math.sin(ang) * 16)
        rocks = [(rand_id(), random.choice(["orange_emissive", "red_emissive", "yellow_emissive"]), rnd_axis(),
                  pun.Vector3(random.uniform(-0.6, 0.6), random.uniform(-0.6, 0.6), random.uniform(-0.6, 0.6)))
                 for _ in range(3)]
        for pid, col, _, _ in rocks:
            await boss_preview(pid, col, "2x2x2_stuffed")
        await play_sfx(333, target, 2.5, 1.0)
        T = random.uniform(1.3, 1.8)
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < T:
            f_ = el / T
            p_ = start + (target - start) * f_
            for pid, _, ax, o_ in rocks:
                await boss_move(pid, p_ + o_, spin_q(ax, el * 9))
            await fx_add(p_ + pun.Vector3(random.uniform(-0.5, 0.5), random.uniform(-0.5, 0.5), random.uniform(-0.5, 0.5)),
                         random.choice(["orange_emissive", "red_emissive", "gray", "yellow_emissive"]),
                         vel=pun.Vector3(random.uniform(-0.8, 0.8), random.uniform(0.2, 1.2), random.uniform(-0.8, 0.8)),
                         life=0.5, spin=10.0)
            await asyncio.sleep(0.08)
        for pid, _, _, _ in rocks:
            await boss_gone(pid)
        await play_sfx(97, target, 2.5, 0.9)
        asyncio.get_running_loop().create_task(explode(target, land=False, power=6.0, radius=5.0))
        for j in range(10):                                          # crater ring + flying embers
            a_ = j * 2 * math.pi / 10
            await fx_add(target + pun.Vector3(0, 0.2, 0), random.choice(["orange_emissive", "red_emissive", "gray_dark"]),
                         vel=pun.Vector3(math.cos(a_) * 6, random.uniform(2, 5), math.sin(a_) * 6), gravity=10.0,
                         life=0.8, spin=12.0)

    async def meteor_shower(near, n=14, secs=12.0):
        g_say(f"meteors: incoming! ({n} meteors)")
        for i in range(n):
            pose = bot.pose_of(near) if near is not None else None
            targets = [bot.pose_of(a_)[0] for a_ in player_actors()]
            if pose and (random.random() < 0.6 or not targets):
                b_ = pose[0]
                tgt = b_ + pun.Vector3(random.uniform(-12, 12), 0.0, random.uniform(-12, 12))
            elif targets:
                tgt = random.choice(targets)
            else:
                break
            feet = pun.Vector3(tgt.x, tgt.y - 0.45, tgt.z)
            asyncio.get_running_loop().create_task(meteor(feet))
            await asyncio.sleep(secs / n * random.uniform(0.5, 1.5))

    async def spawn_blocks(pattern, color, size, at):
        t = bot.actor_by_name(at)
        pose = bot.pose_of(t) if t is not None else None
        if not pose:
            print(f"don't know where {at} is yet"); return
        pos, rot = pose
        gx, gy, gz = bot.grid_of(pos + q_forward(rot) * 2.0)
        w, h, dpt = size
        n = skipped = 0
        DX, DY, DZ = bot.room_dims
        for j in range(h):
            for i in range(w):
                for k in range(dpt):
                    cx, cy, cz = gx + i - w // 2, gy + j, gz + k
                    if not (0 <= cx < DX and 0 <= cy < DY and 0 <= cz < DZ):
                        skipped += 1
                        continue
                    await bot.rpc(bot.view_id, "RpcMapObjectCreated",
                                  [pattern, cx, cy, cz, 1, 2, color, account, False],
                                  receivers=pun.RECV_OTHERS)
                    n += 1
                    await asyncio.sleep(0.03)
        print(f"spawned {n} x {pattern} ({color}) at grid {gx},{gy},{gz}"
              + (f" - {skipped} skipped: outside the {DX}x{DY}x{DZ} build area, move further in" if skipped else ""))

    # --- SURVIVAL WAVES ---------------------------------------------------------------
    # Defend the glowing crystal in the middle of the room from waves of real game mobs
    # (RpcCreateMob, run by the players' games). Every bug alive slowly drains the
    # crystal and bugs that reach it blow up on it. The wave number is shown in big
    # blocks on the wall, the crystal's health as a green/black ring above it, and
    # everyone gets the finger gun. The crystal heals a bit between waves; when it
    # breaks the game restarts from wave 1 after 15 s.
    wv = {"on": False}
    DIGITS = {"0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
              "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
              "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
              "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
              "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"]}
    WV_KINDS = [(1, "insectExploder"), (1, "insectExploder"), (1, "spittingSnail"), (2, "insectSoldier"),
                (3, "insectSoldier"), (3, "spittingSnail"), (4, "insectTank"), (6, "insectTank")]
    CRYSTAL = ([(0.0, 1.0, 0.0), (0.0, -1.0, 0.0)] +
               [(math.cos(a_) * 0.38, 0.5, math.sin(a_) * 0.38) for a_ in (0, 1.571, 3.142, 4.712)] +
               [(math.cos(a_) * 0.38, -0.5, math.sin(a_) * 0.38) for a_ in (0.785, 2.356, 3.927, 5.498)] +
               [(math.cos(a_) * 0.6, 0.0, math.sin(a_) * 0.6) for a_ in (0, 1.047, 2.094, 3.142, 4.189, 5.236)])

    def wv_wall(opposite=False):
        """2 digits of 1 m blocks on the wall the player faces (or the one behind them), 3 m up"""
        DX, DY, DZ = bot.room_dims
        t = bot.actor_by_name(who) or next(iter(player_actors()), None)
        axis, s = facing(t) if t is not None and bot.pose_of(t) else ("z", 1)
        if opposite:
            s = -s
        lim = DZ if axis == "z" else DX
        plane = (lim - 1 - 4) if s > 0 else 3
        return make_layout(axis, s, (DX if axis == "z" else DZ) // 2, 6, plane, (2, 7, 5, 0))

    async def wv_number(n, colour):
        lay = wv["wall"]
        txt = str(min(n, 99)).rjust(2)
        want = {}
        for i, ch in enumerate(txt):
            for r, row in enumerate(DIGITS.get(ch, ["000"] * 5)):
                for c, bit in enumerate(row):
                    if bit == "1":
                        want[lay["pix"][(i * 4 + c, r)]] = colour
        for cell, col in list(wv["shown"].items()):
            if want.get(cell) != col:
                await take(cell, lay["pattern"]); del wv["shown"][cell]
        for cell, col in want.items():
            if cell not in wv["shown"] and cell not in live.cells:
                await put(cell, col, lay["pattern"]); wv["shown"][cell] = col

    async def wv_spawn(kind):
        players = player_actors()
        if not players:
            return
        DX, DY, DZ = bot.room_dims
        owner = (bot.actors.get(random.choice(players)) or {}).get(253)
        R = max(5.0, min(DX, DZ) * 0.25 - 3.0)
        ang = random.uniform(0, 2 * math.pi)
        x, z = math.cos(ang) * R, math.sin(ang) * R
        pos = pun.Vector3(x, ground_y(x, z) + 0.3, z)
        mid = rand_id()
        await bot.rpc(bot.view_id, "RpcCreateMob",
                      [mid, kind, -1, -1, -1, pos, yaw_q(ang + math.pi / 2 * 3), pun.Vector3(), pun.Vector3(),
                       owner, 0, False], receivers=pun.RECV_OTHERS)
        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [pos, pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)
        wv["mobs"][mid] = {"t": time.monotonic(), "pos": pos, "hit": None}

    def on_wv_mob(code, sender, data):
        """track the wave's bugs: where they hop, who hit them last, when they die"""
        if code != pun.EV_RPC or not wv.get("on"):
            return
        r = pun.decode_rpc(data)
        if not r or not r["args"] or not isinstance(r["args"][0], str) or r["args"][0] not in wv["mobs"]:
            return
        m_, a_ = r["method"], r["args"]
        mob = wv["mobs"][a_[0]]
        if m_ == "RpcDoMobHop" and len(a_) > 1 and isinstance(a_[1], pun.Vector3):
            mob["pos"] = a_[1]
        elif m_ == "RpcApplyMobKnockbackFromPlayerOrigin" and len(a_) > 6 and isinstance(a_[6], str):
            if not a_[6].startswith("o_99"):
                mob["hit"] = a_[6]
            else:                                   # our blast = someone's finger gun shot
                p = a_[1] if isinstance(a_[1], pun.Vector3) else mob["pos"]
                now = time.monotonic()
                best = min(((math.hypot(p.x - b_[1].x, p.z - b_[1].z), b_) for b_ in fx_booms
                            if now - b_[0] < 1.5 and b_[2]), default=None, key=lambda x: x[0]) if p else None
                if best and best[0] < 7:
                    mob["hit"] = f"{best[1][2]}$VR${best[1][3]}"
        elif m_ == "RpcApplyMobStatusEffectFromPlayerOrigin" and len(a_) > 3 and isinstance(a_[3], str):
            if not a_[3].startswith("o_99"):
                mob["hit"] = a_[3]
        elif m_ == "RpcDestroyMob":
            wv["mobs"].pop(a_[0], None)
            if mob["hit"]:
                ac = mob["hit"].split("$")[0]
                wv["kills"][ac] = wv["kills"].get(ac, 0) + 1
                wv["names"][ac] = mob["hit"].split("$")[-1]

    bot.handlers.append(on_wv_mob)

    async def wv_start():
        if wv.get("on"):
            await wv_stop()
        now = time.monotonic()
        wv.update(on=True, wave=0, hp=100.0, mobs={}, kills={}, names={}, shown={}, gave=[], to_spawn=0,
                  next_spawn=now, next_wave=now + 4, broken=False, restart=0.0, last_drain=now,
                  crystal=[rand_id() for _ in CRYSTAL], ring=[rand_id() for _ in range(10)],
                  ring_bg=[rand_id() for _ in range(10)], ring_lit=None, tick=0)
        ensure_avatar(bot.actor_by_name(who) or next(iter(player_actors()), None))
        wv["c"] = pun.Vector3(0.0, ground_y(0.0, 0.0) + 1.4, 0.0)
        wv["wall"] = wv_wall()
        for k, pid in enumerate(wv["crystal"]):
            await boss_preview(pid, ("cyan_emissive", "blue_light", "white")[k % 3])
        for pid in wv["ring"]:
            await boss_preview(pid, "green_emissive")
        for pid in wv["ring_bg"]:
            await boss_preview(pid, "black")
        wv["task"] = asyncio.create_task(wv_loop())
        g_say("waves: SURVIVAL - defend the crystal in the middle! Everyone has the finger gun. Wave 1 in 4 s")

    async def wv_clear_mobs():
        for mid in list(wv.get("mobs", {})):
            await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
        wv["mobs"] = {}

    async def wv_stop():
        wv["on"] = False
        t_ = wv.get("task")
        if t_ and t_ is not asyncio.current_task():
            t_.cancel()
        await wv_clear_mobs()
        for pid in wv.get("crystal", []) + wv.get("ring", []) + wv.get("ring_bg", []):
            await boss_gone(pid)
        lay = wv.get("wall")
        for cell in list(wv.get("shown", {})):
            await take(cell, lay["pattern"])
        for ac in wv.get("gave", []):
            fg["accts"].pop(ac, None)
        wv.update(crystal=[], ring=[], ring_bg=[], shown={}, gave=[])

    async def wv_draw(now):
        c = wv["c"]
        spin = now * 0.8
        q_spin = yaw_q(spin)
        qt = (q_spin.x, q_spin.y, q_spin.z, q_spin.w)
        bob = pun.Vector3(0, 0.15 * math.sin(now * 1.5), 0)
        for pid, off in zip(wv["crystal"], CRYSTAL):
            await boss_move(pid, c + bob + q_rotate(qt, off), spin_q([0, 1, 0], spin * 2))
        lit = math.ceil(10 * max(wv["hp"], 0) / 100)
        if lit != wv["ring_lit"] or wv["tick"] % 20 == 0:
            wv["ring_lit"] = lit
            hide = pun.Vector3(0, -200, 0)
            for k, (g_, b_) in enumerate(zip(wv["ring"], wv["ring_bg"])):
                a_ = 2 * math.pi * k / 10
                pos = c + pun.Vector3(math.cos(a_) * 1.3, 1.6, math.sin(a_) * 1.3)
                rot = yaw_q(-a_)
                await boss_move(g_, pos if k < lit else pos + hide, rot)
                await boss_move(b_, pos + hide if k < lit else pos, rot)

    async def wv_loop():
        try:
            while wv.get("on"):
                try:
                    await asyncio.sleep(0.25)
                    now = time.monotonic()
                    wv["tick"] += 1
                    players = player_actors()
                    for a_ in players:                       # finger gun for everyone, also late joiners
                        ac = acct_of(a_)
                        if ac and ac not in fg["accts"]:
                            fg["accts"][ac] = fg_state(); wv["gave"].append(ac)
                    if wv["broken"]:
                        if now >= wv["restart"]:
                            await wv_stop(); await wv_start(); return
                        continue
                    await wv_draw(now)
                    # wave cleared -> 6 s break -> next wave
                    if wv["to_spawn"] == 0 and not wv["mobs"] and wv["wave"] > 0 and not wv.get("brk"):
                        wv["brk"] = True
                        wv["next_wave"] = now + 6
                        g_say(f"waves: wave {wv['wave']} cleared! next one in 6 s")
                    if wv["to_spawn"] == 0 and not wv["mobs"] and now >= wv["next_wave"]:
                        wv["brk"] = False
                        if wv["wave"] > 0:
                            wv["hp"] = min(100.0, wv["hp"] + 15)
                        wv["wave"] += 1
                        n = wv["wave"]
                        wv["to_spawn"] = 4 + 3 * n
                        wv["next_spawn"] = now
                        await wv_number(n, "yellow_emissive")
                        g_say(f"waves: WAVE {n} - {wv['to_spawn']} bugs incoming")
                    # spawn this wave's bugs bit by bit
                    if wv["to_spawn"] > 0 and players and now >= wv["next_spawn"] and len(wv["mobs"]) < 24:
                        n = wv["wave"]
                        await wv_spawn(random.choice([k for w_, k in WV_KINDS if w_ <= n]))
                        wv["to_spawn"] -= 1
                        wv["next_spawn"] = now + max(0.4, 1.6 - 0.12 * n)
                    # bugs that reach the crystal blow up on it; every bug alive drains it
                    c = wv["c"]
                    for mid, mob in list(wv["mobs"].items()):
                        p = mob["pos"]
                        if now - mob["t"] > 150:             # lost track of it
                            await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
                            wv["mobs"].pop(mid, None); continue
                        if p is not None and math.hypot(p.x - c.x, p.z - c.z) < 2.5:
                            wv["mobs"].pop(mid, None)
                            wv["hp"] -= 8
                            await mob_blast(p + pun.Vector3(0, 0.3, 0), 2.5)
                            await bot.rpc(bot.view_id, "RpcDestroyMob", [mid, 2], receivers=pun.RECV_OTHERS)
                            await bot.rpc(bot.view_id, "RpcCreateHitEffect", [c, pun.Vector3(0, 1, 0), 0],
                                          receivers=pun.RECV_OTHERS)
                    if now - wv["last_drain"] >= 1.0:
                        wv["last_drain"] = now
                        wv["hp"] -= 0.12 * len(wv["mobs"])
                    if wv["hp"] <= 0:
                        await wv_broken()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"waves tick error: {e!r}")
        except asyncio.CancelledError:
            pass

    async def wv_broken():
        wv["broken"] = True
        wv["hp"] = 0
        c = wv["c"]
        g_say(f"waves: THE CRYSTAL BROKE on wave {wv['wave']}!")
        await wv_clear_mobs()
        for pid in wv["crystal"]:
            await boss_gone(pid)
        for _ in range(5):
            await explode(c + pun.Vector3(random.uniform(-1, 1), random.uniform(-0.5, 1), random.uniform(-1, 1)),
                          land=False, power=0.0, radius=0.1)
            await asyncio.sleep(0.2)
        await wv_number(wv["wave"], "red_emissive")
        ranking = sorted(wv["kills"].items(), key=lambda kv: -kv[1])[:5]
        g_say("waves: kills - " + (", ".join(f"{clean_name(wv['names'].get(ac, ac))}: {n}" for ac, n in ranking)
                                   or "nobody?") + " - new game in 15 s")
        wv["restart"] = time.monotonic() + 15

    # --- HOT POTATO -----------------------------------------------------------------
    # The outline of a bomb pillow (1x1_basicBomb) sticks to one player's right hand (a
    # red light spins over their head). Touch another player to pass it on. It blows up at a random moment: the
    # holder gets blasted and is out. Last player left wins; then everyone is back in.
    hpot = {"on": False}
    mapgen = {"on": False}                            # `map`: hills on their own, for any game

    def any_terrain():
        return any(st.get("terrain") for st in (koth, boss, hpot, mapgen))

    def hand_world(actor, off=14):
        vd = bot.poses.get(actor)
        if not vd or len(vd) < 3 + off + 7:
            return None
        v = vd[3:]
        try:
            body_p = pun.Vector3(float(v[0]), float(v[1]), float(v[2]))
            return body_p + q_rotate(tuple(float(x) for x in v[3:7]), [float(x) for x in v[off:off + 3]])
        except (TypeError, ValueError):
            return None

    async def hpot_start():
        if hpot.get("on"):
            await hpot_stop()
        hpot.update(on=True, alive=set(), holder=None, item=None, marker=rand_id(), potato=rand_id(), fuse=0.0,
                    passed=0.0, giver=None, next_round=time.monotonic() + 2, spark=0.0, wins={},
                    walls=[wv_wall(), wv_wall(opposite=True)], shown={}, timer=None)
        ensure_avatar(bot.actor_by_name(who) or next(iter(player_actors()), None))
        if not any_terrain():                            # a hill map to run around on
            await koth_terrain(store=hpot, label="potato")
            if not hpot.get("on"):
                return
            hpot["next_round"] = time.monotonic() + 2
        await boss_preview(hpot["marker"], "red_emissive")
        await boss_preview(hpot["potato"], "red", "1x1_basicBomb")      # the bomb pillow outline
        await boss_move(hpot["potato"], pun.Vector3(0, -200, 0), IDENT)
        hpot["task"] = asyncio.create_task(hpot_loop())
        g_say("potato: HOT POTATO - a bomb pillow sticks to someone's hand, touch another player to pass it on!")

    async def hpot_timer(n):
        """the countdown in big blocks on two opposite walls (None = blank)"""
        colour = "white" if n is None or n > 10 else "yellow_emissive" if n > 5 else "red_emissive"
        if hpot.get("timer") == (n, colour):
            return
        hpot["timer"] = (n, colour)
        want = {}
        if n is not None:
            txt = str(max(0, min(n, 99))).rjust(2)
            for lay in hpot["walls"]:
                for i, ch in enumerate(txt):
                    for r, row in enumerate(DIGITS.get(ch, ["000"] * 5)):
                        for c, bit in enumerate(row):
                            if bit == "1":
                                want[lay["pix"][(i * 4 + c, r)]] = colour
        for cell, col in list(hpot["shown"].items()):
            if want.get(cell) != col:
                await take(cell, "2x2x2_stuffed"); del hpot["shown"][cell]
        for cell, col in want.items():
            if cell not in hpot["shown"] and cell not in live.cells:
                await put(cell, col, "2x2x2_stuffed"); hpot["shown"][cell] = col

    async def hpot_drop_item():
        """between rounds: the bomb outline hides under the map"""
        hpot["item"] = None
        if hpot.get("potato"):
            await boss_move(hpot["potato"], pun.Vector3(0, -200, 0), IDENT)

    async def hpot_stop():
        hpot["on"] = False
        t_ = hpot.get("task")
        if t_ and t_ is not asyncio.current_task():
            t_.cancel()
        for k in ("marker", "potato"):
            if hpot.get(k):
                await boss_gone(hpot[k]); hpot[k] = None
        hpot["holder"] = None
        for cell in list(hpot.get("shown") or {}):
            await take(cell, "2x2x2_stuffed")
        hpot["shown"], hpot["timer"] = {}, None
        await clear_terrain(hpot, "potato")

    async def hpot_give(actor, pos):
        """bomb in a new hand (start of a round)"""
        hpot["holder"] = actor
        hpot["item"] = hpot["potato"]
        await boss_move(hpot["potato"], pos, IDENT)

    async def hpot_loop():
        try:
            while hpot.get("on"):
                try:
                    await asyncio.sleep(0.1)
                    now = time.monotonic()
                    players = [a_ for a_ in player_actors() if acct_of(a_)]
                    in_game = [a_ for a_ in players if acct_of(a_) in hpot["alive"]]
                    h = hpot["holder"]
                    if h is None:
                        # between rounds: everyone present is in again once a round is won
                        if now < hpot["next_round"]:
                            continue
                        if len(in_game) < 2:
                            hpot["alive"] = {acct_of(a_) for a_ in players}
                            in_game = players
                        if len(in_game) < 2:
                            continue                             # need 2 players
                        first = random.choice(in_game)
                        pos = hand_world(first) or bot.pose_of(first)[0]
                        await hpot_give(first, pos)
                        hpot.update(fuse=now + random.uniform(12, 28), passed=now, giver=None)
                        g_say(f"potato: {pname(first)} has the bomb! ({len(in_game)} players left)")
                        continue
                    if h not in bot.actors or not bot.pose_of(h):     # holder left: someone else gets it
                        hpot["alive"].discard(acct_of(h) if h in bot.actors else None)
                        others = [a_ for a_ in in_game if a_ != h]
                        if not others:
                            await hpot_drop_item(); hpot["holder"] = None; continue
                        h = random.choice(others)
                        hpot["holder"] = h
                    hand = hand_world(h) or (bot.pose_of(h)[0] + pun.Vector3(0, 1.0, 0))
                    await boss_move(hpot["potato"], hand, spin_q([0, 1, 0], now * 6))
                    await boss_move(hpot["marker"], bot.pose_of(h)[0] + pun.Vector3(0, 1.6, 0),
                                    spin_q([0, 1, 0], now * 4))
                    # touch someone to pass it
                    if now - hpot["passed"] > 0.8:
                        body_h = bot.pose_of(h)[0]
                        for a_ in in_game:
                            if a_ == h or (a_ == hpot["giver"] and now - hpot["passed"] < 1.6):
                                continue
                            p = bot.pose_of(a_)[0]
                            near_hand = math.hypot(hand.x - p.x, hand.z - p.z) < 0.75 and -0.6 < hand.y - p.y < 1.9
                            near_body = math.hypot(body_h.x - p.x, body_h.z - p.z) < 0.6
                            if near_hand or near_body:
                                hpot.update(holder=a_, giver=h, passed=now)
                                await bot.rpc(bot.view_id, "RpcCreateHitEffect", [p + pun.Vector3(0, 1, 0),
                                              pun.Vector3(0, 1, 0), 0], receivers=pun.RECV_OTHERS)
                                g_say(f"potato: {pname(h)} -> {pname(a_)}")
                                break
                    # sparks + the wall countdown as the fuse runs down
                    left = hpot["fuse"] - now
                    await hpot_timer(math.ceil(max(left, 0)))
                    if left < 6 and now >= hpot["spark"]:
                        hpot["spark"] = now + (0.5 if left > 2.5 else 0.15)
                        await bot.rpc(bot.view_id, "RpcCreateHitEffect", [hand, pun.Vector3(0, 1, 0), 0],
                                      receivers=pun.RECV_OTHERS)
                    if left <= 0:
                        await hpot_boom()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"potato tick error: {e!r}")
        except asyncio.CancelledError:
            pass

    async def hpot_boom():
        h = hpot["holder"]
        pos = hand_world(h) or bot.pose_of(h)[0]
        await hpot_drop_item()
        await explode(pos, land=False, power=0.0, radius=0.1)      # real bomb blast + confetti
        props = bot.actors.get(h) or {}
        if props.get(253):
            a_ = random.uniform(0, 2 * math.pi)
            await bot.rpc(bot.view_id, "RpcApplyPlayerKnockback",
                          [props[253], pun.Vector3(math.cos(a_) * 10, 16.0, math.sin(a_) * 10), 3, None, -1],
                          receivers=pun.RECV_OTHERS)
        hpot["alive"].discard(acct_of(h))
        hpot["holder"] = None
        await boss_move(hpot["marker"], pun.Vector3(0, -200, 0), IDENT)
        await hpot_timer(None)
        left = [a_ for a_ in player_actors() if acct_of(a_) in hpot["alive"]]
        g_say(f"potato: BOOM - {pname(h)} is out!")
        if len(left) == 1:
            w_ = left[0]
            ac = acct_of(w_)
            hpot["wins"][ac] = hpot["wins"].get(ac, 0) + 1
            p = bot.pose_of(w_)[0]
            for _ in range(4):
                await bot.rpc(bot.view_id, "RpcCreateConfettiEffectFromPlayerOrigin",
                              [p + pun.Vector3(0, 2.0, 0), pun.Vector3(random.uniform(-.5, .5), 1, random.uniform(-.5, .5)),
                               pun.Vector3(), TypedArray(ord("s"), random.sample(GLOW, 2)), 1, None, user_id],
                              receivers=pun.RECV_OTHERS)
            g_say(f"potato: {pname(w_)} WINS! (wins: {hpot['wins'][ac]}) - new game in 6 s")
            hpot["alive"] = set()
            hpot["next_round"] = time.monotonic() + 6
        else:
            hpot["next_round"] = time.monotonic() + 3

    # --- talk: the owner talks to the players live from the editor (Photon tab) ---
    # The editor relays the owner's mic (s16le, 48 kHz, mono) to a local socket here;
    # it is encoded to Opus and sent through the room's voice chat from a speaker that
    # stays just in front of the hooked player's face. What the players say comes back
    # decoded (24 kHz) so the owner hears them. Follows the player between rooms.
    TALK_PORT = int(os.environ.get("YEEPS_TALK_PORT", "8791"))
    tk = {"on": False, "voice": None, "server": None, "clients": set(), "enc": None, "decs": {},
          "names": {}, "acts": {}, "buf": bytearray(), "follow": None}

    async def talk_send(ws, msg):
        try:
            await ws.send(msg)
        except Exception:
            tk["clients"].discard(ws)

    def talk_push(obj):
        msg = json.dumps(obj)
        for ws in list(tk["clients"]):
            asyncio.get_running_loop().create_task(talk_send(ws, msg))

    def talk_speakers():
        # actors: voice actor -> game actor, so the View tab can play each voice from that player (3D)
        talk_push({"type": "speakers", "names": {str(k): v for k, v in tk["names"].items()},
                   "actors": {str(k): v for k, v in tk["acts"].items()}})

    def talk_on_info(vactor, info):
        view = int(info.get(10) or 0)
        if vactor not in tk.setdefault("seen_info", set()):
            tk["seen_info"].add(vactor)
            print(f"voice info from voice actor {vactor}: {dict(info)!r}"[:400])
        p_ = bot.actors.get(view // 1000) if view else None
        nm = clean_name(p_.get(255)) if isinstance(p_, dict) else ""
        changed = False
        if view and tk["acts"].get(vactor) != view // 1000:
            tk["acts"][vactor] = view // 1000
            changed = True
        if nm and tk["names"].get(vactor) != nm:
            tk["names"][vactor] = nm
            changed = True
        if changed:
            talk_speakers()

    def talk_on_frame(vactor, vid, pkt):
        if not tk["clients"]:
            return
        from photon.opus_rt import Decoder
        dec = tk["decs"].get((vactor, vid))
        if dec is None:
            dec = tk["decs"][(vactor, vid)] = Decoder(24000)
        pcm = dec.decode(pkt)
        if pcm:
            msg = bytes([1, vactor & 0xFF, (vactor >> 8) & 0xFF]) + pcm
            for ws in list(tk["clients"]):
                asyncio.get_running_loop().create_task(talk_send(ws, msg))

    async def talk_connect():
        """(Re)open the talk voice for the room we're in now."""
        from photon.voice import VoiceSender
        if tk["voice"]:
            try:
                await tk["voice"].close()
            except Exception:
                pass
            tk["voice"] = None
        await bot.spawn_speaker()
        v_ = VoiceSender(cur["room"], bot.actor * 1000 + 2, a.name, voice_id=3)
        v_.on_info, v_.on_frame = talk_on_info, talk_on_frame
        await v_.connect()
        tk["voice"] = v_
        tk["decs"].clear(); tk["names"].clear(); tk["acts"].clear()
        talk_speakers()
        talk_push({"type": "status", "msg": f"in {cur['rk']}, your voice plays at {who}'s face"})

    async def talk_mic(chunk):
        v_, enc, buf = tk["voice"], tk["enc"], tk["buf"]
        if not v_ or not enc:
            return
        buf.extend(chunk)
        if len(buf) > 1920 * 25:                       # over 0.5 s behind: drop the oldest
            del buf[:len(buf) - 1920 * 5]
        while len(buf) >= 1920:
            frame = bytes(buf[:1920]); del buf[:1920]
            try:
                await v_.send_frame(enc.encode(frame))
            except Exception as e:
                print(f"talk: send failed ({e!r})")
                return

    async def talk_client(ws):
        tk["clients"].add(ws)
        talk_speakers()
        await talk_send(ws, json.dumps({"type": "status", "msg": f"in {cur['rk']}, your voice plays at {who}'s face"}))
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    await talk_mic(msg)
                else:
                    try:
                        m_ = json.loads(msg)
                        if m_.get("type") == "cam" and len(m_.get("g") or []) == 3 and len(m_.get("d") or []) == 3:
                            if not state.get("fly") or time.monotonic() - state["fly"]["t"] > FLY_STALE:
                                print(f"view: flying with the camera from grid {m_['g']}")
                            state["fly"] = {"t": time.monotonic(), "g": [float(c) for c in m_["g"]],
                                            "d": [float(c) for c in m_["d"]]}
                        elif m_.get("type") == "cam_off":
                            state["fly"] = None
                    except (ValueError, TypeError, AttributeError):
                        pass
        except Exception:
            pass
        finally:
            tk["clients"].discard(ws)
            tk["buf"].clear()

    async def talk_follow():
        while tk["on"]:
            await asyncio.sleep(0.1)
            if tk["voice"] is None and time.monotonic() > tk.get("retry_at", 0):
                tk["retry_at"] = time.monotonic() + 30
                try:
                    await talk_connect()
                    print("talk: voice connected")
                except Exception as e:
                    print(f"talk: voice still not connected ({e!r})")
            fw = fly_world()
            if fw:
                state["speaker"] = fw[0]
                continue
            lv = state.get("last")
            if look_acct and lv and len(lv) >= 10 and not state.get("hidden"):   # the View app: from our own head
                q_ = tuple(float(c) for c in lv[3:7])
                state["speaker"] = pun.Vector3(lv[0], lv[1], lv[2]) + q_rotate(q_, [float(c) for c in lv[7:10]])
                continue
            t0 = bot.actor_by_name(who)
            po = bot.pose_of(t0) if t0 is not None else None
            if po:
                p_, rot = po
                f = q_forward(rot)
                state["speaker"] = pun.Vector3(p_.x + f.x * 0.6, p_.y + 0.4, p_.z + f.z * 0.6)

    async def talk_start():
        import websockets
        from photon.opus_rt import Encoder
        tk["enc"] = Encoder(32000)
        # the socket first: the View tab's camera (bot flies with it) works even when
        # the voice side is full; the voice is retried every 30 s from talk_follow
        if tk["server"] is None:
            tk["server"] = await websockets.serve(talk_client, "127.0.0.1", TALK_PORT, max_size=1 << 20)
        tk["on"] = True
        if tk["follow"] is None:
            tk["follow"] = asyncio.get_running_loop().create_task(talk_follow())
        try:
            await talk_connect()
        except Exception as e:
            tk["retry_at"] = time.monotonic() + 30
            full = "Max CCU" in repr(e)
            talk_push({"type": "status", "msg": "voice is full (20 max) - retrying every 30 s" if full
                       else f"voice failed ({e}) - retrying every 30 s"})
            print(f"talk: voice not connected ({e!r}) - the camera link still works, retrying in 30 s")
            return
        print(f"talk on - the editor's Talk panel is live; your voice plays at {who}'s face")

    async def talk_stop():
        tk["on"] = False
        if tk["follow"]:
            tk["follow"].cancel(); tk["follow"] = None
        for ws in list(tk["clients"]):
            try:
                await ws.close()
            except Exception:
                pass
        tk["clients"].clear()
        if tk["server"]:
            tk["server"].close()
            try:
                await tk["server"].wait_closed()
            except Exception:
                pass
            tk["server"] = None
        if tk["voice"]:
            try:
                await tk["voice"].close()
            except Exception:
                pass
            tk["voice"] = None
        state["speaker"] = None

    async def follow_rooms():
        """Every 5 s: if the target moved rooms (or our connection dropped), follow."""
        while True:
            await asyncio.sleep(5)
            try:
                if cur.get("rejoining"):
                    continue
                info = await asyncio.to_thread(find_player_soft, a.target)
                dropped = bot.game is None or bot.game.closed.is_set()
                if getattr(a, "stay", False) or state["copy"]:   # --stay / mimicking: keep this room
                    if not dropped:
                        continue
                    info = (None, None, cur["room"], cur["rk"])
                if not info or (info[2] == cur["room"] and not dropped):
                    continue
                _aid, _who, new_room, new_rk = info
                print(f"\n{who} is now in {new_rk} - following" if new_room != cur["room"] else "\nreconnecting")
                # games come along: clean up here, rebuild in the new room
                carry = {}
                if not dropped:
                    if koth.get("on"):
                        carry["koth"] = koth.get("round_s", 180)
                        await koth_stop()
                    if mapgen.get("terrain"):
                        carry["map"] = True
                        await clear_terrain(mapgen, "map")
                    if wv.get("on"):
                        carry["waves"] = True
                        await wv_stop()
                    if hpot.get("on"):
                        carry["potato"] = True
                        await hpot_stop()
                    if boss.get("on"):
                        carry["boss"] = boss.get("max", 1500)
                        carry["boss_paused"] = bool(boss.get("paused"))
                        await boss_stop()
                try:
                    await bot.leave()
                except Exception:
                    pass
                bot.actors.clear(); bot.poses.clear(); bot.last_map_sync = None
                live.reset()
                rain["falling"].clear(); fx["objs"].clear(); fx["shots"] = 0; ol["id"] = None
                bot.room_dims = await asyncio.to_thread(room_dims_for, new_rk)
                await bot.join(new_room)
                cur.update(room=new_room, rk=new_rk)
                if look_acct:
                    await apply_owner_look()
                else:
                    await apply_look(a.look or who)
                if not state.get("hidden"):
                    await bot.spawn_messenger()
                if tk["on"]:
                    try:
                        await talk_connect()
                    except Exception as e:
                        print(f"talk: reconnect failed ({e!r})")
                state["last"] = None
                reresolve()
                asyncio.create_task(hide_soon())

                async def _map_fallback(rk_now):
                    # the new room's live map sync can't always be read: use the saved copy
                    await asyncio.sleep(6)
                    if not live.loaded and cur.get("rk") == rk_now:
                        await asyncio.to_thread(live.load_db, rk_now)
                asyncio.create_task(_map_fallback(new_rk))
                if carry:
                    await asyncio.sleep(3)                 # let the room's map sync + poses arrive
                    if "koth" in carry:
                        await koth_start(carry["koth"])
                    if "map" in carry:
                        mapgen["on"] = True
                        await koth_terrain(store=mapgen, label="map")
                    if "waves" in carry:
                        await wv_start()
                    if "potato" in carry:
                        await hpot_start()
                    if "boss" in carry:
                        await boss_start(carry["boss"])
                        if carry.get("boss_paused"):
                            await boss_pause(True)
                print("bot> ", end="", flush=True)
            except Exception as e:
                print(f"(follow/reconnect failed: {e!r} - will retry)")

    follow_task = asyncio.create_task(follow_rooms())

    # --- the editor's View tab: a live snapshot of the room for its 3D view ---
    # Every 0.25 s: players (grid units, facing) -> view.json; the block list ->
    # view_blocks.json only when the map changed. Written atomically into
    # YEEPS_VIEW_DIR (set by the editor's bot launcher; off when unset).
    VIEW_DIR = os.environ.get("YEEPS_VIEW_DIR", "")
    VIEW_SID = f"{os.getpid()}-{int(time.time())}"     # tells the page a restarted bot apart

    def view_write(fn, obj):
        tmp = os.path.join(VIEW_DIR, fn + ".tmp")
        with open(tmp, "w") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, os.path.join(VIEW_DIR, fn))

    async def view_loop():
        last_rev, last_blocks = None, 0.0
        while True:
            await asyncio.sleep(0.25)
            try:
                DX, DY, DZ = bot.room_dims
                players = []
                for act, pr in list(bot.actors.items()):
                    pose = bot.pose_of(act) if act != bot.actor else None
                    if not pose:
                        continue
                    p, rot = pose
                    f = q_forward(rot)
                    pid = str((pr or {}).get(253) or "").split("$")[0]
                    pl = {"a": act, "n": clean_name((pr or {}).get(255)), "id": pid,
                          "x": round(p.x * 2 + DX / 2, 3), "y": round(p.y * 2, 3),
                          "z": round(p.z * 2 + DZ / 2, 3), "yaw": round(math.atan2(f.x, f.z), 3),
                          "q": [round(float(c), 4) for c in rot], "t": pid == a.target}
                    # VR tracking, body-local metres: head pos+rot, right/left hand pos+rot+grips
                    v = (bot.poses.get(act) or [])[3:]
                    try:
                        if len(v) >= 34:
                            pl["h"] = [round(float(c), 4) for c in v[7:14]]
                            pl["r"] = [round(float(c), 4) for c in v[14:24]]
                            pl["l"] = [round(float(c), 4) for c in v[24:34]]
                    except (TypeError, ValueError):
                        pass
                    players.append(pl)
                # the bot itself (the View draws it as "you" + a small picture of its pose)
                me_v = state.get("last")
                if me_v and len(me_v) >= 34:
                    try:
                        fq = tuple(float(c) for c in me_v[3:7]); f = q_forward(fq)
                        players.append({"a": bot.actor, "n": "You (bot)", "id": account, "me": True,
                                        "hidden": bool(state.get("hidden")),
                                        "x": round(me_v[0] * 2 + DX / 2, 3), "y": round(me_v[1] * 2, 3),
                                        "z": round(me_v[2] * 2 + DZ / 2, 3), "yaw": round(math.atan2(f.x, f.z), 3),
                                        "q": [round(c, 4) for c in fq], "t": False,
                                        "h": [round(float(c), 4) for c in me_v[7:14]],
                                        "r": [round(float(c), 4) for c in me_v[14:24]],
                                        "l": [round(float(c), 4) for c in me_v[24:34]]})
                    except (TypeError, ValueError):
                        pass
                now = time.time()
                view_write("view.json", {"t": now, "sid": VIEW_SID, "room": cur.get("room"), "rk": cur.get("rk"),
                                         "dims": [DX, DY, DZ], "rev": live.rev, "loaded": live.loaded,
                                         "target": a.target, "targetName": who, "players": players})
                if live.rev != last_rev and now - last_blocks >= 0.5:
                    blocks = [[nm, x, y, z, *live.rot.get((nm, x, y, z), (4, 2)), col]
                              for (nm, x, y, z), col in list(live.colors.items())]
                    view_write("view_blocks.json", {"rev": live.rev, "sid": VIEW_SID, "rk": cur.get("rk"),
                                                    "blocks": blocks})
                    last_rev, last_blocks = live.rev, now
            except Exception as e:
                print(f"(view snapshot failed: {e!r})")
                await asyncio.sleep(5)

    if VIEW_DIR:
        view_task = asyncio.create_task(view_loop())

    # Avatar colours/cosmetics come from the ACCOUNT on the server (skinColor,
    # eyeColor, activeCosmetics), which other games load when a player joins - not
    # from Photon properties. So copying someone's look = copy those fields onto
    # the bot's account, then leave + rejoin the room so everyone reloads it.
    LOOK_FIELDS = ("skinColor", "eyeColor", "activeCosmetics")
    look_saved = {}

    def _db_copy_look(src_acct):
        sc["ready"] = False                       # the scare outfit is replaced
        d = db()
        src = d.accounts.find_one({"accountID": src_acct}, {"_id": 0, **{k: 1 for k in LOOK_FIELDS}}) or {}
        if not look_saved:
            look_saved.update(d.accounts.find_one({"accountID": account},
                                                  {"_id": 0, **{k: 1 for k in LOOK_FIELDS}}) or {"_none": 1})
        upd = {k: src[k] for k in LOOK_FIELDS if k in src}
        if isinstance(upd.get("activeCosmetics"), list):        # not their staff badges
            upd["activeCosmetics"] = [c for c in upd["activeCosmetics"] if not str(c).startswith("roleBadge")]
        if upd:
            d.accounts.update_one({"accountID": account}, {"$set": upd})
        return bool(upd)

    def _db_set_look(fields):
        d = db()
        if not look_saved:
            look_saved.update(d.accounts.find_one({"accountID": account},
                                                  {"_id": 0, **{k: 1 for k in LOOK_FIELDS}}) or {"_none": 1})
        d.accounts.update_one({"accountID": account}, {"$set": dict(fields)})

    def _db_restore_look():
        sc["ready"] = False
        if look_saved and "_none" not in look_saved:
            db().accounts.update_one({"accountID": account}, {"$set": dict(look_saved)})
        look_saved.clear()

    async def rejoin_same_room(look_name=None, props=None):
        """Leave and rejoin our room (others load our account look fresh)."""
        cur["rejoining"] = True
        try:
            try:
                await bot.leave()
            except Exception:
                pass
            bot.actors.clear(); bot.poses.clear(); bot.last_map_sync = None
            live.reset()
            await bot.join(cur["room"])
            if look_name:
                await apply_look(look_name)
            if props:
                await bot.set_props(props)
            if not state.get("hidden"):
                await bot.spawn_messenger()
            state["last"] = None
            reresolve()
            if tk["on"]:
                try:
                    await talk_connect()
                except Exception as e:
                    print(f"talk: reconnect failed ({e!r})")
        finally:
            cur["rejoining"] = False

    COLOR_SET = set(COLORS) | set(GLOW) | {"white"}

    def is_num(x):
        try:
            float(x); return True
        except ValueError:
            return False

    def hooked(args=()):
        """Who a 'you' command (screen, apple, games...) is for: a player NAMED in args, else
        the hooked player, else (they left / the bot stays in the room) the player nearest
        the View camera or the bot. Returns (actor or None, args without the name)."""
        args = list(args)
        if args:
            t, rest = split_name(args)
            if t is not None and len(rest) < len(args):          # a name was given
                return t, rest
        t = bot.actor_by_name(who)
        if t is not None and bot.pose_of(t):
            return t, args
        fw = fly_world()
        ref = fw[0] if fw else (pun.Vector3(*state["last"][0:3]) if state.get("last") else None)
        best, bd = None, 1e18
        for act in player_actors():
            p_, _r = bot.pose_of(act)
            d = 0.0 if ref is None else (p_.x - ref.x) ** 2 + (p_.y - ref.y) ** 2 + (p_.z - ref.z) ** 2
            if d < bd:
                best, bd = act, d
        return best, args

    def split_name(args):
        """(actor, other args): the longest run of trailing words that names a
        player in the room; colours/numbers/sizes/'off' are never names.
        No name -> you."""
        if "at" in args:                                   # old style: ... at NAME
            i = args.index("at")
            return bot.actor_by_name(" ".join(args[i + 1:])), args[:i]
        for k in range(len(args), 0, -1):
            cand = " ".join(args[-k:])
            if cand.lower() in COLOR_SET or is_num(cand) or cand == "off" or \
                    (cand.count("x") >= 1 and cand.replace("x", "").replace("_", "").isalnum() and "_" in cand):
                continue
            t = bot.actor_by_name(cand)
            if t is not None:
                return t, args[:-k]
        return bot.actor_by_name(who), args

    def pname(t):
        return clean_name((bot.actors.get(t) or {}).get(255)) if t is not None else "?"

    def quiet_errors(loop, ctx):
        e = ctx.get("exception")
        where = getattr(ctx.get("future"), "get_coro", lambda: None)()
        name = getattr(where, "__name__", "") if where else ""
        print(f"\n(error in {name or 'background task'}: {e!r})" if e else f"\n({ctx.get('message')})")

    asyncio.get_running_loop().set_exception_handler(quiet_errors)
    # Read the keyboard on a daemon thread (asyncio.to_thread(input) kept the
    # process alive for up to 5 minutes after Ctrl+C / quit).
    import threading
    lines_q: asyncio.Queue = asyncio.Queue()
    loop_ = asyncio.get_running_loop()

    def reader():
        for raw in sys.stdin:
            loop_.call_soon_threadsafe(lines_q.put_nowait, raw)
        loop_.call_soon_threadsafe(lines_q.put_nowait, None)      # EOF

    threading.Thread(target=reader, daemon=True).start()
    print(MENU)
    try:
        while True:
            print("bot> ", end="", flush=True)
            raw = await lines_q.get()
            if raw is None:
                break
            line = raw.strip()
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            args = rest.split()
            off = bool(args) and args[0].lower() == "off"
            try:
                if cmd in ("help", "menu", "?"):
                    print(MENU)
                elif cmd == "watch":
                    if not args:
                        cur_w = bot.show_rpcs
                        print("printing:", "everything except sounds" if cur_w == "all" else
                              "nothing" if cur_w == "none" else ", ".join(sorted(cur_w)))
                    elif args[0] == "all":
                        bot.show_rpcs = "all"; print("printing every RPC except sound effects")
                    elif off or args[0] == "none":
                        bot.show_rpcs = "none"; print("printing nothing (still logged to file)")
                    else:
                        bot.show_rpcs = set(args); print("printing only:", ", ".join(args))
                elif cmd == "players":
                    for act, pr in sorted(bot.actors.items()):
                        tag = " (bot)" if act == bot.actor else ""
                        has = "" if act in bot.poses or act == bot.actor else " [no pose yet]"
                        print(f"  #{act} {clean_name((pr or {}).get(255))}{tag}{has}")
                elif cmd == "fly":
                    if off:
                        tracked_flight.update(actor=None, enabled=False)
                        tracked_flight["actor_acct"] = None
                        print("tracked-player flight off")
                        continue
                    t = bot.actor_by_name(who)
                    if t is None:
                        print("can't find the tracked player in this room"); continue
                    tracked_flight.update(actor=t, enabled=True, last_boost=0.0)
                    remember(tracked_flight, "actor")
                    print(f"right-trigger flight ON for {pname(t)}")
                elif cmd == "disco":
                    await bot.rpc(bot.view_id, "SendBeginParty", [], receivers=pun.RECV_OTHERS)
                    await bot.rpc(bot.view_id, "SendStartParty", [], receivers=pun.RECV_OTHERS)
                    print("sent Disco party RPCs")
                elif cmd == "vput":                 # editor View tab: vput PATTERN X Y Z COLOR [FWD UP]
                    try:
                        pat, col = args[0], args[4]
                        x, y, z = (int(v) for v in args[1:4])
                        fw = int(args[5]) if len(args) > 5 else 4
                        upv = int(args[6]) if len(args) > 6 else 2
                    except (IndexError, ValueError):
                        print("usage: vput PATTERN X Y Z COLOR [FWD UP]"); continue
                    DX, DY, DZ = bot.room_dims
                    w, h, d = block_ext(pat, fw, upv)
                    cells = [(x + i, y + j, z + k) for i in range(w) for j in range(h) for k in range(d)]
                    if any(not (0 <= c[0] < DX and 0 <= c[1] < DY and 0 <= c[2] < DZ) or c in live.cells
                           for c in cells):
                        print(f"view: can't place {pat} at {x},{y},{z} (taken or outside the room)"); continue
                    await bot.rpc(bot.view_id, "RpcMapObjectCreated", [pat, x, y, z, fw, upv, col, account, False],
                                  receivers=pun.RECV_OTHERS)
                    live.add(pat, x, y, z, owner=account, color=col, fwd=fw, up=upv)
                    print(f"view: placed {pat} ({col}) at {x},{y},{z}")
                elif cmd == "vlimb":                # View app: vlimb head|right|left X Y Z  /  vlimb reset
                    if args and args[0] == "reset":
                        state["limbs"] = {k: list(v) for k, v in LIMB_REST.items()}
                        if state.get("last") and not state.get("hidden"):
                            state["last"] = apply_limbs(list(state["last"]))
                            await bot.send_pose(state["last"])
                        print("view: pose reset (hands behind the back)")
                        continue
                    try:
                        part = args[0]
                        xyz = [max(-1.5, min(1.5, float(x))) for x in args[1:4]]
                        assert part in LIMB_AT and len(xyz) == 3
                    except (IndexError, ValueError, AssertionError):
                        print("usage: vlimb head|right|left X Y Z  (or vlimb reset)"); continue
                    cur_l = state.setdefault("limbs", {}).setdefault(part, list(LIMB_REST[part]))
                    cur_l[0:3] = xyz
                    if state.get("last") and not state.get("hidden"):
                        state["last"] = apply_limbs(list(state["last"]))
                        await bot.send_pose(state["last"])
                elif cmd == "props":                # debug: props NAME -> that player's Photon properties
                    t, _ = split_name(args)
                    pr = bot.actors.get(t if t is not None else bot.actor) or {}
                    for k_, v_ in pr.items():
                        print(f"  {k_!r}: {type(v_).__name__} {v_!r}"[:600])
                elif cmd == "vhide":                # View app: remove the bot's avatar (voice still works)
                    state["hidden"] = True
                    vid = bot.view_id
                    await bot.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_INSTANTIATE), P_DATA: {Byte(7): vid},
                                                         247: Byte(6)})          # RemoveFromRoomCache
                    await bot.game.send(OP_RAISE_EVENT, {P_CODE: Byte(pun.EV_DESTROY), P_DATA: {Byte(0): vid},
                                                         P_RECEIVERS: Byte(pun.RECV_OTHERS)})
                    print("view: avatar hidden")
                elif cmd == "vshow":
                    if state.get("hidden"):
                        state["hidden"] = False
                        await bot.spawn_messenger()
                    print("view: avatar shown")
                elif cmd == "vdims":                # editor View tab: the room's real size (the server reads it like the game)
                    try:
                        d3 = tuple(int(v) for v in args[:3])
                        if len(d3) != 3 or not all(0 < v <= 1024 for v in d3):
                            raise ValueError
                    except ValueError:
                        print("usage: vdims X Y Z"); continue
                    if d3 != tuple(bot.room_dims):
                        bot.room_dims = d3
                        print(f"view: room size is {d3[0]}x{d3[1]}x{d3[2]}")
                elif cmd == "vdel":                 # editor View tab: vdel X Y Z (whatever fills that cell)
                    try:
                        x, y, z = (int(v) for v in args[:3])
                    except ValueError:
                        print("usage: vdel X Y Z"); continue
                    o = live.obj.get((x, y, z))
                    if not o:
                        print(f"view: nothing at {x},{y},{z}"); continue
                    nm, ox, oy, oz = o
                    await bot.rpc(bot.view_id, "RpcDestroyMapObject", [nm, ox, oy, oz, account, 0, False],
                                  receivers=pun.RECV_OTHERS)
                    live.add(nm, ox, oy, oz, sign=-1)
                    print(f"view: removed {nm} at {ox},{oy},{oz}")
                elif cmd == "outline":
                    if off:
                        print("outline removed" if await outline_off() else "no outline showing"); continue
                    t, rest_a = split_name(args)
                    rest_a = [x for x in rest_a if x != "follow"]
                    color = next((x for x in rest_a if x.lower() in COLOR_SET), "white")
                    pattern = next((x for x in rest_a if x.lower() not in COLOR_SET), "1x1_stuffed")
                    await outline_on(pattern, color, True, pname(t))
                elif cmd == "rain":
                    if off:
                        rain["on"] = False
                        print(f"rain off ({rain['placed']} placed; {len(rain['falling'])} still falling)"); continue
                    if args and args[0].lower() == "clean":
                        rain["on"] = False
                        n = len(rain["cells"])
                        for c in rain["cells"]:
                            await bot.rpc(bot.view_id, "RpcDestroyMapObject", ["1x1_stuffed", *c, account, 0, False],
                                          receivers=pun.RECV_OTHERS)
                            live.cells.discard(c)
                            await asyncio.sleep(0.004)
                        rain["cells"] = []
                        print(f"rain cleaned up ({n} blocks removed)"); continue
                    named = any(not is_num(x) for x in args)
                    t, rest_a = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    nums = [float(x) for x in rest_a if is_num(x)]
                    count = int(nums[0]) if nums else -1
                    rain.update(on=True, left=count,
                                rate=min(nums[1], MAX_RATE) if len(nums) > 1 else 15.0,
                                radius=nums[2] if len(nums) > 2 else 8.0, actor=t, placed=0,
                                next=0.0, whole_map=not named)
                    remember(rain, "actor")
                    ensure_avatar(t)
                    where = f"around {pname(t)}" if named else "all over the map"
                    print(f"raining {'until rain off' if count < 0 else count} blocks {where}, "
                          f"{rain['rate']:g} a second"
                          + ("" if live.loaded else " (room map not loaded yet - first ones may land on the floor)"))
                elif cmd == "pelletfx":
                    if off:
                        fx["pellet_actor"] = None; fx["pellet_actor_acct"] = None
                        print("pellet effects off"); continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    ensure_avatar(t)
                    fx["pellet_actor"] = t; remember(fx, "pellet_actor")
                    print(f"pellet effects ON for {pname(t)} - fire a pellet gun")
                elif cmd in ("lightning", "freeze"):
                    # the finger-gun gesture fires lightning / ice instead of a shot
                    if off:
                        rest_n = args[1:]
                        if rest_n:
                            t, _ = split_name(rest_n)
                            if acct_of(t) in fg["accts"] and fg["accts"][acct_of(t)].get("mode") == cmd:
                                fg["accts"].pop(acct_of(t), None)
                            print(f"{cmd} taken from {pname(t)}")
                        else:
                            for k_ in [k for k, v_ in fg["accts"].items() if v_.get("mode") == cmd]:
                                fg["accts"].pop(k_, None)
                            if cmd == "freeze":
                                for ac_ in list(frozen):
                                    await thaw(ac_)
                            print(f"{cmd} off for everyone")
                        continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    ensure_avatar(t)
                    fg["accts"][acct_of(t)] = fg_state(cmd)
                    print(f"{cmd.upper()} hand ON for {pname(t)}: finger gun + pull the trigger")
                elif cmd in ("scare", "scaregun", "jumpscaregun"):
                    if off:
                        for k_ in [k for k, v_ in fg["accts"].items() if v_.get("mode") == "scare"]:
                            fg["accts"].pop(k_, None)
                        await asyncio.to_thread(_db_restore_look)
                        if sc["voice"]:
                            await sc["voice"].close(); sc["voice"] = None
                        print("jump scare gun off")
                        continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    fg["accts"][acct_of(t)] = fg_state("scare")
                    print(f"JUMP SCARE hand ON for {pname(t)}: finger gun at someone + pull the trigger "
                          f"(getting the bot dressed - it rejoins once)")
                    asyncio.get_running_loop().create_task(scare_prepare())
                elif cmd == "jumpscare":
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't find that player"); continue
                    asyncio.get_running_loop().create_task(jumpscare(t))
                elif cmd in ("skel", "spawnskelly", "skeleton1"):
                    nums = [int(x) for x in args if x.isdigit()]
                    sty = [x.lower() for x in args if x.lower() in SKEL_STYLES or x.lower() in ("party", "random")]
                    t, _ = split_name([x for x in args if not x.isdigit() and x.lower() not in SKEL_STYLES
                                       and x.lower() not in ("party", "random", "dance") and x.lower() not in dance_list()])
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    party = "party" in sty
                    style = -1 if (party or "random" in sty) else (SKEL_STYLES[sty[0]] if sty else 0)
                    dl = dance_list()
                    want = next((x.lower() for x in args if x.lower() in dl), "")
                    if "gangnam" in [x.lower() for x in args] and "gangnam_style" in dl:
                        want = "gangnam_style"           # the real Mixamo Gangnam Style beats the hand-made one
                    if "dance" in [x.lower() for x in args] or want:
                        style = 7
                    n = 8 if party and not nums else (min(nums[0], 30) if nums else 1)
                    asyncio.get_running_loop().create_task(skel_spawn(t, n, style=style, dance_slug=want if style == 7 else ""))
                elif cmd in ("skellyblock", "skellyblocks"):
                    if off:
                        skb["actor"] = None; skb["placed"].clear(); print("skelly blocks off"); continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    skb["actor"] = t; skb["placed"].clear()
                    print(f"skelly blocks ON for {pname(t)}: place a block and pick it up -> skeleton party there (creative rooms)")
                elif cmd in ("skelly", "skellies", "skeletons", "doorevent", "door"):
                    evw = [x for x in args if x.lower() in DOOR_EVENTS or x.isdigit()]
                    ev = 0 if cmd != "doorevent" and cmd != "door" else (
                        int(evw[0]) if evw and evw[0].isdigit() else DOOR_EVENTS.get(evw[0].lower(), 0) if evw else 0)
                    ev = max(0, min(5, ev))
                    t, _ = split_name([x for x in args if x not in evw])
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    asyncio.get_running_loop().create_task(door_event(t, ev))
                elif cmd in ("onlyup", "only"):
                    if args and args[0].lower() in ("clear", "off", "remove"):
                        asyncio.get_running_loop().create_task(onlyup_clear()); continue
                    simple = any(x.lower() == "simple" for x in args)
                    t, _ = split_name([x for x in args if x.lower() not in ("up", "simple", "full")])
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    asyncio.get_running_loop().create_task(onlyup_build(t) if simple else onlyup_full(t))
                elif cmd in ("godhand", "hand", "handofgod"):
                    if off:
                        await gh_stop(); print("hand of god off"); continue
                    both = any(x.lower() in ("both", "2", "two") for x in args)
                    t, _ = split_name([x for x in args if x.lower() not in ("both", "2", "two")])
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    await gh_start(t, both)
                elif cmd in ("bigbuild", "giantbuild"):
                    if off:
                        bigb["actor"] = None; bigb["actor_acct"] = None
                        print("giant builder off (still on while the hand of god is)"); continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    bigb["actor"] = t; remember(bigb, "actor")
                    print(f"giant builder ON for {pname(t)}: every 1x1 they place becomes four 2x2x2 blocks")
                elif cmd in ("blackhole", "hole"):
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    ensure_avatar(t)
                    p_, r_ = bot.pose_of(t)
                    f_ = q_forward(r_)
                    fn_ = math.hypot(f_.x, f_.z) or 1.0
                    c_ = p_ + pun.Vector3(f_.x / fn_ * 9, 3.5, f_.z / fn_ * 9)
                    asyncio.get_running_loop().create_task(black_hole(c_))
                elif cmd in ("meteors", "meteor", "meteorshower"):
                    nums = [int(x) for x in args if x.isdigit()]
                    t, _ = split_name([x for x in args if not x.isdigit()])
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    ensure_avatar(t)
                    asyncio.get_running_loop().create_task(meteor_shower(t, nums[0] if nums else 14))
                elif cmd == "house2":
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    asyncio.get_running_loop().create_task(build_house2(t))
                elif cmd == "house":
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't see that player yet"); continue
                    asyncio.get_running_loop().create_task(build_house(t))
                elif cmd in ("smite", "zap"):
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't find that player"); continue
                    ensure_avatar(t)
                    asyncio.get_running_loop().create_task(strike(feet_of(t), t))
                    print(f"lightning strikes {pname(t)}")
                elif cmd in ("ice", "freezeplayer"):
                    t, _ = split_name(args)
                    if t is None or not bot.pose_of(t):
                        print("can't find that player"); continue
                    ensure_avatar(t)
                    asyncio.get_running_loop().create_task(encase(t))
                    print(f"freezing {pname(t)} for 15 s")
                elif cmd == "thaw":
                    for ac_ in list(frozen):
                        await thaw(ac_)
                    print("everyone thawed")
                elif cmd in ("fingergun", "finger"):
                    if off:
                        rest_n = args[1:]
                        if rest_n:                                 # fingergun off NAME
                            t, _ = split_name(rest_n)
                            fg["accts"].pop(acct_of(t), None)
                            print(f"finger gun taken from {pname(t)}")
                        else:
                            fg["accts"].clear(); print("finger gun off for everyone")
                        continue
                    t, _ = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    ensure_avatar(t)
                    fg["accts"][acct_of(t)] = fg_state()
                    names = ", ".join(clean_name((bot.actors.get(x) or {}).get(255)) or a_
                                      for a_ in fg["accts"] for x in [next((k for k, pr in bot.actors.items()
                                      if isinstance(pr, dict) and str(pr.get(253, "")).startswith(a_ + "$")), None)])
                    print(f"finger gun ON for {pname(t)} (has it now: {names})")
                elif cmd in ("coins", "coin", "buttcoin", "buttcoins"):
                    sub = args[0].lower() if args else ""
                    if sub == "off":
                        await ball_clear()
                        co["orbit"] = None; co["orbit_acct"] = None; co["planet"] = None
                        await items_clear(); print("all buttcoins and the planet removed"); continue
                    if sub == "planet":
                        t, _ = split_name(args[1:])
                        ensure_avatar(t if t is not None else bot.actor_by_name(who))
                        DX, DY, DZ = bot.room_dims
                        center = pun.Vector3(0.0, min(16.0, DY * 0.5 - 6), 0.0)
                        print("building the buttcoin planet over the middle of the map (about 5 s)...")
                        await planet_build(center)
                        print(f"spinning buttcoin planet up at {center}: 160 tumbling glowing outlines + 2 buttcoin rings "
                              "- 'coins off' removes everything")
                        continue
                    t, rest_a = split_name(args[1:] if sub == "orbit" else args)
                    if t is None:
                        print("can't find that player"); continue
                    nums = [float(x) for x in rest_a if is_num(x)]
                    ensure_avatar(t)
                    if co["orbit"] != t:
                        await items_clear("orbit")
                    co.update(orbit=t, orbit_n=int(min(nums[0], 8)) if nums else 3,
                              orbit_r=nums[1] if len(nums) > 1 else 1.3)
                    remember(co, "orbit")
                    print(f"{co['orbit_n']} buttcoins orbiting {pname(t)} - 'coins off' to remove")
                elif cmd == "boss":
                    if off:
                        await boss_stop(); print("boss removed"); continue
                    if args and args[0].lower() in ("pause", "stop", "freeze"):
                        await boss_pause(True); continue
                    if args and args[0].lower() in ("resume", "unpause", "go", "play"):
                        await boss_pause(False); continue
                    hp = int(args[0]) if args and args[0].isdigit() else 1500
                    await boss_start(hp)
                elif cmd in ("waves", "wave", "survival"):
                    if off:
                        await wv_stop(); print("waves removed"); continue
                    await wv_start()
                elif cmd in ("map", "mapgen", "terrain"):
                    if off or (args and args[0].lower() in ("clear", "remove", "off")):
                        mapgen["on"] = False
                        await clear_terrain(mapgen, "map"); print("map removed"); continue
                    if mapgen.get("terrain"):
                        await clear_terrain(mapgen, "map")
                    elif any_terrain():
                        print("a game already grew hills here"); continue
                    mapgen["on"] = True
                    await koth_terrain(store=mapgen, label="map")
                    g_say(f"map: ready ({len(mapgen.get('terrain') or [])} blocks) - 'map' again for a new one, 'map off' to remove")
                elif cmd in ("potato", "hotpotato"):
                    if off:
                        await hpot_stop(); print("hot potato removed"); continue
                    await hpot_start()
                elif cmd in ("koth", "hill"):
                    if off:
                        await koth_stop(); print("king of the hill removed"); continue
                    secs = int(float(args[0]) * 60) if args and is_num(args[0]) else 180
                    await koth_start(max(secs, 30))
                elif cmd == "canvas":
                    sub = args[0].lower() if args else ""
                    if off:
                        cv["pick"] = None
                        await canvas_off(); print("canvas removed"); continue
                    if sub == "clear" and cv["lay"]:
                        for cell, col in list(cv["shown"].items()):
                            if col != "white":
                                await take(cell, "1x1_stuffed"); await put(cell, "white", "1x1_stuffed")
                                cv["shown"][cell] = "white"
                        print("canvas wiped"); continue
                    size_name = "big" if "big" in [x.lower() for x in args] else "small"
                    if sub == "at":
                        xyz = tuple(int(float(v)) for v in args[1:4])
                        await canvas_build(xyz, size_name, None)
                        lay = cv["lay"]
                        base = lay["pix"][(lay["w"] // 2, lay["h"] - 1)]
                        bx, by, bz = base
                        spot = (bx, by, bz - lay["s"] * 6) if lay["axis"] == "z" else (bx - lay["s"] * 6, by, bz)
                        await canvas_palette(spot, None); continue
                    cv["pick"] = {"actor": hooked(args)[0], "size": size_name, "stage": "canvas"}
                    print("canvas: place a 1x1 pillow where the BOTTOM-MIDDLE of the canvas should go "
                          "(then a second one for the colour palette)")
                elif cmd in ("tetris", "snake", "pong", "game", "flappy", "flappybird",
                             "minesweeper", "mines", "tictactoe", "ttt", "connectfour", "connect4"):
                    aliases = {"flappybird": "flappy", "mines": "minesweeper",
                               "ttt": "tictactoe", "connect4": "connectfour"}
                    kind = gm["kind"] if cmd == "game" else aliases.get(cmd, cmd)
                    sub = args[0].lower() if args else ""
                    if off or (cmd == "game" and not kind):
                        gm["pick"] = None
                        await g_off(); print("game removed"); continue
                    if sub == "restart":
                        if gm["lay"] and gm["kind"] == kind:
                            g_new_game()
                            if not gm["task"]:
                                gm["task"] = asyncio.create_task(g_loop())
                            print(f"{kind} restarted")
                        else:
                            print(f"no {kind} board - type {kind} first")
                        continue
                    if kind == "pong" and sub == "hand":
                        gm["hand"] = not gm.get("hand")
                        print("pong: your right hand now moves your paddle - raise it to go up"
                              if gm["hand"] else "pong: hand control off (use the buttons)"); continue
                    size_name = "big" if "big" in [x.lower() for x in args] else "small"
                    gm["twop"] = kind == "pong" and any(x.lower() in ("2p", "2", "two") for x in args)
                    if sub == "at":                           # test: board at X Y Z, controls in front
                        xyz = tuple(int(float(v)) for v in args[1:4])
                        await g_build_board(kind, xyz, size_name, None)
                        await g_start(); continue
                    gm["pick"] = {"actor": hooked(args)[0], "kind": kind, "size": size_name, "stage": "board"}
                    print(f"{kind}: place a 1x1 pillow where the BOTTOM-MIDDLE of the board should go "
                          + ("(then one for the LEFT player's controller, then one for the RIGHT player's)"
                             if gm["twop"] else "(then a second one for the controller)"))
                elif cmd in ("controls", "controller"):
                    if not gm["lay"]:
                        print("no game board"); continue
                    meters = float(args[0]) if args and is_num(args[0]) else 6.0
                    await g_move_controls(max(2, int(round(meters * 2))))
                    print(f"controls moved to {meters:g} m in front of the board")
                elif cmd == "t" and args:
                    m = {"l": "left", "left": "left", "r": "right", "right": "right", "rot": "rotate",
                         "rotate": "rotate", "d": "drop", "drop": "drop", "down": "down", "up": "up",
                         "u": "up", "up2": "up2", "u2": "up2", "down2": "down2", "d2": "down2",
                         "mode": "mode", "m": "mode", "u3": "upx3", "d3": "downx3",
                         "u3_2": "upx3_2", "d3_2": "downx3_2"}.get(args[0].lower())
                    if m and gm["lay"]:
                        gm["queue"].append(m)
                    else:
                        print("t left | right | up | down | rot | drop  (needs a game board)")
                elif cmd == "cleanup":
                    # delete every block one account placed in this room (default: the
                    # "firebox" spam bot, which joins Photon anonymously so bans can't stop it)
                    who_arg = args[0] if args else "o_PLAYER0000000005"
                    if not who_arg.startswith("o_"):
                        t_ = bot.actor_by_name(who_arg)
                        uid = str((bot.actors.get(t_) or {}).get(253) or (bot.actors.get(t_) or {}).get("253") or "") if t_ else ""
                        who_arg = uid.split("$", 1)[0] if uid else who_arg
                    mine = [k for k, o in live.owners.items() if o.split("$", 1)[0] == who_arg]
                    if not mine:
                        print(f"no blocks from {who_arg} in this room (that I know of)"); continue
                    print(f"cleanup: deleting {len(mine)} blocks placed by {who_arg}...")

                    async def _cleanup(keys, acct_):
                        n_ = 0
                        for (nm, x, y, z) in keys:
                            await bot.rpc(bot.view_id, "RpcDestroyMapObject", [nm, x, y, z, account, 0, False],
                                          receivers=pun.RECV_OTHERS)
                            live.add(nm, x, y, z, sign=-1)
                            n_ += 1
                            await asyncio.sleep(0.025)          # ~40 a second so nobody stutters
                        print(f"\ncleanup: removed {n_} blocks from {acct_}\nbot> ", end="", flush=True)

                    asyncio.get_running_loop().create_task(_cleanup(mine, who_arg))
                elif cmd in ("applesign", "signapple"):
                    if off:
                        await signs_off()
                        print("applesign off - playback stopped; your sign stays in the room"); continue
                    if asg["on"]:
                        print("applesign is already on"); continue
                    t0, args = hooked(args)
                    if t0 is None:
                        print("no player to do that for"); continue
                    asg["actor"], asg["on"] = t0, True
                    asg["signs"].clear()
                    print("applesign on - the first sign you place will play Bad Apple as text; "
                          "'applesign off' stops playback and keeps your sign.")
                elif cmd == "talk":
                    if off:
                        await talk_stop()
                        print("talk off"); continue
                    if tk["on"]:
                        print("talk is already on"); continue
                    await talk_start()
                elif cmd == "pixelart":
                    if off:
                        pixel_art["pick"] = None
                        await remove_pixel_art()
                        print("pixel art removed"); continue
                    path = os.getenv("YEEPS_PIXEL_ART_IMAGE", "")
                    if not path or not os.path.isfile(path):
                        print("upload an image in the editor's Bot tab first"); continue
                    t0, _ = hooked(args)
                    if t0 is None:
                        print("no player to place the marker for"); continue
                    pixel_art["pick"] = {"actor": t0}
                    print("place a 1x1 pillow where the BOTTOM-MIDDLE of the pixel art should go")
                elif cmd == "soundkey":
                    if off:
                        await soundkey_stop()
                        print("soundkey stopped"); continue
                    if soundkey["task"]:
                        print("soundkey is already playing - use 'soundkey off' first"); continue
                    if ap["task"]:
                        print("stop Bad Apple before starting SoundKey audio"); continue
                    path = os.getenv("YEEPS_SOUNDKEY_AUDIO", "")
                    if not path or not os.path.isfile(path):
                        print("upload audio in the editor's Bot tab first"); continue
                    t0, _ = hooked(args)
                    if t0 is None or not bot.pose_of(t0):
                        print("no player to play SoundKey for"); continue
                    soundkey["stop"] = False
                    soundkey["task"] = asyncio.create_task(soundkey_play(t0))
                elif cmd == "screen":
                    if off:
                        ap["pick"] = None
                        ap["stop"] = True
                        if ap["task"]:
                            ap["task"].cancel(); ap["task"] = None
                        if ap["screen"]:
                            await remove_screen(ap["screen"]); ap["screen"] = None
                        ap["stop"] = False
                        print("screen removed"); continue
                    t0, args = hooked(args)
                    if t0 is None or not bot.pose_of(t0):
                        print("no player to do that for"); continue
                    size = size_for("small" if "small" in [x.lower() for x in args] else "big")
                    lw = [x.lower() for x in args]
                    if "at" in lw:                                # screen [small] at X Y Z: no marker needed
                        xyz = [int(float(v)) for v in args[lw.index("at") + 1:lw.index("at") + 4]]
                        await screen_at(tuple(xyz), None, {"actor": t0, "size": size}); continue
                    ap["pick"] = {"actor": t0, "size": size}
                    print("place a 1x1 pillow where the BOTTOM-MIDDLE of the screen should go - it grows "
                          "up and out to both sides from there and faces into the room")
                elif cmd == "apple":
                    if off:
                        ap["stop"] = True
                        if ap["voice"]:
                            await ap["voice"].close(); ap["voice"] = None
                        state["speaker"] = None
                        if ap["task"]:
                            ap["task"].cancel(); ap["task"] = None
                        lay = ap.get("playing")
                        if lay:
                            await (clear_pixels(lay) if lay is ap["screen"] else remove_screen(lay))
                        state["last"] = None
                        t0 = bot.actor_by_name(who)
                        if t0 is not None:
                            ensure_avatar(t0)
                        ap["stop"] = False
                        print("apple stopped"); continue
                    if ap["task"]:
                        print("already playing - 'apple off' first"); continue
                    t0, args = hooked(args)
                    words = [x.lower() for x in args]
                    want = "small" if "small" in words else ("big" if "big" in words else None)
                    fps = None
                    fpath = None
                    for x in args:
                        if x.lower() in ("small", "big", "mute"):
                            continue
                        if x.lower().endswith("fps") and x[:-3].isdigit():
                            fps = min(int(x[:-3]), 12)
                        else:
                            fpath = x
                    path = find_video(fpath)
                    if not path:
                        print("no video found - give a path: apple ~/Downloads/video.mp4"); continue
                    if t0 is None or not bot.pose_of(t0):
                        print("no player to do that for"); continue
                    lay = ap["screen"]
                    temporary = False
                    if lay is None or (want and (want == "small") != (lay["sc"] == 1)):
                        lay = wall_layout(t0, size_for(want or "big"))
                        temporary = True
                    ap["stop"] = False
                    ap["playing"] = lay
                    ap["task"] = asyncio.create_task(
                        apple_play(path, lay, fps or lay["fps"], temporary, audio="mute" not in words))
                elif cmd == "bomb":
                    kind = (args[0].lower() if args else "")
                    if kind in BOMBS or kind == "off":
                        bomb["kind"] = kind
                        print(f"explosions now use: {kind if kind != 'off' else 'no bomb (effects only)'}")
                    else:
                        print(f"bomb is {bomb['kind']} - choose: bomb basic | impulse | pumpkin | radiation | off")
                elif cmd == "boom":
                    t, _ = split_name(args)
                    pose = bot.pose_of(t) if t is not None else None
                    if not pose:
                        print("don't know where they are yet"); continue
                    ensure_avatar(t)
                    p0 = pose[0] + q_forward(pose[1]) * 4.0 + pun.Vector3(0, 0.8, 0)
                    await explode(p0)
                    print(f"boom in front of {pname(t)}")
                elif cmd == "mirrorbuild":
                    if off:
                        print(f"mirror build off ({mb['count']} blocks copied)")
                        mb["actor"] = None; mb["actor_acct"] = None; continue
                    t, rest_a = split_name(args)
                    nums = [int(float(x)) for x in rest_a if is_num(x)]
                    if t is None:
                        print("can't find that player"); continue
                    await start_mirrorbuild(pname(t), nums[0] if nums else 5)
                elif cmd == "look":
                    t, _ = split_name(args)
                    await apply_look(pname(t))
                elif cmd in ("mimic", "mirror"):
                    if not args:
                        print(f"who? e.g. {cmd} Steve"); continue
                    t, rest_a = split_name(args)
                    if t is None:
                        print("can't find that player"); continue
                    nums = [float(x) for x in rest_a if is_num(x)]
                    if cmd == "mirror":
                        state.update(mimic=t, mimic_acct=acct_of(t), mirror=True, copy=False,
                                     dist=nums[0] if nums else 1.6)
                        print(f"mirroring {pname(t)} - type stop to end"); continue
                    # mimic: a full copy beside them - same walking, turning, hands,
                    # look, items and blocks, always the same distance away.
                    d_ = nums[0] if nums else 2.0
                    pose = None
                    for _ in range(30):
                        pose = bot.pose_of(t)
                        if pose:
                            break
                        await asyncio.sleep(0.1)
                    if not pose:
                        print(f"can't see {pname(t)} yet - try again in a second"); continue
                    await mimic_items_clear()
                    t_acct, t_name = acct_of(t), pname(t)
                    state.update(mimic=None, copy=False)
                    if t_acct and await asyncio.to_thread(_db_copy_look, t_acct):
                        print(f"(copying {t_name}'s colours: rejoining so everyone sees them)")
                        await rejoin_same_room(t_name)
                        t = next((x for x, pr in bot.actors.items() if x != bot.actor and isinstance(pr, dict)
                                  and str(pr.get(253) or "").startswith(t_acct + "$")), None)
                        if t is None:
                            print(f"{t_name} left the room"); continue
                        pose = None
                        for _ in range(50):
                            pose = bot.pose_of(t)
                            if pose:
                                break
                            await asyncio.sleep(0.1)
                        if not pose:
                            print(f"can't see {t_name} yet - try again in a second"); continue
                    else:
                        await apply_look(t_name)
                        await bot.respawn_avatar()
                    clone_spawn(pose[0], pose[1], d_)
                    state.update(mimic=t, mimic_acct=t_acct, mirror=False, copy=True, dist=d_)
                    mb.update(actor=t, count=0, by_mimic=True, cells={})
                    remember(mb, "actor")
                    print(f"mimicking {pname(t)}: its own player, spawned {d_:g} m in front facing them - "
                          f"copies their moves, look, items and blocks; walls stop it, hits knock it back "
                          f"- type stop to end")
                elif cmd == "stop":
                    state["mimic"] = None; state["mimic_acct"] = None; state["last"] = None
                    if state["copy"]:
                        state["copy"] = False; state["b"] = None
                        await mimic_items_clear()
                        if mb.get("by_mimic"):
                            mb.update(actor=None, actor_acct=None, by_mimic=False)
                        await asyncio.to_thread(_db_restore_look)
                        await apply_look(clean_name(a.look or who))
                    t0 = bot.actor_by_name(who)
                    if t0 is not None:
                        ensure_avatar(t0)
                    print("stopped - the bot is hidden under the map again")
                elif cmd == "block" and args:
                    t, rest_a = split_name(args)
                    pattern = rest_a[0] if rest_a else "1x1_stuffed"
                    color = next((x for x in rest_a[1:] if x.lower() in COLOR_SET), "white")
                    size = (1, 1, 1)
                    for x in rest_a[1:]:
                        if x.count("x") == 2 and x.replace("x", "").isdigit():
                            size = tuple(min(int(n), 12) for n in x.split("x"))
                    await spawn_blocks(pattern, color, size, pname(t))
                elif cmd == "rpc" and args:
                    method = args[0]
                    jargs = json.loads(rest[len(method):].strip() or "[]")
                    await bot.rpc(bot.view_id, method, jargs, receivers=pun.RECV_OTHERS)
                    print(f"sent {method}")
                elif cmd in ("quit", "exit", "q"):
                    break
                else:
                    print("unknown command - type menu to see them all")
            except Exception as e:
                print(f"error: {e!r}")
    finally:
        loop_task.cancel()
        outline_task.cancel()
        rain_task.cancel()
        fx_task.cancel()
        coin_task.cancel()
        soundkey["stop"] = True
        if soundkey["task"]:
            soundkey["task"].cancel()
            await asyncio.gather(soundkey["task"], return_exceptions=True)
        if ap["task"]:
            ap["task"].cancel()
        try:
            if soundkey["voice"]:
                await soundkey["voice"].close()
            if ap["voice"]:
                await ap["voice"].close()
            await remove_pixel_art()
            if ap.get("playing"):
                await remove_screen(ap["playing"])
            if ap["screen"]:
                await remove_screen(ap["screen"])
        except Exception:
            pass
        try:
            await ball_clear()
            await items_clear()
        except Exception:
            pass
        follow_task.cancel()
        try:
            await asyncio.to_thread(_db_restore_look)
        except Exception:
            pass
        try:
            for ac_ in list(frozen):
                await thaw(ac_)
        except Exception:
            pass
        try:
            await wv_stop()
            await hpot_stop()
            await clear_terrain(mapgen, "map")
        except Exception:
            pass
        try:
            await boss_stop()
            await koth_stop()
            await g_off()
            await canvas_off()
        except Exception:
            pass
        try:
            await outline_off()
        except Exception:
            pass
        await bot.leave()


def take_over(bot_id):
    """One copy per bot id: close an older copy still running (it would hold the
    same Photon account + a CCU slot), then record our pid."""
    import signal
    d = os.path.expanduser("~/.cache/yeeps-bot")
    os.makedirs(d, exist_ok=True)
    pf = os.path.join(d, f"bot-{bot_id}.pid")
    try:
        old = int(open(pf).read().strip())
        if old != os.getpid() and "bot.py" in open(f"/proc/{old}/cmdline", "rb").read().decode(errors="replace"):
            os.kill(old, signal.SIGTERM)
            print(f"(closed an older copy of bot {bot_id}, pid {old})")
            time.sleep(1.5)
    except (OSError, ValueError):
        pass
    with open(pf, "w") as f:
        f.write(str(os.getpid()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="YeeperBot")
    ap.add_argument("--bot-id", type=int, default=1)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("listen")
    s.add_argument("--target", default=OWNER)
    s.add_argument("--room")
    s.add_argument("--all", action="store_true", help="also print sound-effect RPCs")
    s.add_argument("--only", help="comma-separated RPC names to print")
    s = sub.add_parser("run")
    s.add_argument("--target", default=OWNER)
    s.add_argument("--look", help="copy this player's look (default: the target)")
    s.add_argument("--stay", action="store_true",
                   help="stay in the room the target is in now (only reconnect); don't follow them out")
    s.add_argument("--mirrorbuild", type=int, metavar="N",
                   help="start straight away copying the target's builds N blocks ahead")
    argv = sys.argv[1:]
    if not any(x in ("listen", "run") for x in argv) and not any(x in ("-h", "--help") for x in argv):
        argv = argv + ["run"]                 # plain "python3 bot.py" = the menu
    a = ap.parse_args(argv)
    take_over(a.bot_id)
    try:
        asyncio.run({"listen": cmd_listen, "run": cmd_run}[a.cmd](a))
    except KeyboardInterrupt:
        pass
    except PhotonError as e:
        raise SystemExit(f"Photon: {e}")


if __name__ == "__main__":
    main()
