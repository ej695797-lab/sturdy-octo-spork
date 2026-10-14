"""
Rough IP location, entirely local: DB-IP "IP to City Lite" (free, CC BY 4.0,
https://db-ip.com) read with maxminddb. Player IPs are never sent anywhere.

Used by services/device_auth.py: a headset may be tied to an account from a new
connection if it's within GEO_NEAR_KM of where that account usually plays.

DB file: GEO_DB_PATH (default ~/.local/share/yeeps/dbip-city-lite.mmdb). Refresh
monthly from https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz.
If the file or the library is missing, every lookup returns None (no relaxing).
"""

import math
import os

GEO_DB_PATH = os.path.expanduser(os.getenv("GEO_DB_PATH", "~/.local/share/yeeps/dbip-city-lite.mmdb"))
GEO_NEAR_KM = float(os.getenv("GEO_NEAR_KM", "300"))

_reader = None
_failed = False


def _db():
    global _reader, _failed
    if _reader is None and not _failed:
        try:
            import maxminddb
            _reader = maxminddb.open_database(GEO_DB_PATH, maxminddb.MODE_MMAP)
        except Exception as e:
            _failed = True
            print(f"[geo] location database unavailable: {e}")
    return _reader


def locate(ip: str):
    """(lat, lon) of an IP, or None."""
    db = _db()
    if not db or not ip:
        return None
    try:
        rec = db.get(ip) or {}
        loc = rec.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:
        return None


def distance_km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def nearest_km(ip: str, known_ips) -> float | None:
    """Distance (km) from ip to the closest of known_ips, or None if unknown."""
    here = locate(ip)
    if not here:
        return None
    best = None
    for k in known_ips:
        there = locate(k)
        if there:
            d = distance_km(here, there)
            best = d if best is None else min(best, d)
    return best


# Consumer VPN / relay egress ranges worth flagging (the Oct 9 editor intruder
# came through Cloudflare WARP).
_VPN_NETS = (
    ("104.28.0.0/16", "Cloudflare WARP"),
    ("2a09:bac0::/29", "Cloudflare WARP"),
    ("172.224.0.0/12", "iCloud Private Relay"),
    ("146.75.0.0/16", "iCloud Private Relay"),
    # VPN / hosting ranges the Oct 9-10 raiders used (M247 / Datacamp / GSL host
    # most commercial VPN exits), plus DigitalOcean.
    ("89.238.128.0/18", "VPN (M247)"),
    ("146.70.0.0/16", "VPN (M247)"),
    ("37.120.128.0/17", "VPN (M247)"),
    ("185.184.192.0/22", "VPN"),
    ("185.132.134.0/24", "VPN"),
    ("195.242.214.0/24", "VPN"),
    ("205.147.16.0/20", "VPN"),
    ("89.187.160.0/19", "VPN (Datacamp)"),
    ("212.102.32.0/19", "VPN (Datacamp)"),
    ("143.244.32.0/19", "VPN (Datacamp)"),
    ("195.181.160.0/20", "VPN (Datacamp)"),
    ("138.199.0.0/17", "VPN (Datacamp)"),
    ("2a02:6ea0::/29", "VPN (Datacamp)"),
    ("2a0d:5600::/29", "VPN (GSL)"),
    ("104.131.0.0/16", "DigitalOcean"),
    ("162.243.0.0/16", "DigitalOcean"),
    ("159.203.0.0/16", "DigitalOcean"),
    ("165.227.0.0/16", "DigitalOcean"),
    ("167.99.0.0/16", "DigitalOcean"),
    ("138.197.0.0/16", "DigitalOcean"),
    ("134.209.0.0/16", "DigitalOcean"),
    ("206.189.0.0/16", "DigitalOcean"),
    ("68.183.0.0/16", "DigitalOcean"),
    ("157.245.0.0/16", "DigitalOcean"),
    ("142.93.0.0/16", "DigitalOcean"),
    ("161.35.0.0/16", "DigitalOcean"),
    ("164.90.0.0/16", "DigitalOcean"),
    ("143.198.0.0/16", "DigitalOcean"),
    ("137.184.0.0/16", "DigitalOcean"),
    ("64.225.0.0/16", "DigitalOcean"),
    ("52.23.177.0/24", "AWS"),
)


def vpn_hint(ip: str) -> str:
    """Name of a known VPN/relay the IP belongs to, or ""."""
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        for net, label in _VPN_NETS:
            n = ipaddress.ip_network(net)
            if a.version == n.version and a in n:
                return label
    except Exception:
        pass
    return ""


def describe(ip: str) -> str:
    """'City, Region, Country' for an IP (best effort), or 'unknown location'."""
    db = _db()
    if not db or not ip:
        return "unknown location"
    try:
        rec = db.get(ip) or {}
        en = lambda d: ((d or {}).get("names") or {}).get("en", "")
        parts = [en(rec.get("city")), en((rec.get("subdivisions") or [{}])[0]), en(rec.get("country"))]
        out = ", ".join(p for p in parts if p)
        return out or "unknown location"
    except Exception:
        return "unknown location"
