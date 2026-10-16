"""YeeperBot - a Photon Realtime bot for the owner's Yeeps rooms.

The 1.31 client speaks **raw Photon Realtime** (no PUN view layer): everything
that happens in a room arrives as custom event ``200`` whose content is the
envelope

    {0: 1001, 2: <increasing seq>, 5: <type>, 4: <payload>}

captured from the client (see _pun_capture.txt). ``5`` is the game's message
type (17/20/26/30/32/33/37...) and ``4`` its payload.

This module only needs to *be in the room* to be useful (players see an extra
actor, and the room's live feed becomes readable from /editor). Sending world
messages is possible through :meth:`YeeperBot.raise_game`, which wraps a payload
in the same envelope.

It is deliberately dependency-light: pyphotonrealtime is imported lazily so the
web server still boots when the package (or the whole bot) is absent.
"""

from __future__ import annotations

import time
from typing import Any

GAME_EVENT = 200          # the client's custom event code for in-world messages
GAME_CHANNEL = 1001       # envelope key 0 - constant in every captured message
_KEY_CHANNEL = 0
_KEY_SEQ = 2
_KEY_TYPE = 5
_KEY_DATA = 4

_MAX_EVENTS = 200


def _env_int(d: Any, key: int) -> int | None:
    try:
        v = d.get(key)
    except Exception:
        return None
    return v if isinstance(v, int) else None


class YeeperBot:
    """One Photon connection, driven by a caller that pumps :meth:`service`."""

    def __init__(
        self,
        *,
        name: str,
        app_id: str,
        app_version: str,
        bot_key: str,
        bot_id: int = 1,
        room: str = "",
    ) -> None:
        self.name = name
        self.app_id = app_id
        self.app_version = app_version
        self.bot_key = bot_key
        self.bot_id = int(bot_id)
        self.room = room

        self.state = "idle"          # idle | connecting | connected | in_room | error
        self.error = ""
        self.seq = 0
        self._want_room = room
        self._join_sent_for = ""
        self._join_retry_at = 0.0
        self.events: list[dict[str, Any]] = []
        self.raw_events: list[dict[str, Any]] = []

        self._client = None           # RealtimeClient
        self._EventData = None
        self._cb = None

    # ── construction ─────────────────────────────────────────────────
    def _build(self) -> None:
        from pyphotonrealtime import (AppSettings, ConnectionProtocol,
                                      RealtimeClient)
        from pyphotonrealtime.realtime.authentication import (
            AuthenticationValues, CustomAuthenticationType)
        from pyphotonrealtime.realtime.callbacks import (
            ConnectionCallbacks, InRoomCallbacks, MatchmakingCallbacks,
            OnEventCallback)
        from pyphotonrealtime.realtime.options import EnterRoomParams

        bot = self

        class _Cb(ConnectionCallbacks, MatchmakingCallbacks, InRoomCallbacks,
                  OnEventCallback):
            def on_connected(self) -> None:
                bot.state = "connected"

            def on_connected_to_master(self) -> None:
                bot.state = "connected"
                bot._maybe_join()

            def on_disconnected(self, cause) -> None:
                bot.state = "idle"
                bot._join_sent_for = ""
                bot.error = f"disconnected: {cause}"

            def on_custom_authentication_failed(self, msg: str) -> None:
                bot.state = "error"
                bot.error = f"auth failed: {msg}"

            def on_created_room(self) -> None:
                bot.state = "in_room"

            def on_joined_room(self) -> None:
                bot.state = "in_room"

            def on_join_room_failed(self, code: int, msg: str) -> None:
                bot.error = f"join failed {code}: {msg}"
                bot._join_sent_for = ""
                bot._join_retry_at = time.time() + 5

            def on_create_room_failed(self, code: int, msg: str) -> None:
                bot.error = f"create room failed {code}: {msg}"
                bot._join_sent_for = ""
                bot._join_retry_at = time.time() + 5

            def on_left_room(self) -> None:
                bot._join_sent_for = ""

            def on_player_entered_room(self, player) -> None:
                bot._record("player_entered", who=getattr(player, "nick_name", None))

            def on_player_left_room(self, player) -> None:
                bot._record("player_left", who=getattr(player, "nick_name", None))

            def on_event(self, event) -> None:
                bot._on_event(event)

        self._client = RealtimeClient()
        self._cb = _Cb()
        self._client.add_callback_target(self._cb)
        self._enter_room_params = EnterRoomParams
        self._client.auth_values = AuthenticationValues(
            user_id=f"o_99000000000000{self.bot_id:02d}",
            auth_type=CustomAuthenticationType.Custom,
            auth_get_parameters=(
                f"botKey={self.bot_key}&botId={self.bot_id}&botName={self.name}"),
        )
        self._settings = AppSettings(
            app_id_realtime=self.app_id,
            app_version=self.app_version,
            protocol=ConnectionProtocol.Tcp,
        )

    # ── lifecycle ────────────────────────────────────────────────────
    def connect(self) -> bool:
        if self._client is None:
            self._build()
        self.state = "connecting"
        self.error = ""
        ok = self._client.connect_using_settings(self._settings)
        if not ok:
            self.state = "error"
            self.error = "connect_using_settings returned False"
        return bool(ok)

    def service(self) -> None:
        if self._client is not None:
            self._client.service()

    def disconnect(self) -> None:
        try:
            if self._client is not None:
                self._client.disconnect()
        except Exception as e:  # noqa: BLE001
            self.error = f"disconnect: {e}"
        self.state = "idle"

    def set_room(self, room: str) -> None:
        """Ask the bot to be in ``room`` (joined on the next pump)."""
        self.room = room or ""
        self._want_room = self.room
        self._join_sent_for = ""
        self._join_retry_at = 0.0
        self._maybe_join()

    # ── room ─────────────────────────────────────────────────────────
    def _maybe_join(self) -> None:
        c = self._client
        if c is None or not self._want_room:
            return
        # Joining before the client reaches the master server only drops the
        # request; wait until it is ready (guarded each pump).
        if not getattr(c, "is_connected_and_ready", False):
            return
        if time.time() < self._join_retry_at:
            return
        if c.in_room:
            cur = getattr(c.current_room, "name", "")
            if cur == self._want_room:
                return
            # Leaving: the next pump joins the target once we're ready again.
            self._client.op_leave_room()
            self._join_sent_for = ""
            return
        if self._join_sent_for == self._want_room:
            return
        self._join_sent_for = self._want_room
        try:
            self._client.op_join_or_create_room(
                self._enter_room_params(room_name=self._want_room))
        except Exception as e:  # noqa: BLE001
            self.error = f"join: {e}"
            self._join_sent_for = ""

    def leave_room(self) -> None:
        self._want_room = ""
        self.room = ""
        self._join_sent_for = ""
        if self._client is not None and self._client.in_room:
            self._client.op_leave_room()

    # ── messages ─────────────────────────────────────────────────────
    def raise_game(self, msg_type: int, payload: Any) -> bool:
        """Send an in-world message (event 200, envelope ``{0,2,5,4}``).

        pyphotonrealtime's ``op_raise_event`` only accepts codes 1-199 (PUN
        reserves 200+), but 200 is exactly the code the game sends its in-world
        traffic on, so the operation is built and queued directly here.
        """
        c = self._client
        if c is None or not c.in_room:
            return False
        from pyphotonrealtime.protocol.operation_code import OperationCode
        from pyphotonrealtime.protocol.param.int8_param import Int8Parameter
        from pyphotonrealtime.protocol.param.parameter_key import ParameterKey
        from pyphotonrealtime.realtime._convert import to_param

        self.seq += 1
        content = {
            _KEY_CHANNEL: GAME_CHANNEL,
            _KEY_SEQ: self.seq,
            _KEY_TYPE: int(msg_type),
            _KEY_DATA: payload,
        }
        try:
            params = {
                ParameterKey.Code: Int8Parameter(GAME_EVENT),
                ParameterKey.Data: to_param(content),
            }
            return bool(c.peer.send_operation(OperationCode.RaiseEvent, params))
        except Exception as e:  # noqa: BLE001
            self.error = f"raise: {e}"
            return False

    def _on_event(self, event) -> None:
        data = getattr(event, "custom_data", None)
        entry = {
            "t": round(time.time(), 3),
            "code": getattr(event, "code", None),
            "sender": getattr(event, "sender", None),
        }
        if isinstance(data, dict):
            entry["seq"] = _env_int(data, _KEY_SEQ)
            entry["type"] = _env_int(data, _KEY_TYPE)
            entry["data"] = data.get(_KEY_DATA)
            if entry["type"] is None and getattr(event, "code", None) != GAME_EVENT:
                entry["data"] = data
        else:
            entry["data"] = data
        self.events.append(entry)
        del self.events[:-_MAX_EVENTS]

    def _record(self, kind: str, **kw: Any) -> None:
        self.events.append({"t": round(time.time(), 3), "kind": kind, **kw})
        del self.events[:-_MAX_EVENTS]

    # ── status ───────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        c = self._client
        room = None
        players: list[dict[str, Any]] = []
        in_room = bool(c is not None and c.in_room)
        if in_room:
            r = c.current_room
            room = {
                "name": getattr(r, "name", ""),
                "playerCount": getattr(r, "player_count", None),
                "maxPlayers": getattr(r, "max_players", None),
            }
            for num, p in (getattr(r, "players", {}) or {}).items():
                players.append({
                    "actor": num,
                    "nick": getattr(p, "nick_name", None),
                    "userId": getattr(p, "user_id", None),
                    "local": getattr(p, "is_local", False),
                    "props": getattr(p, "custom_properties", None),
                })
        return {
            "name": self.name,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "state": self.state,
            "error": self.error,
            "clientState": str(getattr(c, "state", "")) if c is not None else "not-started",
            "inRoom": in_room,
            "wantRoom": self._want_room,
            "room": room,
            "players": players,
            "events": self.events[-40:],
        }
