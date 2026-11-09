"""Photon Voice 2 sender: the bot "talks" pre-encoded Opus audio through the
game's own voice chat, so every player hears it from the bot's avatar.

Captured from real players (2026-10-08):
  voice app 7af0a7f1-..., AppVersion "" (empty), region us, anonymous auth;
  room = <PUN room name> + "_voice_".
  event 202 (VoiceInfo, sent to Others; re-sent to each newcomer):
      object[] [byte 0, byte 1(VoiceInfo), object[] [Dictionary<byte,object> {
          1: byte voiceId, 12: int 11 (Opus), 2: int 48000, 3: int 1 (mono),
          4: int 20000 (frame us), 5: int 30000 (bitrate), 6..9: int 0,
          10: int speakerViewId (PUN Photon/PhotonSpeakerPublic view = actor*1000+2),
          11: byte eventNumber }]]
  event 203 (frame): byte[] [4 (Frame), voiceId, eventNumber, 0 (flags)] + Opus packet
"""
import asyncio
import os
import subprocess
import time

from .client import (NAME_SERVER, OP_AUTHENTICATE, OP_JOIN_ROOM, OP_RAISE_EVENT, P_ADDRESS, P_BROADCAST,
                     P_CODE, P_DATA, P_PLAYER_PROPS, P_RECEIVERS, P_ROOM_NAME, P_TOKEN, Connection,
                     auth_params, check)
from .protocol16 import Byte, Dictionary, IntArray

VOICE_APP = "00000000-0000-0000-0000-000000000004"
EV_VOICE_INFO, EV_FRAME = 202, 203
FRAME_SEC = 0.02


def opus_packets(path, cache_dir=None, bitrate="30k"):
    """Audio of any media file -> list of 20 ms mono 48 kHz Opus packets
    (ffmpeg libopus -> Ogg, then the Ogg pages are unpacked). Cached as .ogg."""
    import imageio_ffmpeg
    cache_dir = cache_dir or os.path.dirname(path)
    ogg = os.path.join(cache_dir, os.path.splitext(os.path.basename(path))[0] + "_voice.ogg")
    if not os.path.isfile(ogg) or os.path.getmtime(ogg) < os.path.getmtime(path):
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-i", path, "-vn", "-ac", "1",
                        "-ar", "48000", "-c:a", "libopus", "-b:a", bitrate, "-frame_duration", "20",
                        "-application", "audio", "-f", "ogg", ogg], check=True)
    data = open(ogg, "rb").read()
    pkts, cur, i = [], b"", 0
    while i < len(data):
        if data[i:i + 4] != b"OggS":
            raise ValueError("bad Ogg page")
        nseg = data[i + 26]
        lace = data[i + 27:i + 27 + nseg]
        off = i + 27 + nseg
        for n in lace:
            cur += data[off:off + n]
            off += n
            if n < 255:
                pkts.append(cur)
                cur = b""
        i = off
    return [p for p in pkts if not p.startswith((b"OpusHead", b"OpusTags"))]


class VoiceSender:
    def __init__(self, pun_room, speaker_view, name="YeeperBot", region="us", voice_id=1):
        # 1.31: voice app VOICE_APP, AppVersion "", room "<game room>_voice_". Another
        # game version sets YEEPS_VOICE_APP / YEEPS_VOICE_VERSION / YEEPS_VOICE_ROOM.
        self.app = os.environ.get("YEEPS_VOICE_APP") or VOICE_APP
        self.ver = os.environ.get("YEEPS_VOICE_VERSION", "")
        self.room = os.environ.get("YEEPS_VOICE_ROOM", "{room}_voice_").format(room=pun_room)
        self.speaker_view = speaker_view
        self.name, self.region, self.voice_id = name, region, voice_id
        self.conn = None
        self.ev = 0
        self.actor = None
        # Live talk (editor voice): what other players say. on_info(actor, info dict)
        # when a voice stream is announced, on_frame(actor, voice_id, opus) per frame.
        self.on_info = None
        self.on_frame = None

    async def connect(self):
        """Live (2026-10-18): voice may be on our voice app (live46+ clients) or on the
        realtime app (older live builds forced it there), with an unknown AppVersion, so
        try YEEPS_VOICE_CANDIDATES ("app|version,..."; {gv} = the room's game version)
        in order until the voice room is found. Custom-auth apps get YEEPS_VOICE_AUTH."""
        cands = []
        gv = os.environ.get("YEEPS_GAME_VER", "")
        for c in filter(None, (x.strip() for x in os.environ.get("YEEPS_VOICE_CANDIDATES", "").split(","))):
            app, _, ver = c.partition("|")
            cands.append((app, ver.replace("{gv}", gv)))
        if not cands:
            cands = [(self.app, self.ver)]
        last = None
        for app, ver in cands:
            self.app, self.ver = app, ver
            try:
                await self._connect_one()
                print(f"(voice: joined {self.room} on app {app[:8]} version {ver!r})")
                return
            except Exception as e:
                last = e
                try:
                    if self.conn:
                        await self.conn.close()
                except Exception:
                    pass
                self.conn = None
        raise last

    async def _connect_one(self):
        aq = os.environ.get("YEEPS_VOICE_AUTH") or None
        ns = Connection(NAME_SERVER, self.app, label="voice-ns")
        await ns.open()
        try:
            p = check(await ns.op(OP_AUTHENTICATE, auth_params(self.app, self.ver, self.region, auth_query=aq)), "voice login")
        finally:
            await ns.close()
        ms = Connection(p[P_ADDRESS], self.app, label="voice-master")
        await ms.open()
        try:
            check(await ms.op(OP_AUTHENTICATE, auth_params(self.app, self.ver, self.region, token=p[P_TOKEN])),
                  "voice master")
            g = check(await ms.op(OP_JOIN_ROOM, {P_ROOM_NAME: self.room}), "voice room")
        finally:
            await ms.close()
        self.conn = Connection(g[P_ADDRESS], self.app, self._on_event, label="voice")
        await self.conn.open()
        check(await self.conn.op(OP_AUTHENTICATE, auth_params(self.app, self.ver, self.region, token=g[P_TOKEN])),
              "voice game")
        r = check(await self.conn.op(OP_JOIN_ROOM, {P_ROOM_NAME: self.room, P_PLAYER_PROPS: {Byte(255): self.name},
                                                     P_BROADCAST: True}), "voice join")
        self.actor = r.get(254)
        await self.announce()

    def _info(self):
        d = Dictionary(ord("b"), 0)
        d.update({Byte(1): Byte(self.voice_id), Byte(12): 11, Byte(2): 48000, Byte(3): 1, Byte(4): 20000,
                  Byte(5): 30000, Byte(6): 0, Byte(7): 0, Byte(8): 0, Byte(9): 0,
                  Byte(10): int(self.speaker_view), Byte(11): Byte(self.ev & 0xFF)})
        return [Byte(0), Byte(1), [d]]

    async def announce(self, actors=None):
        params = {P_CODE: Byte(EV_VOICE_INFO), P_DATA: self._info(), P_RECEIVERS: Byte(0)}
        if actors:
            params = {P_CODE: Byte(EV_VOICE_INFO), P_DATA: self._info(), 252: IntArray(actors)}
        await self.conn.send(OP_RAISE_EVENT, params)

    def _on_event(self, code, params):
        if code == 255 and params.get(254) != self.actor:         # someone joined voice: tell them about us
            asyncio.get_running_loop().create_task(self.announce([int(params.get(254))]))
        sender = params.get(254)
        if sender is None or sender == self.actor:
            return
        try:
            if code == EV_FRAME and self.on_frame:
                d = bytes(params.get(P_DATA) or b"")
                if len(d) > 4 and d[0] == 4:                       # [4 Frame, voiceId, eventNumber, flags] + Opus
                    self.on_frame(int(sender), d[1], d[4:])
            elif code == EV_VOICE_INFO and self.on_info:
                d = params.get(P_DATA) or []
                for info in (d[2] if len(d) > 2 and isinstance(d[2], (list, tuple)) else []):
                    if isinstance(info, dict):
                        self.on_info(int(sender), {int(k): v for k, v in info.items()})
        except Exception:
            pass

    async def send_frame(self, pkt):
        """One live 20 ms Opus frame, sent right away (talking from the editor)."""
        if self.conn is None or self.conn.closed.is_set():
            return
        body = bytes([4, self.voice_id, self.ev & 0xFF, 0]) + pkt
        self.ev += 1
        await self.conn.send(OP_RAISE_EVENT, {P_CODE: Byte(EV_FRAME), P_DATA: body, P_RECEIVERS: Byte(0)})

    async def play(self, packets, start, stop=lambda: False):
        """Send packets in real time starting at monotonic time `start`."""
        for i, pkt in enumerate(packets):
            if stop() or self.conn is None or self.conn.closed.is_set():
                break
            delay = start + i * FRAME_SEC - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:
                continue                                        # fell behind: skip to stay in sync
            body = bytes([4, self.voice_id, self.ev & 0xFF, 0]) + pkt
            self.ev += 1
            await self.conn.send(OP_RAISE_EVENT, {P_CODE: Byte(EV_FRAME), P_DATA: body, P_RECEIVERS: Byte(0)})

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None
