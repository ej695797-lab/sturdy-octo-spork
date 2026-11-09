"""Minimal Photon Realtime client over secure websockets (protocol 1.6).

Flow: name server Authenticate -> master server Authenticate(token) ->
JoinRoom on master (gets game server address) -> game server Authenticate ->
JoinRoom. Events are delivered to `on_event(code, params)`.
"""
import asyncio
import itertools
import time

import websockets

from .protocol16 import Byte, Reader, op_request

NAME_SERVER = "wss://ns.photonengine.io:19093"
LIB_VERSION = "4.1.8.0"

# Operation codes
OP_AUTHENTICATE, OP_JOIN_ROOM, OP_LEAVE, OP_RAISE_EVENT, OP_SET_PROPERTIES = 230, 226, 254, 253, 252
OP_JOIN_GAME_OR_CREATE = 226
# Parameter codes
P_APP_ID, P_APP_VERSION, P_REGION, P_ADDRESS, P_TOKEN, P_USER_ID = 224, 220, 210, 230, 221, 225
P_AUTH_TYPE, P_AUTH_PARAMS, P_ROOM_NAME, P_PLAYER_PROPS, P_BROADCAST = 217, 216, 255, 249, 250
AUTH_TYPE_CUSTOM = 0
P_ACTOR_NR, P_GAME_PROPS, P_ACTOR_LIST, P_CODE, P_DATA, P_CACHE, P_RECEIVERS = 254, 248, 252, 244, 245, 247, 246
P_JOIN_MODE, P_PLUGINS = 215, 204


class PhotonError(Exception):
    pass


class Connection:
    """One websocket to one Photon server (name, master or game)."""

    def __init__(self, url, app_id, on_event=None, label=""):
        self.url, self.app_id, self.label = url, app_id, label
        self.on_event = on_event or (lambda code, params: None)
        self.ws = None
        self._pending = {}          # op code -> future (Photon answers ops in order)
        self._reader = self._pinger = None
        self.closed = asyncio.Event()

    async def open(self):
        sep = "&" if "?" in self.url else "/?"
        url = f"{self.url}{sep}libversion={LIB_VERSION}&sid=30&app={self.app_id}"
        self.ws = await websockets.connect(url, subprotocols=["GpBinaryV16"], open_timeout=15,
                                           max_size=8 * 1024 * 1024, ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop())
        self._pinger = asyncio.create_task(self._ping_loop())

    async def close(self):
        for t in (self._pinger, self._reader):
            if t:
                t.cancel()
        if self.ws:
            await self.ws.close()
        self.closed.set()

    async def _ping_loop(self):
        # Internal op 1 (ping) keeps the TCP/WS peer alive (server drops idle peers).
        try:
            while True:
                await asyncio.sleep(1.0)
                body = op_request(1, {1: int(time.monotonic() * 1000) & 0x7FFFFFFF})
                await self.ws.send(b"\xf3\x06" + body)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _read_loop(self):
        try:
            async for msg in self.ws:
                if not isinstance(msg, bytes) or len(msg) < 2 or msg[0] != 0xF3:
                    continue
                kind = msg[1] & 0x7F
                r = Reader(msg[2:])
                if kind == 3:                      # operation response
                    resp = r.op_response()
                    fut = self._pending.pop(resp["op"], None)
                    if fut and not fut.done():
                        fut.set_result(resp)
                elif kind == 4:                    # event
                    ev = r.event()
                    try:
                        self.on_event(ev["code"], ev["params"])
                    except Exception as e:
                        print(f"[{self.label}] event handler error: {e!r}")
                # 1 = init response, 7 = internal (ping) response: ignored
        except websockets.ConnectionClosed as e:
            print(f"[{self.label}] connection closed: {e.code} {e.reason}")
        except asyncio.CancelledError:
            pass
        finally:
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(PhotonError(f"{self.label} disconnected"))
            self.closed.set()

    async def op(self, code, params, timeout=15):
        fut = asyncio.get_running_loop().create_future()
        self._pending[code] = fut
        await self.ws.send(b"\xf3\x02" + op_request(code, params))
        return await asyncio.wait_for(fut, timeout)

    async def send(self, code, params):
        """Fire-and-forget operation (RaiseEvent etc.). Silently dropped if the
        connection is closed (e.g. mid room-switch) instead of killing the caller."""
        if self.ws is None:
            return
        try:
            await self.ws.send(b"\xf3\x02" + op_request(code, params))
        except websockets.ConnectionClosed:
            pass


def auth_params(app_id, app_version, region, auth_query=None, token=None):
    p = {P_APP_ID: app_id, P_APP_VERSION: app_version, P_REGION: region}
    if token:
        p[P_TOKEN] = token
    elif auth_query is not None:
        p[P_AUTH_TYPE] = Byte(AUTH_TYPE_CUSTOM)
        p[P_AUTH_PARAMS] = auth_query   # query string forwarded to the auth URL
    return p


def check(resp, what):
    if resp["rc"] != 0:
        raise PhotonError(f"{what} failed: rc={resp['rc']} {resp.get('msg')}")
    return resp["params"]
