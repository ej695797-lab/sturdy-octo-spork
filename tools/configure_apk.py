#!/usr/bin/env python3
"""Make a playable APK for YOUR server from the release template APK.

The template APK (GitHub release) has no server or Photon app set. This writes
your settings into it (lib/arm64-v8a/libblazeconfig.so, a small text file the
game's mod library reads at startup), then re-aligns and signs it with your own
key. Hand the output APK to your players; they install it like any APK.

    python tools/configure_apk.py --apk Yeeps-1.31.0-blaze-kit1.apk \
        --server https://your-name.trycloudflare.com \
        --photon-app-id 11111111-2222-3333-4444-555555555555 \
        --photon-voice-app-id 66666666-7777-8888-9999-000000000000

Needs Java (keytool) and Android build-tools (zipalign, apksigner) on PATH or
under $ANDROID_HOME/build-tools/<version>/. The first run creates a signing key
in tools/.keys/ (keep it: a later APK signed with a different key can't be
installed over this one without uninstalling first).
"""
import argparse
import glob
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile

CONFIG_ENTRY = "lib/arm64-v8a/libblazeconfig.so"
LIB_ENTRY = "lib/arm64-v8a/libBNMExplorer.so"
DATA_ENTRIES = {"assets/bin/Data/data.unity3d": "data.unity3d",
                "assets/bin/Data/sharedassets1.resource": "sharedassets1.resource"}
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_DIR = os.path.join(HERE, ".keys")
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def find_tool(name: str) -> str:
    exe = name + (".bat" if os.name == "nt" and name == "apksigner" else ".exe" if os.name == "nt" else "")
    hit = shutil.which(name) or shutil.which(exe)
    if hit:
        return hit
    for root in filter(None, [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]):
        cands = sorted(glob.glob(os.path.join(root, "build-tools", "*", exe)))
        if cands:
            return cands[-1]
    sys.exit(f"Can't find {name}. Install Android build-tools and put them on PATH "
             f"(or set ANDROID_HOME).")


def settings_text(a) -> str:
    return ("# Yeeps 1.31.0-blaze self-host settings (written by tools/configure_apk.py)\n"
            f"server={a.server}\n"
            f"photon_app_id={a.photon_app_id}\n"
            f"photon_voice_app_id={a.photon_voice_app_id}\n"
            f"photon_region={a.photon_region}\n"
            f"discord={a.discord}\n")


def ensure_key(path: str, password: str) -> None:
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keytool = shutil.which("keytool") or sys.exit("Can't find keytool (it comes with Java). Install a JDK.")
    subprocess.run([keytool, "-genkeypair", "-keystore", path, "-alias", "yeeps", "-keyalg", "RSA",
                    "-keysize", "2048", "-validity", "10000", "-storepass", password, "-keypass", password,
                    "-dname", "CN=Yeeps self-host"], check=True)
    print(f"created signing key {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", required=True, help="the release template APK")
    ap.add_argument("--server", required=True, help="your server's base URL (https://... or http://LAN-IP:8000)")
    ap.add_argument("--photon-app-id", required=True, help="your Photon PUN app id")
    ap.add_argument("--photon-voice-app-id", required=True, help="your Photon Voice app id")
    ap.add_argument("--photon-region", default="", help="optional fixed Photon region (us, usw, eu, ...)")
    ap.add_argument("--discord", default="https://discord.gg/twD66vfbeA", help="link for the news-board Discord button")
    ap.add_argument("--out", default="", help="output APK (default: Yeeps-1.31.0-blaze-<host>.apk)")
    ap.add_argument("--keystore", default=os.path.join(KEY_DIR, "yeeps-selfhost.keystore"))
    ap.add_argument("--data", default="", help="also put back edited game data from this folder "
                                               "(data.unity3d / sharedassets1.resource, see tools/data/)")
    ap.add_argument("--lib", default="", help="also replace the mod library with this libBNMExplorer.so "
                                              "(built from client/native, see CLAUDE.md)")
    ap.add_argument("--settings-only", action="store_true",
                    help="only print the settings file (for the on-headset override), don't build")
    a = ap.parse_args()

    a.server = a.server.strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+(:\d+)?$", a.server):
        sys.exit("--server must look like https://host or http://192.168.1.20:8000 (no path)")
    for name in ("photon_app_id", "photon_voice_app_id"):
        if not GUID.match(getattr(a, name)):
            sys.exit(f"--{name.replace('_', '-')} must be a Photon app id like 1234abcd-...-...")
    text = settings_text(a)
    if a.settings_only:
        print(text)
        return

    host = re.sub(r"[^A-Za-z0-9.-]", "_", a.server.split("://", 1)[1])
    out = a.out or f"Yeeps-1.31.0-blaze-{host}.apk"
    pw_file = a.keystore + ".pass"
    if os.path.exists(pw_file):
        password = open(pw_file).read().strip()
    else:
        password = secrets.token_urlsafe(18)
    ensure_key(a.keystore, password)
    if not os.path.exists(pw_file):
        with open(pw_file, "w") as f:
            f.write(password)

    zipalign, apksigner = find_tool("zipalign"), find_tool("apksigner")
    # Work next to the output (two ~380 MB files): the system temp folder can be small.
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(out)) or ".") as tmp:
        raw = os.path.join(tmp, "raw.apk")
        aligned = os.path.join(tmp, "aligned.apk")
        print("writing settings into the APK (takes a minute: the APK is ~380 MB)...")
        with zipfile.ZipFile(a.apk) as zin, zipfile.ZipFile(raw, "w") as zout:
            if CONFIG_ENTRY not in zin.namelist():
                sys.exit(f"{a.apk} has no {CONFIG_ENTRY}: use the kit release APK")
            for item in zin.infolist():
                n = item.filename
                if n.startswith("META-INF/") and n.rsplit(".", 1)[-1].upper() in ("SF", "RSA", "DSA", "EC", "MF"):
                    continue                       # old signature, re-signed below
                if n == CONFIG_ENTRY:
                    data = text.encode()
                elif n == LIB_ENTRY and a.lib:
                    data = open(a.lib, "rb").read()
                elif n in DATA_ENTRIES and a.data and os.path.exists(os.path.join(a.data, DATA_ENTRIES[n])):
                    data = open(os.path.join(a.data, DATA_ENTRIES[n]), "rb").read()
                    print(f"  using edited {DATA_ENTRIES[n]}")
                else:
                    data = zin.read(item)
                zout.writestr(item, data, compress_type=item.compress_type)
        subprocess.run([zipalign, "-p", "-f", "4", raw, aligned], check=True)
        subprocess.run([apksigner, "sign", "--ks", a.keystore, "--ks-pass", f"pass:{password}",
                        "--out", out, aligned], check=True)
    print(f"done: {out}")
    print("Install it on each headset (SideQuest, or: adb install -r " + out + ")")


if __name__ == "__main__":
    main()
