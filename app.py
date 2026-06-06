#!/usr/bin/env python3
"""
rotator/app.py — Single-button IP rotator.

GET  /         → the page
POST /refresh  → rotate IP, returns {"ok": true} or {"ok": false}
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, request, render_template_string

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _require(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: required env var {name!r} is not set.")
    return val


DO_TOKEN   = _require("DO_TOKEN")
DROPLET_ID = int(_require("DROPLET_ID"))
DNS_FQDN   = _require("DNS_FQDN")
DNS_TTL    = int(os.environ.get("DNS_TTL", "300"))
COOLDOWN   = int(os.environ.get("COOLDOWN_SECONDS", "900"))

DO_API = "https://api.digitalocean.com/v2"


# ---------------------------------------------------------------------------
# DigitalOcean API
# ---------------------------------------------------------------------------
def do_req(method, path, body=None):
    url = DO_API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + DO_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"DO {method} {path} -> {e.code}: {detail}")
    except OSError as e:
        raise RuntimeError(f"DO {method} {path} network error: {e}")


def do_paginate(path, key):
    items, page = [], path
    while page:
        resp = do_req("GET", page)
        items.extend(resp.get(key, []))
        nxt = resp.get("links", {}).get("pages", {}).get("next")
        page = nxt.replace(DO_API, "") if nxt else None
    return items


def list_droplet_reserved_ips():
    return [
        r["ip"]
        for r in do_paginate("/reserved_ips?per_page=200", "reserved_ips")
        if (r.get("droplet") or {}).get("id") == DROPLET_ID
    ]


def poll_action(ip, action_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = do_req("GET", f"/reserved_ips/{ip}/actions/{action_id}")
        status = resp.get("action", {}).get("status")
        if status == "completed":
            return
        if status == "errored":
            raise RuntimeError(f"Action {action_id} on {ip} errored.")
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for action {action_id} on {ip}.")


def unassign_reserved_ip(ip):
    resp = do_req("POST", f"/reserved_ips/{ip}/actions", {"type": "unassign"})
    if aid := (resp.get("action") or {}).get("id"):
        poll_action(ip, aid)


def create_and_assign(timeout=120):
    resp = do_req("POST", "/reserved_ips", {"droplet_id": DROPLET_ID})
    ip = (resp.get("reserved_ip") or {}).get("ip")
    if not ip:
        raise RuntimeError(f"Create returned no IP: {json.dumps(resp)}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = do_req("GET", f"/reserved_ips/{ip}")
        if (cur.get("reserved_ip", {}).get("droplet") or {}).get("id") == DROPLET_ID:
            return ip
        time.sleep(3)
    raise RuntimeError(f"IP {ip} was created but never confirmed assigned.")


def delete_reserved_ip(ip):
    do_req("DELETE", f"/reserved_ips/{ip}")


def cleanup_unassigned_reserved_ips():
    all_ips = do_paginate("/reserved_ips?per_page=200", "reserved_ips")
    for rip in all_ips:
        if rip.get("droplet") is None:
            try:
                delete_reserved_ip(rip["ip"])
            except Exception:
                pass


def split_fqdn(fqdn):
    parts = fqdn.split(".")
    if len(parts) < 3:
        raise ValueError(f"DNS_FQDN {fqdn!r} must have at least 3 labels.")
    return ".".join(parts[:-2]), ".".join(parts[-2:])


def find_a_record(domain, record_name):
    records = do_paginate(
        f"/domains/{domain}/records?type=A&per_page=200", "domain_records"
    )
    for r in records:
        if r.get("name") == record_name:
            return r["id"], r.get("ttl", DNS_TTL)
    return None, DNS_TTL


def run_rotation():
    record_name, domain = split_fqdn(DNS_FQDN)

    cleanup_unassigned_reserved_ips()

    old_ips = list_droplet_reserved_ips()
    unassigned: list[str] = []
    for ip in old_ips:
        unassign_reserved_ip(ip)
        unassigned.append(ip)

    try:
        new_ip = create_and_assign()
    except Exception:
        cleanup_unassigned_reserved_ips()
        raise

    try:
        record_id, existing_ttl = find_a_record(domain, record_name)
        if record_id:
            do_req("PATCH", f"/domains/{domain}/records/{record_id}",
                   {"data": new_ip, "ttl": existing_ttl})
        else:
            do_req("POST", f"/domains/{domain}/records",
                   {"type": "A", "name": record_name, "data": new_ip, "ttl": DNS_TTL})
    except Exception:
        cleanup_unassigned_reserved_ips()
        raise

    cleanup_unassigned_reserved_ips()
    return new_ip


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
application = app = Flask(__name__)

_lock = threading.Lock()
_last_rotation: float = 0.0


@app.route("/")
def index():
    return HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/refresh", methods=["POST"])
def refresh():
    global _last_rotation

    elapsed = time.time() - _last_rotation
    if elapsed < COOLDOWN:
        return jsonify(ok=False), 429

    if not _lock.acquire(blocking=False):
        return jsonify(ok=False), 409

    try:
        run_rotation()
        _last_rotation = time.time()
        return jsonify(ok=True)
    except Exception:
        return jsonify(ok=False), 500
    finally:
        _lock.release()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Refresh</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: #0c0c0c;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    -webkit-font-smoothing: antialiased;
  }

  .stage {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 200px;
    height: 200px;
  }

  /* ---- Button ---- */
  #btn {
    width: 160px;
    height: 160px;
    border-radius: 50%;
    border: 1.5px solid rgba(255,255,255,0.22);
    background: transparent;
    color: rgba(255,255,255,0.75);
    font-size: 0.7rem;
    font-family: inherit;
    font-weight: 600;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    cursor: pointer;
    transition: border-color 0.25s, color 0.25s, background 0.25s, transform 0.1s, box-shadow 0.25s;
    outline: none;
  }

  #btn:hover {
    border-color: rgba(255,255,255,0.65);
    color: rgba(255,255,255,1);
    background: rgba(255,255,255,0.04);
    box-shadow: 0 0 40px rgba(255,255,255,0.04);
  }

  #btn:active { transform: scale(0.95); }

  /* ---- Spinner ---- */
  #spinner {
    width: 52px;
    height: 52px;
    border: 2px solid rgba(255,255,255,0.08);
    border-top-color: rgba(255,255,255,0.55);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- Outcome marks ---- */
  .mark {
    font-size: 3.5rem;
    font-weight: 300;
    line-height: 1;
    animation: pop 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275) both;
  }

  @keyframes pop {
    from { opacity: 0; transform: scale(0.6); }
    to   { opacity: 1; transform: scale(1);   }
  }

  #ok-mark  { color: #4ade80; }
  #err-mark { color: #f87171; }

  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="stage">
  <button id="btn" onclick="doRefresh()">REFRESH</button>
  <div    id="spinner"  class="hidden"></div>
  <span   id="ok-mark"  class="mark hidden">&#10003;</span>
  <span   id="err-mark" class="mark hidden">&#10007;</span>
</div>
<script>
  const btn     = document.getElementById('btn');
  const spinner = document.getElementById('spinner');
  const okMark  = document.getElementById('ok-mark');
  const errMark = document.getElementById('err-mark');

  function show(el) {
    [btn, spinner, okMark, errMark].forEach(e => e.classList.add('hidden'));
    el.classList.remove('hidden');
  }

  async function doRefresh() {
    show(spinner);
    try {
      const resp = await fetch('/refresh', { method: 'POST' });
      const data = await resp.json();
      show(data.ok ? okMark : errMark);
    } catch {
      show(errMark);
    }
    setTimeout(() => show(btn), 2500);
  }
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
