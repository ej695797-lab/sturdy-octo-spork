"""
Display-name visibility (owner's rule, 2026-10-10): a name must show at least
one visible character in game. Blank names ("", spaces, zero-width or other
invisible characters, or text hidden with rich-text tags like <size=0> or a
fully transparent colour) make a player impossible to pick out or name.

Rich-text tags are understood the way the game's TextMeshPro draws them:
<quad>/<sprite> draw a visible glyph; <size>, <alpha> and <color> with an
alpha byte can hide whatever follows them.
"""

import re
import unicodedata

# Characters that draw nothing but aren't in an "invisible" Unicode category.
_BLANK_CHARS = set("ᅟᅠㅤﾠ⠀᠎͏឴឵"
                   "⁡⁢⁣⁤﻿\U0001d159"
                   "\U0001d173\U0001d174\U0001d175\U0001d176\U0001d177\U0001d178\U0001d179\U0001d17a")
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Cs", "Zs", "Zl", "Zp", "Mn", "Me"}
_TAG = re.compile(r"<[^<>]*>")


def _char_visible(c: str) -> bool:
    if c.isspace() or c in _BLANK_CHARS:
        return False
    return unicodedata.category(c) not in _INVISIBLE_CATEGORIES


def _alpha_of(value: str):
    """Alpha byte (0-255) of '#RRGGBBAA' / '#RGBA' / '#AA', else None (opaque)."""
    v = value.strip().strip('"').lstrip("#")
    try:
        if len(v) == 8:
            return int(v[6:8], 16)
        if len(v) == 4:
            return int(v[3] * 2, 16)
        if len(v) == 2:
            return int(v, 16)
    except ValueError:
        return None
    return None


def _size_hides(value: str) -> bool:
    m = re.match(r"\s*([+-]?[\d.]+)\s*(px|em|%)?", value or "")
    if not m:
        return False
    try:
        n = float(m.group(1))
    except ValueError:
        return False
    if value.strip().startswith(("+", "-")):
        return False                       # relative change, not an absolute size
    unit = m.group(2) or "px"
    return n < {"px": 3, "em": 0.2, "%": 20}[unit]


def visible_count(name) -> int:
    """How many characters (or <quad>/<sprite> glyphs) of the name actually show."""
    s = str(name or "")
    size_hidden, alpha_hidden, colors = False, False, []
    count, pos = 0, 0
    for m in _TAG.finditer(s):
        text = s[pos:m.start()]
        pos = m.end()
        hidden = size_hidden or alpha_hidden or (colors and colors[-1])
        if not hidden:
            count += sum(1 for c in text if _char_visible(c))
        tag = m.group(0)[1:-1].strip()
        low = tag.lower()
        if low.startswith("size="):
            size_hidden = _size_hides(tag[5:])
        elif low == "/size":
            size_hidden = False
        elif low.startswith("alpha="):
            a = _alpha_of(tag[6:])
            alpha_hidden = a is not None and a < 0x20
        elif low.startswith("color="):
            a = _alpha_of(tag[6:])
            colors.append(a is not None and a < 0x20)
        elif low == "/color":
            if colors:
                colors.pop()
        elif low.startswith(("quad", "sprite")):
            if not hidden:
                count += 1
    tail = s[pos:]
    if not (size_hidden or alpha_hidden or (colors and colors[-1])):
        count += sum(1 for c in tail if _char_visible(c))
    return count


def is_visible_name(name) -> bool:
    return visible_count(name) > 0


def fallback_name(oculus_id: str, account_id: str) -> str:
    """What a player with an invisible name is shown as instead."""
    if is_visible_name(oculus_id):
        return str(oculus_id)
    return "Yeep" + re.sub(r"\D", "", str(account_id or ""))[-4:]
