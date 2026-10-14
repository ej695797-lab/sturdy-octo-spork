#!/usr/bin/env python3
"""Check a running server end to end, the way a 1.31.0-blaze headset talks to it.

    python tools/smoke_test.py http://127.0.0.1:8000 --editor-password YOURPASS

Logs in a throwaway test account (o_1000000000000001, "KitTester"), reads its
account, the news board, the world map, Starter House and its (empty) map, checks that
community worlds are refused, that a Photon join would be allowed, the client
feed, and the editor sign-in. Every line says PASS or FAIL. The test account
stays in the database; delete it in the editor's Players tab if you like.
"""
import argparse
import hashlib
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("base", nargs="?", default="http://127.0.0.1:8000", help="server address")
ap.add_argument("--editor-password", default="", help="EDITOR_PASSWORD from server/.env (skips editor checks if empty)")
a = ap.parse_args()
BASE = a.base.rstrip("/")
DK = hashlib.sha256(b"smoke-test-headset").hexdigest()
ACC = "o_1000000000000001"
PFX = f"/xxxxxxxxxxxxxxxxxx/k/{DK}/c/kit1.0000000000000000.0000000000000000"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
failed = []


def req(path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def js(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def check(name, cond, info=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {info}")
    if not cond:
        failed.append(name)


def dyn(op, body):
    s, raw = req(PFX + "/", body, {"X-Amz-Target": f"DynamoDB_20120810.{op}",
                                    "Content-Type": "application/x-amz-json-1.0"})
    return s, js(raw)


def lam(fn, body):
    s, raw = req(PFX + f"/2015-03-31/functions/{fn}/invocations", body)
    outer = js(raw)
    inner = js(outer.get("body")) if isinstance(outer.get("body"), str) else outer
    return s, inner.get("data", inner)


s, raw = req(PFX + "/questLogIntoAccount", {"accountID": ACC, "oculusID": "KitTester",
                                            "applicationVersion": "1.31.0-blaze", "trueVersion": "1.31.0-blaze",
                                            "dataVersion": 2, "propertiesToGet": []})
d = js(raw).get("data") or {}
check("login", s == 200 and bool(d), f"status {s} {raw[:120] if s != 200 else ''}")
check("login: credential pair present", bool(d.get("e_accessKey")) and bool(d.get("e_secretKey")))
check("login: not blocked", not d.get("isPermabanned") and not d.get("isIsolated"),
      f"reason={d.get('banReason')!r}")
check("login: owns items", len(d.get("ownedPatterns") or []) > 1000, f"{len(d.get('ownedPatterns') or [])} items")
check("login: spawn room", d.get("spawnRoomKey") == "official_1", str(d.get("spawnRoomKey")))
check("login: session ids", bool(js(raw).get("publicSessionID")) and bool(js(raw).get("challengeNonce") or d.get("challengeNonce")))

s, j = dyn("GetItem", {"TableName": "G2_Accounts", "Key": {"accountID": {"S": ACC}},
                       "ProjectionExpression": "displayName"})
check("account read", j.get("Item", {}).get("displayName", {}).get("S") == "KitTester")

s, j = dyn("GetItem", {"TableName": "G2_Globals", "Key": {"key": {"S": "announcementData"}}})
check("news board", "Welcome to Blaze's Yeeps Apk!" in json.dumps(j), "(edit it in the editor's News tab)")

s, j = dyn("GetItem", {"TableName": "G2_RoomMap", "Key": {"versionKey": {"S": "mobs1"}},
                       "ProjectionExpression": "data"})
nodes = j.get("Item", {}).get("data", {}).get("L", [])
check("world map", len(nodes) >= 20, f"{len(nodes)} nodes")

s, j = dyn("GetItem", {"TableName": "G2_Rooms", "Key": {"roomKey": {"S": "official_1"}}})
it = j.get("Item", {})
check("Starter House room", it.get("roomName", {}).get("S") == "Starter House")
check("room session id is a number", "N" in it.get("lastEditedSessionID", {}))
sid = it.get("lastEditedSessionID", {}).get("N", "1")
s, j = dyn("GetItem", {"TableName": "G2_Maps", "Key": {"mapKey": {"S": f"official_1$map_{sid}_save0"}},
                       "ProjectionExpression": "mapDataVersion, saveData, saveInternalStates"})
sd = j.get("Item", {}).get("saveData", {}).get("S", "")
# The kit ships with no map builds, so a fresh server answers with an empty map.
check("Starter House map answers", sd.startswith("U1BBTQ"), f"{len(sd)} chars (empty until you add a map)")

s, d = lam("g2_cwGetCanEnter", {"accountID": ACC, "worldName": "FREEBLOCKS"})
check("community worlds refused", d.get("canEnter") == 0, str(d)[:100])

s, raw = req(f"/?accountID={ACC}&platform=VR&gameSessionID=1&nonce=1&clientVersion=1.31.0-blaze&dk={DK}")
check("Photon join allowed", js(raw).get("ResultCode") == 1, raw[:120])

s, raw = req("/client/feed.txt")
check("client feed", s == 200, f"status {s}")

if a.editor_password:
    s, raw = req("/editor/login", {"name": "smoke-test", "password": a.editor_password})
    check("editor sign-in", s == 200 and js(raw).get("ok") is True, raw[:100])
    s, raw = req("/editor/api/me")
    check("editor access", s == 200 and js(raw).get("tier") == "owner", raw[:120])
    s, raw = req("/editor/api/players?limit=5&q=KitTester")
    check("editor player search", s == 200 and ACC in raw)
else:
    print("skip  editor checks (pass --editor-password)")

print("\nALL PASS" if not failed else f"\n{len(failed)} FAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
