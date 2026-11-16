#!/usr/bin/env python3
import sys
from pymongo import MongoClient

MONGO_URI = sys.argv[1] if len(sys.argv) > 1 else "mongodb://127.0.0.1:27017"
DB_NAME = sys.argv[2] if len(sys.argv) > 2 else "Yeeps131"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
maps = db["maps"]
imported = db["imported_maps"]

for rk in ["official_0", "official_1", "official_2"]:
    imp = imported.find_one({"roomKey": rk}, {"_id": 0})
    if not imp:
        print(f"{rk}: no imported map")
        continue
    # Set mapDataVersion and saveInternalStates on the imported doc
    imp["mapDataVersion"] = 3
    if "saveInternalStates" not in imp:
        imp["saveInternalStates"] = ""
    # Update all map docs for this room
    for d in maps.find({"roomKey": rk}, {"_id": 1, "mapKey": 1}):
        maps.update_one({"_id": d["_id"]}, {"$set": {
            "saveData": imp["saveData"],
            "mapDataVersion": 3,
            "saveInternalStates": imp.get("saveInternalStates", ""),
            "blockCount": imp.get("blockCount"),
            "dimensions": imp.get("dimensions"),
        }})
    print(f"{rk}: updated {maps.count_documents({'roomKey': rk})} map docs")

client.close()
print("done")
