#!/usr/bin/env python3
"""
Flowtrack Dashboard — local web UI
Served on http://127.0.0.1:7070  (localhost only, never exposed to internet)

Start:   systemctl --user start flowtrack-dashboard
Open:    xdg-open http://127.0.0.1:7070
Manual:  python3 ~/.focusaudit/dashboard.py
"""
from __future__ import annotations

import datetime
import json
import os
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path.home() / ".focusaudit"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOG_DIR         = BASE_DIR / "logs"
REPORTS_DIR     = BASE_DIR / "reports"
VENV_PYTHON     = str(BASE_DIR / "venv" / "bin" / "python3")
ANALYZE_SCRIPT  = str(BASE_DIR / "analyze.py")
SERVICE_NAME    = "focusaudit"
HOST            = "127.0.0.1"   # localhost only — never 0.0.0.0
PORT            = 7070
SCREENSHOT_CAP_GB = 3

OLLAMA_URL = "http://localhost:11434/api/generate"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
VISION_API_KEY = os.getenv("VISION_API_KEY", "")  # For Google Vision or Claude Vision

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── System helpers ─────────────────────────────────────────────────────────────

def _sh(cmd: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def service_status() -> dict:
  if platform.system() != "Linux":
    # On macOS/Windows, systemd is unavailable. Keep dashboard usable.
    return {"active": False, "status": "unsupported", "pid": 0, "ram_mb": 0.0}

  active = _sh(["systemctl", "--user", "is-active", SERVICE_NAME])
  prop   = _sh(["systemctl", "--user", "show", SERVICE_NAME,
          "--property=MainPID,MemoryCurrent"])
  pid, mem = 0, 0
  for line in prop.splitlines():
    k, _, v = line.partition("=")
    if k == "MainPID" and v.isdigit():
      pid = int(v)
    elif k == "MemoryCurrent" and v.isdigit():
      mem = int(v)
  ram_mb = round(mem / (1024 * 1024), 1)
  if ram_mb == 0 and pid > 0:
    try:
      for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
        if ln.startswith("VmRSS:"):
          ram_mb = round(int(ln.split()[1]) / 1024, 1)
          break
    except OSError:
      pass
  return {"active": active == "active", "status": active,
      "pid": pid, "ram_mb": ram_mb}


def storage_stats() -> dict:
    def _mb(p: Path, pat: str) -> float:
        if not p.exists():
            return 0.0
        return round(
            sum(f.stat().st_size for f in p.glob(pat) if f.is_file()) / (1024 * 1024), 2
        )
    total    = sum(f.stat().st_size for f in BASE_DIR.rglob("*") if f.is_file())
    sc_count = len(list(SCREENSHOTS_DIR.glob("*.jpg"))) if SCREENSHOTS_DIR.exists() else 0
    return {
        "total_mb":         round(total / (1024 * 1024), 2),
        "screenshots_mb":   _mb(SCREENSHOTS_DIR, "*.jpg"),
        "logs_kb":          round(_mb(LOG_DIR, "*.jsonl") * 1024, 1),
        "screenshot_count": sc_count,
        "cap_gb":           SCREENSHOT_CAP_GB,
    }


def sync_json_to_cloud(provider: str, target: str, api_key: str) -> dict:
    """Upload all JSONL logs to a user-selected cloud target."""
    provider = provider.lower().strip()
    payload = {
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "logs": {},
    }
    for f in sorted(LOG_DIR.glob("*.jsonl")):
        payload["logs"][f.name] = f.read_text(encoding="utf-8")

    data_text = json.dumps(payload, ensure_ascii=False, indent=2)

    if provider == "webhook":
        if not target:
            return {"ok": False, "error": "Webhook URL is required."}
        req = urllib.request.Request(
            target,
            data=data_text.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return {"ok": True, "message": "JSON backup sent to webhook."}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"Webhook upload failed: {exc}"}

    if provider == "gist":
        if not api_key:
            return {"ok": False, "error": "GitHub token is required for gist backup."}
        files = {
            f"flowtrack_logs_{datetime.date.today().isoformat()}.json": {
                "content": data_text
            }
        }
        body = json.dumps({
            "description": "Flowtrack JSON backup",
            "public": False,
            "files": files,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/gists",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/vnd.github+json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                gist = json.loads(resp.read())
                return {"ok": True, "message": "JSON backup uploaded to private gist.", "url": gist.get("html_url", "")}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"Gist upload failed: {exc}"}

    return {"ok": False, "error": "Unsupported provider. Use gist or webhook."}


def _ensure_ollama_running() -> bool:
    """Start `ollama serve` on demand if not already running.
    Returns True once the API is reachable, False if ollama is not installed."""
    # Fast path — already up?
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except Exception:
        pass
    # Try to start it as a background process.
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False  # ollama binary not on PATH
    # Wait up to 10 seconds for the server to become ready.
    for _ in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
                return True
        except Exception:
            continue
    return False


def query_llm(prompt: str, provider: str, model: str, api_key: str, base_url: str = "") -> tuple[str | None, str | None]:
  provider = provider.lower().strip()
  try:
    if provider == "ollama":
      model = (model or "llama3").strip()
      ollama_url = (base_url or OLLAMA_URL).strip()
      if ollama_url.endswith("/"):
        ollama_url = ollama_url[:-1]
      if ollama_url.endswith(":11434"):
        ollama_url = ollama_url + "/api/generate"
      elif "/api/" not in ollama_url:
        ollama_url = ollama_url + "/api/generate"

      # Start ollama on-demand if not running.
      if not _ensure_ollama_running():
        return None, "Ollama is not installed or failed to start. Install from https://ollama.com then run: ollama pull llama3"

      # If requested model is missing, fall back to an installed Ollama model.
      try:
        tags_url = ollama_url.replace("/api/generate", "/api/tags")
        with urllib.request.urlopen(tags_url, timeout=10) as resp:
          tags = json.loads(resp.read())
        installed = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
        if not installed:
          return None, "Ollama is running but no models are installed. Run: ollama pull llama3"
        if model in installed:
          pass
        elif ":" not in model:
          pref = next((m for m in installed if m.startswith(model + ":")), "")
          model = pref or installed[0]
        else:
          model = installed[0]
      except Exception:
        pass

      # keep_alive=5m: model stays loaded for 5 min of inactivity, then auto-unloads.
      payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "5m"}).encode("utf-8")
      req = urllib.request.Request(
        ollama_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
        text = data.get("response", "").strip()
        return (text if text else None, None if text else "Ollama returned an empty response.")

    if provider == "openai":
      if not api_key:
        return None, "OpenAI API key is required."
      payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
      }).encode("utf-8")
      req = urllib.request.Request(
        base_url or OPENAI_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        return (text if text else None, None if text else "OpenAI returned an empty response.")

    if provider == "anthropic":
      if not api_key:
        return None, "Anthropic API key is required."
      payload = json.dumps({
        "model": model,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
      }).encode("utf-8")
      req = urllib.request.Request(
        base_url or ANTHROPIC_URL,
        data=payload,
        headers={
          "Content-Type": "application/json",
          "x-api-key": api_key,
          "anthropic-version": "2023-06-01",
        },
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        parts = data.get("content", [])
        text = parts[0].get("text", "").strip() if parts else ""
        return (text if text else None, None if text else "Anthropic returned an empty response.")

    if provider == "gemini":
      if not api_key:
        return None, "Gemini API key is required."
      url = (base_url or GEMINI_URL_TMPL).format(model=model, key=api_key)
      payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
      req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
      with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return (text if text else None, None if text else "Gemini returned an empty response.")

    return None, f"Unsupported provider: {provider}"

  except urllib.error.HTTPError as exc:
    detail = ""
    try:
      detail = exc.read().decode("utf-8", errors="replace")[:400]
    except Exception:
      detail = str(exc)
    return None, f"HTTP {exc.code} from {provider}: {detail}"
  except urllib.error.URLError as exc:
    if provider == "ollama":
      return None, "Cannot reach Ollama at http://localhost:11434. Start Ollama and run: ollama run llama3"
    return None, f"Network error for {provider}: {exc.reason}"
  except (json.JSONDecodeError, OSError, KeyError, IndexError, TypeError) as exc:
    return None, f"{provider} request failed: {exc}"


def today_events() -> list[dict]:
    f = LOG_DIR / f"{datetime.date.today().isoformat()}.jsonl"
    if not f.exists():
        return []
    out = []
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def recent_screenshots(limit: int = 12) -> list[str]:
    if not SCREENSHOTS_DIR.exists():
        return []
    files = sorted(
        SCREENSHOTS_DIR.glob("*.jpg"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return [f.name for f in files[:limit]]


def analyze_screenshot_image(image_path: str, api_key: str = "", provider: str = "gemini") -> str | None:
    """Analyze a screenshot image with vision API to detect distractions, social media, etc."""
    try:
        # Read image file and encode as base64
        img_file = SCREENSHOTS_DIR / image_path
        if not img_file.exists() or not img_file.is_file():
            return None
        
        with open(img_file, "rb") as f:
            import base64
            img_data = base64.b64encode(f.read()).decode('utf-8')
        
        if provider == "gemini" and api_key:
            # Use Google Gemini Vision API
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            payload = json.dumps({
                "contents": [{
                    "parts": [{
                        "text": "Analyze this screenshot: What application is open? Is the user watching video (YouTube, Netflix, TikTok, etc)? Are they on social media (Instagram, Twitter, Facebook, etc)? List any distracting elements. What is their focus level? Be concise in 2-3 sentences."
                    }, {
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": img_data
                        }
                    }]
                }]
            }).encode("utf-8")
            
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                return text if text else None
        
        return None
    except Exception as exc:
        return None


def export_logs_by_date(start_date: str = "", end_date: str = "") -> dict:
    """Export logs for a date range. If empty, returns today's logs."""
    try:
        if not start_date:
            start_date = datetime.date.today().isoformat()
        if not end_date:
            end_date = datetime.date.today().isoformat()
        
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        
        payload = {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "logs": {}}
        
        current = start
        while current <= end:
            f = LOG_DIR / f"{current.isoformat()}.jsonl"
            if f.exists():
                payload["logs"][f.name] = f.read_text(encoding="utf-8")
            current += datetime.timedelta(days=1)
        
        return payload
    except Exception as exc:
        return {"error": str(exc)}


def latest_report() -> str:
    if not REPORTS_DIR.exists():
        return ""
    reports = sorted(REPORTS_DIR.glob("analysis_*.txt"), reverse=True)
    return reports[0].read_text(encoding="utf-8") if reports else ""


def _quick_focus(entries: list[dict]) -> str:
    if len(entries) < 2:
        return "N/A"
    try:
        durations  = [float(e.get("duration", 30)) for e in entries]
        avg_dur    = sum(durations) / len(durations)
        time_score = min(100.0, (avg_dur / 300) * 100)
        rate_score = max(0.0, 100.0 - len(entries) * 0.8)
        return str(round(0.5 * time_score + 0.5 * rate_score, 1))
    except Exception:
        return "N/A"


# ── Background analysis ────────────────────────────────────────────────────────

_lock    = threading.Lock()
_running = False
_result: dict = {"status": "idle", "output": "No analysis run yet. Click Run Analysis."}


def _start_analysis(use_ai: bool, provider: str = "", model: str = "", api_key: str = "") -> None:
    global _running, _result

    def _work() -> None:
        global _running, _result
        try:
            cmd = [VENV_PYTHON, ANALYZE_SCRIPT]
            if not use_ai:
                cmd.append("--no-ai")
            elif provider:
                cmd.extend(["--provider", provider, "--model", model or "gpt-4o-mini"])
                if api_key:
                    cmd.extend(["--api-key", api_key])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out = r.stdout + (("\n\nSTDERR:\n" + r.stderr) if r.stderr else "")
            _result = {"status": "done" if r.returncode == 0 else "error", "output": out}
        except subprocess.TimeoutExpired:
            _result = {"status": "error", "output": "Analysis timed out after 120 s."}
        except Exception as exc:
            _result = {"status": "error", "output": str(exc)}
        finally:
            _running = False

    with _lock:
        if _running:
            return
        _running = True
        _result  = {"status": "running", "output": "Analysis in progress…"}
    threading.Thread(target=_work, daemon=True).start()


# ── Embedded HTML / CSS / JS ───────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flowtrack Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#111111;
  --surface:#1c1c1c;
  --surface-2:#242424;
  --border:#2e2e2e;
  --accent:#f97316;
  --accent-dim:rgba(249,115,22,.12);
  --accent-border:rgba(249,115,22,.35);
  --blue:#60a5fa;
  --blue-dim:rgba(96,165,250,.12);
  --success:#34d399;
  --success-dim:rgba(52,211,153,.12);
  --danger:#f87171;
  --danger-dim:rgba(248,113,113,.12);
  --warn:#fbbf24;
  --warn-dim:rgba(251,191,36,.12);
  --text:#f5f5f4;
  --muted:#737373;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);
  color:var(--text);
  font-family:'Inter',system-ui,sans-serif;
  min-height:100vh;
  padding-bottom:48px;
}
/* Header */
header{
  background:var(--surface);
  border-bottom:1px solid var(--border);
  padding:14px 28px;
  display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;
}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{
  width:40px;height:40px;
  background:var(--accent);
  border-radius:9px;display:flex;align-items:center;justify-content:center;
  font-size:18px;
}
.logo h1{font-size:19px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}.logo h1 span{color:var(--accent)}
.hdr-right{display:flex;align-items:center;gap:20px}
.badge{display:flex;align-items:center;gap:7px;padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;border:1px solid}
.badge.active{border-color:rgba(52,211,153,.4);color:var(--success);background:var(--success-dim)}
.badge.inactive{border-color:rgba(248,113,113,.4);color:var(--danger);background:var(--danger-dim)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex-shrink:0}
.badge.active .dot{animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
/* Main */
main{max-width:1440px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;padding:20px 22px;
  transition:border-color .2s;
}
.card:hover{border-color:#444}
.card-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);margin-bottom:10px}
.card-value{font-size:32px;font-weight:800;letter-spacing:-1px;line-height:1;color:var(--text);margin:8px 0}
.card-sub{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.5}
.c-accent-1{color:var(--accent)}.c-accent-2{color:var(--blue)}.c-accent-3{color:var(--warn)}.c-success{color:var(--success)}.c-warn{color:var(--warn)}.c-danger{color:var(--danger)}
/* Controls */
.controls{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;padding:18px 22px;
}
.ctrl-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px}
.ctrl-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted)}
.btn{
  padding:8px 16px;border-radius:8px;font-size:12px;font-weight:500;
  border:1px solid transparent;cursor:pointer;
  transition:all .15s ease;display:inline-flex;align-items:center;gap:6px;white-space:nowrap;
  font-family:'Inter',system-ui,sans-serif;
}
.btn:active{opacity:.8}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-green,.btn-success{background:var(--success-dim);color:var(--success);border-color:rgba(52,211,153,.3)}
.btn-green:hover,.btn-success:hover{border-color:rgba(52,211,153,.6);background:rgba(52,211,153,.18)}
.btn-red,.btn-danger{background:var(--danger-dim);color:var(--danger);border-color:rgba(248,113,113,.3)}
.btn-red:hover,.btn-danger:hover{border-color:rgba(248,113,113,.6);background:rgba(248,113,113,.18)}
.btn-yellow,.btn-warn{background:var(--warn-dim);color:var(--warn);border-color:rgba(251,191,36,.3)}
.btn-yellow:hover,.btn-warn:hover{border-color:rgba(251,191,36,.6);background:rgba(251,191,36,.18)}
.btn-accent{background:var(--accent-dim);color:var(--accent);border-color:var(--accent-border)}
.btn-accent:hover{background:rgba(249,115,22,.2);border-color:rgba(249,115,22,.6)}
.btn-accent2{background:var(--blue-dim);color:var(--blue);border-color:rgba(96,165,250,.3)}
.btn-accent2:hover{background:rgba(96,165,250,.2);border-color:rgba(96,165,250,.6)}
.btn-muted{background:var(--surface-2);color:var(--muted);border-color:var(--border)}
.btn-muted:hover{background:#2e2e2e;color:var(--text);border-color:#444}
.sep{width:1px;height:28px;background:var(--border);margin:0 2px;flex-shrink:0}
/* Panel */
.panel{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;overflow:hidden;
}
.panel-hdr{
  padding:13px 18px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--surface-2);
}
.panel-title{font-size:12px;font-weight:700;display:flex;align-items:center;gap:9px;color:var(--text);text-transform:uppercase;letter-spacing:.6px}
.pill{font-size:10px;padding:3px 10px;border-radius:999px;font-weight:800;text-transform:uppercase}
.pill-success{background:rgba(74,222,128,.15);color:var(--success)}
.pill-accent{background:rgba(167,139,250,.18);color:var(--accent)}
/* Input fields */
input[type="text"],input[type="password"],input[type="email"],textarea,select{
  background:var(--surface-2);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:8px;
  padding:9px 12px;
  font-size:12.5px;
  transition:border-color .15s;
  font-family:'Inter',system-ui,sans-serif;
}
input[type="text"]:focus,input[type="password"]:focus,textarea:focus,select:focus{
  outline:none;
  border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(249,115,22,.15);
}
input[type="date"],input[type="datetime-local"]{
  background:var(--surface-2);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:8px;
  padding:9px 12px;
  font-size:12.5px;
  font-family:'Inter',system-ui,sans-serif;
}
/* Log table */
.tbl-wrap{overflow-y:auto;max-height:420px}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{padding:9px 14px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface-2);font-family:'Inter',system-ui,sans-serif}
td{padding:8px 14px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;font-family:'JetBrains Mono','Courier New',monospace;font-size:11px;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.03)}
.ev-change{color:var(--accent);font-weight:600}.ev-interval{color:var(--muted)}.ev-ts{color:var(--muted);font-size:10px}
/* Screenshots */
.shots-grid{padding:12px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px;overflow-y:auto;max-height:420px}
.shot-wrap{display:flex;flex-direction:column;gap:3px}
.shot{
  border-radius:8px;overflow:hidden;cursor:zoom-in;
  border:1px solid var(--border);
  transition:border-color .2s,transform .2s;
  aspect-ratio:16/10;
  background:var(--surface-2);
}
.shot:hover{border-color:#555;transform:scale(1.03)}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot-ts{font-size:9px;color:var(--muted);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Analysis */
.analysis{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;overflow:hidden;
  margin-bottom:20px;
}
.analysis-toolbar{
  padding:13px 18px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  background:var(--surface-2);
}
.analysis-output{
  padding:20px;
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:12px;
  line-height:1.8;
  white-space:pre-wrap;
  word-break:break-word;
  max-height:520px;
  overflow-y:auto;
  color:#a3a3a3;
}
.ao-done{color:var(--text)}.ao-error{color:var(--danger)}.ao-running{color:var(--warn);animation:pulse-text 1.2s infinite}
@keyframes pulse-text{0%,100%{opacity:1}50%{opacity:.6}}
/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:999;align-items:center;justify-content:center}
.modal.open{display:flex;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal img{max-width:95vw;max-height:90vh;border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.7);border:1px solid var(--border)}
.modal-x{position:fixed;top:20px;right:24px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);width:36px;height:36px;border-radius:8px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;z-index:1000;transition:background .15s}
.modal-x:hover{background:#333;border-color:#555}
/* Misc */
.rtag{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:7px;font-weight:600}
.rdot{width:6px;height:6px;border-radius:50%;background:var(--border)}
.rdot.on{background:var(--success)}
.empty{text-align:center;color:var(--muted);padding:32px 16px;font-size:12px}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#333;border-radius:3px}::-webkit-scrollbar-thumb:hover{background:#444}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,1fr)}.two-col{grid-template-columns:1fr}}
@media(max-width:640px){.cards{grid-template-columns:1fr}header{padding:12px 16px}main{padding:16px 12px}}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <h1>Flow<span>track</span></h1>
  </div>
  <div class="hdr-right">
    <div class="rtag"><span class="rdot" id="rdot"></span> auto-refresh 3 s</div>
    <div class="badge inactive" id="svcBadge">
      <span class="dot"></span><span id="svcTxt">checking…</span>
    </div>
  </div>
</header>

<main>

  <!-- ── Stat cards ── -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Focus Score</div>
      <div class="card-value c-accent" id="cScore">—</div>
      <div class="card-sub">out of 100 · based on today</div>
    </div>
    <div class="card">
      <div class="card-label">Tracker RAM</div>
      <div class="card-value c-green" id="cRam">—</div>
      <div class="card-sub" id="cRamSub">MB used by daemon</div>
    </div>
    <div class="card">
      <div class="card-label">Storage Used</div>
      <div class="card-value c-yellow" id="cStorage">—</div>
      <div class="card-sub" id="cStorageSub">MB in ~/.focusaudit/</div>
    </div>
    <div class="card">
      <div class="card-label">Events Today</div>
      <div class="card-value c-accent" id="cEvents">—</div>
      <div class="card-sub">window changes logged</div>
    </div>
  </div>

  <!-- ── Controls ── -->
  <div class="controls">
    <div class="ctrl-label">🎛️ Service &amp; Tools</div>
    <div class="ctrl-row">
      <button class="btn btn-green"  id="btnStart"   onclick="svc('start')">▶ Start Tracker</button>
      <button class="btn btn-red"    id="btnStop"    onclick="svc('stop')">■ Stop Tracker</button>
      <button class="btn btn-yellow" id="btnRestart" onclick="svc('restart')">↺ Restart</button>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-left:8px;cursor:pointer" title="Auto-start tracker and dashboard on login (Linux systemd only)">
        <input type="checkbox" id="autoStartToggle" onchange="toggleAutoStart(this.checked)" style="accent-color:var(--warn)">
        Auto-start on boot
      </label>
      <div class="sep"></div>
      <button class="btn btn-accent" onclick="runAnalysis(false)">📊 Run Text Analysis</button>
      <button class="btn btn-muted"  onclick="runAnalysis(true)">🤖 Run with Selected AI</button>
      <div class="sep"></div>
      <button class="btn btn-muted"  onclick="openFolder()">🗂 Open Screenshots</button>
      <button class="btn btn-muted"  onclick="syncJson()">☁ Backup JSON</button>
      <button class="btn btn-muted"  onclick="window.open('/api/logs?limit=500','_blank')">📄 Raw Log JSON</button>
    </div>
    <div class="ctrl-row" style="margin-top:12px">
      <label style="font-size:11px;color:var(--muted);font-weight:700">Cloud Backup (optional):</label>
      <select id="syncProvider" class="btn btn-muted" style="padding:7px 10px" onchange="updateBackupUI()">
        <option value="gist">GitHub Gist (private backup)</option>
        <option value="webhook">Webhook URL (POST to your server)</option>
      </select>
      <input id="syncTarget" placeholder="Webhook URL (required for webhook provider)" style="flex:1;min-width:260px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px" value="">
      <input id="syncApiKey" type="password" placeholder="GitHub token (required for gist provider)" style="flex:1;min-width:260px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px" value="">
      <button class="btn btn-muted" onclick="syncJson()">☁ Upload Now</button>
    </div>
    <div id="syncMsg" style="margin-top:8px;font-size:11px;color:var(--muted)">Cloud backup is optional. Choose provider, add credentials, then click Upload.</div>

    <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
      <div class="ctrl-row" style="margin-top:0">
        <label style="font-size:11px;color:var(--muted);font-weight:700">📅 Download/Backup Logs:</label>
        <select id="backupType" class="btn btn-muted" onchange="updateBackupDateUI()" style="padding:7px 10px">
          <option value="today">Today only</option>
          <option value="all">All logs</option>
          <option value="custom">Custom date range</option>
        </select>
      </div>
      <div id="dateRangeDiv" style="display:none;margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <label style="font-size:11px;color:var(--muted)">From:</label>
        <input id="backupStartDate" type="date" style="min-width:140px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px">
        <label style="font-size:11px;color:var(--muted)">To:</label>
        <input id="backupEndDate" type="date" style="min-width:140px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px">
      </div>
      <div class="ctrl-row" style="margin-top:10px;gap:8px">
        <button class="btn btn-accent" onclick="downloadBackup()">💾 Download to Laptop</button>
        <select id="uploadProvider" class="btn btn-muted" style="padding:7px 10px;min-width:140px" onchange="updateUploadUI()">
          <option value="none">No cloud upload</option>
          <option value="gist">GitHub Gist</option>
          <option value="gdrive">Google Drive</option>
          <option value="webhook">Webhook URL</option>
        </select>
        <input id="uploadTarget" placeholder="GitHub token / Google token / Webhook URL" style="flex:1;min-width:200px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px;display:none" value="">
        <button class="btn btn-accent2" onclick="uploadBackup()" style="display:none" id="uploadBtn">☁ Upload to Cloud</button>
      </div>
      <div id="backupMsg" style="margin-top:8px;font-size:11px;color:var(--muted)"></div>
    </div>

    <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
      <div class="ctrl-row" style="margin-top:0">
        <label style="font-size:11px;color:var(--muted);font-weight:700">📷 Idle Detection (Optional):</label>
        <label style="display:inline-flex;align-items:center;cursor:pointer;gap:6px">
          <input type="checkbox" id="cameraToggle" onchange="toggleCamera()" style="width:16px;height:16px;cursor:pointer">
          <span style="font-size:11px;color:var(--text)">Enable Camera Monitoring</span>
        </label>
      </div>
      <div id="cameraMsg" style="margin-top:8px;font-size:11px;display:none"></div>
      <p style="margin-top:8px;font-size:10px;color:var(--muted)">When enabled, Flowtrack will silently capture your face (no flash) every 5 minutes on idle windows. Helps detect if you're actually working or distracted. Completely optional.</p>
    </div>
  </div>

  <!-- ── Live Log + Screenshots ── -->
  <div class="two-col">

    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">📋 Live Activity Log <span class="pill pill-success">LIVE</span></div>
        <span style="font-size:11px;color:var(--muted)" id="logMeta">—</span>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Event</th><th>App</th><th style="max-width:none">Window Title</th><th>Sec</th>
          </tr></thead>
          <tbody id="logBody"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">📸 Recent Screenshots <span class="pill pill-accent" id="shotCount">0</span></div>
        <span style="font-size:11px;color:var(--muted)">48 h auto-purge</span>
      </div>
      <div class="shots-grid" id="shotsGrid">
        <div class="empty" style="grid-column:1/-1">Loading…</div>
      </div>
    </div>

  </div>

  <!-- ── AI Analysis output ── -->
  <div class="analysis">
    <div class="analysis-toolbar">
      <strong style="font-size:13px">🤖 AI Analysis Report</strong>
      <span style="font-size:11px;color:var(--muted)" id="analysisMeta">Idle</span>
      <button class="btn btn-muted" style="margin-left:auto" onclick="scrollToTop('analysisOut')">↑ Top</button>
    </div>
    <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <label style="font-size:11px;color:var(--muted);font-weight:700">Provider for analysis:</label>
      <select id="analysisProvider" class="btn btn-muted" onchange="updateAnalysisPlaceholders();refreshOllamaBar('analysis');updateModelList('analysis')" style="padding:7px 10px">
        <option value="ollama" selected>Ollama (default)</option>
        <option value="none">No AI (text only)</option>
        <option value="openai">OpenAI</option>
        <option value="anthropic">Anthropic</option>
        <option value="gemini">Gemini</option>
      </select>
      <input id="analysisModel" list="analysisModelList" placeholder="Model name" style="flex:1;min-width:200px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px" value="llama3">
      <datalist id="analysisModelList"></datalist>
      <input id="analysisApiKey" type="password" placeholder="Not needed for Ollama (required for cloud providers)" style="flex:1;min-width:240px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <button class="btn btn-yellow" onclick="verifyAnalysisKey()">✓ Verify Key</button>
    </div>
    <div id="ollamaBarAnalysis" style="display:none;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span id="ollamaStatusAnalysis" style="font-size:11px;color:var(--muted)">Checking Ollama…</span>
      <button class="btn btn-muted" style="font-size:11px;padding:4px 10px" onclick="ollamaStart('analysis')">▶ Start</button>
      <button class="btn btn-warn" style="font-size:11px;padding:4px 10px" onclick="ollamaFreeRAM('analysis')">⬡ Free RAM</button>
    </div>
    <div id="analysisKeyStatus" style="padding:0 20px 8px 20px;font-size:11px;color:var(--muted);display:none"></div>
    <div class="analysis-output ao-done" id="analysisOut">No analysis run yet. Click "Run Analysis" above.</div>
  </div>

  <div class="analysis">
    <div class="analysis-toolbar">
      <strong style="font-size:13px">💬 Ask AI About Your Patterns</strong>
      <span style="font-size:11px;color:var(--muted)">Provider and key are used in-memory only</span>
    </div>
    <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <select id="chatProvider" class="btn btn-muted" onchange="updateChatPlaceholders();refreshOllamaBar('chat');updateModelList('chat')" style="padding:7px 10px">
        <option value="ollama">Ollama (local, no key)</option>
        <option value="openai">OpenAI (requires API key)</option>
        <option value="anthropic">Anthropic (requires API key)</option>
        <option value="gemini">Gemini (requires API key)</option>
      </select>
      <input id="chatModel" list="chatModelList" placeholder="Model name" value="llama3" style="min-width:200px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <datalist id="chatModelList"></datalist>
      <input id="chatApiKey" type="password" placeholder="API key (not needed for Ollama)" style="min-width:240px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <input id="chatBaseUrl" placeholder="Custom base URL (optional)" style="min-width:220px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
    </div>
    <div style="padding:12px 20px;border-bottom:1px solid var(--border)">
      <textarea id="chatPrompt" placeholder="Ask anything. Example: What is my biggest distraction pattern this week and what should I change tomorrow?" style="width:100%;min-height:88px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px;font-size:12px;resize:vertical"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn btn-accent" onclick="chatAsk()">Send</button>
        <button class="btn btn-muted" onclick="fillChatTemplate()">Use suggestion template</button>
        <button class="btn btn-yellow" onclick="verifyChatKey()" style="margin-left:auto">✓ Test Key</button>
      </div>
    </div>
    <div id="ollamaBarChat" style="display:none;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span id="ollamaStatusChat" style="font-size:11px;color:var(--muted)">Checking Ollama…</span>
      <button class="btn btn-muted" style="font-size:11px;padding:4px 10px" onclick="ollamaStart('chat')">▶ Start</button>
      <button class="btn btn-warn" style="font-size:11px;padding:4px 10px" onclick="ollamaFreeRAM('chat')">⬡ Free RAM</button>
    </div>
    <div id="chatKeyStatus" style="padding:0 20px 8px 20px;font-size:11px;color:var(--muted);display:none"></div>
    <div class="analysis-output ao-done" id="chatOut">No chat yet.</div>
  </div>

</main>

<!-- Modal -->
<div class="modal" id="modal" onclick="closeModal()">
  <button class="modal-x" onclick="closeModal()">✕</button>
  <img id="modalImg" src="" alt="">
</div>

<script>
let pollTimer = null;

// ── API ───────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  try { return await (await fetch(path, opts)).json(); }
  catch { return null; }
}

// ── Refresh dot ───────────────────────────────────────────────────────────────
function flash() {
  const d = document.getElementById('rdot');
  d.classList.add('on');
  setTimeout(() => d.classList.remove('on'), 400);
}

// ── Status ────────────────────────────────────────────────────────────────────
async function fetchStatus() {
  const d = await api('/api/status');
  if (!d) return;

  const badge = document.getElementById('svcBadge');
  badge.className = 'badge ' + (d.active ? 'active' : 'inactive');
  document.getElementById('svcTxt').textContent = d.active ? 'Tracker Active' : 'Tracker Stopped';

  document.getElementById('btnStart').disabled   =  d.active;
  document.getElementById('btnStop').disabled    = !d.active;
  document.getElementById('btnRestart').disabled = !d.active;

  document.getElementById('cRam').textContent     = d.ram_mb + ' MB';
  document.getElementById('cStorage').textContent = d.total_mb + ' MB';
  document.getElementById('cStorageSub').textContent =
    d.screenshots_mb + ' MB screenshots · ' + d.logs_kb + ' KB logs · ' + d.screenshot_count + ' files';

  flash();
}

// ── Logs ──────────────────────────────────────────────────────────────────────
async function fetchLogs() {
  const d = await api('/api/logs?limit=120');
  if (!d) return;

  document.getElementById('logMeta').textContent = d.total + ' events today';
  document.getElementById('cEvents').textContent = d.total;
  document.getElementById('cScore').textContent  = d.focus_score;

  const rows = [...d.entries].reverse();
  const tbody = document.getElementById('logBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No events yet. Tracker logs every 30 s or on window change.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(e => {
    const t   = (e.ts || '').split('T')[1] || e.ts || '';
    const cls = e.event === 'change' ? 'ev-change' : 'ev-interval';
    const dur = e.duration !== undefined ? Math.round(e.duration) : '';
    const ttl = (e.title || '').replace(/</g,'&lt;').substring(0, 90);
    return `<tr>
      <td class="ev-ts">${t}</td>
      <td class="${cls}">${e.event || ''}</td>
      <td>${(e.app || 'unknown').substring(0, 16)}</td>
      <td style="max-width:280px" title="${ttl}">${ttl}</td>
      <td style="text-align:right;color:var(--muted)">${dur}</td>
    </tr>`;
  }).join('');
}

// ── Screenshots ───────────────────────────────────────────────────────────────
async function fetchShots() {
  const d = await api('/api/screenshots');
  if (!d) return;
  document.getElementById('shotCount').textContent = d.length;
  const grid = document.getElementById('shotsGrid');
  if (!d.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1">No screenshots yet. They appear within 30 s of the tracker starting.</div>';
    return;
  }
  grid.innerHTML = d.map(name => {
    const label = name.replace(/\.jpg$/, '').replace(/_/g, ' ');
    return `<div class="shot-wrap">
      <div class="shot" onclick="openModal('/screenshots/${name}')">
        <img src="/screenshots/${name}" loading="lazy" alt="${name}">
      </div>
      <div class="shot-ts">${label}</div>
    </div>`;
  }).join('');
}

// ── Service control ───────────────────────────────────────────────────────────
async function svc(action) {
  const labels = {start:'Starting…', stop:'Stopping…', restart:'Restarting…'};
  document.getElementById('svcTxt').textContent = labels[action] || action;
  await api('/api/service', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action}),
  });
  setTimeout(fetchStatus, 1800);
}

async function toggleAutoStart(enable) {
  const action = enable ? 'enable' : 'disable';
  try {
    const r = await api('/api/service', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
    const d = await r.json();
    if (!d.ok) {
      document.getElementById('svcTxt').textContent = d.error || 'Auto-start change failed';
      document.getElementById('autoStartToggle').checked = !enable;
    } else {
      document.getElementById('svcTxt').textContent = enable ? 'Auto-start enabled (starts on login)' : 'Auto-start disabled';
    }
  } catch(e) {
    document.getElementById('autoStartToggle').checked = !enable;
  }
}

async function checkAutoStart() {
  try {
    const r = await fetch('/api/autostart');
    if (!r.ok) return;
    const d = await r.json();
    const cb = document.getElementById('autoStartToggle');
    if (cb) cb.checked = d.enabled;
  } catch(e) {}
}

// ── Analysis ──────────────────────────────────────────────────────────────────
async function runAnalysis(useAI) {
  setAnalysisUI('running', 'Starting analysis…');
  document.getElementById('analysisMeta').textContent = 'Running…';
  
  let provider = '';
  let model = '';
  let apiKey = '';
  
  if (useAI) {
    provider = document.getElementById('analysisProvider').value;
    model = document.getElementById('analysisModel').value.trim();
    apiKey = document.getElementById('analysisApiKey').value.trim();
    
    if (provider !== 'none' && provider !== 'ollama' && !model) {
      setAnalysisUI('error', 'Error: Model name is required for ' + provider);
      return;
    }
  }
  
  await api('/api/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ai: useAI,
      provider: provider,
      model: model,
      api_key: apiKey,
    }),
  });
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollAnalysis, 1500);
}

async function pollAnalysis() {
  const d = await api('/api/analysis');
  if (!d) return;
  setAnalysisUI(d.status, d.output);
  if (d.status !== 'running') {
    clearInterval(pollTimer);
    pollTimer = null;
    document.getElementById('analysisMeta').textContent =
      d.status === 'done' ? 'Complete ✓' : 'Error ✗';
  }
}

function setAnalysisUI(status, text) {
  const el = document.getElementById('analysisOut');
  el.className = 'analysis-output ao-' + status;
  el.textContent = text;
}

function scrollToTop(id) {
  document.getElementById(id).scrollTop = 0;
}

// ── Open folder ───────────────────────────────────────────────────────────────
function openFolder() {
  api('/api/open-screenshots', {method: 'POST'});
}

function updateBackupUI() {
  const provider = document.getElementById('syncProvider').value;
  const target = document.getElementById('syncTarget');
  const apiKey = document.getElementById('syncApiKey');
  
  if (provider === 'webhook') {
    target.placeholder = 'Your webhook URL (e.g., https://example.com/webhook)';
    target.style.display = 'block';
    apiKey.placeholder = 'Leave empty for webhook';
    apiKey.style.display = 'block';
  } else {
    target.placeholder = 'Leave empty for gist';
    target.style.display = 'block';
    apiKey.placeholder = 'Your GitHub token (required for gist)';
    apiKey.style.display = 'block';
  }
}

async function syncJson() {
  const provider = document.getElementById('syncProvider').value;
  const target   = document.getElementById('syncTarget').value.trim();
  const apiKey   = document.getElementById('syncApiKey').value.trim();
  const msg = document.getElementById('syncMsg');
  
  if (provider === 'gist' && !apiKey) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Error: GitHub token is required for Gist backup.';
    return;
  }
  
  if (provider === 'webhook' && !target) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Error: Webhook URL is required for webhook backup.';
    return;
  }
  
  msg.style.color = 'var(--yellow)';
  msg.textContent = 'Uploading backup to ' + provider + '...';
  
  const d = await api('/api/sync-json', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({provider, target, api_key: apiKey}),
  });
  
  if (!d) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Backup failed: no response from server.';
    return;
  }
  
  if (d.ok) {
    msg.style.color = 'var(--green)';
    const url = d.url ? ' - View: ' + d.url : '';
    msg.textContent = '✓ ' + d.message + url;
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Backup failed: ' + (d.error || 'Unknown error');
  }
}

function fillChatTemplate() {
  document.getElementById('chatPrompt').value =
    'Analyze my recent Flowtrack behavior and give me: 1) top 3 focus problems with numbers, 2) practical fixes for tomorrow, 3) one simple rule I should enforce.';
}

// ── Model presets per provider ───────────────────────────────────────────────
const MODEL_PRESETS = {
  ollama:    ['llama3', 'llama3.2', 'llama3.1', 'llama3.2:1b', 'gemma3', 'gemma2', 'mistral', 'phi4', 'phi3', 'deepseek-r1', 'qwen2.5', 'codellama', 'nomic-embed-text'],
  openai:    ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo', 'o1-mini', 'o3-mini'],
  anthropic: ['claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022', 'claude-3-opus-20240229', 'claude-3-haiku-20240307'],
  gemini:    ['gemini-2.0-flash', 'gemini-2.0-flash-lite', 'gemini-1.5-pro', 'gemini-1.5-flash'],
};
const MODEL_DEFAULTS = {
  ollama: 'llama3', openai: 'gpt-4o-mini',
  anthropic: 'claude-3-5-sonnet-20241022', gemini: 'gemini-2.0-flash', none: '',
};

async function updateModelList(section) {
  const provider = document.getElementById(section + 'Provider').value;
  const list = document.getElementById(section + 'ModelList');
  const modelInput = document.getElementById(section + 'Model');
  if (!list) return;
  const presets = MODEL_PRESETS[provider] || [];
  let options = [...presets];
  // Merge in actually-installed Ollama models so locally pulled ones appear too.
  if (provider === 'ollama') {
    const d = await api('/api/ollama');
    if (d && d.models) {
      d.models.forEach(m => { if (!options.includes(m)) options.unshift(m); });
    }
  }
  list.innerHTML = options.map(m => `<option value="${m}">`).join('');
  // Auto-set default when switching providers.
  if (modelInput && MODEL_DEFAULTS[provider] !== undefined) {
    modelInput.value = MODEL_DEFAULTS[provider];
  }
}

function updateChatPlaceholders() {
  const provider = document.getElementById('chatProvider').value;
  const keyInput = document.getElementById('chatApiKey');
  const keyPH = {
    ollama: 'Not needed for Ollama — leave empty',
    openai: 'sk-... (OpenAI API key)',
    anthropic: 'sk-ant-... (Anthropic API key)',
    gemini: 'AIza... (Google AI API key)',
  };
  keyInput.placeholder = keyPH[provider] || 'API key';
}

function updateAnalysisPlaceholders() {
  const provider = document.getElementById('analysisProvider').value;
  const keyInput = document.getElementById('analysisApiKey');
  const keyPH = {
    ollama: 'Not needed for Ollama — leave empty',
    none:   'No AI selected',
    openai: 'sk-... (OpenAI API key)',
    anthropic: 'sk-ant-... (Anthropic API key)',
    gemini: 'AIza... (Google AI API key)',
  };
  keyInput.placeholder = keyPH[provider] || 'API key';
}

// ── Ollama on-demand controls ─────────────────────────────────────────────────
function _ollamaIds(section) {
  return {
    bar: document.getElementById('ollamaBar' + section.charAt(0).toUpperCase() + section.slice(1)),
    status: document.getElementById('ollamaStatus' + section.charAt(0).toUpperCase() + section.slice(1)),
    provider: document.getElementById(section + 'Provider'),
    model: document.getElementById(section + 'Model'),
  };
}

async function refreshOllamaBar(section) {
  const {bar, status, provider} = _ollamaIds(section);
  if (!bar) return;
  if (!provider || provider.value !== 'ollama') { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  status.style.color = 'var(--muted)';
  status.textContent = 'Checking Ollama…';
  const d = await api('/api/ollama');
  if (!d) { status.textContent = 'Could not reach dashboard backend.'; return; }
  if (!d.running) {
    status.style.color = 'var(--danger)';
    status.textContent = '● Not running — click ▶ Start or just send a message (auto-starts).';
  } else {
    const loaded = d.loaded && d.loaded.length ? d.loaded.join(', ') : 'none loaded';
    const models = d.models && d.models.length ? d.models.join(', ') : 'none installed';
    status.style.color = 'var(--success)';
    status.textContent = `● Running · In RAM: ${loaded} · Installed: ${models}`;
  }
}

async function ollamaStart(section) {
  const {status} = _ollamaIds(section);
  status.style.color = 'var(--warn)';
  status.textContent = 'Starting Ollama…';
  const d = await api('/api/ollama', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'start'}),
  });
  if (d && d.ok) { refreshOllamaBar(section); }
  else { status.style.color = 'var(--danger)'; status.textContent = (d && d.error) || 'Failed to start.'; }
}

async function ollamaFreeRAM(section) {
  const {status, model} = _ollamaIds(section);
  const modelName = (model && model.value.trim()) || 'llama3';
  status.style.color = 'var(--warn)';
  status.textContent = `Unloading ${modelName} from RAM…`;
  const d = await api('/api/ollama', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'unload', model: modelName}),
  });
  if (d && d.ok) {
    status.style.color = 'var(--muted)';
    status.textContent = `✓ ${d.message}`;
    setTimeout(() => refreshOllamaBar(section), 1500);
  } else {
    status.style.color = 'var(--danger)';
    status.textContent = (d && d.error) || 'Unload failed.';
  }
}

function ollamaErrHtml(errMsg) {
  const m = errMsg && errMsg.match(/Run:\s*(ollama\s+\S+(?:\s+\S+)?)/i);
  if (!m) return null;
  const cmd = m[1];
  return `<span style="color:var(--danger)">${errMsg.replace(cmd,'').replace('Run:','').trim()}</span>
<div style="display:flex;align-items:center;gap:8px;margin-top:8px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;">
  <code style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--accent);flex:1">Run: ${cmd}</code>
  <button onclick="navigator.clipboard.writeText('${cmd}').then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)})" style="background:var(--accent-dim);border:1px solid var(--accent-border);color:var(--accent);border-radius:5px;padding:3px 10px;font-size:11px;cursor:pointer;white-space:nowrap">Copy</button>
</div>`;
}

async function verifyChatKey() {
  const provider = document.getElementById('chatProvider').value;
  const model = document.getElementById('chatModel').value.trim() || 'llama3';
  const apiKey = document.getElementById('chatApiKey').value.trim();
  const baseUrl = document.getElementById('chatBaseUrl').value.trim();
  const statusDiv = document.getElementById('chatKeyStatus');
  
  if (provider !== 'ollama' && !apiKey) {
    statusDiv.style.color = 'var(--red)';
    statusDiv.textContent = 'API key is required for ' + provider + '.';
    statusDiv.style.display = 'block';
    return;
  }
  
  statusDiv.style.color = 'var(--yellow)';
  statusDiv.textContent = provider === 'ollama'
    ? 'Testing Ollama connection...'
    : ('Testing ' + provider + ' API key...');
  statusDiv.style.display = 'block';
  
  const testPrompt = 'Say "OK" and nothing else.';
  const d = await api('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      provider,
      model,
      api_key: apiKey,
      base_url: baseUrl,
      prompt: testPrompt,
    }),
  });
  
  if (d && d.ok) {
    statusDiv.style.color = 'var(--green)';
    statusDiv.textContent = provider === 'ollama'
      ? 'Ollama connection verified! Response: ' + (d.reply ? d.reply.substring(0, 100) : 'OK')
      : ('API key verified! Response: ' + (d.reply ? d.reply.substring(0, 100) : 'OK'));
  } else {
    statusDiv.style.color = 'var(--red)';
    statusDiv.textContent = 'Verification failed: ' + (d ? d.error : 'No response') + '. Check provider, model, and network.';
  }
}

async function verifyAnalysisKey() {
  const provider = document.getElementById('analysisProvider').value;
  const model = document.getElementById('analysisModel').value.trim() || 'llama3';
  const apiKey = document.getElementById('analysisApiKey').value.trim();
  const statusDiv = document.getElementById('analysisKeyStatus');
  
  if (provider === 'none') {
    statusDiv.textContent = '';
    statusDiv.style.display = 'none';
    return;
  }
  
  if (provider !== 'ollama' && !apiKey) {
    statusDiv.style.color = 'var(--red)';
    statusDiv.textContent = 'API key is required for ' + provider + '.';
    statusDiv.style.display = 'block';
    return;
  }
  
  statusDiv.style.color = 'var(--yellow)';
  statusDiv.textContent = provider === 'ollama'
    ? 'Testing Ollama connection for analysis...'
    : ('Testing ' + provider + ' API key for analysis...');
  statusDiv.style.display = 'block';
  
  const testPrompt = 'Say "OK" and nothing else.';
  const d = await api('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      provider,
      model: model || 'gpt-4o-mini',
      api_key: apiKey,
      prompt: testPrompt,
    }),
  });
  
  if (d && d.ok) {
    statusDiv.style.color = 'var(--green)';
    statusDiv.textContent = provider === 'ollama'
      ? 'Ollama connection verified for analysis!'
      : 'API key verified for analysis!';
  } else {
    statusDiv.style.color = 'var(--red)';
    statusDiv.textContent = 'Verification failed: ' + (d ? d.error : 'No response') + '. Check provider, model, and network.';
  }
}

async function chatAsk() {
  const prompt = document.getElementById('chatPrompt').value.trim();
  if (!prompt) return;
  const out = document.getElementById('chatOut');
  const sendBtn = document.querySelector('button[onclick="chatAsk()"]');
  out.className = 'analysis-output ao-running';
  out.textContent = 'Thinking… 0s';
  if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '…'; }

  // Elapsed-time counter so user knows it's working (Ollama on CPU can be slow).
  const t0 = Date.now();
  const ticker = setInterval(() => {
    if (out.className.includes('ao-running'))
      out.textContent = `Thinking… ${Math.round((Date.now()-t0)/1000)}s`;
  }, 1000);

  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), 660000); // 11 min hard cap

  let d = null;
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        provider: document.getElementById('chatProvider').value,
        model: document.getElementById('chatModel').value.trim() || 'llama3',
        api_key: document.getElementById('chatApiKey').value.trim(),
        base_url: document.getElementById('chatBaseUrl').value.trim(),
        prompt,
      }),
      signal: controller.signal,
    });
    d = await resp.json();
  } catch(e) {
    d = null;
  } finally {
    clearInterval(ticker);
    clearTimeout(abort);
    if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = 'Send'; }
  }

  if (!d) {
    out.className = 'analysis-output ao-error';
    out.textContent = 'Request timed out or no response from backend.\nFor Ollama on CPU, try a shorter prompt or switch to a cloud provider.';
    return;
  }
  if (d.ok) {
    out.className = 'analysis-output ao-done';
    out.textContent = d.reply;
  } else {
    out.className = 'analysis-output ao-error';
    const errMsg = d.error || 'Chat failed.';
    const html = ollamaErrHtml(errMsg);
    if (html) { out.style.fontFamily = 'inherit'; out.innerHTML = html; }
    else { out.textContent = errMsg; }
  }
}

// ── Camera Capture (new feature) ───────────────────────────────────────────────
async function toggleCamera() {
  const checkbox = document.getElementById('cameraToggle');
  const msg = document.getElementById('cameraMsg');
  msg.style.display = 'block';
  
  if (checkbox.checked) {
    // Request permission
    try {
      const stream = await navigator.mediaDevices.getUserMedia({video: {width: 1280, height: 720}, audio: false});
      stream.getTracks().forEach(track => track.stop());
      msg.textContent = '✓ Camera enabled! Idle detection active (silent, no flash).';
      msg.style.color = 'var(--success)';
      localStorage.setItem('flowtrack_camera_enabled', 'true');
    } catch (err) {
      checkbox.checked = false;
      if (err.name === 'NotAllowedError') {
        msg.textContent = '✗ Camera permission denied. Check browser privacy settings.';
      } else {
        msg.textContent = '✗ Camera not available: ' + err.message;
      }
      msg.style.color = 'var(--danger)';
    }
  } else {
    // Disable
    msg.textContent = '✓ Camera monitoring disabled.';
    msg.style.color = 'var(--muted)';
    localStorage.setItem('flowtrack_camera_enabled', 'false');
  }
}

// Check if camera was previously enabled
window.addEventListener('DOMContentLoaded', () => {
  const wasEnabled = localStorage.getItem('flowtrack_camera_enabled') === 'true';
  document.getElementById('cameraToggle').checked = wasEnabled;
});

// ── Backup Date Range (new feature) ────────────────────────────────────────────
function updateBackupDateUI() {
  const backupType = document.getElementById('backupType').value;
  const dateRangeDiv = document.getElementById('dateRangeDiv');
  dateRangeDiv.style.display = backupType === 'custom' ? 'flex' : 'none';
}

function updateUploadUI() {
  const provider = document.getElementById('uploadProvider').value;
  const targetInput = document.getElementById('uploadTarget');
  const uploadBtn = document.getElementById('uploadBtn');
  
  if (provider === 'none') {
    targetInput.style.display = 'none';
    uploadBtn.style.display = 'none';
  } else {
    targetInput.style.display = 'block';
    uploadBtn.style.display = 'block';
    if (provider === 'gist') {
      targetInput.placeholder = 'GitHub personal access token (required)';
      targetInput.type = 'password';
    } else if (provider === 'gdrive') {
      targetInput.placeholder = 'Google Drive API token or folder ID (required)';
      targetInput.type = 'text';
    } else if (provider === 'webhook') {
      targetInput.placeholder = 'Webhook URL (required)';
      targetInput.type = 'text';
    }
  }
}

async function downloadBackup() {
  const backupType = document.getElementById('backupType').value;
  const msg = document.getElementById('backupMsg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '⏳ Preparing download...';
  
  try {
    let startDate = '';
    let endDate = '';
    
    if (backupType === 'custom') {
      startDate = document.getElementById('backupStartDate').value;
      endDate = document.getElementById('backupEndDate').value;
      if (!startDate || !endDate) {
        msg.textContent = '✗ Please select both start and end dates.';
        msg.style.color = 'var(--danger)';
        return;
      }
    }
    
    const response = await fetch('/api/backup-download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({backup_type: backupType, start_date: startDate, end_date: endDate}),
    });
    
    if (!response.ok) {
      const err = await response.json();
      msg.textContent = '✗ Download failed: ' + (err.error || 'Unknown error');
      msg.style.color = 'var(--danger)';
      return;
    }
    
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `flowtrack-backup-${new Date().toISOString().slice(0,10)}.jsonl`;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
    
    msg.textContent = '✓ Backup downloaded successfully!';
    msg.style.color = 'var(--success)';
  } catch (err) {
    msg.textContent = '✗ Download error: ' + err.message;
    msg.style.color = 'var(--danger)';
  }
}

async function uploadBackup() {
  const backupType = document.getElementById('backupType').value;
  const provider = document.getElementById('uploadProvider').value;
  const credential = document.getElementById('uploadTarget').value.trim();
  const msg = document.getElementById('backupMsg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '⏳ Uploading...';
  
  if (!credential) {
    msg.textContent = '✗ Please enter ' + (provider === 'gist' ? 'GitHub token' : provider === 'gdrive' ? 'Google token' : 'webhook URL');
    msg.style.color = 'var(--danger)';
    return;
  }
  
  try {
    let startDate = '';
    let endDate = '';
    
    if (backupType === 'custom') {
      startDate = document.getElementById('backupStartDate').value;
      endDate = document.getElementById('backupEndDate').value;
      if (!startDate || !endDate) {
        msg.textContent = '✗ Please select both start and end dates.';
        msg.style.color = 'var(--danger)';
        return;
      }
    }
    
    const payload = {
      backup_type: backupType,
      start_date: startDate,
      end_date: endDate,
      provider: provider,
      credential: credential,
    };
    
    const response = await fetch('/api/backup-upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    
    const data = await response.json();
    if (data.ok) {
      msg.textContent = '✓ ' + (data.message || 'Uploaded successfully!');
      if (data.url) msg.textContent += ' View: ' + data.url;
      msg.style.color = 'var(--success)';
    } else {
      msg.textContent = '✗ Upload failed: ' + (data.error || 'Unknown error');
      msg.style.color = 'var(--danger)';
    }
  } catch (err) {
    msg.textContent = '✗ Upload error: ' + err.message;
    msg.style.color = 'var(--danger)';
  }
}

async function backupWithDateRange() {
  const backupType = document.getElementById('backupType').value;
  const provider = document.getElementById('syncProvider').value;
  const target = document.getElementById('syncTarget').value.trim();
  const apiKey = document.getElementById('syncApiKey').value.trim();
  const msg = document.getElementById('syncMsg');
  
  if (provider === 'gist' && !apiKey) {
    msg.textContent = '✗ GitHub token required for Gist.';
    msg.style.color = 'var(--danger)';
    return;
  }
  if (provider === 'webhook' && !target) {
    msg.textContent = '✗ Webhook URL required.';
    msg.style.color = 'var(--danger)';
    return;
  }
  
  let startDate = '';
  let endDate = '';
  
  if (backupType === 'custom') {
    startDate = document.getElementById('backupStartDate').value;
    endDate = document.getElementById('backupEndDate').value;
    if (!startDate || !endDate) {
      msg.textContent = '✗ Select start and end dates.';
      msg.style.color = 'var(--danger)';
      return;
    }
  }
  
  msg.style.color = 'var(--warn)';
  msg.textContent = 'Exporting ' + backupType + ' logs...';
  
  const d = await api('/api/backup-date-range', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      backup_type: backupType,
      start_date: startDate,
      end_date: endDate,
      provider,
      target,
      api_key: apiKey,
    }),
  });
  
  if (!d) {
    msg.style.color = 'var(--danger)';
    msg.textContent = '✗ No response from server.';
    return;
  }
  
  if (d.ok) {
    msg.style.color = 'var(--success)';
    if (d.download_url) {
      msg.innerHTML = '✓ ' + d.message + ' <a href="' + d.download_url + '" style="color:var(--accent);text-decoration:underline">Download here</a>';
    } else {
      msg.textContent = '✓ ' + d.message;
    }
  } else {
    msg.style.color = 'var(--danger)';
    msg.textContent = '✗ ' + (d.error || 'Backup failed.');
  }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(src) {
  document.getElementById('modalImg').src = src;
  document.getElementById('modal').classList.add('open');
}
function closeModal() {
  document.getElementById('modal').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Init ──────────────────────────────────────────────────────────────────────
function refresh() { fetchStatus(); fetchLogs(); }

fetchStatus();
fetchLogs();
fetchShots();
updateBackupUI();
updateBackupDateUI();
updateUploadUI();
updateChatPlaceholders();
updateAnalysisPlaceholders();
checkAutoStart();
refreshOllamaBar('chat');
refreshOllamaBar('analysis');
updateModelList('chat');
updateModelList('analysis');

// Load latest saved report on first open
api('/api/analysis').then(d => {
  if (d && d.status !== 'idle') setAnalysisUI(d.status, d.output);
});

setInterval(refresh, 3000);
setInterval(fetchShots, 8000);
</script>
</body>
</html>"""


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *_):
        pass  # silence access logs

    def _json(self, data: dict | list, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        # Restrict to same origin (localhost)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self._html(HTML)

        elif path == "/api/status":
            svc  = service_status()
            stor = storage_stats()
            self._json({**svc, **stor})

        elif path == "/api/ollama":
            # Report Ollama state: running, installed models, currently loaded models.
            try:
                with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
                    tags = json.loads(r.read())
                models = [m.get("name") for m in tags.get("models", []) if m.get("name")]
                loaded: list[str] = []
                try:
                    with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=3) as r:
                        ps = json.loads(r.read())
                    loaded = [m.get("name", "") for m in ps.get("models", []) if m.get("name")]
                except Exception:
                    pass
                self._json({"running": True, "models": models, "loaded": loaded})
            except Exception:
                self._json({"running": False, "models": [], "loaded": []})

        elif path == "/api/autostart":
            if platform.system() != "Linux":
                self._json({"enabled": False, "supported": False})
                return
            result = _sh(["systemctl", "--user", "is-enabled", SERVICE_NAME])
            self._json({"enabled": result.strip() == "enabled", "supported": True})

        elif path == "/api/logs":
            limit   = min(int(qs.get("limit", ["100"])[0]), 500)
            entries = today_events()
            self._json({
                "total":       len(entries),
                "entries":     entries[-limit:],
                "focus_score": _quick_focus(entries),
            })

        elif path == "/api/screenshots":
            self._json(recent_screenshots(12))

        elif path.startswith("/screenshots/"):
            # ── Security: strict filename validation prevents path traversal ──
            name = path[len("/screenshots/"):]
            if not re.fullmatch(r"[\w\-]+\.jpe?g", name):
                self.send_response(400); self.end_headers(); return
            f = SCREENSHOTS_DIR / name
            if not f.exists() or not f.is_file():
                self.send_response(404); self.end_headers(); return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        elif path == "/api/analysis":
            if _result["status"] == "idle":
                report = latest_report()
                if report:
                    self._json({"status": "done", "output": report})
                    return
            self._json(_result)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/ollama":
            action = str(body.get("action", ""))
            model = str(body.get("model", "llama3")).strip()
            if action == "start":
                ok = _ensure_ollama_running()
                self._json({"ok": ok, "message": "Ollama started." if ok else "Failed to start Ollama. Is it installed?"})
            elif action == "unload":
                # Sending keep_alive=0 tells Ollama to immediately evict the model from RAM/VRAM.
                try:
                    payload = json.dumps({"model": model, "keep_alive": 0}).encode()
                    req = urllib.request.Request(
                        "http://localhost:11434/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=10).close()
                    self._json({"ok": True, "message": f"'{model}' unloaded — RAM freed."})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)})
            else:
                self._json({"ok": False, "error": "Unknown action. Use start or unload."}, code=400)

        elif path == "/api/service":
            action = body.get("action", "")
            # Whitelist only safe systemctl actions
            if action in ("start", "stop", "restart"):
                if platform.system() != "Linux":
                    self._json({"ok": False, "error": "Service controls use systemd and are Linux-only. Run tracker manually on this OS."}, code=400)
                    return
                try:
                    subprocess.Popen(
                        ["systemctl", "--user", action, SERVICE_NAME],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    self._json({"ok": False, "error": f"Service action failed: {exc}"}, code=500)
                    return
            elif action in ("enable", "disable"):
                if platform.system() != "Linux":
                    self._json({"ok": False, "error": "Auto-start uses systemd and is Linux-only."}, code=400)
                    return
                units = [SERVICE_NAME, "flowtrack-dashboard.service"]
                for unit in units:
                    try:
                        subprocess.run(
                            ["systemctl", "--user", action, unit],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    except OSError:
                        pass
                enabled = action == "enable"
                self._json({"ok": True, "enabled": enabled})
                return
            self._json({"ok": True})

        elif path == "/api/analyze":
            provider = str(body.get("provider", ""))
            model = str(body.get("model", ""))
            api_key = str(body.get("api_key", ""))
            _start_analysis(bool(body.get("ai", False)), provider=provider, model=model, api_key=api_key)
            self._json({"ok": True, "status": "started"})

        elif path == "/api/open-screenshots":
          opener = "xdg-open"
          if platform.system() == "Darwin":
            opener = "open"
          elif platform.system() == "Windows":
            opener = "explorer"
          try:
            subprocess.Popen(
              [opener, str(SCREENSHOTS_DIR)],
              stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL,
            )
          except OSError as exc:
            self._json({"ok": False, "error": f"Open folder failed: {exc}"}, code=500)
            return
            self._json({"ok": True})

        elif path == "/api/sync-json":
          provider = str(body.get("provider", "gist"))
          target = str(body.get("target", ""))
          api_key = str(body.get("api_key", ""))
          result = sync_json_to_cloud(provider=provider, target=target, api_key=api_key)
          self._json(result)

        elif path == "/api/chat":
          provider = str(body.get("provider", "ollama"))
          model = str(body.get("model", "")).strip()
          api_key = str(body.get("api_key", ""))
          base_url = str(body.get("base_url", ""))
          if not model:
            if provider == "openai":
              model = "gpt-4o-mini"
            elif provider == "anthropic":
              model = "claude-3-5-sonnet-20241022"
            elif provider == "gemini":
              model = "gemini-2.0-flash"
            else:
              model = "llama3"
          prompt = str(body.get("prompt", "")).strip()
          if not prompt:
            self._json({"ok": False, "error": "Prompt is empty."}, code=400)
            return
          # Add short context so model responses stay grounded.
          entries = today_events()
          context = {
            "events_today": len(entries),
            "focus_score": _quick_focus(entries),
            "top_titles": [e.get("title", "")[:120] for e in entries[-12:]],
          }
          final_prompt = (
            "You are a productivity coach. Use this Flowtrack context first, then answer the user.\n"
            f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}\n\n"
            f"User question:\n{prompt}\n"
          )
          reply, err = query_llm(final_prompt, provider=provider, model=model, api_key=api_key, base_url=base_url)
          if reply:
            self._json({"ok": True, "reply": reply})
          else:
            self._json({"ok": False, "error": err or "LLM request failed. Check provider, model, API key, and network."})

        elif path == "/api/backup-download":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          
          # Export logs by date
          if backup_type == "custom":
            data = export_logs_by_date(start_date, end_date)
          elif backup_type == "today":
            data = export_logs_by_date()
          else:  # all
            data = {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "logs": {f.name: f.read_text(encoding="utf-8") for f in LOG_DIR.glob("*.jsonl")}}
          
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")})
            return
          
          # Convert to JSONL format (one JSON object per line)
          content = ""
          for filename, file_content in data.get("logs", {}).items():
            for line in file_content.strip().split("\n"):
              if line.strip():
                content += line + "\n"
          
          # Send as downloadable file
          self.send_response(200)
          self.send_header("Content-Type", "application/octet-stream")
          self.send_header("Content-Disposition", f'attachment; filename="flowtrack-backup-{datetime.datetime.now().strftime("%Y-%m-%d")}.jsonl"')
          self.send_header("Content-Length", len(content.encode()))
          self.end_headers()
          self.wfile.write(content.encode())

        elif path == "/api/backup-upload":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          provider = str(body.get("provider", "gist"))
          credential = str(body.get("credential", ""))
          
          if not credential:
            self._json({"ok": False, "error": f"Missing credential for {provider}"})
            return
          
          # Export logs by date
          if backup_type == "custom":
            data = export_logs_by_date(start_date, end_date)
          elif backup_type == "today":
            data = export_logs_by_date()
          else:  # all
            data = {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "logs": {f.name: f.read_text(encoding="utf-8") for f in LOG_DIR.glob("*.jsonl")}}
          
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")})
            return
          
          # Convert to JSONL
          content = ""
          for filename, file_content in data.get("logs", {}).items():
            for line in file_content.strip().split("\n"):
              if line.strip():
                content += line + "\n"
          
          # Upload based on provider
          if provider == "gist":
            try:
              url = "https://api.github.com/gists"
              payload = {
                "description": f"Flowtrack logs backup {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "public": False,
                "files": {"flowtrack-backup.jsonl": {"content": content}},
              }
              req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                  "Content-Type": "application/json",
                  "Authorization": f"Bearer {credential}",
                  "Accept": "application/vnd.github+json",
                },
                method="POST",
              )
              with urllib.request.urlopen(req, timeout=20) as resp:
                gist = json.loads(resp.read())
              if gist.get("html_url"):
                gist_url = gist.get("html_url", "")
                self._json({"ok": True, "message": "Uploaded to GitHub Gist", "url": gist_url})
              else:
                self._json({"ok": False, "error": "GitHub did not return a gist URL."})
            except urllib.error.HTTPError as e:
              detail = e.read().decode("utf-8", errors="replace")
              self._json({"ok": False, "error": f"GitHub upload failed (HTTP {e.code}): {detail[:300]}"})
            except Exception as e:
              self._json({"ok": False, "error": f"GitHub upload failed: {str(e)}"})
          
          elif provider == "gdrive":
            self._json({"ok": False, "error": "Google Drive upload coming soon. For now, use GitHub Gist or webhook."})
          
          elif provider == "webhook":
            try:
              payload = {"backup_data": content, "timestamp": datetime.datetime.now().isoformat(timespec="seconds"), "backup_type": backup_type}
              req = urllib.request.Request(
                credential,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
              )
              with urllib.request.urlopen(req, timeout=20) as resp:
                status = resp.status
              if status in [200, 201]:
                self._json({"ok": True, "message": "Webhook upload successful"})
              else:
                self._json({"ok": False, "error": f"Webhook returned {status}"})
            except urllib.error.HTTPError as e:
              detail = e.read().decode("utf-8", errors="replace")
              self._json({"ok": False, "error": f"Webhook upload failed (HTTP {e.code}): {detail[:300]}"})
            except Exception as e:
              self._json({"ok": False, "error": f"Webhook upload failed: {str(e)}"})
          
          else:
            self._json({"ok": False, "error": f"Unknown provider: {provider}"})

        elif path == "/api/backup-date-range":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          provider = str(body.get("provider", "gist"))
          target = str(body.get("target", ""))
          api_key = str(body.get("api_key", ""))
          
          # Export logs by date
          data = export_logs_by_date(start_date, end_date) if backup_type == "custom" else (
            export_logs_by_date() if backup_type == "today" else (
              {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "logs": {f.name: f.read_text(encoding="utf-8") for f in LOG_DIR.glob("*.jsonl")}} if backup_type == "all" else {}
            )
          )
          
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")})
            return
          
          # Upload to cloud
          result = sync_json_to_cloud(provider, target, api_key)
          if result["ok"]:
            self._json({"ok": True, "message": result.get("message", "Backup successful"), "url": result.get("url", "")})
          else:
            self._json({"ok": False, "error": result.get("error", "Backup failed")})

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    server = ThreadedServer((HOST, PORT), Handler)
    url    = f"http://{HOST}:{PORT}"
    print(f"Flowtrack Dashboard → {url}")
    print("Press Ctrl+C to stop.")
    # Auto-open browser (best-effort, non-blocking)
    opener = "xdg-open"
    if platform.system() == "Darwin":
        opener = "open"
    elif platform.system() == "Windows":
        opener = "explorer"
    subprocess.Popen(
        [opener, url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
