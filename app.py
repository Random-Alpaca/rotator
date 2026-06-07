#!/usr/bin/env python3
"""
rotator/app.py — Single-button rotator.

GET  /         → the page
POST /refresh  → rotate, returns {"ok": true} or {"ok": false}
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# pyrefly: ignore [missing-import]
from flask import Flask, jsonify, render_template_string, Response, stream_with_context

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

    yield "cleanup_old", "Clean up..."
    cleanup_unassigned_reserved_ips()

    yield "listing_ips", "Checking broken addresses..."
    old_ips = list_droplet_reserved_ips()
    unassigned: list[str] = []
    for ip in old_ips:
        yield "unassigning", "Fixing broken addresses..."
        unassign_reserved_ip(ip)
        unassigned.append(ip)

    try:
        yield "creating_ip", "Creating new address..."
        new_ip = create_and_assign()
    except Exception:
        cleanup_unassigned_reserved_ips()
        raise

    try:
        yield "updating_dns", "Updating Pointer..."
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

    yield "cleanup_new", "Running final cleanups..."
    cleanup_unassigned_reserved_ips()
    yield "success", new_ip


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
application = app = Flask(__name__)

_lock = threading.Lock()
_last_rotation: float = 0.0


@app.route("/")
def index():
    now = time.time()
    elapsed = now - _last_rotation
    remaining = max(0, int(COOLDOWN - elapsed))
    return render_template_string(HTML, fqdn=DNS_FQDN, cooldown=COOLDOWN, remaining=remaining), 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
    }


@app.route("/refresh", methods=["POST"])
def refresh():
    global _last_rotation

    elapsed = time.time() - _last_rotation
    if elapsed < COOLDOWN:
        return jsonify(ok=False), 429

    if not _lock.acquire(blocking=False):
        return jsonify(ok=False), 409

    try:
        rotation_result = run_rotation()
        if isinstance(rotation_result, str):
            _last_rotation = time.time()
            def generate_static():
                try:
                    yield json.dumps({"ok": True, "done": True, "ip": rotation_result}) + "\n"
                finally:
                    if _lock.locked():
                        try:
                            _lock.release()
                        except RuntimeError:
                            pass
            return Response(generate_static(), mimetype="application/x-ndjson")

        try:
            first_step = next(rotation_result)
        except StopIteration:
            _last_rotation = time.time()
            def generate_empty():
                try:
                    yield json.dumps({"ok": True, "done": True}) + "\n"
                finally:
                    if _lock.locked():
                        try:
                            _lock.release()
                        except RuntimeError:
                            pass
            return Response(generate_empty(), mimetype="application/x-ndjson")
    except Exception:
        if _lock.locked():
            try:
                _lock.release()
            except RuntimeError:
                pass
        return jsonify(ok=False), 500

    def generate(generator_obj, initial_item):
        global _last_rotation
        try:
            step, message = initial_item
            yield json.dumps({"ok": True, "done": False, "step": step, "message": message}) + "\n"

            new_ip = None
            for step, message in generator_obj:
                if step == "success":
                    new_ip = message
                else:
                    yield json.dumps({"ok": True, "done": False, "step": step, "message": message}) + "\n"

            if new_ip:
                _last_rotation = time.time()
                yield json.dumps({"ok": True, "done": True, "ip": new_ip}) + "\n"
            else:
                yield json.dumps({"ok": False, "done": True, "error": "Rotation did not return an IP"}) + "\n"
        except Exception as e:
            yield json.dumps({"ok": False, "done": True, "error": str(e)}) + "\n"
        finally:
            if _lock.locked():
                try:
                    _lock.release()
                except RuntimeError:
                    pass

    return Response(stream_with_context(generate(rotation_result, first_step)), mimetype="application/x-ndjson")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Manual Refresher</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: radial-gradient(circle at center, #18181b 0%, #09090b 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
    -webkit-font-smoothing: antialiased;
    color: #f4f4f5;
    overflow: hidden;
  }

  /* ---- Card Container ---- */
  .card {
    background: rgba(20, 20, 25, 0.65);
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 24px;
    padding: 32px;
    width: 360px;
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.8),
                inset 0 1px 1px rgba(255, 255, 255, 0.1);
    display: flex;
    flex-direction: column;
    align-items: center;
    position: relative;
  }

  /* ---- Header ---- */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    margin-bottom: 24px;
  }

  .title-group {
    display: flex;
    flex-direction: column;
  }

  .title {
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    color: rgba(255, 255, 255, 0.9);
    text-transform: uppercase;
  }

  .subtitle {
    font-size: 0.7rem;
    font-weight: 400;
    color: rgba(255, 255, 255, 0.4);
    margin-top: 2px;
    letter-spacing: 0.05em;
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    color: #4ade80;
    background: rgba(74, 222, 128, 0.1);
    padding: 4px 10px;
    border-radius: 12px;
    border: 1px solid rgba(74, 222, 128, 0.2);
    transition: all 0.3s ease;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    background-color: #4ade80;
    border-radius: 50%;
    box-shadow: 0 0 8px #4ade80;
    animation: pulse 2s infinite;
    transition: all 0.3s ease;
  }

  @keyframes pulse {
    0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }
    70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(74, 222, 128, 0); }
    100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(74, 222, 128, 0); }
  }

  /* ---- Stage / Center Area ---- */
  .stage {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 220px;
    height: 220px;
    position: relative;
    margin: 16px 0;
  }

  /* ---- Button ---- */
  #btn {
    width: 160px;
    height: 160px;
    border-radius: 50%;
    border: 1px solid rgba(255, 255, 255, 0.12);
    background: radial-gradient(circle, rgba(255, 255, 255, 0.04) 0%, rgba(255, 255, 255, 0) 100%);
    color: rgba(255, 255, 255, 0.85);
    font-size: 0.75rem;
    font-family: inherit;
    font-weight: 600;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    cursor: pointer;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    outline: none;
    position: absolute;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
  }

  #btn:hover:not(:disabled) {
    border-color: rgba(255, 255, 255, 0.6);
    color: #fff;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.08) 0%, rgba(255, 255, 255, 0.01) 100%);
    box-shadow: 0 0 30px rgba(255, 255, 255, 0.08), 0 8px 32px rgba(0, 0, 0, 0.3);
    transform: scale(1.02);
  }

  #btn:active:not(:disabled) {
    transform: scale(0.98);
  }

  #btn:disabled {
    cursor: not-allowed;
    border-color: rgba(255, 255, 255, 0.05);
    background: transparent;
    color: rgba(255, 255, 255, 0.25);
    box-shadow: none;
  }

  /* ---- Spinner ---- */
  #spinner {
    width: 60px;
    height: 60px;
    border: 2px solid rgba(255, 255, 255, 0.05);
    border-top-color: rgba(255, 255, 255, 0.65);
    border-radius: 50%;
    animation: spin 0.8s cubic-bezier(0.5, 0.1, 0.5, 0.9) infinite;
    position: absolute;
    box-shadow: 0 0 15px rgba(255, 255, 255, 0.05);
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  #loading-text {
    position: absolute;
    bottom: 24px;
    font-size: 0.62rem;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.45);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    text-align: center;
    width: 200px;
    line-height: 1.4;
    animation: fadeIn 0.3s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
  }

  /* ---- Outcome marks ---- */
  .mark {
    font-size: 4rem;
    font-weight: 300;
    line-height: 1;
    position: absolute;
    animation: pop 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.275) both;
  }

  @keyframes pop {
    from { opacity: 0; transform: scale(0.5); }
    to   { opacity: 1; transform: scale(1);   }
  }

  #ok-mark  { color: #4ade80; text-shadow: 0 0 20px rgba(74, 222, 128, 0.3); }
  #err-mark { color: #f87171; text-shadow: 0 0 20px rgba(248, 113, 113, 0.3); }

  /* ---- Info Panel / Footer ---- */
  .footer {
    width: 100%;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    padding-top: 20px;
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .info-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.7rem;
    letter-spacing: 0.02em;
  }

  .info-label {
    color: rgba(255, 255, 255, 0.35);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .info-value {
    color: rgba(255, 255, 255, 0.8);
    font-weight: 600;
    font-family: monospace;
    font-size: 0.75rem;
  }

  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="title-group">
      <span class="title">Manual Refresher</span>
      <span class="subtitle">For when you have broken code.</span>
    </div>
    <div class="status-indicator">
      <div class="status-dot"></div>
      <span id="status-text">READY</span>
    </div>
  </div>

  <div class="stage">
    <!-- Main button -->
    <button id="btn" onclick="doRefresh()">REFRESH</button>

    <!-- Spinner -->
    <div id="spinner" class="hidden"></div>
    <div id="loading-text" class="hidden"></div>

    <!-- Feedback Feedback Marks -->
    <span id="ok-mark" class="mark hidden">&#10003;</span>
    <span id="err-mark" class="mark hidden">&#10007;</span>
  </div>

  <div class="footer">
    <div class="info-row">
      <span class="info-label">Cost-Control Cooldown</span>
      <span class="info-value" id="cooldown-val">{{ cooldown }}s</span>
    </div>
  </div>
</div>

<script>
  const btn           = document.getElementById('btn');
  const spinner       = document.getElementById('spinner');
  const loadingText   = document.getElementById('loading-text');
  const okMark        = document.getElementById('ok-mark');
  const errMark       = document.getElementById('err-mark');
  const statusText    = document.getElementById('status-text');
  const statusIndicator = document.querySelector('.status-indicator');
  const statusDot     = document.querySelector('.status-dot');

  const COOLDOWN_DURATION = parseInt('{{ cooldown }}') || 900;
  let remainingCooldown = parseInt('{{ remaining }}') || 0;
  let cooldownInterval;

  function show(el, el2) {
    [btn, spinner, loadingText, okMark, errMark].forEach(e => e.classList.add('hidden'));
    if (el) el.classList.remove('hidden');
    if (el2) el2.classList.remove('hidden');
  }

  async function doRefresh() {
    show(spinner, loadingText);
    loadingText.textContent = 'STARTING ROTATION...';
    updateStatus('STARTING...', 'yellow');

    try {
      const resp = await fetch('/refresh', { method: 'POST' });
      if (resp.status === 429) {
        remainingCooldown = COOLDOWN_DURATION;
        startCooldownTimer();
        show(errMark);
        updateStatus('COOLDOWN', 'yellow');
        setTimeout(() => show(btn), 2500);
        return;
      }
      if (resp.status === 409) {
        show(errMark);
        updateStatus('IN PROGRESS', 'red');
        setTimeout(() => show(btn), 2500);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(String.fromCharCode(10));
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line);
            if (data.done) {
              if (data.ok) {
                show(okMark);
                updateStatus('SUCCESS', 'green');
                remainingCooldown = COOLDOWN_DURATION;
                startCooldownTimer();
              } else {
                show(errMark);
                updateStatus('FAILED', 'red');
              }
            } else {
              loadingText.textContent = data.message;
              updateStatus(data.step.toUpperCase().replace('_', ' '), 'yellow');
            }
          } catch (e) {
            console.error("Failed to parse stream line:", e);
          }
        }
      }
    } catch (err) {
      show(errMark);
      updateStatus('ERROR', 'red');
    }

    setTimeout(() => {
      show(btn);
      if (remainingCooldown <= 0) {
        updateStatus('READY', 'green');
      }
    }, 2500);
  }

  function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function updateStatus(text, type) {
    statusText.textContent = text;
    if (type === 'green') {
      statusIndicator.style.color = '#4ade80';
      statusIndicator.style.borderColor = 'rgba(74, 222, 128, 0.2)';
      statusIndicator.style.backgroundColor = 'rgba(74, 222, 128, 0.1)';
      statusDot.style.backgroundColor = '#4ade80';
      statusDot.style.boxShadow = '0 0 8px #4ade80';
    } else if (type === 'yellow') {
      statusIndicator.style.color = '#fbbf24';
      statusIndicator.style.borderColor = 'rgba(251, 191, 36, 0.2)';
      statusIndicator.style.backgroundColor = 'rgba(251, 191, 36, 0.1)';
      statusDot.style.backgroundColor = '#fbbf24';
      statusDot.style.boxShadow = '0 0 8px #fbbf24';
    } else if (type === 'red') {
      statusIndicator.style.color = '#f87171';
      statusIndicator.style.borderColor = 'rgba(248, 113, 113, 0.2)';
      statusIndicator.style.backgroundColor = 'rgba(248, 113, 113, 0.1)';
      statusDot.style.backgroundColor = '#f87171';
      statusDot.style.boxShadow = '0 0 8px #f87171';
    }
  }

  function startCooldownTimer() {
    clearInterval(cooldownInterval);
    if (remainingCooldown <= 0) return;

    btn.disabled = true;
    updateCooldownUI();

    cooldownInterval = setInterval(() => {
      remainingCooldown--;
      if (remainingCooldown <= 0) {
        clearInterval(cooldownInterval);
        btn.disabled = false;
        btn.textContent = 'REFRESH';
        updateStatus('READY', 'green');
      } else {
        updateCooldownUI();
      }
    }, 1000);
  }

  function updateCooldownUI() {
    btn.textContent = formatTime(remainingCooldown);
    updateStatus(`COOLDOWN (${formatTime(remainingCooldown)})`, 'yellow');
  }

  if (remainingCooldown > 0) {
    startCooldownTimer();
  }
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
