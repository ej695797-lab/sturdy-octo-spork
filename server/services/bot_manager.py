"""Runs one YeeperBot in a background thread and exposes it to the editor.

pyphotonrealtime needs its socket pumped (``client.service()``); that runs in a
daemon thread so the FastAPI event loop is never blocked. The thread imports the
package lazily, so the web server boots fine when the bot is absent or disabled.
"""

from __future__ import annotations

import os
import time

from config import BOT_KEY

# The Photon app id the client uses, the app version it connects with, and the
# cloud region it lands in. Photon keeps rooms inside an (appId, appVersion,
# region) tuple: a bot on a different app version (or a "best region" that
# resolves elsewhere) sits in another matchmaking pool and never shares a room
# with players.
# Verified live from the running client (Explorer /api/lua/exec):
#   PhotonNetwork.AppVersion = "1.31.0_2.45", PhotonNetwork.CloudRegion = "us"
APP_ID = os.getenv("PHOTON_APP_ID", "a279e749-3e24-464c-9336-f3a412bdfdbc")
APP_VERSION = os.getenv("PHOTON_APP_VERSION", "1.31.0_2.45")
APP_REGION = os.getenv("PHOTON_REGION", "us")
# Transport for the bot's Photon connection. Raw TCP (4530/4533) and UDP
# (5055/5058) are blocked on some hosts (Render's free tier blocks non-HTTP
# outbound), which leaves the bot stuck in "connecting" and it never joins.
# WebSocketSecure (19090/19093) is TLS over HTTP ports and gets through where
# the others don't, so it is the safe default. Override with PHOTON_PROTOCOL:
# udp | tcp | ws | wss.
APP_PROTOCOL = (os.getenv("PHOTON_PROTOCOL", "wss") or "wss").strip().lower()
_PROTOCOLS = {"udp": "Udp", "tcp": "Tcp", "ws": "WebSocket", "websocket": "WebSocket",
              "wss": "WebSocketSecure", "websocketsecure": "WebSocketSecure"}

_MAX_LOG = 400


class BotManager:
    def __init__(self) -> None:
        self._bot = None
        self._thread = None
        self._stop = None
        self.error = ""
        self.startedAt = 0.0
        self.log: list[str] = []

    # ── helpers ──────────────────────────────────────────────────────
    def _note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        del self.log[:-_MAX_LOG]
        print(f"[bot] {text}")

    def available(self) -> tuple[bool, str]:
        if not BOT_KEY:
            return False, "BOT_KEY is not set on the server (bots are off)"
        try:
            import pyphotonrealtime  # noqa: F401
        except Exception as e:  # noqa: BLE001
            return False, f"pyphotonrealtime not installed: {e}"
        return True, ""

    def _alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── commands ─────────────────────────────────────────────────────
    def start(self, *, name: str = "", room: str = "", spawn: bool = True) -> dict:
        ok, why = self.available()
        if not ok:
            self.error = why
            return self.status()
        if self._alive():
            if room:
                self._bot.set_room(room)
            if name:
                self._bot.name = name[:24]
            self._bot.spawn = bool(spawn)
            return self.status()

        from threading import Event, Thread

        from bot.photon_bot import YeeperBot

        self.error = ""
        self.startedAt = time.time()
        self._bot = YeeperBot(
            name=(name or "YeeperBot")[:24],
            app_id=APP_ID,
            app_version=APP_VERSION,
            bot_key=BOT_KEY,
            room=room or "",
            spawn=bool(spawn),
            region=APP_REGION,
            protocol=_PROTOCOLS.get(APP_PROTOCOL, "WebSocketSecure"),
        )
        if not self._bot.connect():
            self.error = self._bot.error or "connect failed"
            return self.status()
        self._stop = Event()
        self._thread = Thread(target=self._loop, name="yeeperbot", daemon=True)
        self._thread.start()
        self._note(f"started as {self._bot.name}" + (f", joining {room}" if room else "")
                   + (" (spawn on)" if spawn else " (spawn off)"))
        return self.status()

    def _loop(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            try:
                self._bot.service()
                self._bot._maybe_join()
                self._bot._maybe_spawn()
                self._bot._note_remotes()
            except Exception as e:  # noqa: BLE001
                self.error = f"service: {e}"
                self._note(f"service error: {e}")
                break
            time.sleep(0.02)
        try:
            self._bot.disconnect()
        except Exception:
            pass
        self._note("stopped")

    def stop(self) -> dict:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._stop = None
        return self.status()

    def set_room(self, room: str) -> dict:
        if self._bot is not None:
            self._bot.set_room(room)
            self._note(f"room -> {room or '(none)'}")
        return self.status()

    def raise_game(self, msg_type: int, payload) -> dict:
        if self._bot is None:
            return {"ok": False, "error": "bot not running"}
        return {"ok": self._bot.raise_game(msg_type, payload), "type": msg_type}

    def spawn_player(self) -> dict:
        """Manually announce the bot's avatar (Photon Instantiate 202)."""
        if self._bot is None:
            return {"ok": False, "error": "bot not running"}
        ok = self._bot.spawn_player()
        return {"ok": ok, "viewId": self._bot.view_id, "inRoom": self._bot._client.in_room
                if self._bot._client else False, "error": self._bot.error}

    def status(self) -> dict:
        ok, why = self.available()
        base = {
            "enabled": bool(BOT_KEY),
            "available": ok,
            "reason": why,
            "running": self._thread is not None and self._thread.is_alive(),
            "error": self.error,
            "uptime": round(time.time() - self.startedAt, 1) if self.startedAt else 0,
            "log": self.log[-60:],
        }
        if self._bot is not None:
            base.update(self._bot.status())
            if self._bot.error and not base["error"]:
                base["error"] = self._bot.error
        return base

    def events(self) -> list:
        return list(self._bot.events) if self._bot is not None else []


manager = BotManager()
