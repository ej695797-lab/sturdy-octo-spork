#!/usr/bin/env python3
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://sapphireunnecessary_db_user:xmcijefSg2kRp1hd@ogyeeps.ftiowhe.mongodb.net"
client = MongoClient(MONGO_URI)
db = client["Yeeps131"]
rooms = db["rooms"]

for rk in ["official_0", "official_1", "official_2"]:
    rooms.update_one({"roomKey": rk}, {"$set": {"noSave": 0, "lastEditedSessionID": 1}})
    doc = rooms.find_one({"roomKey": rk}, {"_id": 0, "lastEditedSessionID": 1, "noSave": 1})
    print(rk, doc)

client.close()
print("done")
