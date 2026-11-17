#!/usr/bin/env python3
import os, re

env_vars = set()
for root, dirs, files in os.walk("server"):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            c = open(p, encoding="utf-8", errors="replace").read()
            for m in re.finditer(r'os\.getenv\(["\x27]([A-Z_]+)', c):
                env_vars.add(m.group(1))
            for m in re.finditer(r'os\.environ\.get\(["\x27]([A-Z_]+)', c):
                env_vars.add(m.group(1))

if os.path.exists("server/.env.example"):
    c = open("server/.env.example").read()
    for line in c.splitlines():
        if "=" in line and not line.startswith("#"):
            env_vars.add(line.split("=")[0].strip())

for v in sorted(env_vars):
    print(v)