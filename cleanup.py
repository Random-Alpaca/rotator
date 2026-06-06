#!/usr/bin/env python3
"""
cleanup.py — Delete unassigned DigitalOcean reserved IPs.

Reads config from the same config.env as the main app. Only logs when
there is something to report (deleted IPs or errors), so the log stays
quiet on clean runs.

Run every 30 minutes via /etc/cron.d/rotator as a billing safety net for
the case where a rotation fails and leaves an orphaned unassigned IP that
the main app never gets a chance to clean up itself.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Load config.env into the environment if vars aren't already set.
_env_file = Path(__file__).parent / "config.env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip()
            if _k and _k not in os.environ:
                os.environ[_k] = _v

DO_TOKEN = os.environ.get("DO_TOKEN", "")
if not DO_TOKEN:
    print(f"{datetime.now()}: ERROR: DO_TOKEN not set.", flush=True)
    sys.exit(1)

DO_API = "https://api.digitalocean.com/v2"


def do_req(method, path, body=None):
    url = DO_API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + DO_TOKEN)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def do_paginate(path, key):
    items, page = [], path
    while page:
        resp = do_req("GET", page)
        items.extend(resp.get(key, []))
        nxt = resp.get("links", {}).get("pages", {}).get("next")
        page = nxt.replace(DO_API, "") if nxt else None
    return items


def cleanup():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        all_ips = do_paginate("/reserved_ips?per_page=200", "reserved_ips")
        unassigned = [r["ip"] for r in all_ips if r.get("droplet") is None]

        if not unassigned:
            return  # nothing to do — stay silent

        deleted, failed = [], []
        for ip in unassigned:
            try:
                do_req("DELETE", f"/reserved_ips/{ip}")
                deleted.append(ip)
            except Exception as e:
                failed.append(f"{ip} ({e})")

        if deleted:
            print(f"{ts}: deleted {deleted}", flush=True)
        if failed:
            print(f"{ts}: failed to delete {failed}", flush=True)

    except Exception as e:
        print(f"{ts}: ERROR: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    cleanup()
