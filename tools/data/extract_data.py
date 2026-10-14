#!/usr/bin/env python3
"""Copy the game data files out of an APK so the tools in tools/data can edit them.

    python tools/data/extract_data.py Yeeps-1.31.0-blaze-kit1.apk data

Writes data/data.unity3d (all game objects: blocks, materials, ...) and
data/sharedassets1.resource (audio). After editing, put them back with
tools/configure_apk.py ... --data data
"""
import os
import sys
import zipfile

FILES = ("assets/bin/Data/data.unity3d", "assets/bin/Data/sharedassets1.resource")


def main(apk: str, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(apk) as z:
        for name in FILES:
            dest = os.path.join(out, os.path.basename(name))
            with z.open(name) as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            print(f"{dest}  ({os.path.getsize(dest) // (1 << 20)} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python tools/data/extract_data.py <apk> <output folder>")
    main(sys.argv[1], sys.argv[2])
