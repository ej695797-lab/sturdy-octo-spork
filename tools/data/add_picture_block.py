#!/usr/bin/env python3
"""
Add your own picture block to the Yeeps 1.31 game data: a 1x1x1 cotton cube
(0.5 m) with your picture on all six faces, listed next to the 1x1 Pillow.

    python tools/data/add_picture_block.py data/data.unity3d data/data.unity3d \
        --key 1x1_stuffedMyDog --name "My Dog" --image dog.png [--cost 0]

- Texture2D: the picture (center-cropped square, RGBA32, 512 px).
- Material: clone of the textured sign material with that picture.
- Prefab: the BLAZE/YEEP word block's structure (GameObject, Transform,
  MapObject, MeshFilter, MeshRenderer, BoxCollider), cotton tag (grab-able),
  pillow sounds, size 1x1x1, registered under mapobjects/complexdecorationstuffed/.
- Picture material registered as Resources path "blazepic/mat_<key lowercase>":
  the mod library looks that path up for every block key and keeps slot 1 on it
  (the game repaints every slot with the palette color otherwise).

The key must start with "1x1_" and be unique. Players need the key in their
owned items to place it: add it to ownedPatterns in the editor's Default player
data (new players) or on each player. Map imports in the editor only keep keys
listed in the "valid_mapobject_keys" global, so add it there too if you import
maps that use it. Then put the data back into an APK with
tools/configure_apk.py --data.
"""

import copy
import struct
import sys

import UnityPy
from PIL import Image
from UnityPy.enums import TextureFormat

KEY = ""            # set from the command line
DISPLAY = ""
COST = 0
RES_PATH = ""
MAT_PATH = ""
YEEP_PATH = "mapobjects/decoration/1x4x2_forbidden_textyeep"
PILLOW_KEY = b"1x1_stuffed"
PICTURE_MATERIAL_PID = 54           # solid_decoration_1 (MapObjectSolidTextured) in resources.assets
STUFFED_MATERIAL_PID = 110          # solid_stuffed (slot 0: takes the paint color)
TEXTURE_TEMPLATE = "discordIcon"    # inline (not streamed) texture to copy settings from
TEX_SIZE = 512
S = 0.5                             # cube edge (one grid unit)


def build_mesh():
    """Two submeshes, like the BLAZE Screen: 0 = a hidden degenerate triangle
    (the game paints slot 0 with the palette colour), 1 = the cube, every face
    showing the whole picture upright. The client hook keeps slot 1 on the
    picture material. Vertex layout matches YEEP: pos f3, normal f3, color f4, uv f2."""
    verts, idx = [], []
    for _ in range(3):                       # submesh 0: zero-area triangle at the centre
        verts.append(((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 1.0, 1.0), (0.5, 0.5)))
    idx0 = [0, 1, 2]

    def quad(ps, n, uvs):
        base = len(verts)
        for p, uv in zip(ps, uvs):
            verts.append((p, n, (1.0, 1.0, 1.0, 1.0), uv))
        a, b, c, d = base, base + 1, base + 2, base + 3
        u = [ps[1][i] - ps[0][i] for i in range(3)]
        v = [ps[2][i] - ps[0][i] for i in range(3)]
        cr = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
        if sum(cr[i] * n[i] for i in range(3)) > 0:
            idx.extend([a, b, c, a, c, d])
        else:
            idx.extend([a, c, b, a, d, c])

    h = S / 2
    # Corners listed bottom-left, bottom-right, top-right, top-left AS SEEN from
    # outside each face, so uv (0,0),(1,0),(1,1),(0,1) keeps the picture upright.
    up = [(0, 0), (1, 0), (1, 1), (0, 1)]
    quad([(h, -h, h), (-h, -h, h), (-h, h, h), (h, h, h)], (0, 0, 1), up)        # +Z (viewer at +Z sees +X on the left)
    quad([(-h, -h, -h), (h, -h, -h), (h, h, -h), (-h, h, -h)], (0, 0, -1), up)   # -Z
    quad([(h, -h, -h), (h, -h, h), (h, h, h), (h, h, -h)], (1, 0, 0), up)        # +X
    quad([(-h, -h, h), (-h, -h, -h), (-h, h, -h), (-h, h, h)], (-1, 0, 0), up)   # -X
    quad([(-h, h, -h), (h, h, -h), (h, h, h), (-h, h, h)], (0, 1, 0), up)        # top
    quad([(-h, -h, h), (h, -h, h), (h, -h, -h), (-h, -h, -h)], (0, -1, 0), up)   # bottom

    blob = bytearray()
    for p, n, col, uv in verts:
        blob += struct.pack("<3f3f4f2f", *p, *n, *col, *uv)
    return bytes(blob), idx0, idx, len(verts), {"x": h, "y": h, "z": h}


def clone(f, reader, pid):
    new = copy.copy(reader)
    new.path_id = pid
    f.objects[pid] = new
    return new


def pptr(pid):
    return {"m_FileID": 0, "m_PathID": pid}


def s(t):
    b = t.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((-len(b)) % 4)


def list_count_offset(raw, entry_off):
    """(count offset, entry offsets, end offset) of the pattern list holding
    the entry at entry_off (same walker as build_screen_blocks.py)."""
    def walk(p, n):
        hits = []
        for _ in range(n):
            hits.append(p)
            for _f in range(3):
                L = struct.unpack_from("<i", raw, p)[0]
                if not 0 <= L < 200:
                    return None
                p = (p + 4 + L + 3) & ~3
            p += 4
            L = struct.unpack_from("<i", raw, p)[0]
            if not 0 <= L < 200:
                return None
            p = (p + 4 + L + 3) & ~3
            p += 4                                   # cost (cotton)
            L = struct.unpack_from("<i", raw, p)[0]  # trailing string (usually empty)
            if not 0 <= L < 200:
                return None
            p = (p + 4 + L + 3) & ~3
        return hits, p
    for c in range(entry_off - 4, max(0, entry_off - 600000), -4):
        n = struct.unpack_from("<i", raw, c)[0]
        if not 1 <= n <= 3000:
            continue
        try:
            r = walk(c + 4, n)
        except struct.error:
            continue
        if r and entry_off in r[0]:
            return c, r[0], r[1]
    raise SystemExit("pattern list not found")


def picture(path):
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return im.crop((left, top, left + side, top + side)).resize((TEX_SIZE, TEX_SIZE), Image.LANCZOS)


def main(src, dst, image_path):
    env = UnityPy.load(src)
    rm_obj = next(o for o in env.objects if o.type.name == "ResourceManager")
    rm_tree = rm_obj.read_typetree()
    cont = dict((k, v) for k, v in rm_tree["m_Container"])
    if RES_PATH in cont:
        raise SystemExit(f"{KEY} already present")
    yeep_ref = cont[YEEP_PATH]
    yeep_go = next(o for o in env.objects
                   if o.path_id == yeep_ref["m_PathID"] and o.type.name == "GameObject"
                   and o.assets_file.name == "resources.assets")
    f = yeep_go.assets_file
    nxt = max(f.objects) + 1
    ids = {k: nxt + i for i, k in enumerate(["go", "tr", "mb", "mf", "mr", "bc", "mesh", "mat", "tex"])}

    comps = {}
    for c in yeep_go.read_typetree()["m_Component"]:
        r = f.objects[c["component"]["m_PathID"]]
        comps[r.type.name] = r

    # ---- texture (clone an inline texture's settings, then put the picture in)
    tmpl = next(o for o in f.objects.values()
                if o.type.name == "Texture2D" and o.read().m_Name == TEXTURE_TEMPLATE)
    tex_reader = clone(f, tmpl, ids["tex"])
    tex = tex_reader.read()
    tex.m_Name = KEY
    tex.set_image(picture(image_path), target_format=TextureFormat.RGBA32, mipmap_count=1)
    tex.save()

    # ---- material: textured sign material showing the picture
    pic_src = f.objects[PICTURE_MATERIAL_PID]
    mat = pic_src.read_typetree()
    mat["m_Name"] = KEY
    mat["m_SavedProperties"]["m_TexEnvs"] = [
        (k, {**v, "m_Texture": pptr(ids["tex"])}) if k == "_MainTex" else (k, v)
        for k, v in mat["m_SavedProperties"]["m_TexEnvs"]]
    clone(f, pic_src, ids["mat"]).save_typetree(mat)

    # ---- mesh
    mesh_src = f.objects[comps["MeshFilter"].read_typetree()["m_Mesh"]["m_PathID"]]
    mt = mesh_src.read_typetree()
    blob, idx0, idx, vcount, ext = build_mesh()
    mt["m_Name"] = KEY
    mt["m_VertexData"]["m_VertexCount"] = vcount
    mt["m_VertexData"]["m_DataSize"] = blob
    ib = struct.pack(f"<{len(idx0) + len(idx)}H", *(idx0 + idx))
    mt["m_IndexBuffer"] = list(ib) if isinstance(mt["m_IndexBuffer"], list) else ib
    aabb = {"m_Center": {"x": 0.0, "y": 0.0, "z": 0.0}, "m_Extent": ext}
    sm = mt["m_SubMeshes"][0]
    mt["m_SubMeshes"] = [
        {**sm, "firstByte": 0, "indexCount": len(idx0), "baseVertex": 0,
         "firstVertex": 0, "vertexCount": 3, "localAABB": aabb},
        {**sm, "firstByte": len(idx0) * 2, "indexCount": len(idx), "baseVertex": 0,
         "firstVertex": 3, "vertexCount": vcount - 3, "localAABB": aabb}]
    mt["m_LocalAABB"] = aabb
    for k in ("m_BakedConvexCollisionMesh", "m_BakedTriangleCollisionMesh"):
        if k in mt:
            mt[k] = b"" if isinstance(mt[k], (bytes, bytearray)) else []
    clone(f, mesh_src, ids["mesh"]).save_typetree(mt)

    # ---- GameObject (cotton tag: grab-able like the other pillows)
    gt = yeep_go.read_typetree()
    gt["m_Name"] = KEY
    gt["m_Tag"] = 20000
    gt["m_Component"] = [{"component": pptr(ids[k])} for k in ("tr", "mb", "mf", "mr", "bc")]
    clone(f, yeep_go, ids["go"]).save_typetree(gt)

    tt = comps["Transform"].read_typetree()
    tt["m_GameObject"] = pptr(ids["go"])
    tt["m_Children"] = []
    tt["m_Father"] = pptr(0)
    clone(f, comps["Transform"], ids["tr"]).save_typetree(tt)

    ft = comps["MeshFilter"].read_typetree()
    ft["m_GameObject"] = pptr(ids["go"])
    ft["m_Mesh"] = pptr(ids["mesh"])
    clone(f, comps["MeshFilter"], ids["mf"]).save_typetree(ft)

    rt = comps["MeshRenderer"].read_typetree()
    rt["m_GameObject"] = pptr(ids["go"])
    rt["m_Materials"] = [pptr(STUFFED_MATERIAL_PID), pptr(ids["mat"])]
    clone(f, comps["MeshRenderer"], ids["mr"]).save_typetree(rt)

    bt = comps["BoxCollider"].read_typetree()
    bt["m_GameObject"] = pptr(ids["go"])
    bt["m_Size"] = {"x": S, "y": S, "z": S}
    bt["m_Center"] = {"x": 0.0, "y": 0.0, "z": 0.0}
    clone(f, comps["BoxCollider"], ids["bc"]).save_typetree(bt)

    # ---- MapObject (raw, same layout as the BLAZE block): size 1x1x1, pillow sounds
    yraw = comps["MonoBehaviour"].get_raw_data()
    script = yraw[16:28]
    mb = (struct.pack("<iq", 0, ids["go"]) + yraw[12:16] + script
          + s("") + struct.pack("<3i", 1, 1, 1) + b"\0" * 16
          + s("") + struct.pack("<i", 0) + s("placePillow") + struct.pack("<fi", 0.2, 0)
          + s("breakPillow") + s("footstepPillow") + struct.pack("<3i", 0, 0, 1))
    clone(f, comps["MonoBehaviour"], ids["mb"]).set_raw_data(mb)

    # ---- ResourceManager
    rm_tree["m_Container"].append((RES_PATH, {"m_FileID": yeep_ref["m_FileID"], "m_PathID": ids["go"]}))
    rm_tree["m_Container"].append((MAT_PATH, {"m_FileID": yeep_ref["m_FileID"], "m_PathID": ids["mat"]}))
    rm_obj.save_typetree(rm_tree)

    # ---- pattern list: next to the 1x1 Pillow, white, COST cotton
    db = next(o for o in env.objects if o.type.name == "MonoBehaviour"
              and PILLOW_KEY in o.get_raw_data() and o.get_raw_data().count(b"mapObject") > 100)
    raw = bytearray(db.get_raw_data())
    entry_off = raw.find(struct.pack("<i", len(PILLOW_KEY)) + PILLOW_KEY + b"\0")
    cnt_off, entries, end = list_count_offset(bytes(raw), entry_off)
    count = struct.unpack_from("<i", raw, cnt_off)[0]
    entry = s(KEY) + s(DISPLAY) + s("mapObject") + struct.pack("<i", 0) + s("white") + struct.pack("<ii", COST, 0)
    raw[end:end] = entry
    struct.pack_into("<i", raw, cnt_off, count + 1)
    db.set_raw_data(bytes(raw))

    open(dst, "wb").write(env.file.save(packer="lz4"))
    print(f"added {KEY}: go={ids['go']} tex={ids['tex']} mat={ids['mat']} verts={vcount}; "
          f"pillow list {count}->{count + 1}, cost {COST}")


if __name__ == "__main__":
    import argparse
    import re
    ap = argparse.ArgumentParser(description="Add a picture block to the 1.31 game data.")
    ap.add_argument("src", help="input data.unity3d")
    ap.add_argument("dst", help="output data.unity3d (can be the same file)")
    ap.add_argument("--key", required=True, help='block key, e.g. 1x1_stuffedMyDog (must start with "1x1_")')
    ap.add_argument("--name", required=True, help="name shown in game")
    ap.add_argument("--image", required=True, help="picture file (png/jpg); center-cropped to a square")
    ap.add_argument("--cost", type=int, default=0, help="cotton cost (default 0)")
    a = ap.parse_args()
    if not re.fullmatch(r"1x1_[A-Za-z0-9]+", a.key):
        raise SystemExit('--key must look like 1x1_stuffedMyDog (letters/digits after "1x1_")')
    KEY, DISPLAY, COST = a.key, a.name, a.cost
    RES_PATH = "mapobjects/complexdecorationstuffed/" + KEY.lower()
    MAT_PATH = "blazepic/mat_" + KEY.lower()
    main(a.src, a.dst, a.image)
