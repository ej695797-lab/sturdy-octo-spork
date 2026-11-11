"""Own the editor-controlled Photon bot process and its fixed action surface."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from bot.actions import (ACTION_COMMANDS, MEDIA_ACTIONS, PIXEL_ART_ACTIONS, SOUNDKEY_ACTIONS,
                         command_for)
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
        self._pixel_art_path = ""
        self._pixel_art_ready = False
        self._soundkey_path = ""
        self._soundkey_ready = False
        self._view_temp: tempfile.TemporaryDirectory | None = None
        self._view_dir = ""

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
            with self._lock:
                if self._process is process:
                    self._remove_view_dir()

    def _new_media_file(self, data: bytes = b"", ext: str = "mp3", prefix: str = "yeeper-bad-apple-") -> str:
        suffix = "." + ext if ext in {"mp3", "mp4", "wav", "ogg", "m4a", "png"} else ".bin"
        with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False) as audio:
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

    def _remove_pixel_art_file(self) -> None:
        path, self._pixel_art_path = self._pixel_art_path, ""
        self._pixel_art_ready = False
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._note(f"couldn't remove temporary pixel-art image: {exc}")

    def _remove_soundkey_file(self) -> None:
        path, self._soundkey_path = self._soundkey_path, ""
        self._soundkey_ready = False
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._note(f"couldn't remove temporary SoundKey audio: {exc}")

    def _remove_view_dir(self) -> None:
        view_temp, self._view_temp = self._view_temp, None
        self._view_dir = ""
        if view_temp:
            try:
                view_temp.cleanup()
            except OSError as exc:
                self._note(f"couldn't remove temporary bot map data: {exc}")

    def set_media(self, data: bytes, ext: str = "mp3") -> bool:
        with self._lock:
            if self._audio_path:
                expected_suffix = ".mp4" if ext == "mp4" else ".mp3"
                if Path(self._audio_path).suffix != expected_suffix:
                    self._note("uploaded media type changed; stop and restart the bot to load it")
                    return True
                self._audio_ready = True
                with open(self._audio_path, "wb") as audio:
                    audio.write(data)
        return False

    def set_pixel_art(self, data: bytes) -> None:
        with self._lock:
            if self._pixel_art_path:
                with open(self._pixel_art_path, "wb") as image:
                    image.write(data)
            self._pixel_art_ready = bool(data)

    def set_soundkey(self, data: bytes, ext: str = "mp3") -> bool:
        with self._lock:
            if self._soundkey_path:
                expected_suffix = "." + ext
                if Path(self._soundkey_path).suffix != expected_suffix:
                    self._note("uploaded SoundKey audio type changed; stop and restart the bot to load it")
                    return True
                with open(self._soundkey_path, "wb") as audio:
                    audio.write(data)
            self._soundkey_ready = bool(data)
        return False

    def start(self, *, target: str, target_name: str, room: str,
              media_data: bytes = b"", media_ext: str = "mp3",
              pixel_art_data: bytes = b"", soundkey_data: bytes = b"",
              soundkey_ext: str = "mp3") -> dict:
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
            self._remove_pixel_art_file()
            self._remove_soundkey_file()
            self._remove_view_dir()
            try:
                self._audio_path = self._new_media_file(media_data, media_ext)
                self._audio_ready = bool(media_data)
                self._pixel_art_path = (self._new_media_file(pixel_art_data, "png", "yeeper-pixel-art-")
                                        if pixel_art_data else "")
                self._pixel_art_ready = bool(pixel_art_data)
                self._soundkey_path = (self._new_media_file(soundkey_data, soundkey_ext, "yeeper-soundkey-")
                                       if soundkey_data else "")
                self._soundkey_ready = bool(soundkey_data)
                self._view_temp = tempfile.TemporaryDirectory(prefix="yeeper-bot-view-")
                self._view_dir = self._view_temp.name
            except OSError as exc:
                self._remove_audio_file()
                self._remove_pixel_art_file()
                self._remove_soundkey_file()
                self._remove_view_dir()
                with self._lock:
                    self.error = f"couldn't prepare bot media: {exc}"
                return {"ok": False, **self.status()}

            env = os.environ.copy()
            env.pop("YEEPS_VIEW_DIR", None)
            env.pop("YEEPS_TALK_PORT", None)
            env.pop("YEEPS_BOT_ENV", None)
            env.pop("YEEPS_LOOK_ACCT", None)
            env.pop("AUTH", None)
            env["YEEPS_VIEW_DIR"] = self._view_dir
            env["PYTHONPATH"] = str(SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["YEEPS_BAD_APPLE_MEDIA"] = self._audio_path
            env["YEEPS_PIXEL_ART_IMAGE"] = self._pixel_art_path
            env["YEEPS_SOUNDKEY_AUDIO"] = self._soundkey_path
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
                self._remove_pixel_art_file()
                self._remove_soundkey_file()
                self._remove_view_dir()
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
                self._remove_pixel_art_file()
                self._remove_soundkey_file()
                self._remove_view_dir()
            return {"ok": True, **self.status()}

    def map_snapshot(self, sid: str = "", rev: int = -1) -> dict:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            view_dir = self._view_dir
        if not running or not view_dir:
            return {"available": False, "blocks": [], "players": []}

        view_path = Path(view_dir) / "view.json"
        blocks_path = Path(view_dir) / "view_blocks.json"
        try:
            view = json.loads(view_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"available": False, "blocks": [], "players": []}
        try:
            block_view = json.loads(blocks_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            block_view = {}

        blocks = block_view.get("blocks", [])
        blocks_sid = str(block_view.get("sid") or "")
        blocks_rev = int(block_view.get("rev", -1))
        view_sid = str(view.get("sid") or "")
        view_rev = int(view.get("rev", -1))
        include_blocks = (sid != view_sid or rev != blocks_rev or blocks_sid != view_sid
                          or blocks_rev != view_rev)
        players = []
        for player in view.get("players", []):
            players.append({
                key: player[key] for key in ("a", "n", "x", "y", "z", "yaw", "t", "me", "hidden")
                if key in player
            })
        return {
            "available": True,
            "sid": view_sid,
            "room": str(view.get("room") or ""),
            "rk": str(view.get("rk") or ""),
            "dims": view.get("dims") or [],
            "loaded": bool(view.get("loaded")),
            "rev": view_rev,
            "blocksRev": blocks_rev,
            "blocks": blocks if include_blocks else None,
            "players": players,
            "updatedAt": float(view.get("t") or 0),
        }

    def action(self, action: str, target_name: str = "", item_id: str = "") -> dict:
        if action not in ACTION_COMMANDS:
            return {"ok": False, "error": "Unknown bot action"}
        if action in MEDIA_ACTIONS and not (self._audio_ready or BAD_APPLE_VIDEO.is_file()):
            return {"ok": False, "error": "Upload an MP3 or MP4 in the Bot tab to enable Bad Apple"}
        if action in PIXEL_ART_ACTIONS and not self._pixel_art_ready:
            return {"ok": False, "error": "Upload an image in the Bot tab to enable pixel art"}
        if action in SOUNDKEY_ACTIONS and not self._soundkey_ready:
            return {"ok": False, "error": "Upload audio in the Bot tab to enable SoundKey playback"}
        command = command_for(action, target_name)
        if command is None:
            return {"ok": False, "error": "Choose a player for this action"}
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
                "badAppleType": Path(self._audio_path).suffix.lstrip(".") if self._audio_ready else "",
                "pixelArtAvailable": self._pixel_art_ready,
                "soundKeyAvailable": self._soundkey_ready,
                "soundKeyType": Path(self._soundkey_path).suffix.lstrip(".") if self._soundkey_ready else "",
                "log": list(self.log[-60:]),
            }


manager = BotManager()
