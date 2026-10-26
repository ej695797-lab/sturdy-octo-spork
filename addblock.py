#!/usr/bin/env python3
"""920Man block workshop: add custom blocks to the Yeeps 1.31 game data from
an interactive menu.  Run it from anywhere:

    python tools\\data\\addblock.py

Menu:
    1  picture block            texture on a cotton cube (any AxB key)
    2  basic mesh block         paste a path to a Unity asset bundle
                                (.bundle) and its WHOLE prefab is imported --
                                every mesh, material and texture -- or just
                                Enter for a plain cube
    3  music disk               flat disk that plays your song on a loop
    4  custom block             mix and match:
         1 animation + mesh          2 audio + mesh
         3 texture + mesh            4 texture + animation + mesh
         5 texture + audio + animation + mesh
    5  remove a block           takes it out of the menu / container again
    6  list my blocks           what this tool has added
    7  build the APK            packs the edited data into a signed APK

A .bundle import keeps the prefab's own look: each renderer becomes a submesh
with its own material (the bundle's _MainTex, or its _BaseColor if it has no
texture), remapped onto the game's mapobject shaders.  Bundles are the files a
Unity project writes to GeneratedAssetBundles.  A texture you pick is ignored
for bundle blocks (the bundle carries its own).  When the bundle carries an
AudioClip it is used automatically for the block's sound; its AnimationClips
are detected and reported, and a spin/bob is used in their place.

Every add updates the pattern list, the block container, the two
globals.json lists and role_locks.ALWAYS_OWNED_PATTERNS (with .bak backups),
and remembers everything in tools/data/addblock_state.json so remove works.
After adding, run menu 7 (or tools/configure_apk.py by hand) and install the
APK on the headset.  Redeploy the server after globals.json changes.

Audio blocks store their .ogg at the end of sharedassets1.resource (the same
file the dancing-skeleton music lives in); remove truncates it when the block
is the last one appended.
"""

import copy
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

import UnityPy
from PIL import Image, ImageDraw, ImageFont
from UnityPy.enums import TextureFormat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_picture_block import list_count_offset, picture, s  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DATA = os.path.join(HERE, "data", "data.unity3d")
RES = os.path.join(HERE, "data", "sharedassets1.resource")
STATE_PATH = os.path.join(HERE, "addblock_state.json")
GLOBALS = os.path.join(ROOT, "server", "seed", "world", "globals.json")
ROLE_LOCKS = os.path.join(ROOT, "server", "services", "role_locks.py")

YEEP_PATH = "mapobjects/decoration/1x4x2_forbidden_textyeep"
PILLOW_KEY = b"1x1_stuffed"
PICTURE_MATERIAL_PID = 54
STUFFED_MATERIAL_PID = 110
TEXTURE_TEMPLATE = "discordIcon"
OLD_GO, OLD_MB, OLD_BC = 132695, 132697, 132700
PREF_CLIP, PREF_AUDIO, PREF_ANIM = 53109, 53126, 53021
SPIN_PERIOD, BOB_PERIOD = 2.0, 1.2
# Mesh reduction (grid vertex clustering).  On: a 3.1M-vertex mesh alone is
# ~220MB of vertex+index data, which exceeds the Quest's ~460MB GPU budget and
# gets the game OOM-killed (signal 9) partway through loading.  Bundles that are
# already light are left alone, so nothing small loses detail.
REDUCE_TARGET_TRIS, REDUCE_OVER_TRIS = 150_000, 200_000
MAX_VERTS = 4_000_000
# Bundle textures larger than this are shrunk on import - a 2048 body map is
# wasted on a 2m block and would add ~17MB to the game data each time.
TEX_MAX = 1024
NULL = {"m_FileID": 0, "m_PathID": 0}
ZERO3 = {"x": 0.0, "y": 0.0, "z": 0.0}
IDENT_Q = {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
KEY_RE = re.compile(r"[0-9]+x[0-9]+(?:x[0-9]+)?_[A-Za-z0-9]+")
_PREFAB_CACHE = {}
ALLOWED_EXTERNALS = {"resources.assets", "globalgamemanagers.assets",
                     "unity_builtin_extra", "unity default resources",
                     "sharedassets0.assets"}
BUILD_CMD = [sys.executable, os.path.join(ROOT, "tools", "configure_apk.py"),
             "--apk", os.path.join(ROOT, "Yeeps-1.31.0-yeeper920.apk"),
             "--server", "https://yeeper920-yeeps.onrender.com",
             "--photon-app-id", "a279e749-3e24-464c-9336-f3a412bdfdbc",
             "--photon-voice-app-id", "2d9a7f4b-96b8-4305-b87f-0ea0aa722d1f",
             "--data", os.path.join(ROOT, "tools", "data", "data"),
             "--lib", os.path.join(ROOT, "build", "native", "libBNMExplorer.so"),
             "--out", os.path.join(ROOT, "tools", "Yeeps-1.31.0-yeeper920.apk")]


# ----------------------------------------------------------------- helpers
def clone(f, reader, pid):
    new = copy.copy(reader)
    new.path_id = pid
    f.objects[pid] = new
    return new


def pptr(pid):
    return {"m_FileID": 0, "m_PathID": pid}


def xptr(file_id, pid):
    return {"m_FileID": file_id, "m_PathID": pid}


def parse_size(key):
    parts = key.split("_", 1)[0].lower().split("x")
    if len(parts) == 2:
        return (int(parts[0]), 1, int(parts[1]))
    if len(parts) == 3:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    raise SystemExit(f'cannot read a size from key "{key}"')


def template(f, typ, prefer=None):
    if prefer is not None and prefer in f.objects and f.objects[prefer].type.name == typ:
        return f.objects[prefer]
    return next(o for o in f.objects.values() if o.type.name == typ)


def files(env):
    return {a.assets_file.name: a.assets_file for a in env.objects}


def state_load():
    return json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}


def state_save(st):
    json.dump(st, open(STATE_PATH, "w"), indent=1)


def backup(path):
    import shutil
    bak = path + ".pre-addblock"
    if not os.path.exists(bak):
        shutil.copyfile(path, bak)
    return bak


# ---------------------------------------------------------------- geometry
def _quad(verts, idx, ps, n, uvs):
    base = len(verts)
    for p, uv in zip(ps, uvs):
        verts.append((p, n, (1.0, 1.0, 1.0, 1.0), uv))
    a, b, c, d = base, base + 1, base + 2, base + 3
    u = [ps[1][i] - ps[0][i] for i in range(3)]
    v = [ps[2][i] - ps[0][i] for i in range(3)]
    cr = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
          u[0] * v[1] - u[1] * v[0])
    if sum(cr[i] * n[i] for i in range(3)) > 0:
        idx.extend([a, b, c, a, c, d])
    else:
        idx.extend([a, c, b, a, d, c])


def build_cube(size, textured):
    hx, hy, hz = size[0] * 0.25, size[1] * 0.25, size[2] * 0.25
    verts, idx = [], []
    if textured:
        for _ in range(3):
            verts.append(((0.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                          (1.0, 1.0, 1.0, 1.0), (0.5, 0.5)))
    up = [(0, 0), (1, 0), (1, 1), (0, 1)] if textured else [(0.5, 0.5)] * 4
    _quad(verts, idx, [(hx, -hy, hz), (-hx, -hy, hz), (-hx, hy, hz), (hx, hy, hz)], (0, 0, 1), up)
    _quad(verts, idx, [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz)], (0, 0, -1), up)
    _quad(verts, idx, [(hx, -hy, -hz), (hx, -hy, hz), (hx, hy, hz), (hx, hy, -hz)], (1, 0, 0), up)
    _quad(verts, idx, [(-hx, -hy, hz), (-hx, -hy, -hz), (-hx, hy, -hz), (-hx, hy, hz)], (-1, 0, 0), up)
    _quad(verts, idx, [(-hx, hy, -hz), (hx, hy, -hz), (hx, hy, hz), (-hx, hy, hz)], (0, 1, 0), up)
    _quad(verts, idx, [(-hx, -hy, hz), (hx, -hy, hz), (hx, -hy, -hz), (-hx, -hy, -hz)], (0, -1, 0), up)
    return _finish(verts, idx, textured, (hx, hy, hz))


def build_disk(size, textured):
    r = max(size[0], size[2]) * 0.25
    t = min(size[1] * 0.1, 0.04)
    n = 16
    verts, idx = [], []
    if textured:
        for _ in range(3):
            verts.append(((0.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                          (1.0, 1.0, 1.0, 1.0), (0.5, 0.5)))
    uv = None if textured else [(0.5, 0.5)] * 4
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        c0, s0_, c1, s1_ = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        nrm = ((c0 + c1) / 2, 0.0, (s0_ + s1_) / 2)
        ln = math.hypot(nrm[0], nrm[2]) or 1.0
        nrm = (nrm[0] / ln, 0.0, nrm[2] / ln)
        quv = ([(i / n, 0), ((i + 1) / n, 0), ((i + 1) / n, 1), (i / n, 1)]
               if uv is None else uv)
        _quad(verts, idx,
              [(c0 * r, -t, s0_ * r), (c1 * r, -t, s1_ * r),
               (c1 * r, t, s1_ * r), (c0 * r, t, s0_ * r)], nrm, quv)
    for sign in (1.0, -1.0):
        base = len(verts)
        for i in range(n):
            a = 2 * math.pi * i / n
            p = (math.cos(a) * r, sign * t, math.sin(a) * r)
            tuv = ((0.5 + math.cos(a) / 2, 0.5 + math.sin(a) / 2)
                   if uv is None else (0.5, 0.5))
            verts.append((p, (0.0, sign, 0.0), (1.0, 1.0, 1.0, 1.0), tuv))
        centre = len(verts)
        verts.append(((0.0, sign * t, 0.0), (0.0, sign, 0.0),
                      (1.0, 1.0, 1.0, 1.0), (0.5, 0.5)))
        for i in range(n):
            a, b = base + i, base + (i + 1) % n
            idx.extend([centre, b, a] if sign > 0 else [centre, a, b])
    return _finish(verts, idx, textured, (r, t, r))


def build_bundle(bundle_path, scale, textured):
    from add_mesh_block import build_geometry, parse_obj
    if not textured:
        blob, idx, vcount, ext = build_geometry(bundle_path, scale)
        return blob, None, idx, vcount, ext
    env = UnityPy.load(bundle_path)
    mesh_obj = next((o for o in env.objects if o.type.name == "Mesh"), None)
    if mesh_obj is None:
        raise SystemExit(f"no Mesh in {bundle_path!r}")
    verts, norms, corners = parse_obj(mesh_obj.read().export())
    if not verts or not corners:
        raise SystemExit(f"no geometry in {bundle_path}")
    xs, ys, zs = [v[0] for v in verts], [v[1] for v in verts], [v[2] for v in verts]
    lo, hi = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    centre = tuple((lo[i] + hi[i]) / 2 for i in range(3))
    half = tuple((hi[i] - lo[i]) / 2 for i in range(3))
    k = (scale / 2.0) / (max(half) or 1.0)
    blob = bytearray()
    for _ in range(3):
        blob += struct.pack("<3f3f4f2f", 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 0.5, 0.5)
    h = scale / 2.0
    for vi, ni in corners:
        p = verts[vi]
        n = norms[ni] if 0 <= ni < len(norms) else (0.0, 1.0, 0.0)
        pos = tuple((p[i] - centre[i]) * k for i in range(3))
        an = tuple(abs(n[i]) for i in range(3))
        ax = an.index(max(an))
        o1, o2 = [i for i in range(3) if i != ax]
        tu = min(1.0, max(0.0, (pos[o1] / h + 1) / 2))
        tv = min(1.0, max(0.0, (pos[o2] / h + 1) / 2))
        blob += struct.pack("<3f3f4f2f", *pos, *n, 1.0, 1.0, 1.0, 1.0, tu, tv)
    vcount = len(corners) + 3
    ext = {"x": round(half[0] * k, 6), "y": round(half[1] * k, 6), "z": round(half[2] * k, 6)}
    return bytes(blob), [0, 1, 2], list(range(3, vcount)), vcount, ext


def _finish(verts, idx, textured, ext):
    blob = bytearray()
    for p, n, col, uv in verts:
        blob += struct.pack("<3f3f4f2f", *p, *n, *col, *uv)
    if textured:
        return bytes(blob), [0, 1, 2], idx, len(verts), ext
    return bytes(blob), None, idx, len(verts), ext


def geo_for(key_size, mesh_choice, textured):
    if mesh_choice == "disk":
        return build_disk(key_size, textured)
    if mesh_choice and mesh_choice != "cube":
        return build_bundle(mesh_choice, max(key_size) * 0.5, textured)
    return build_cube(key_size, textured)


def _write_mesh(f, tmpl_pid, target_pid, key, blob, allidx, subs, vcount, ext):
    """Clone a game mesh template to target_pid with new vertex data and one
    submesh per (firstVertex, vertexCount, firstIndex, indexCount) in `subs`."""
    mesh_src = f.objects[tmpl_pid]
    mt = mesh_src.read_typetree()
    if isinstance(ext, (tuple, list)):
        ext = {"x": ext[0], "y": ext[1], "z": ext[2]}
    mt["m_Name"] = key
    mt["m_VertexData"]["m_VertexCount"] = vcount
    mt["m_VertexData"]["m_DataSize"] = blob
    big = vcount > 0xFFFF
    if "m_IndexFormat" in mt:
        mt["m_IndexFormat"] = 1 if big else 0
    isz = 4 if big else 2
    ib = struct.pack(f"<{len(allidx)}{'I' if big else 'H'}", *allidx)
    mt["m_IndexBuffer"] = list(ib) if isinstance(mt["m_IndexBuffer"], list) else ib
    aabb = {"m_Center": {"x": 0.0, "y": 0.0, "z": 0.0}, "m_Extent": ext}
    base = mt["m_SubMeshes"][0]
    mt["m_SubMeshes"] = [
        {**base, "firstByte": fi * isz, "indexCount": ic, "baseVertex": 0,
         "firstVertex": fv, "vertexCount": vc, "localAABB": aabb}
        for (fv, vc, fi, ic) in subs]
    mt["m_LocalAABB"] = aabb
    for k in ("m_BakedConvexCollisionMesh", "m_BakedTriangleCollisionMesh"):
        if k in mt:
            mt[k] = b"" if isinstance(mt[k], (bytes, bytearray)) else []
    clone(f, mesh_src, target_pid).save_typetree(mt)


def write_mesh(f, tmpl_pid, target_pid, key, blob, idx0, idx, vcount, ext):
    """Single-piece mesh: an optional hidden degenerate submesh 0 (idx0 = [0,1,2])
    then the geometry (idx) as submesh 1, matching the game's own blocks."""
    allidx = (idx0 + idx) if idx0 else idx
    if idx0:
        subs = [(0, 3, 0, len(idx0)),
                (3, vcount - 3, len(idx0), len(idx))]
    else:
        subs = [(0, vcount, 0, len(idx))]
    _write_mesh(f, tmpl_pid, target_pid, key, blob, allidx, subs, vcount, ext)



def add_texture(res, key, image_path, start):
    """Picture texture + sign material in resources.assets; next free pid."""
    tmpl = next(o for o in res.objects.values()
                if o.type.name == "Texture2D" and o.read().m_Name == TEXTURE_TEMPLATE)
    tex = clone(res, tmpl, start).read()
    tex.m_Name = key
    tex.set_image(picture(image_path), target_format=TextureFormat.RGBA32, mipmap_count=1)
    tex.save()
    pic_src = res.objects[PICTURE_MATERIAL_PID]
    mat = pic_src.read_typetree()
    mat["m_Name"] = key
    mat["m_SavedProperties"]["m_TexEnvs"] = [
        (k, {**v, "m_Texture": pptr(start)}) if k == "_MainTex" else (k, v)
        for k, v in mat["m_SavedProperties"]["m_TexEnvs"]]
    clone(res, pic_src, start + 1).save_typetree(mat)
    return start, start + 1, start + 2


# ---------------------------------------------------------------- prefab bundles
def _quat_matrix(q, p, s):
    x, y, z, w = q["x"], q["y"], q["z"], q["w"]
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    ax = "xyz"
    m = [[r[i][c] * s[ax[c]] for c in range(3)] + [p[ax[i]]] for i in range(3)]
    m.append([0.0, 0.0, 0.0, 1.0])
    return m


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def _xform(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3]
                 for i in range(3))


def _xform_dir(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2]
                 for i in range(3))


def _matrix_trs(m, sc=1.0):
    """Row-major 4x4 (from _quat_matrix) -> (position, quaternion, scale)
    with an extra uniform scale applied - used to place particle systems."""
    ns = [math.sqrt(sum(m[i][j] ** 2 for i in range(3))) or 1.0
          for j in range(3)]
    r = [[m[i][j] / ns[j] for j in range(3)] for i in range(3)]
    px, py, pz = m[0][3] * sc, m[1][3] * sc, m[2][3] * sc
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        qw, qx = 0.25 * s, (r[2][1] - r[1][2]) / s
        qy, qz = (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        qw, qx = (r[2][1] - r[1][2]) / s, 0.25 * s
        qy, qz = (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        qw, qx = (r[0][2] - r[2][0]) / s, (r[0][1] + r[1][0]) / s
        qy, qz = 0.25 * s, (r[1][2] + r[2][1]) / s
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        qw, qx = (r[1][0] - r[0][1]) / s, (r[0][2] + r[2][0]) / s
        qy, qz = (r[1][2] + r[2][1]) / s, 0.25 * s
    return ((px, py, pz), (qx, qy, qz, qw),
            (ns[0] * sc, ns[1] * sc, ns[2] * sc))


def _obj_geometry(text):
    """(positions, uvs, normals, corners) from UnityPy Mesh.export() OBJ text;
    each corner is (vertex index, uv index, normal index)."""
    pos, uv, nrm, corners = [], [], [], []
    for line in text.splitlines():
        if line.startswith("v "):
            _, x, y, z = line.split()[:4]
            pos.append((float(x), float(y), float(z)))
        elif line.startswith("vt "):
            f = line.split()
            uv.append((float(f[1]), float(f[2]) if len(f) > 2 else 0.0))
        elif line.startswith("vn "):
            _, x, y, z = line.split()[:4]
            nrm.append((float(x), float(y), float(z)))
        elif line.startswith("f "):
            poly = []
            for tok in line.split()[1:]:
                c = tok.split("/")
                vi = int(c[0]) - 1
                ti = int(c[1]) - 1 if len(c) > 1 and c[1] else -1
                ni = int(c[2]) - 1 if len(c) > 2 and c[2] else -1
                poly.append((vi, ti, ni))
            for i in range(1, len(poly) - 1):
                for cc in (poly[0], poly[i], poly[i + 1]):
                    corners.append(cc)
    return pos, uv, nrm, corners


def import_prefab(path):
    """Read a Unity asset bundle into pieces, each already placed in the
    bundle's own root space.  Returns {pieces, materials, textures, audio,
    clips}: audio is the bundle's first AudioClip decoded to WAV bytes (or
    None), clips maps each AnimationClip path id to its name, legacy flag and
    the object paths its curves drive."""
    ck = (os.path.abspath(path), os.path.getmtime(path), os.path.getsize(path))
    if ck in _PREFAB_CACHE:
        return _PREFAB_CACHE[ck]
    env = UnityPy.load(path)
    objs = list(env.objects)
    trs = {o.path_id: o.read_typetree() for o in objs if o.type.name == "Transform"}
    world = {}

    def world_of(pid, seen=()):
        if pid in world:
            return world[pid]
        t = trs.get(pid)
        if t is None:
            return None
        local = _quat_matrix(t["m_LocalRotation"], t["m_LocalPosition"], t["m_LocalScale"])
        father = t.get("m_Father", {}).get("m_PathID", 0)
        if father and father in trs and father not in seen:
            pm = world_of(father, seen + (pid,))
            m = _mat_mul(pm, local) if pm else local
        else:
            m = local
        world[pid] = m
        return m

    go_tr, go_mesh, go_mats = {}, {}, {}
    for o in objs:
        if o.type.name not in ("Transform", "MeshFilter", "MeshRenderer",
                               "SkinnedMeshRenderer"):
            continue
        t = o.read_typetree()
        go = t["m_GameObject"]["m_PathID"]
        if o.type.name == "Transform":
            go_tr[go] = o.path_id
        elif o.type.name == "MeshFilter":
            go_mesh[go] = t["m_Mesh"]["m_PathID"]
        elif o.type.name == "MeshRenderer":
            go_mats[go] = [m["m_PathID"] for m in t.get("m_Materials", [])]
        else:
            go_mesh[go] = t.get("m_Mesh", {}).get("m_PathID", 0)
            go_mats[go] = [m["m_PathID"] for m in t.get("m_Materials", [])]

    meshes = {o.path_id: o for o in objs if o.type.name == "Mesh"}
    mats = {o.path_id: o.read_typetree() for o in objs if o.type.name == "Material"}
    texs = {}
    for o in objs:
        if o.type.name == "Texture2D":
            try:
                img = o.read().image
            except Exception:
                img = None
            if img is not None:
                if img.width > TEX_MAX or img.height > TEX_MAX:
                    img.thumbnail((TEX_MAX, TEX_MAX), Image.LANCZOS)
                texs[o.path_id] = img

    pieces = []
    for go, mesh_pid in go_mesh.items():
        if not mesh_pid or mesh_pid not in meshes:
            continue
        m = world_of(go_tr.get(go, 0)) or [[1, 0, 0, 0], [0, 1, 0, 0],
                                           [0, 0, 1, 0], [0, 0, 0, 1]]
        p, u, n, corners = _obj_geometry(meshes[mesh_pid].read().export())
        if not corners:
            continue
        vlist = []
        for (vi, ti, ni) in corners:
            pos = _xform(m, p[vi]) if 0 <= vi < len(p) else (0.0, 0.0, 0.0)
            nn = _xform_dir(m, n[ni]) if 0 <= ni < len(n) else (0.0, 1.0, 0.0)
            uu = u[ti] if 0 <= ti < len(u) else (0.5, 0.5)
            vlist.append((pos, nn, uu))
        mat = (go_mats.get(go) or [0])[0]
        pieces.append({"verts": vlist, "mat": mat})
    if not pieces:
        raise SystemExit(f"no MeshFilter/SkinnedMeshRenderer meshes in {path!r}")
    go_ps, go_psr = {}, {}
    for o in objs:
        if o.type.name not in ("ParticleSystem", "ParticleSystemRenderer"):
            continue
        t = o.read_typetree()
        go = t["m_GameObject"]["m_PathID"]
        if o.type.name == "ParticleSystem":
            go_ps[go] = t
        else:
            go_psr[go] = t
    go_names = {}
    for o in objs:
        if o.type.name == "GameObject" and o.path_id in go_ps:
            go_names[o.path_id] = o.read_typetree().get("m_Name", "fx")
    particles = []
    for go, ps in go_ps.items():
        psr = go_psr.get(go)
        if psr is None:
            continue
        m = world_of(go_tr.get(go, 0))
        if m is None:
            continue
        particles.append({"name": go_names.get(go, "fx")[:60], "matrix": m,
                          "ps": ps, "psr": psr})
    audio = None
    for o in objs:
        if o.type.name == "AudioClip":
            try:
                samp = o.read().samples
            except Exception:
                samp = None
            if samp:
                nm, data = next(iter(samp.items()))
                audio = {"name": nm, "data": data}
                break
    clips = {}
    for o in objs:
        if o.type.name == "AnimationClip":
            try:
                t = o.read_typetree()
            except Exception:
                continue
            paths = set()
            for grp in ("m_PositionCurves", "m_FloatCurves", "m_EulerCurves",
                        "m_ScaleCurves"):
                for c in t.get(grp) or []:
                    paths.add(c.get("path"))
            clips[o.path_id] = {"name": t.get("m_Name"),
                                "legacy": t.get("m_Legacy"), "paths": paths}
    imp = {"pieces": pieces, "materials": mats, "textures": texs,
           "audio": audio, "clips": clips, "particles": particles}
    _PREFAB_CACHE[ck] = imp
    return imp


def _reduce_geometry(verts, allidx, subs, mats):
    """Grid vertex clustering: collapse vertices that fall in the same grid
    cell onto its centroid, then drop triangles that became degenerate.  Keeps
    the hidden submesh 0 and each submesh's material, so a very heavy bundle
    (a whole Minecraft map, say) shrinks to something the headset can draw.

    The cell key is position-only: a Minecraft-style mesh is made of isolated
    faces, so a key that also carried the normal/UV would keep every triangle
    alive and the mesh would never shrink.  Each cell keeps the first vertex's
    normal and UV, which preserves the hard voxel edges instead of averaging
    them away."""
    pts = [v[0] for v in verts[3:]]
    if not pts:
        return verts, allidx, subs, mats
    lo = [min(p[i] for p in pts) for i in range(3)]
    diag = math.sqrt(sum((max(p[i] for p in pts) - lo[i]) ** 2
                         for i in range(3))) or 1.0

    def tri_count(cell):
        inv = 1.0 / cell

        def cellkey(p):
            return (int((p[0] - lo[0]) * inv),
                    int((p[1] - lo[1]) * inv),
                    int((p[2] - lo[2]) * inv))

        keys = [cellkey(v[0]) for v in verts[3:]]
        live = 0
        for si, (fv, vc, fi, ic) in enumerate(subs):
            if si == 0:
                continue
            for a2 in range(fi, fi + ic, 3):
                k0 = keys[allidx[a2] - 3]
                k1 = keys[allidx[a2 + 1] - 3]
                k2 = keys[allidx[a2 + 2] - 3]
                if k0 != k1 and k1 != k2 and k0 != k2:
                    live += 1
        return live

    def cluster(want):
        # Grow the grid until the number of *surviving triangles* is near the
        # target, then really merge on that grid.
        cell_lo, cell_hi = diag / 1_000_000, diag
        for _ in range(24):
            mid = math.sqrt(cell_lo * cell_hi)
            if tri_count(mid) > want:
                cell_lo = mid
            else:
                cell_hi = mid
        inv = 1.0 / cell_hi

        def ckey(p):
            return (int((p[0] - lo[0]) * inv),
                    int((p[1] - lo[1]) * inv),
                    int((p[2] - lo[2]) * inv))

        new = [v for v in verts[:3]]
        cellmap, csum = {}, []
        remap = [0, 1, 2] + [0] * (len(verts) - 3)
        for i in range(3, len(verts)):
            p, n, c, u = verts[i]
            key = ckey(p)
            j = cellmap.get(key)
            if j is None:
                j = len(new)
                cellmap[key] = j
                new.append([p, n, c, u])
                csum.append([p[0], p[1], p[2], 1])
            else:
                s = csum[j - 3]
                s[0] += p[0]
                s[1] += p[1]
                s[2] += p[2]
                s[3] += 1
            remap[i] = j
        for j in range(3, len(new)):
            s = csum[j - 3]
            new[j][0] = (s[0] / s[3], s[1] / s[3], s[2] / s[3])
        nv = [(tuple(p), n, c, u) for p, n, c, u in new]
        ni, nsubs, nmats = [0, 1, 2], [(0, 3, 0, 3)], []
        for si, (fv, vc, fi, ic) in enumerate(subs):
            if si == 0:
                continue
            start = len(ni)
            for a2 in range(fi, fi + ic, 3):
                i0 = remap[allidx[a2]]
                i1 = remap[allidx[a2 + 1]]
                i2 = remap[allidx[a2 + 2]]
                if i0 != i1 and i1 != i2 and i0 != i2:
                    ni.extend((i0, i1, i2))
            if len(ni) > start:
                nsubs.append((0, len(nv), start, len(ni) - start))
                nmats.append(mats[si - 1])
        return nv, ni, nsubs, nmats

    return cluster(REDUCE_TARGET_TRIS)


def build_prefab_geometry(imp, size):
    """Merge every piece into one block mesh (scaled to `size`, centred).
    Returns (blob, index list, submesh ranges, vertex count, extent, mat list)."""
    scale = max(size) * 0.5
    pts = [v[0] for pc in imp["pieces"] for v in pc["verts"]]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    half = [(hi[i] - lo[i]) / 2 for i in range(3)]
    k = (scale / 2.0) / (max(half) or 1.0)

    verts = [((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 1.0, 1.0), (0.5, 0.5))
             for _ in range(3)]
    allidx = [0, 1, 2]
    subs, mats = [(0, 3, 0, 3)], []

    def vkey(p, n, u):
        return (round(p[0], 5), round(p[1], 5), round(p[2], 5),
                round(n[0], 4), round(n[1], 4), round(n[2], 4),
                round(u[0], 5), round(u[1], 5))

    vmap = {vkey(p, n, u): i for i, (p, n, _c, u) in enumerate(verts)}

    def add_vert(p, n, u):
        key = vkey(p, n, u)
        i = vmap.get(key)
        if i is None:
            i = len(verts)
            vmap[key] = i
            verts.append((p, n, (1.0, 1.0, 1.0, 1.0), u))
        return i

    # Emit every triangle, then group the indices by material.  A bundle often
    # has thousands of pieces sharing a handful of materials (a Minecraft map:
    # 1072 pieces, 101 materials), and one submesh per piece means one draw call
    # each - hundreds on the headset.  Grouping by material cuts that ~10x while
    # leaving the geometry identical.
    per_mat = {}
    order = []
    for pc in imp["pieces"]:
        src = pc["verts"]
        if len(src) < 3:
            continue
        m = pc["mat"]
        bucket = per_mat.get(m)
        if bucket is None:
            bucket = per_mat[m] = []
            order.append(m)
        for i in range(0, len(src) - 2, 3):
            for (pos, nn, uu) in src[i:i + 3]:
                p = tuple((pos[j] - centre[j]) * k for j in range(3))
                bucket.append(add_vert(p, nn, uu))
    for m in order:
        bucket = per_mat[m]
        if not bucket:
            continue
        first_i = len(allidx)
        allidx.extend(bucket)
        subs.append((0, len(verts), first_i, len(allidx) - first_i))
        mats.append(m)
    if len(allidx) // 3 > REDUCE_OVER_TRIS:
        tris = len(allidx) // 3
        print(f"  mesh has {tris:,} triangles - clustering it down to about "
              f"{REDUCE_TARGET_TRIS:,} for the headset ...")
        t0 = time.time()
        verts, allidx, subs, mats = _reduce_geometry(verts, allidx, subs, mats)
        print(f"  reduced to {len(verts):,} vertices / {len(allidx) // 3:,} "
              f"triangles in {time.time() - t0:.0f}s")
    if len(verts) > 4_000_000:
        raise SystemExit(f"prefab has {len(verts)} vertices - too big to load; "
                         "split it into smaller pieces or decimate the model")
    blob = bytearray()
    for p, n, col, uvv in verts:
        blob += struct.pack("<3f3f4f2f", *p, *n, *col, *uvv)
    ext = {"x": round(half[0] * k, 6), "y": round(half[1] * k, 6),
           "z": round(half[2] * k, 6)}
    if len(verts) > 1_000_000:
        print(f"  warning: {len(verts)} vertices - an extremely heavy block; "
              "expect long load times and poor frame rate on the headset")
    elif len(verts) > 200_000:
        print(f"  note: {len(verts)} vertices - a very heavy block; "
              "it will use a 32-bit mesh and may load slowly in game")
    return bytes(blob), allidx, subs, len(verts), ext, mats


def add_texture_img(res, name, img, start):
    tmpl = next(o for o in res.objects.values()
                if o.type.name == "Texture2D" and o.read().m_Name == TEXTURE_TEMPLATE)
    tex = clone(res, tmpl, start).read()
    tex.m_Name = name
    tex.set_image(img.convert("RGBA"), target_format=TextureFormat.RGBA32, mipmap_count=1)
    tex.save()
    return start


def import_bundle_materials(res, imp, mats, start):
    """Clone the game's textured mapobject material once per bundle material,
    with the bundle's _MainTex (or a solid colour swatch).  Returns
    (map bundle mat pid -> game material pid, next free pid)."""
    src = res.objects[PICTURE_MATERIAL_PID]
    mapping = {}
    for bpid, mt in mats.items():
        props = mt.get("m_SavedProperties", {})
        teximg, main = None, 0
        for k, v in props.get("m_TexEnvs", []):
            if k == "_MainTex":
                main = v.get("m_Texture", {}).get("m_PathID", 0)
        if main and main in imp["textures"]:
            teximg = imp["textures"][main]
        if teximg is None:
            col = (1.0, 1.0, 1.0, 1.0)
            for k, v in props.get("m_Colors", []):
                if k in ("_BaseColor", "_Color"):
                    col = (v["r"], v["g"], v["b"], v["a"])
            teximg = Image.new("RGBA", (8, 8),
                               tuple(int(c * 255) for c in
                                     (col[0], col[1], col[2], col[3])))
        name = mt.get("m_Name", "mat")
        tex_pid = add_texture_img(res, f"{name}_tex", teximg, start)
        mat = src.read_typetree()
        mat["m_Name"] = name
        mat["m_SavedProperties"]["m_TexEnvs"] = [
            (k, {**v, "m_Texture": pptr(tex_pid)}) if k == "_MainTex" else (k, v)
            for k, v in mat["m_SavedProperties"]["m_TexEnvs"]]
        clone(res, src, start + 1).save_typetree(mat)
        mapping[bpid] = start + 1
        start += 2
    return mapping, start


# ---------------------------------------------------------------- audio
_FFMPEG = None


def bundle_audio_wav(audio):
    """Write a bundle AudioClip's decoded WAV bytes to a temp file."""
    p = os.path.join(tempfile.gettempdir(),
                     f"addblock_bundle_{os.getpid()}.wav")
    with open(p, "wb") as fh:
        fh.write(audio["data"])
    return p


def ffmpeg():
    global _FFMPEG
    if _FFMPEG is None:
        try:
            import imageio_ffmpeg
            _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            raise SystemExit(f"ffmpeg not available (pip install imageio-ffmpeg): {e}")
    return _FFMPEG


def make_ogg(src_path, dst_path):
    exe = ffmpeg()
    r = subprocess.run([exe, "-y", "-i", src_path, "-ac", "2",
                        "-c:a", "libvorbis", "-q:a", "4", dst_path],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst_path):
        raise SystemExit(f"ffmpeg could not encode {src_path}:\n{r.stderr[-600:]}")
    info = subprocess.run([exe, "-i", dst_path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info.stderr)
    h, mnt, sec = (float(x) for x in m.groups())
    mf = re.search(r"Audio: \w+, (\d+) Hz, (\w+)", info.stderr)
    return h * 3600 + mnt * 60 + sec, int(mf.group(1)), (2 if "stereo" in mf.group(2) else 1)


def append_audio(s1, clip_pid, key, src_path):
    """Encode to .ogg, append to sharedassets1.resource, create the AudioClip."""
    tmpl = template(s1, "AudioClip", PREF_AUDIO)
    ogg = os.path.join(tempfile.gettempdir(), f"addblock_{os.getpid()}.ogg")
    try:
        length, freq, ch = make_ogg(src_path, ogg)
        with open(ogg, "rb") as fh:
            blob = fh.read()
    finally:
        if os.path.exists(ogg):
            os.remove(ogg)
    offset = os.path.getsize(RES)
    with open(RES, "ab") as fh:
        fh.write(blob)
    ac = tmpl.read_typetree()
    ac["m_Name"] = key
    ac["m_Channels"] = ch
    ac["m_Frequency"] = freq
    ac["m_Length"] = length
    ac["m_PreloadAudioData"] = True
    ac["m_CompressionFormat"] = 1
    ac["m_Resource"] = {"m_Source": "sharedassets1.resource",
                        "m_Offset": offset, "m_Size": len(blob)}
    clone(s1, tmpl, clip_pid).save_typetree(ac)
    return offset, len(blob), length


# ---------------------------------------------------------------- clips
def _rot_key(t, yaw):
    qy, qw = math.sin(yaw / 2), math.cos(yaw / 2)
    dt = math.pi / SPIN_PERIOD
    w4 = {"x": 1 / 3, "y": 1 / 3, "z": 1 / 3, "w": 1 / 3}
    return {"time": t, "value": {"x": 0.0, "y": qy, "z": 0.0, "w": qw},
            "inSlope": {"x": 0.0, "y": dt * qw, "z": 0.0, "w": -dt * qy},
            "outSlope": {"x": 0.0, "y": dt * qw, "z": 0.0, "w": -dt * qy},
            "weightedMode": 0, "inWeight": dict(w4), "outWeight": dict(w4)}


def _pos_key(t, y, slope):
    w3 = {"x": 1 / 3, "y": 1 / 3, "z": 1 / 3}
    return {"time": t, "value": {"x": 0.0, "y": y, "z": 0.0},
            "inSlope": {"x": 0.0, "y": slope, "z": 0.0},
            "outSlope": {"x": 0.0, "y": slope, "z": 0.0},
            "weightedMode": 0, "inWeight": dict(w3), "outWeight": dict(w3)}


def make_clip(s1, clip_pid, key, preset, amp):
    tmpl = template(s1, "AnimationClip", PREF_CLIP)
    clip = tmpl.read_typetree()
    clip["m_Name"] = key + "_anim"
    clip["m_Legacy"] = True
    clip["m_WrapMode"] = 2
    clip["m_SampleRate"] = 60
    clip["m_ClipBindingConstant"] = {"genericBindings": [], "pptrCurveMapping": []}
    clip["m_FloatCurves"] = []
    clip["m_PPtrCurves"] = []
    clip["m_EulerCurves"] = []
    clip["m_CompressedRotationCurves"] = []
    clip["m_Events"] = []
    clip["m_HasGenericRootTransform"] = False
    clip["m_HasMotionFloatCurves"] = False
    if preset == "spin":
        keys = [_rot_key(t * SPIN_PERIOD / 4, 2 * math.pi * t / 4) for t in range(5)]
        clip["m_RotationCurves"] = [{
            "curve": {"m_Curve": keys, "m_PreInfinity": 2, "m_PostInfinity": 2,
                      "m_RotationOrder": 4}, "path": ""}]
        clip["m_PositionCurves"] = []
    else:
        keys = []
        for i in range(5):
            t = i * BOB_PERIOD / 4
            y = amp * (1 - math.cos(2 * math.pi * t / BOB_PERIOD)) / 2
            slope = amp * math.pi * math.sin(2 * math.pi * t / BOB_PERIOD) / BOB_PERIOD
            keys.append(_pos_key(t, y, slope))
        clip["m_PositionCurves"] = [{
            "curve": {"m_Curve": keys, "m_PreInfinity": 2, "m_PostInfinity": 2,
                      "m_RotationOrder": 4}, "path": ""}]
        clip["m_RotationCurves"] = []
    clip["m_ScaleCurves"] = []
    clone(s1, tmpl, clip_pid).save_typetree(clip)


# ---------------------------------------------------------------- patterns
def pattern_db(env):
    return next(o for o in env.objects if o.type.name == "MonoBehaviour"
                and PILLOW_KEY in o.get_raw_data()
                and o.get_raw_data().count(b"mapObject") > 100)


def pattern_find(raw, key):
    kb = key.encode()
    needle = struct.pack("<i", len(kb)) + kb
    off = -1
    while True:
        off = raw.find(needle, off + 1)
        if off < 0:
            return None
        try:
            cnt_off, entries, end = list_count_offset(bytes(raw), off)
        except SystemExit:
            continue
        if off in entries:
            return cnt_off, entries, end, off


def pattern_add(env, key, display, cost):
    db = pattern_db(env)
    raw = bytearray(db.get_raw_data())
    if pattern_find(bytes(raw), key):
        raise SystemExit(f"{key} is already in the pattern list")
    pillow_off = raw.find(struct.pack("<i", len(PILLOW_KEY)) + PILLOW_KEY + b"\0")
    cnt_off, entries, end = list_count_offset(bytes(raw), pillow_off)
    count = struct.unpack_from("<i", raw, cnt_off)[0]
    entry = (s(key) + s(display) + s("mapObject") + struct.pack("<i", 0)
             + s("white") + struct.pack("<ii", cost, 0))
    raw[end:end] = entry
    struct.pack_into("<i", raw, cnt_off, count + 1)
    db.set_raw_data(bytes(raw))


def pattern_remove(env, key):
    db = pattern_db(env)
    raw = bytearray(db.get_raw_data())
    hit = pattern_find(bytes(raw), key)
    if not hit:
        raise SystemExit(f"{key} is not in the pattern list")
    cnt_off, entries, end, off = hit
    i = entries.index(off)
    nxt = entries[i + 1] if i + 1 < len(entries) else end
    del raw[off:nxt]
    count = struct.unpack_from("<i", raw, cnt_off)[0]
    struct.pack_into("<i", raw, cnt_off, count - 1)
    db.set_raw_data(bytes(raw))


# ---------------------------------------------------------------- server files
def server_patch(key, add):
    changed = []
    g = json.load(open(GLOBALS, encoding="utf-8"))
    if add:
        backup(GLOBALS)
    for item in g:
        if item.get("key") == "valid_mapobject_keys":
            if add and key not in item["value"]:
                item["value"].append(key)
                changed.append("valid_mapobject_keys")
            elif not add and key in item["value"]:
                item["value"].remove(key)
                changed.append("valid_mapobject_keys")
        if item.get("key") == "default_account_override":
            op = item["value"].setdefault("ownedPatterns", [])
            if add and key not in op:
                op.append(key)
                changed.append("ownedPatterns")
            elif not add and key in op:
                op.remove(key)
                changed.append("ownedPatterns")
    if changed:
        json.dump(g, open(GLOBALS, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
    src = open(ROLE_LOCKS, encoding="utf-8").read()
    m = re.search(r"ALWAYS_OWNED_PATTERNS: list = \[([^\]]*)\]", src)
    if m:
        has = f'"{key}"' in m.group(1)
        if add and not has:
            backup(ROLE_LOCKS)
            body = m.group(1).rstrip()
            if not body.endswith(","):
                body += ","
            src = (src[:m.start()]
                   + f'ALWAYS_OWNED_PATTERNS: list = [{body}\n    "{key}"]'
                   + src[m.end():])
            open(ROLE_LOCKS, "w", encoding="utf-8").write(src)
            changed.append("ALWAYS_OWNED_PATTERNS")
        elif not add and has:
            backup(ROLE_LOCKS)
            body = re.sub(rf',?\s*"{re.escape(key)}"', "", m.group(1))
            src = (src[:m.start()]
                   + f"ALWAYS_OWNED_PATTERNS: list = [{body}]"
                   + src[m.end():])
            open(ROLE_LOCKS, "w", encoding="utf-8").write(src)
            changed.append("ALWAYS_OWNED_PATTERNS")
    return changed


# ---------------------------------------------------------------- builders
def build_resources_block(env, key, size, mesh_choice, texture_path, fid):
    """Picture / mesh / texture-only blocks: flat on the root GO, everything
    in resources.assets (the proven add_picture_block / add_mesh_block layout)."""
    rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
    rm_tree = rm_obj.read_typetree()
    cont = dict((k, v) for k, v in rm_tree["m_Container"])
    res_path = "mapobjects/complexdecorationstuffed/" + key.lower()
    if res_path in cont:
        raise SystemExit(f"{key} already present in the container")
    f = files(env)["resources.assets"]
    yeep_go = f.objects[cont[YEEP_PATH]["m_PathID"]]
    comps = {r.type.name: r for r in
             (f.objects[c["component"]["m_PathID"]]
              for c in yeep_go.read_typetree()["m_Component"])}

    textured = bool(texture_path)
    tmpl_mesh = comps["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]
    nxt = max(f.objects) + 1
    tex_pid = mat_pid = None
    if textured:
        tex_pid, mat_pid, nxt = add_texture(f, key, texture_path, nxt)
    blob, idx0, idx, vcount, ext = geo_for(size, mesh_choice, textured)
    mesh_pid = nxt
    write_mesh(f, tmpl_mesh, mesh_pid, key, blob, idx0, idx, vcount, ext)
    base = nxt + 1
    ids = {k: base + i for i, k in enumerate(("go", "tr", "mb", "mf", "mr", "bc"))}
    ids["mesh"], ids["tex"], ids["mat"] = mesh_pid, tex_pid, mat_pid

    gt = yeep_go.read_typetree()
    gt["m_Name"] = key
    gt["m_Tag"] = 20000
    gt["m_Component"] = [{"component": pptr(ids[k])} for k in ("tr", "mb", "mf", "mr", "bc")]
    clone(f, yeep_go, ids["go"]).save_typetree(gt)

    tt = comps["Transform"].read_typetree()
    tt["m_GameObject"] = pptr(ids["go"])
    tt["m_Children"] = []
    tt["m_Father"] = NULL
    clone(f, comps["Transform"], ids["tr"]).save_typetree(tt)

    ft = comps["MeshFilter"].read_typetree()
    ft["m_GameObject"] = pptr(ids["go"])
    ft["m_Mesh"] = pptr(mesh_pid)
    clone(f, comps["MeshFilter"], ids["mf"]).save_typetree(ft)

    rt = comps["MeshRenderer"].read_typetree()
    rt["m_GameObject"] = pptr(ids["go"])
    rt["m_Materials"] = ([pptr(STUFFED_MATERIAL_PID), pptr(mat_pid)] if textured
                         else [pptr(STUFFED_MATERIAL_PID)])
    clone(f, comps["MeshRenderer"], ids["mr"]).save_typetree(rt)

    bt = comps["BoxCollider"].read_typetree()
    bt["m_GameObject"] = pptr(ids["go"])
    bt["m_Size"] = {"x": size[0] * 0.5, "y": size[1] * 0.5, "z": size[2] * 0.5}
    bt["m_Center"] = ZERO3
    clone(f, comps["BoxCollider"], ids["bc"]).save_typetree(bt)

    yraw = comps["MonoBehaviour"].get_raw_data()
    mb = (struct.pack("<iq", 0, ids["go"]) + yraw[12:16] + yraw[16:28]
          + s("") + struct.pack("<3i", *size) + b"\0" * 16
          + s("") + struct.pack("<i", 0) + s("placePillow") + struct.pack("<fi", 0.2, 0)
          + s("breakPillow") + s("footstepPillow") + struct.pack("<3i", 0, 0, 1))
    clone(f, comps["MonoBehaviour"], ids["mb"]).set_raw_data(mb)

    rm_tree["m_Container"].append((res_path, xptr(fid, ids["go"])))
    if textured:
        rm_tree["m_Container"].append(("blazepic/mat_" + key.lower(),
                                       xptr(fid, mat_pid)))
    rm_obj.save_typetree(rm_tree)
    return ids, res_path, textured


def build_shared1_block(env, key, size, mesh_choice, texture_path,
                        anim, with_audio, fid_s1, fid_res):
    """Skeleton-style blocks (audio and/or animation): root GO
    [Transform, MapObject, Collider, AudioSource?] + VisualContainer child with
    MeshFilter / MeshRenderer / Animation.  Mesh + texture stay in
    resources.assets, referenced across via fileID."""
    ggm_f = files(env)["globalgamemanagers"]
    rm_obj = next(o for o in env.objects
                  if o.type.name == "ResourceManager" and o.assets_file is ggm_f)
    rm_tree = rm_obj.read_typetree()
    cont = dict((k, v) for k, v in rm_tree["m_Container"])
    res_path = "mapobjects/complexdecorationstuffed/" + key.lower()
    if res_path in cont:
        raise SystemExit(f"{key} already present in the container")
    s1 = files(env)["sharedassets1.assets"]
    res = files(env)["resources.assets"]
    yeep_go = res.objects[cont[YEEP_PATH]["m_PathID"]]
    old_go = res.objects[OLD_GO].read_typetree()
    old_bc = res.objects[OLD_BC].read_typetree()
    old_mb_raw = bytearray(res.objects[OLD_MB].get_raw_data())

    textured = bool(texture_path)
    yeep_comps = {r.type.name: r for r in
                  (res.objects[c["component"]["m_PathID"]]
                   for c in yeep_go.read_typetree()["m_Component"])}
    tmpl_mesh = yeep_comps["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]
    nxt_res = max(res.objects) + 1
    tex_pid = mat_pid = None
    if textured:
        tex_pid, mat_pid, nxt_res = add_texture(res, key, texture_path, nxt_res)
    blob, idx0, idx, vcount, ext = geo_for(size, mesh_choice, textured)
    mesh_pid = nxt_res
    write_mesh(res, tmpl_mesh, mesh_pid, key, blob, idx0, idx, vcount, ext)

    need = ["go_root", "tr_root", "mb", "bc", "go_vc", "tr_vc", "mf", "mr"]
    if with_audio:
        need.append("audio")
    if anim:
        need.append("anim")
    nxt = max(s1.objects) + 1
    ids = {k: nxt + i for i, k in enumerate(need)}
    ids["mesh"], ids["tex"], ids["mat"] = mesh_pid, tex_pid, mat_pid

    go = copy.deepcopy(old_go)
    go["m_Name"] = key
    go["m_Component"] = [{"component": pptr(ids["tr_root"])},
                         {"component": pptr(ids["mb"])},
                         {"component": pptr(ids["bc"])}]
    if with_audio:
        go["m_Component"].append({"component": pptr(ids["audio"])})
    clone(s1, template(s1, "GameObject"), ids["go_root"]).save_typetree(go)

    tr_t = template(s1, "Transform").read_typetree()
    tr_t["m_GameObject"] = pptr(ids["go_root"])
    tr_t["m_Children"] = [pptr(ids["tr_vc"])]
    tr_t["m_Father"] = NULL
    tr_t["m_LocalPosition"] = ZERO3
    tr_t["m_LocalRotation"] = IDENT_Q
    tr_t["m_LocalScale"] = {"x": 1.0, "y": 1.0, "z": 1.0}
    clone(s1, template(s1, "Transform"), ids["tr_root"]).save_typetree(tr_t)

    struct.pack_into("<q", old_mb_raw, 4, ids["go_root"])
    struct.pack_into("<3i", old_mb_raw, 32, *size)
    clone(s1, template(s1, "MonoBehaviour"), ids["mb"]).set_raw_data(bytes(old_mb_raw))

    bc = copy.deepcopy(old_bc)
    bc["m_GameObject"] = pptr(ids["go_root"])
    bc["m_Material"] = NULL
    bc["m_Size"] = {"x": size[0] * 0.5, "y": size[1] * 0.5, "z": size[2] * 0.5}
    bc["m_Center"] = ZERO3
    clone(s1, template(s1, "BoxCollider"), ids["bc"]).save_typetree(bc)

    vgo = copy.deepcopy(old_go)
    vgo["m_Name"] = "VisualContainer"
    vgo["m_Component"] = [{"component": pptr(ids["tr_vc"])},
                          {"component": pptr(ids["mf"])},
                          {"component": pptr(ids["mr"])}]
    if anim:
        vgo["m_Component"].append({"component": pptr(ids["anim"])})
    clone(s1, template(s1, "GameObject"), ids["go_vc"]).save_typetree(vgo)

    vtr = copy.deepcopy(tr_t)
    vtr["m_GameObject"] = pptr(ids["go_vc"])
    vtr["m_Children"] = []
    vtr["m_Father"] = pptr(ids["tr_root"])
    vtr["m_LocalPosition"] = ZERO3
    clone(s1, template(s1, "Transform"), ids["tr_vc"]).save_typetree(vtr)

    mf = template(s1, "MeshFilter").read_typetree()
    mf["m_GameObject"] = pptr(ids["go_vc"])
    mf["m_Mesh"] = xptr(fid_res, mesh_pid)
    clone(s1, template(s1, "MeshFilter"), ids["mf"]).save_typetree(mf)

    mr = template(s1, "MeshRenderer").read_typetree()
    mr["m_GameObject"] = pptr(ids["go_vc"])
    mr["m_Materials"] = ([xptr(fid_res, STUFFED_MATERIAL_PID),
                          xptr(fid_res, mat_pid)] if textured
                         else [xptr(fid_res, STUFFED_MATERIAL_PID)])
    clone(s1, template(s1, "MeshRenderer"), ids["mr"]).save_typetree(mr)

    if anim:
        clip_pid = nxt + len(need)
        make_clip(s1, clip_pid, key, anim, max(size) * 0.16)
        an = template(s1, "Animation", PREF_ANIM).read_typetree()
        an["m_GameObject"] = pptr(ids["go_vc"])
        an["m_Animation"] = pptr(clip_pid)
        an["m_Animations"] = [pptr(clip_pid)]
        an["m_WrapMode"] = 2
        an["m_PlayAutomatically"] = True
        clone(s1, template(s1, "Animation", PREF_ANIM), ids["anim"]).save_typetree(an)
        ids["animclip"] = clip_pid

    rm_tree["m_Container"].append((res_path, xptr(fid_s1, ids["go_root"])))
    if textured:
        rm_tree["m_Container"].append(("blazepic/mat_" + key.lower(),
                                       xptr(fid_res, mat_pid)))
    rm_obj.save_typetree(rm_tree)
    return ids, res_path, textured


def build_prefab_block(env, key, size, bundle_path, anim, with_audio,
                       fid_s1, fid_res):
    """Whole .bundle block: import every mesh/material/texture from a Unity
    asset bundle and place it under a VisualContainer (skeleton-style
    hierarchy).  Meshes/materials/textures live in resources.assets and are
    referenced across files; the root GO + collider + audio/animation live in
    sharedassets1, so a bundle can carry sound and a spin/bob too."""
    ggm_f = files(env)["globalgamemanagers"]
    rm_obj = next(o for o in env.objects
                  if o.type.name == "ResourceManager" and o.assets_file is ggm_f)
    rm_tree = rm_obj.read_typetree()
    cont = dict((k, v) for k, v in rm_tree["m_Container"])
    res_path = "mapobjects/complexdecorationstuffed/" + key.lower()
    if res_path in cont:
        raise SystemExit(f"{key} already present in the container")
    s1 = files(env)["sharedassets1.assets"]
    res = files(env)["resources.assets"]
    yeep_go = res.objects[cont[YEEP_PATH]["m_PathID"]]
    old_go = res.objects[OLD_GO].read_typetree()
    old_bc = res.objects[OLD_BC].read_typetree()
    old_mb_raw = bytearray(res.objects[OLD_MB].get_raw_data())
    yeep_comps = {r.type.name: r for r in
                  (res.objects[c["component"]["m_PathID"]]
                   for c in yeep_go.read_typetree()["m_Component"])}
    tmpl_mesh = yeep_comps["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]

    imp = import_prefab(bundle_path)
    blob, allidx, subs, vcount, ext, piece_mats = build_prefab_geometry(imp, size)
    nxt_res = max(res.objects) + 1
    mat_map, nxt_res = import_bundle_materials(res, imp, imp["materials"], nxt_res)
    mesh_pid = nxt_res
    _write_mesh(res, tmpl_mesh, mesh_pid, key, blob, allidx, subs, vcount, ext)

    need = ["go_root", "tr_root", "mb", "bc", "go_vc", "tr_vc", "mf", "mr"]
    if with_audio:
        need.append("audio")
    if anim:
        need.append("anim")
    nxt = max(s1.objects) + 1
    ids = {k: nxt + i for i, k in enumerate(need)}
    ids["mesh"] = mesh_pid
    ids["mats"] = [mat_map.get(pm, STUFFED_MATERIAL_PID) for pm in piece_mats]
    ids["tex"] = ids["mat"] = None
    fx = imp.get("particles") or []
    clip_off = nxt + len(need)
    fx_base = clip_off + (1 if anim else 0)
    ids["fx"] = len(fx)
    ids["fx_end"] = fx_base + 4 * len(fx) - 1 if fx else 0

    go = copy.deepcopy(old_go)
    go["m_Name"] = key
    go["m_Component"] = [{"component": pptr(ids["tr_root"])},
                         {"component": pptr(ids["mb"])},
                         {"component": pptr(ids["bc"])}]
    if with_audio:
        go["m_Component"].append({"component": pptr(ids["audio"])})
    clone(s1, template(s1, "GameObject"), ids["go_root"]).save_typetree(go)

    tr_t = template(s1, "Transform").read_typetree()
    tr_t["m_GameObject"] = pptr(ids["go_root"])
    tr_t["m_Children"] = [pptr(ids["tr_vc"])] + [
        pptr(fx_base + 4 * i + 1) for i in range(len(fx))]
    tr_t["m_Father"] = NULL
    tr_t["m_LocalPosition"] = ZERO3
    tr_t["m_LocalRotation"] = IDENT_Q
    tr_t["m_LocalScale"] = {"x": 1.0, "y": 1.0, "z": 1.0}
    clone(s1, template(s1, "Transform"), ids["tr_root"]).save_typetree(tr_t)

    struct.pack_into("<q", old_mb_raw, 4, ids["go_root"])
    struct.pack_into("<3i", old_mb_raw, 32, *size)
    clone(s1, template(s1, "MonoBehaviour"), ids["mb"]).set_raw_data(bytes(old_mb_raw))

    bc = copy.deepcopy(old_bc)
    bc["m_GameObject"] = pptr(ids["go_root"])
    bc["m_Material"] = NULL
    bc["m_Size"] = {"x": size[0] * 0.5, "y": size[1] * 0.5, "z": size[2] * 0.5}
    bc["m_Center"] = ZERO3
    clone(s1, template(s1, "BoxCollider"), ids["bc"]).save_typetree(bc)

    vgo = copy.deepcopy(old_go)
    vgo["m_Name"] = "VisualContainer"
    vgo["m_Component"] = [{"component": pptr(ids["tr_vc"])},
                          {"component": pptr(ids["mf"])},
                          {"component": pptr(ids["mr"])}]
    if anim:
        vgo["m_Component"].append({"component": pptr(ids["anim"])})
    clone(s1, template(s1, "GameObject"), ids["go_vc"]).save_typetree(vgo)

    vtr = copy.deepcopy(tr_t)
    vtr["m_GameObject"] = pptr(ids["go_vc"])
    vtr["m_Children"] = []
    vtr["m_Father"] = pptr(ids["tr_root"])
    vtr["m_LocalPosition"] = ZERO3
    clone(s1, template(s1, "Transform"), ids["tr_vc"]).save_typetree(vtr)

    mf = template(s1, "MeshFilter").read_typetree()
    mf["m_GameObject"] = pptr(ids["go_vc"])
    mf["m_Mesh"] = xptr(fid_res, mesh_pid)
    clone(s1, template(s1, "MeshFilter"), ids["mf"]).save_typetree(mf)

    mr = template(s1, "MeshRenderer").read_typetree()
    mr["m_GameObject"] = pptr(ids["go_vc"])
    mr["m_Materials"] = [xptr(fid_res, STUFFED_MATERIAL_PID)] + [
        xptr(fid_res, mp) for mp in ids["mats"]]
    clone(s1, template(s1, "MeshRenderer"), ids["mr"]).save_typetree(mr)

    if anim:
        clip_pid = nxt + len(need)
        make_clip(s1, clip_pid, key, anim, max(size) * 0.16)
        an = template(s1, "Animation", PREF_ANIM).read_typetree()
        an["m_GameObject"] = pptr(ids["go_vc"])
        an["m_Animation"] = pptr(clip_pid)
        an["m_Animations"] = [pptr(clip_pid)]
        an["m_WrapMode"] = 2
        an["m_PlayAutomatically"] = True
        clone(s1, template(s1, "Animation", PREF_ANIM), ids["anim"]).save_typetree(an)
        ids["animclip"] = clip_pid

    pts_all = [v[0] for pc in imp["pieces"] for v in pc["verts"]]
    lo = [min(p[i] for p in pts_all) for i in range(3)]
    hi = [max(p[i] for p in pts_all) for i in range(3)]
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    half = [(hi[i] - lo[i]) / 2 for i in range(3)]
    fk = (max(size) * 0.25) / (max(half) or 1.0)
    for i, p in enumerate(fx):
        go_fx = fx_base + 4 * i
        tr_fx = go_fx + 1
        ps_pid = go_fx + 2
        psr_pid = go_fx + 3
        pos, quat, scl = _matrix_trs(p["matrix"], 1.0)
        pos = tuple((pos[j] - centre[j]) * fk for j in range(3))
        scl = tuple(s * fk for s in scl)
        fgo = copy.deepcopy(old_go)
        fgo["m_Name"] = p["name"]
        fgo["m_Component"] = [{"component": pptr(tr_fx)},
                              {"component": pptr(ps_pid)},
                              {"component": pptr(psr_pid)}]
        clone(s1, template(s1, "GameObject"), go_fx).save_typetree(fgo)
        ftr = copy.deepcopy(tr_t)
        ftr["m_GameObject"] = pptr(go_fx)
        ftr["m_Children"] = []
        ftr["m_Father"] = pptr(ids["tr_root"])
        ftr["m_LocalPosition"] = {"x": pos[0], "y": pos[1], "z": pos[2]}
        ftr["m_LocalRotation"] = {"x": quat[0], "y": quat[1],
                                  "z": quat[2], "w": quat[3]}
        ftr["m_LocalScale"] = {"x": scl[0], "y": scl[1], "z": scl[2]}
        clone(s1, template(s1, "Transform"), tr_fx).save_typetree(ftr)
        ps = copy.deepcopy(p["ps"])
        ps["m_GameObject"] = pptr(go_fx)
        ps.pop("m_PrefabInstance", None)
        ps.pop("m_PrefabAsset", None)
        clone(s1, template(s1, "ParticleSystem"), ps_pid).save_typetree(ps)
        psr = copy.deepcopy(p["psr"])
        psr["m_GameObject"] = pptr(go_fx)
        pmats = [m.get("m_PathID", 0) for m in (psr.get("m_Materials") or [])]
        psr["m_Materials"] = [
            xptr(fid_res, mat_map.get(mp, STUFFED_MATERIAL_PID)) for mp in pmats
        ] or [xptr(fid_res, STUFFED_MATERIAL_PID)]
        psr.pop("m_PrefabInstance", None)
        psr.pop("m_PrefabAsset", None)
        clone(s1, template(s1, "ParticleSystemRenderer"), psr_pid).save_typetree(psr)

    rm_tree["m_Container"].append((res_path, xptr(fid_s1, ids["go_root"])))
    rm_obj.save_typetree(rm_tree)
    return ids, res_path, True


# ---------------------------------------------------------------- verify
def verify(key, host, pids):
    env = UnityPy.load(DATA)
    rm = next(o for o in env.objects if o.type.name == "ResourceManager")
    cont = dict((k, v) for k, v in rm.read_typetree()["m_Container"])
    res_path = "mapobjects/complexdecorationstuffed/" + key.lower()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + msg)
        ok = ok and bool(cond)

    check(res_path in cont, f"container entry -> {cont.get(res_path)}")
    check(pattern_find(pattern_db(env).get_raw_data(), key) is not None,
          "pattern list entry")
    mp = pids.get("mesh")
    if mp:
        mt = files(env)["resources.assets"].objects[mp].read_typetree()
        vc = mt["m_VertexData"]["m_VertexCount"]
        fmt = mt.get("m_IndexFormat", 0)
        isz = 4 if fmt else 2
        tot = sum(s["indexCount"] for s in mt["m_SubMeshes"])
        check(fmt == (1 if vc > 0xFFFF else 0)
              and len(mt["m_IndexBuffer"]) == tot * isz
              and len(mt["m_VertexData"]["m_DataSize"]) == vc * 48,
              f"mesh {vc} verts, index fmt {fmt}, "
              f"{len(mt['m_SubMeshes'])} submeshes")
    if host == "shared1":
        s1 = files(env)["sharedassets1.assets"]
        go = s1.objects[pids["go_root"]].read_typetree()
        names = [s1.objects[c["component"]["m_PathID"]].type.name
                 for c in go["m_Component"]]
        check(go["m_Name"] == key and names[0] == "Transform"
              and "MonoBehaviour" in names and "BoxCollider" in names,
              f"root GO {names}")
        vc = s1.objects[pids["go_vc"]].read_typetree()
        vnames = [s1.objects[c["component"]["m_PathID"]].type.name
                  for c in vc["m_Component"]]
        check("MeshFilter" in vnames and "MeshRenderer" in vnames,
              f"VisualContainer {vnames}")
        bad = []

        def refs(x):
            if isinstance(x, dict):
                if "m_FileID" in x and "m_PathID" in x:
                    yield x
                else:
                    for v in x.values():
                        yield from refs(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    yield from refs(v)

        for pid in pids.values():
            if not isinstance(pid, int):
                continue
            o = s1.objects.get(pid)
            if o is None or o.type.name in ("MonoBehaviour",):
                continue
            try:
                t = o.read_typetree()
            except Exception:
                continue
            for r in refs(t):
                if r["m_PathID"] and r["m_FileID"]:
                    nm = s1.externals[r["m_FileID"] - 1].name
                    if nm not in ALLOWED_EXTERNALS:
                        bad.append((o.type.name, nm))
        check(not bad, f"external refs resolve ({bad[:3]})")
        for pid in pids.values():
            o = s1.objects.get(pid) if isinstance(pid, int) else None
            if o is None:
                continue
            if o.type.name == "AudioClip":
                r = o.read_typetree()["m_Resource"]
                check(r["m_Source"] == "sharedassets1.resource"
                      and r["m_Offset"] + r["m_Size"] <= os.path.getsize(RES),
                      f"AudioClip bytes off={r['m_Offset']} size={r['m_Size']}")
            if o.type.name == "AnimationClip":
                c = o.read_typetree()
                curves = c["m_PositionCurves"] or c["m_RotationCurves"]
                check(c.get("m_WrapMode") == 2 and curves
                      and curves[0]["path"] == "",
                      f"clip {c['m_Name']} loop, self-path, {len(curves)} curve")
        if "audio" in pids and "clip" in pids:
            asrc = s1.objects[pids["audio"]].read_typetree()
            check(asrc["m_audioClip"] == pptr(pids["clip"])
                  and asrc.get("Loop") is True,
                  "AudioSource -> clip, looping")
    print("VERIFY", "OK" if ok else "FAILED")
    return ok


# ---------------------------------------------------------------- add/remove
def do_add(kind, combo=None):
    env = UnityPy.load(DATA)
    ggm_f = files(env)["globalgamemanagers"]
    rm = next(o for o in env.objects
              if o.type.name == "ResourceManager" and o.assets_file is ggm_f)
    cont = dict((k, v) for k, v in rm.read_typetree()["m_Container"])
    fid_s1 = [e.name for e in ggm_f.externals].index("sharedassets1.assets") + 1
    fid_res = [e.name for e in ggm_f.externals].index("resources.assets") + 1

    key = ask("Block key (e.g. 1x1_coolblock, 2x2_dancething)", regex=KEY_RE)
    if ("mapobjects/complexdecorationstuffed/" + key.lower()) in cont:
        raise SystemExit(f"{key} already exists in the game data")
    if pattern_find(pattern_db(env).get_raw_data(), key):
        raise SystemExit(f"{key} already exists in the pattern list")
    display = ask("Display name",
                  default=re.sub(r"^[0-9x]+_", "", key).replace("_", " ").title())
    cost = ask_int("Cotton cost", 0, 0, 10 ** 6)

    texture_path = audio_path = None
    anim = None
    bundle = False
    bundle_tmp = None
    host = "resources"
    if kind == "disk":
        mesh_choice = "disk"
        audio_path = ask_audio()
        host = "shared1"
    else:
        mesh_choice = ask_mesh()
        bundle = mesh_choice not in ("cube", "disk") and os.path.exists(mesh_choice)
        imp = import_prefab(mesh_choice) if bundle else None
        if kind == "picture":
            texture_path = ask_image()
            mesh_choice = "cube"
        elif kind == "custom":
            flags = {1: "anim", 2: "audio", 3: "tex",
                     4: "tex+anim", 5: "tex+audio+anim"}[combo]
            if "tex" in flags and not bundle:
                texture_path = ask_image()
            if "audio" in flags:
                if imp and imp.get("audio"):
                    audio_path = bundle_tmp = bundle_audio_wav(imp["audio"])
                    print(f"  using the bundle's own audio: {imp['audio']['name']}")
                else:
                    audio_path = ask_audio()
            if "anim" in flags:
                if imp and imp.get("clips"):
                    names = ", ".join(sorted({c["name"] for c in imp["clips"].values()
                                              if c["name"]}))
                    print(f"  bundle animations found ({names}) - not applied yet; "
                          "choosing a simple motion instead")
                anim = ask_choice("Animation",
                                  [("spin", "spin (loop 2s)"),
                                   ("bob", "bob (loop 1.2s)")])
        if bundle or audio_path or anim:
            host = "shared1"

    size = parse_size(key)
    if host == "resources":
        ids, res_path, textured = build_resources_block(
            env, key, size, mesh_choice, texture_path, fid_res)
    else:
        if bundle:
            ids, res_path, textured = build_prefab_block(
                env, key, size, mesh_choice, anim, bool(audio_path),
                fid_s1, fid_res)
        else:
            ids, res_path, textured = build_shared1_block(
                env, key, size, mesh_choice, texture_path, anim,
                bool(audio_path), fid_s1, fid_res)
        if audio_path:
            s1 = files(env)["sharedassets1.assets"]
            clip_pid = max(v for v in ids.values() if isinstance(v, int)) + 1
            offset, asize, length = append_audio(s1, clip_pid, key, audio_path)
            src = template(s1, "AudioSource", PREF_AUDIO).read_typetree()
            src["m_GameObject"] = pptr(ids["go_root"])
            src["m_audioClip"] = pptr(clip_pid)
            src["m_PlayOnAwake"] = True
            src["Loop"] = True
            clone(s1, template(s1, "AudioSource", PREF_AUDIO),
                  ids["audio"]).save_typetree(src)
            ids["clip"] = clip_pid
            audio_meta = {"offset": offset, "size": asize}
        else:
            audio_meta = None

    if bundle_tmp and os.path.exists(bundle_tmp):
        os.remove(bundle_tmp)

    pattern_add(env, key, display, cost)
    open(DATA, "wb").write(env.file.save(packer="lz4"))
    changed = server_patch(key, True)

    st = state_load()
    st[key] = {"kind": kind, "combo": combo, "name": display, "host": host,
               "pids": ids, "audio": audio_meta, "textured": textured,
               "created": time.strftime("%Y-%m-%d %H:%M")}
    state_save(st)

    print(f"\nadded {key} ({kind}) -> {res_path}")
    if changed:
        print("server files updated:", ", ".join(changed), "(redeploy to apply)")
    print("structural check:")
    if not verify(key, host, ids):
        raise SystemExit("verification failed - fix before building the APK")
    print("\nnext: menu 7 to build the APK, then install it on the headset.")


def do_remove():
    st = state_load()
    key = ask("Key to remove")
    info = st.get(key)
    if not info:
        print("not in this tool's state; trying the game data anyway")
    env = UnityPy.load(DATA)
    rm = next(o for o in env.objects if o.type.name == "ResourceManager")
    rm_tree = rm.read_typetree()
    res_path = "mapobjects/complexdecorationstuffed/" + key.lower()
    mat_path = "blazepic/mat_" + key.lower()
    before = len(rm_tree["m_Container"])
    rm_tree["m_Container"] = [(k, v) for k, v in rm_tree["m_Container"]
                              if k not in (res_path, mat_path)]
    if len(rm_tree["m_Container"]) == before:
        print("no container entry found")
    else:
        rm.save_typetree(rm_tree)
    try:
        pattern_remove(env, key)
        print("pattern list entry removed")
    except SystemExit as e:
        print(e)
    open(DATA, "wb").write(env.file.save(packer="lz4"))

    info = st.pop(key, None)
    if info and info.get("audio"):
        off, sz = info["audio"]["offset"], info["audio"]["size"]
        if os.path.getsize(RES) == off + sz:
            with open(RES, "r+b") as fh:
                fh.truncate(off)
            print(f"truncated sharedassets1.resource back to {off}")
        else:
            print("audio is not the last one in the resource file; left in place")
    changed = server_patch(key, False)
    if changed:
        print("server files updated:", ", ".join(changed), "(redeploy to apply)")
    if info is not None:
        state_save(st)
    print(f"removed {key} (its Unity objects stay in the file, unused)")


def do_list():
    st = state_load()
    if not st:
        print("nothing added yet")
        return
    for k, v in sorted(st.items()):
        extra = v["kind"] + (f" combo {v['combo']}" if v.get("combo") else "")
        print(f"  {k:32s} {v['name']:24s} [{extra}] host={v['host']} {v['created']}")


def do_build():
    print("building APK (2-4 minutes)...")
    r = subprocess.run(BUILD_CMD, cwd=ROOT)
    if r.returncode == 0:
        print("done - install tools/Yeeps-1.31.0-yeeper920-blocks.apk on the headset")
    else:
        print("build failed")


# ---------------------------------------------------------------- prompts
def ask(prompt, default=None, regex=None):
    while True:
        suffix = f" [{default}]" if default else ""
        v = input(f"{prompt}{suffix}: ").strip().strip('"').strip("'")
        if not v and default is not None:
            v = str(default)
        if not v:
            print("  required")
            continue
        if regex and not regex.fullmatch(v):
            print('  must look like 1x1_name, 2x2_name or 2x2x2_name')
            continue
        return v


def ask_int(prompt, default, lo, hi):
    while True:
        v = input(f"{prompt} [{default}]: ").strip()
        if not v:
            return default
        try:
            n = int(v)
            if lo <= n <= hi:
                return n
        except ValueError:
            pass
        print(f"  a number {lo}-{hi} please")


def ask_choice(prompt, options):
    print(prompt + ":")
    for i, (_val, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        v = input("> ").strip()
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        print("  pick a number")


def ask_mesh():
    v = input("Mesh: Enter = block cube, 'disk', or paste a path to a Unity "
              "asset bundle (.bundle) and its whole prefab is imported: "
              ).strip().strip('"').strip("'")
    if not v:
        return "cube"
    if not os.path.exists(v):
        print("  no such file - using the cube")
        return "cube"
    return v


def ask_image():
    while True:
        v = input("Picture file (png/jpg): ").strip().strip('"').strip("'")
        if v and os.path.exists(v):
            try:
                Image.open(v).convert("RGBA")
                return v
            except Exception as e:
                print(f"  cannot read image: {e}")
        else:
            print("  no such file")


def ask_audio():
    while True:
        v = input("Audio file (mp3/wav/ogg/m4a...): ").strip().strip('"').strip("'")
        if v and os.path.exists(v):
            return v
        print("  no such file")


# ---------------------------------------------------------------- ascii art
def ascii_art():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 4)
        except Exception:
            pass
    text, cols = "920Man", 78
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\consolab.ttf", 48)
    except Exception:
        print(text.center(cols))
        return
    probe = ImageDraw.Draw(Image.new("L", (10, 10)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 8
    img = Image.new("L", (tw + pad * 2, th + pad * 2))
    ImageDraw.Draw(img).text((pad, pad), text, font=font, fill=255)
    rows = max(8, round(cols * img.height / img.width * 0.5))
    px = img.resize((cols, rows), Image.BOX).tobytes()
    ramp = " .:-=+*#%@"
    for r in range(rows):
        grad = 1.0 - 0.55 * (r / max(1, rows - 1))
        line, total = [], 0.0
        for c in range(cols):
            v = px[r * cols + c] * grad
            total += v
            line.append(ramp[min(len(ramp) - 1, int(v / 255 * len(ramp)))])
        grey = 232 + max(0, min(23, round(total / cols / 255 * 23)))
        print(f"\x1b[38;5;{grey}m{''.join(line)}\x1b[0m")
    print()


# ---------------------------------------------------------------- menu
MENU = """
  1. picture block       texture on a cotton cube
  2. basic mesh block    your model (bundle) or a plain cube, paintable
  3. music disk          flat disk that plays your song on a loop
  4. custom block        mix texture / audio / animation
  5. remove a block
  6. list my blocks
  7. build the APK
  q. quit
"""


def main():
    if not os.path.exists(DATA):
        raise SystemExit(f"missing {DATA}")
    ascii_art()
    while True:
        print(MENU)
        try:
            c = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        try:
            if c == "q":
                return
            elif c == "1":
                do_add("picture")
            elif c == "2":
                do_add("mesh")
            elif c == "3":
                do_add("disk")
            elif c == "4":
                combo = ask_choice("What should the block have?", [
                    (1, "animation + mesh"),
                    (2, "audio + mesh"),
                    (3, "texture + mesh"),
                    (4, "texture + animation + mesh"),
                    (5, "texture + audio + animation + mesh")])
                do_add("custom", combo)
            elif c == "5":
                do_remove()
            elif c == "6":
                do_list()
            elif c == "7":
                do_build()
            else:
                print("pick 1-7 or q")
        except SystemExit as e:
            print(f"error: {e}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"error: {e}")


if __name__ == "__main__":
    main()
