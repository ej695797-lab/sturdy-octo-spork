#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from pymongo import MongoClient
from services.map_owners import owner_ids, rewrite_owners

MONGO_URI = sys.argv[1] if len(sys.argv) > 1 else "mongodb://127.0.0.1:27017"
DB_NAME = sys.argv[2] if len(sys.argv) > 2 else "Yeeps131"
TARGET_OWNER = "o_25596005490046939"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
maps = db["maps"]

for rk in ["official_0", "official_1", "official_2"]:
    for doc in maps.find({"roomKey": rk}):
        sd = doc.get("saveData", "")
        if not sd:
            print(f"{rk} {doc.get('mapKey')}: no saveData")
            continue
        ids = owner_ids(sd)
        # Map ALL owners (including empty string) to target
        mapping = {oid: TARGET_OWNER for oid in ids}
        # Remove identity mappings
        mapping = {k: v for k, v in mapping.items() if k != v}
        if not mapping:
            print(f"{rk} {doc.get('mapKey')}: no owners to change (ids={ids})")
            continue
        new_sd, n = rewrite_owners(sd, mapping)
        maps.update_one({"_id": doc["_id"]}, {"$set": {"saveData": new_sd}})
        print(f"{rk} {doc.get('mapKey')}: changed {n} owner entries (ids={ids})")

# Also fix imported_maps
imported = db["imported_maps"]
for rk in ["official_0", "official_1", "official_2"]:
    doc = imported.find_one({"roomKey": rk})
    if not doc:
        continue
    sd = doc.get("saveData", "")
    if not sd:
        continue
    ids = owner_ids(sd)
    mapping = {oid: TARGET_OWNER for oid in ids}
    mapping = {k: v for k, v in mapping.items() if k != v}
    if not mapping:
        print(f"imported {rk}: no owners to change (ids={ids})")
        continue
    new_sd, n = rewrite_owners(sd, mapping)
    imported.update_one({"roomKey": rk}, {"$set": {"saveData": new_sd}})
    print(f"imported {rk}: changed {n} owner entries")

client.close()
print("done")
