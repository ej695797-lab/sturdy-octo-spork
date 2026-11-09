"""Own the editor-controlled Photon bot process and its fixed action surface."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from bot.actions import ACTION_COMMANDS, ITEM_ACTIONS, MEDIA_ACTIONS, command_for
from config import BOT_KEY

SERVER_DIR = Path(__file__).resolve().parent.parent
BOT_SCRIPT = SERVER_DIR / "bot" / "command_bot.py"
BAD_APPLE_VIDEO = SERVER_DIR / "bot" / "media" / "bad_apple.mp4"
BOT_ID = 1
_MAX_LOG = 160
_MENU_SEPARATOR = re.compile(r"^=+$")


class BotManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self.error = ""
        self.started_at = 0.0
        self.target_account = ""
        self.target_name = ""
        self.room = ""
        self.log: list[str] = []
        self._audio_path = ""
        self._audio_ready = False

    def available(self) -> tuple[bool, str]:
        if not BOT_KEY:
            return False, "BOT_KEY is not set on the server (bots are off)"
        if not os.getenv("PHOTON_APP_ID"):
            return False, "PHOTON_APP_ID must be set to the PUN app used by the game"
        if not BOT_SCRIPT.is_file():
            return False, "The Photon command engine is missing"
        for package in ("websockets", "pymongo"):
            if importlib.util.find_spec(package) is None:
                return False, f"{package} is not installed"
        return True, ""

    def _note(self, text: str) -> None:
        with self._lock:
            self.log.append(f"{time.strftime('%H:%M:%S')} {text[:300]}")
            del self.log[:-_MAX_LOG]
        print(f"[bot] {text}")

    def _read_output(self, process: subprocess.Popen) -> None:
        in_menu = False
        try:
            for raw in process.stdout or ():
                line = raw.rstrip()
                if "YEEPS BOT" in line:
                    in_menu = True
                    continue
                if in_menu:
                    if _MENU_SEPARATOR.match(line.strip()):
                        in_menu = False
                    continue
                line = line.replace("bot> ", "").strip()
                if not line:
                    continue
                room_match = re.search(r"\(Photon room ([^)]+)\)", line)
                if room_match:
                    with self._lock:
                        self.room = room_match.group(1)
                joined_match = re.match(r"joined (\S+) as actor \d+", line)
                if joined_match:
                    with self._lock:
                        self.room = joined_match.group(1)
                self._note(line)
        except (OSError, ValueError) as exc:
            self._note(f"output reader stopped: {exc}")
        finally:
            code = process.poll()
            if code not in (None, 0):
                with self._lock:
                    self.error = self.log[-1].split(" ", 1)[-1] if self.log else f"bot exited ({code})"

    def _new_audio_file(self, data: bytes = b"") -> str:
        with tempfile.NamedTemporaryFile(prefix="yeeper-bad-apple-", suffix=".mp3", delete=False) as audio:
            audio.write(data)
            return audio.name

    def _remove_audio_file(self) -> None:
        path, self._audio_path = self._audio_path, ""
        self._audio_ready = False
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._note(f"couldn't remove temporary Bad Apple audio: {exc}")

    def set_media_audio(self, data: bytes) -> None:
        with self._lock:
            if self._audio_path:
                with open(self._audio_path, "wb") as audio:
                    audio.write(data)
                self._audio_ready = True

    def start(self, *, target: str, target_name: str, room: str,
              media_data: bytes = b"") -> dict:
        available, reason = self.available()
        if not available:
            with self._lock:
                self.error = reason
            return {"ok": False, **self.status()}
        target = str(target or "").strip()
        if not target.startswith("o_") or len(target) > 80:
            return {"ok": False, "error": "Choose an online player to hook the bot to"}

        with self._lifecycle:
            with self._lock:
                old_process = self._process
                same_target = (old_process is not None and old_process.poll() is None
                               and self.target_account == target)
            if same_target:
                return {"ok": True, **self.status()}
            if old_process is not None and old_process.poll() is None:
                self._stop_process(old_process)
            self._remove_audio_file()
            try:
                self._audio_path = self._new_audio_file(media_data)
                self._audio_ready = bool(media_data)
            except OSError as exc:
                with self._lock:
                    self.error = f"couldn't prepare Bad Apple audio: {exc}"
                return {"ok": False, **self.status()}

            env = os.environ.copy()
            env.pop("YEEPS_VIEW_DIR", None)
            env.pop("YEEPS_TALK_PORT", None)
            env.pop("YEEPS_BOT_ENV", None)
            env.pop("YEEPS_LOOK_ACCT", None)
            env.pop("AUTH", None)
            env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["YEEPS_BAD_APPLE_MEDIA"] = self._audio_path
            command = [
                sys.executable, "-u", "-m", "bot.command_bot",
                "--name", "YeeperBot", "--bot-id", str(BOT_ID),
                "run", "--target", target,
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(SERVER_DIR),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                self._remove_audio_file()
                with self._lock:
                    self.error = f"couldn't start the bot: {exc}"
                return {"ok": False, **self.status()}

            with self._lock:
                self._process = process
                self._reader = threading.Thread(
                    target=self._read_output, args=(process,), name="yeeperbot-output", daemon=True)
                self.error = ""
                self.started_at = time.time()
                self.target_account = target
                self.target_name = " ".join(str(target_name or "").split())[:64]
                self.room = str(room or "")
                self.log = []
                reader = self._reader
            reader.start()
            self._note(f"starting for {self.target_name or target}")
            return {"ok": True, **self.status()}

    def _stop_process(self, process: subprocess.Popen, wait_s: float = 30) -> None:
        try:
            if process.stdin and process.poll() is None:
                process.stdin.write("quit\n")
                process.stdin.flush()
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            process.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def stop(self) -> dict:
        with self._lifecycle:
            with self._lock:
                process = self._process
            if process is not None and process.poll() is None:
                self._stop_process(process)
                self._note("stopped")
            with self._lock:
                self._process = None
                self._reader = None
                self.target_account = ""
                self.target_name = ""
                self.room = ""
                self._remove_audio_file()
            return {"ok": True, **self.status()}

    def action(self, action: str, target_name: str = "", item_id: str = "") -> dict:
        if action not in ACTION_COMMANDS:
            return {"ok": False, "error": "Unknown bot action"}
        if action in MEDIA_ACTIONS and not (self._audio_ready or BAD_APPLE_VIDEO.is_file()):
            return {"ok": False, "error": "Upload an MP3 in the Bot tab to enable Bad Apple"}
        command = command_for(action, target_name, item_id)
        if command is None:
            message = "Choose an item from the confirmed item list" if action in ITEM_ACTIONS \
                else "Choose a player for this action"
            return {"ok": False, "error": message}
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return {"ok": False, "error": "Hook the bot to an online player first"}
            try:
                if process.stdin is None:
                    return {"ok": False, "error": "The bot command input is unavailable"}
                process.stdin.write(command + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return {"ok": False, "error": "The bot stopped before the action could be sent"}
        return {"ok": True, "queued": True}

    def status(self) -> dict:
        available, reason = self.available()
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "enabled": bool(BOT_KEY),
                "available": available,
                "reason": reason,
                "running": running,
                "state": "running" if running else ("error" if self.error else "stopped"),
                "error": self.error,
                "uptime": round(time.time() - self.started_at, 1) if running else 0,
                "target": self.target_account,
                "targetName": self.target_name,
                "room": self.room,
                "badAppleAvailable": self._audio_ready or BAD_APPLE_VIDEO.is_file(),
                "log": list(self.log[-60:]),
            }


manager = BotManager()
