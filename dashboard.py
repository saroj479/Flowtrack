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
import re
import subprocess
import threading
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


def query_llm(prompt: str, provider: str, model: str, api_key: str, base_url: str = "") -> str | None:
    provider = provider.lower().strip()
    try:
        if provider == "ollama":
            payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
            req = urllib.request.Request(
                base_url or OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read()).get("response", "").strip()

        if provider == "openai":
            if not api_key:
                return None
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
                return data["choices"][0]["message"]["content"].strip()

        if provider == "anthropic":
            if not api_key:
                return None
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
                return parts[0].get("text", "").strip() if parts else None

        if provider == "gemini":
            if not api_key:
                return None
            url = (base_url or GEMINI_URL_TMPL).format(model=model, key=api_key)
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError, IndexError, TypeError):
        return None
    return None


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


def _start_analysis(use_ai: bool) -> None:
    global _running, _result

    def _work() -> None:
        global _running, _result
        try:
            cmd = [VENV_PYTHON, ANALYZE_SCRIPT]
            if not use_ai:
                cmd.append("--no-ai")
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
<style>
:root{
  --bg:#0f172a;--card:#1e293b;--border:#334155;
  --accent:#818cf8;--green:#34d399;--red:#f87171;
  --yellow:#fbbf24;--text:#e2e8f0;--muted:#94a3b8;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;min-height:100vh;padding-bottom:48px}
/* Header */
header{background:#0f172a;border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;backdrop-filter:blur(8px)}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:34px;height:34px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:18px}
.logo h1{font-size:20px;font-weight:700;letter-spacing:-.5px}
.logo h1 span{color:var(--accent)}
.hdr-right{display:flex;align-items:center;gap:16px}
.badge{display:flex;align-items:center;gap:7px;padding:5px 13px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--border)}
.badge.active{border-color:var(--green);color:var(--green);background:rgba(52,211,153,.08)}
.badge.inactive{border-color:var(--red);color:var(--red);background:rgba(248,113,113,.08)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.badge.active .dot{animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
/* Main */
main{max-width:1440px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:24px}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 22px}
.card-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:10px}
.card-value{font-size:34px;font-weight:800;letter-spacing:-1.5px;line-height:1}
.card-sub{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.4}
.c-accent{color:var(--accent)}.c-green{color:var(--green)}.c-yellow{color:var(--yellow)}.c-red{color:var(--red)}
/* Controls */
.controls{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 22px}
.ctrl-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}
.ctrl-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
.btn{padding:8px 18px;border-radius:8px;font-size:12px;font-weight:700;border:1px solid transparent;cursor:pointer;transition:opacity .15s,transform .1s;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:active{transform:scale(.96)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.btn-green{background:rgba(52,211,153,.12);color:var(--green);border-color:rgba(52,211,153,.25)}
.btn-red{background:rgba(248,113,113,.12);color:var(--red);border-color:rgba(248,113,113,.25)}
.btn-yellow{background:rgba(251,191,36,.12);color:var(--yellow);border-color:rgba(251,191,36,.25)}
.btn-accent{background:rgba(129,140,248,.12);color:var(--accent);border-color:rgba(129,140,248,.25)}
.btn-muted{background:rgba(148,163,184,.08);color:var(--muted);border-color:var(--border)}
.sep{width:1px;height:28px;background:var(--border);margin:0 2px}
/* Two column */
.two-col{display:grid;grid-template-columns:1fr 400px;gap:18px;align-items:start}
/* Panel */
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.panel-hdr{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.panel-title{font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px}
.pill{font-size:10px;padding:2px 8px;border-radius:999px;font-weight:700}
.pill-green{background:rgba(52,211,153,.12);color:var(--green)}
.pill-accent{background:rgba(129,140,248,.12);color:var(--accent)}
/* Log table */
.tbl-wrap{overflow-y:auto;max-height:420px}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{padding:8px 12px;text-align:left;color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)}
td{padding:6px 12px;border-bottom:1px solid rgba(51,65,85,.5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;font-family:monospace}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}
.ev-change{color:var(--accent)}.ev-interval{color:var(--muted)}.ev-ts{color:var(--muted);font-size:10.5px}
/* Screenshots */
.shots-grid{padding:14px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px;overflow-y:auto;max-height:420px}
.shot-wrap{display:flex;flex-direction:column;gap:3px}
.shot{border-radius:7px;overflow:hidden;cursor:zoom-in;border:2px solid transparent;transition:border-color .15s,transform .12s;aspect-ratio:16/10;background:#0f172a}
.shot:hover{border-color:var(--accent);transform:scale(1.03)}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot-ts{font-size:9.5px;color:var(--muted);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Analysis */
.analysis{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.analysis-toolbar{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.analysis-output{padding:18px 20px;font-family:monospace;font-size:12px;line-height:1.75;white-space:pre-wrap;word-break:break-word;max-height:520px;overflow-y:auto;color:var(--muted)}
.ao-done{color:var(--text)}.ao-error{color:var(--red)}.ao-running{color:var(--yellow);animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.5}}
/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:999;align-items:center;justify-content:center}
.modal.open{display:flex}
.modal img{max-width:95vw;max-height:90vh;border-radius:10px;box-shadow:0 30px 80px rgba(0,0,0,.8)}
.modal-x{position:fixed;top:18px;right:22px;background:var(--card);border:1px solid var(--border);color:var(--text);width:34px;height:34px;border-radius:50%;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;z-index:1000}
/* Misc */
.rtag{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px}
.rdot{width:5px;height:5px;border-radius:50%;background:var(--muted);transition:background .3s}
.rdot.on{background:var(--green)}
.empty{text-align:center;color:var(--muted);padding:32px 16px;font-size:12px}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,1fr)}.two-col{grid-template-columns:1fr}}
@media(max-width:640px){.cards{grid-template-columns:1fr}header{padding:11px 14px}main{padding:14px 12px}}
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
      <div class="sep"></div>
      <button class="btn btn-accent" onclick="runAnalysis(false)">🤖 Run Analysis</button>
      <button class="btn btn-muted"  onclick="runAnalysis(true)">🧠 Run with Ollama AI</button>
      <div class="sep"></div>
      <button class="btn btn-muted"  onclick="openFolder()">🗂 Open Screenshots</button>
      <button class="btn btn-muted"  onclick="syncJson()">☁ Backup JSON</button>
      <button class="btn btn-muted"  onclick="window.open('/api/logs?limit=500','_blank')">📄 Raw Log JSON</button>
    </div>
    <div class="ctrl-row" style="margin-top:12px">
      <select id="syncProvider" class="btn btn-muted" style="padding:7px 10px">
        <option value="gist">GitHub Gist (private)</option>
        <option value="webhook">Webhook URL</option>
      </select>
      <input id="syncTarget" placeholder="Webhook URL (only for webhook provider)" style="flex:1;min-width:260px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <input id="syncApiKey" type="password" placeholder="GitHub token (for gist provider)" style="flex:1;min-width:260px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
    </div>
    <div id="syncMsg" style="margin-top:8px;font-size:11px;color:var(--muted)">Cloud backup is optional and disabled by default.</div>
  </div>

  <!-- ── Live Log + Screenshots ── -->
  <div class="two-col">

    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">📋 Live Activity Log <span class="pill pill-green">LIVE</span></div>
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
      <strong style="font-size:13px">🤖 Analysis Report</strong>
      <span style="font-size:11px;color:var(--muted)" id="analysisMeta">Idle</span>
      <button class="btn btn-muted" style="margin-left:auto" onclick="scrollToTop('analysisOut')">↑ Top</button>
    </div>
    <div class="analysis-output ao-done" id="analysisOut">No analysis run yet. Click "Run Analysis" above.</div>
  </div>

  <div class="analysis">
    <div class="analysis-toolbar">
      <strong style="font-size:13px">💬 Ask AI About Your Patterns</strong>
      <span style="font-size:11px;color:var(--muted)">Provider and key are used in-memory only</span>
    </div>
    <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap">
      <select id="chatProvider" class="btn btn-muted" style="padding:7px 10px">
        <option value="ollama">Ollama</option>
        <option value="openai">OpenAI</option>
        <option value="anthropic">Anthropic</option>
        <option value="gemini">Gemini</option>
      </select>
      <input id="chatModel" placeholder="Model, for example llama3 or gpt-4o-mini" value="llama3" style="min-width:240px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <input id="chatApiKey" type="password" placeholder="API key (not needed for Ollama)" style="min-width:240px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <input id="chatBaseUrl" placeholder="Custom base URL (optional)" style="min-width:220px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
    </div>
    <div style="padding:12px 20px;border-bottom:1px solid var(--border)">
      <textarea id="chatPrompt" placeholder="Ask anything. Example: What is my biggest distraction pattern this week and what should I change tomorrow?" style="width:100%;min-height:88px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px;font-size:12px;resize:vertical"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn btn-accent" onclick="chatAsk()">Send</button>
        <button class="btn btn-muted" onclick="fillChatTemplate()">Use suggestion template</button>
      </div>
    </div>
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

// ── Analysis ──────────────────────────────────────────────────────────────────
async function runAnalysis(useAI) {
  setAnalysisUI('running', 'Starting analysis…');
  document.getElementById('analysisMeta').textContent = 'Running…';
  await api('/api/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ai: useAI}),
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

async function syncJson() {
  const provider = document.getElementById('syncProvider').value;
  const target   = document.getElementById('syncTarget').value.trim();
  const apiKey   = document.getElementById('syncApiKey').value.trim();
  const msg = document.getElementById('syncMsg');
  msg.style.color = 'var(--yellow)';
  msg.textContent = 'Running cloud backup...';
  const d = await api('/api/sync-json', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({provider, target, api_key: apiKey}),
  });
  if (!d) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Backup failed: no response.';
    return;
  }
  if (d.ok) {
    msg.style.color = 'var(--green)';
    msg.textContent = d.url ? (d.message + ' ' + d.url) : d.message;
  } else {
    msg.style.color = 'var(--red)';
    msg.textContent = d.error || 'Backup failed.';
  }
}

function fillChatTemplate() {
  document.getElementById('chatPrompt').value =
    'Analyze my recent Flowtrack behavior and give me: 1) top 3 focus problems with numbers, 2) practical fixes for tomorrow, 3) one simple rule I should enforce.';
}

async function chatAsk() {
  const prompt = document.getElementById('chatPrompt').value.trim();
  if (!prompt) return;
  const out = document.getElementById('chatOut');
  out.className = 'analysis-output ao-running';
  out.textContent = 'Thinking...';
  const payload = {
    provider: document.getElementById('chatProvider').value,
    model: document.getElementById('chatModel').value.trim() || 'llama3',
    api_key: document.getElementById('chatApiKey').value.trim(),
    base_url: document.getElementById('chatBaseUrl').value.trim(),
    prompt,
  };
  const d = await api('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!d) {
    out.className = 'analysis-output ao-error';
    out.textContent = 'No response from backend.';
    return;
  }
  if (d.ok) {
    out.className = 'analysis-output ao-done';
    out.textContent = d.reply;
  } else {
    out.className = 'analysis-output ao-error';
    out.textContent = d.error || 'Chat failed.';
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

        if path == "/api/service":
            action = body.get("action", "")
            # Whitelist only safe systemctl actions
            if action in ("start", "stop", "restart"):
                subprocess.Popen(
                    ["systemctl", "--user", action, SERVICE_NAME],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._json({"ok": True})

        elif path == "/api/analyze":
            _start_analysis(bool(body.get("ai", False)))
            self._json({"ok": True, "status": "started"})

        elif path == "/api/open-screenshots":
            subprocess.Popen(
                ["xdg-open", str(SCREENSHOTS_DIR)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._json({"ok": True})

        elif path == "/api/sync-json":
          provider = str(body.get("provider", "gist"))
          target = str(body.get("target", ""))
          api_key = str(body.get("api_key", ""))
          result = sync_json_to_cloud(provider=provider, target=target, api_key=api_key)
          self._json(result)

        elif path == "/api/chat":
          provider = str(body.get("provider", "ollama"))
          model = str(body.get("model", "llama3"))
          api_key = str(body.get("api_key", ""))
          base_url = str(body.get("base_url", ""))
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
          reply = query_llm(final_prompt, provider=provider, model=model, api_key=api_key, base_url=base_url)
          if reply:
            self._json({"ok": True, "reply": reply})
          else:
            self._json({"ok": False, "error": "LLM request failed. Check provider, model, API key, and network."})

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
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
