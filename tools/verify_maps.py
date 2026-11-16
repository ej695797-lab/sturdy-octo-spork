#!/usr/bin/env python3
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://sapphireunnecessary_db_user:xmcijefSg2kRp1hd@ogyeeps.ftiowhe.mongodb.net"
client = MongoClient(MONGO_URI)
db = client["Yeeps131"]
maps = db["maps"]
rooms = db["rooms"]

for rk in ["official_0", "official_1", "official_2"]:
    rdoc = rooms.find_one({"roomKey": rk}, {"_id": 0, "lastEditedSessionID": 1, "noSave": 1})
    target = rk + "$current"
    mdoc = maps.find_one({"roomKey": rk, "mapKey": target}, {"_id": 0, "mapDataVersion": 1})
    print(rk, "room:", rdoc, "map mdv:", mdoc.get("mapDataVersion") if mdoc else None)

client.close()
print("done")
