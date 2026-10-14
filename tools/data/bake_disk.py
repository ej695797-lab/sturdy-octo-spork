#!/usr/bin/env python3
"""Bake your own music into one of the game's record disks, permanently, in the APK.

(Easier, no APK rebuild: the editor's Disks tab swaps a record's music for
everyone from the server. Use this tool only to ship the music inside the APK.)

Works on a folder holding the game's data.unity3d and sharedassets1.resource
(get them with tools/data/extract_data.py). The audio is converted to mono
16-bit PCM (FSB5) and appended to sharedassets1.resource, and the record's
AudioClip is pointed at it. Optionally the record's label art gets a picture.

    python tools/data/bake_disk.py data --list                       # record clips + art names
    python tools/data/bake_disk.py data --clip SuburbTheme --audio song.mp3 \\
        [--art recordArtTexture_suburbTheme --label cover.png [--crop x0,y0,x1,y1]]

Then put both files back into an APK: tools/configure_apk.py ... --data data
"""
import argparse
import os
import struct
import subprocess

import UnityPy
import imageio_ffmpeg
from PIL import Image

AR = 44100
FREQ_IDX = {8000: 1, 11000: 2, 11025: 3, 16000: 4, 22050: 5, 24000: 6, 32000: 7, 44100: 8, 48000: 9}[AR]


def list_records(data_dir: str) -> None:
    env = UnityPy.load(os.path.join(data_dir, "data.unity3d"))
    clips, arts = [], []
    for o in env.objects:
        if o.assets_file.name != "sharedassets1.assets":
            continue
        if o.type.name == "AudioClip":
            clips.append(o.read().m_Name)
        elif o.type.name == "Texture2D":
            n = o.read().m_Name
            if n.startswith("recordArtTexture_"):
                arts.append(n)
    print("AudioClips (use with --clip):")
    print("  " + ", ".join(sorted(clips)))
    print("Record label art (use with --art):")
    print("  " + ", ".join(sorted(arts)))


def bake(data_dir, clip_name, audio_src, tex_name=None, label=None, crop=None) -> None:
    pcm = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                          "-i", audio_src, "-vn", "-ac", "1", "-ar", str(AR),
                          "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    n = len(pcm) // 2
    pcm = pcm[:2 * n]
    v = (0) | (FREQ_IDX << 1) | (0 << 5) | (0 << 6) | (n << 34)          # mono, data offset 0
    fsb = (b"FSB5" + struct.pack("<6I", 1, 1, 36, 0, len(pcm), 2) + b"\0" * 32
           + struct.pack("<Q", v) + b"\0" * 28 + pcm)
    assert len(fsb) == 96 + len(pcm)

    env = UnityPy.load(os.path.join(data_dir, "data.unity3d"))
    targets = [o for o in env.objects if o.type.name == "AudioClip" and o.read().m_Name == clip_name]
    if not targets:
        raise SystemExit(f"no AudioClip named {clip_name!r} (see --list)")

    res = os.path.join(data_dir, "sharedassets1.resource")
    size = os.path.getsize(res)
    pad = (-size) % 32
    with open(res, "ab") as f:
        f.write(b"\0" * pad)
        off = size + pad
        f.write(fsb)
    print(f"audio: {n} samples ({n / AR:.1f}s), {len(fsb)} bytes at offset {off}")

    done = set()
    for o in targets:
        t = o.read_typetree()
        t.update(m_Channels=1, m_Frequency=AR, m_BitsPerSample=16, m_Length=float(n / AR),
                 m_LoadInBackground=True, m_CompressionFormat=0)
        t["m_Resource"].update(m_Offset=off, m_Size=len(fsb))
        o.save_typetree(t)
        done.add("clip")
    if label:
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            d = o.read()
            if d.m_Name != tex_name:
                continue
            base = d.image.convert("RGB")                       # small atlas, upright
            k = max(1, 256 // base.width)
            atlas = base.resize((base.width * k, base.height * k), Image.NEAREST)
            pic = Image.open(label).convert("RGB")
            if crop:
                pic = pic.crop(crop)
            q = atlas.width // 2                                # label = top-left quadrant
            atlas.paste(pic.resize((q, q), Image.LANCZOS), (0, 0))
            d.image = atlas
            d.save()
            done.add("art")
        if "art" not in done:
            raise SystemExit(f"no Texture2D named {tex_name!r} (see --list)")
    open(os.path.join(data_dir, "data.unity3d"), "wb").write(env.file.save(packer="lz4"))
    print("updated:", ", ".join(sorted(done)), "- saved data.unity3d and sharedassets1.resource")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bake music into a record disk in the game data.")
    ap.add_argument("data_dir", help="folder with data.unity3d + sharedassets1.resource")
    ap.add_argument("--list", action="store_true", help="list record AudioClips and label art names")
    ap.add_argument("--clip", help="AudioClip to replace (see --list), e.g. SuburbTheme")
    ap.add_argument("--audio", help="your audio file (mp3/ogg/wav/...)")
    ap.add_argument("--art", help="record label Texture2D to change (optional, see --list)")
    ap.add_argument("--label", help="picture for the record label (needs --art)")
    ap.add_argument("--crop", help="crop the picture first: x0,y0,x1,y1")
    a = ap.parse_args()
    if a.list:
        list_records(a.data_dir)
    else:
        if not (a.clip and a.audio):
            ap.error("--clip and --audio are required (or use --list)")
        if a.label and not a.art:
            ap.error("--label needs --art")
        crop = tuple(int(x) for x in a.crop.split(",")) if a.crop else None
        bake(a.data_dir, a.clip, a.audio, a.art, a.label, crop)
