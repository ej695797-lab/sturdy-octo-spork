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
    # Clean up all existing map docs for this room
    maps.delete_many({"roomKey": rk})
    # Insert under every possible key variant
    for suffix in ["$current", "$map_0_save0", "$map_1_save0", "$map_2_save0", "$map_0", "$map_1"]:
        maps.insert_one({**imp, "mapKey": f"{rk}{suffix}", "roomKey": rk})
    print(f"{rk}: inserted map under multiple key variants")

client.close()
print("done")