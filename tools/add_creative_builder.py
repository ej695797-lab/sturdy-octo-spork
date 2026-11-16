#!/usr/bin/env python3
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://sapphireunnecessary_db_user:xmcijefSg2kRp1hd@ogyeeps.ftiowhe.mongodb.net"
client = MongoClient(MONGO_URI)
db = client["Yeeps131"]
globals_col = db["globals"]

# Get current creative_builders list
doc = globals_col.find_one({"key": "creative_builders"}, {"_id": 0, "value": 1})
ids = doc.get("value", []) if doc else []
if not isinstance(ids, list):
    ids = []

target = "o_25596005490046939"
if target not in ids:
    ids.append(target)

globals_col.update_one(
    {"key": "creative_builders"},
    {"$set": {"key": "creative_builders", "value": ids}},
    upsert=True
)
print(f"Creative builders: {ids}")

client.close()
print("done")
