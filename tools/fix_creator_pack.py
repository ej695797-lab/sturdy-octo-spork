#!/usr/bin/env python3
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://sapphireunnecessary_db_user:xmcijefSg2kRp1hd@ogyeeps.ftiowhe.mongodb.net"
client = MongoClient(MONGO_URI)
db = client["Yeeps131"]
accounts = db["accounts"]

# Set hasCreatorPack=0 for everyone except o_25596005490046939
result = accounts.update_many(
    {"accountID": {"$ne": "o_25596005490046939"}},
    {"$set": {"hasCreatorPack": 0}}
)
print(f"Updated {result.modified_count} accounts (hasCreatorPack=0)")

# Set hasCreatorPack=1 for o_25596005490046939
result2 = accounts.update_one(
    {"accountID": "o_25596005490046939"},
    {"$set": {"hasCreatorPack": 1}}
)
print(f"Updated target account (hasCreatorPack=1): modified={result2.modified_count}")

client.close()
print("done")
