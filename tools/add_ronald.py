#!/usr/bin/env python3
path = "server/handlers/login.py"
lines = open(path, encoding="utf-8").read().splitlines()
insert_at = 2637  # 1-based, insert after this line (0-indexed: 2636)
new_lines = ['        if "1x4x2_ronald" not in _op:', '            _op.append("1x4x2_ronald")']
lines = lines[:insert_at] + new_lines + lines[insert_at:]
open(path, "w", encoding="utf-8").write("\n".join(lines))
print("done")
