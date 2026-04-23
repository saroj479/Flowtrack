# Flowtrack

<p align="center">
  <img src="https://img.shields.io/github/license/saroj479/Flowtrack?color=00d9ff" alt="MIT License">
  <img src="https://img.shields.io/github/stars/saroj479/Flowtrack?style=flat&color=ffbe0b" alt="Stars">
  <img src="https://img.shields.io/github/issues/saroj479/Flowtrack?color=ff006e" alt="Issues">
  <img src="https://img.shields.io/github/actions/workflow/status/saroj479/Flowtrack/ci.yml?label=CI&color=00ff88" alt="CI">
  <img src="https://img.shields.io/badge/python-3.10%2B-8338ec" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-00d9ff" alt="Platform">
  <img src="https://img.shields.io/badge/AI-Ollama%20%7C%20OpenAI%20%7C%20Anthropic%20%7C%20Gemini-ff006e" alt="AI Providers">
</p>

<p align="center">
  <strong>Local-first productivity tracker with an AI-powered browser dashboard.</strong><br>
  All data stays on your machine. No cloud required. No accounts. No subscriptions.
</p>

---

## What it does

Flowtrack runs as a background service and:

- **Tracks active windows** — app name, window title, timestamps, and context switches
- **Captures screenshots** — grayscale JPEG, compressed, auto-cleaned (48h max age, 3 GB hard cap)
- **Serves a browser dashboard** at `http://127.0.0.1:7070` with live logs, screenshot gallery, and storage stats
- **Runs AI analysis** on your activity patterns using Ollama (local), OpenAI, Anthropic, or Gemini
- **AI chat panel** — ask questions about your own focus data
- **Backup your logs** — download as JSONL or push to a private GitHub Gist or webhook

Everything binds to `127.0.0.1` only. Nothing is ever sent anywhere unless you explicitly trigger a backup.

---

## Quick start (Linux / Ubuntu / Debian)

```bash
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
bash install.sh
```

After install:

```bash
flowtrack          # opens http://127.0.0.1:7070 in your browser
```

Tracker and dashboard run as systemd user services automatically.

---

## Data stored

All data lives in `~/.focusaudit/`:

| Path | Content |
|------|---------|
| `logs/YYYY-MM-DD.jsonl` | Activity events (permanent unless deleted) |
| `screenshots/*.jpg` | Compressed grayscale screenshots (auto-cleaned) |
| `reports/analysis_*.txt` | AI analysis outputs |
| `tracker.log` / `dashboard.log` | Service logs |

Storage policy: screenshots > 48h are deleted; hard cap at 3 GB (oldest first). JSONL logs are never auto-deleted.

---

## AI providers

**Ollama is the default — no API key needed.**

| Provider | Key required | Default model |
|----------|-------------|---------------|
| Ollama (local) | No | `llama3` |
| OpenAI | Yes | `gpt-4o-mini` |
| Anthropic | Yes | `claude-3-haiku-20240307` |
| Gemini | Yes | `gemini-1.5-flash` |

Install Ollama: [ollama.com](https://ollama.com) → `ollama pull llama3`

---

## Privacy and security

- Dashboard binds to `127.0.0.1` only — never exposed to the network
- No telemetry, no analytics, no external connections by default
- API keys used in-memory per request — never written to disk
- Screenshot filenames validated to block path traversal
- Service actions whitelisted to `start`, `stop`, `restart` only
- Window titles may capture sensitive text (banking pages etc.) — add exclusions in `tracker.py`

---

## Platform support

| Platform | Tracker | Dashboard | Service management |
|----------|---------|-----------|--------------------|
| Linux (Ubuntu/Debian) | ✅ systemd auto | ✅ systemd auto | ✅ dashboard buttons |
| macOS | ✅ manual terminal | ✅ manual terminal | terminal only |
| Windows | ✅ manual terminal | ✅ manual terminal | terminal only |

### macOS setup

```bash
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip mss Pillow
ollama pull llama3
```

Terminal 1 — tracker:
```bash
source .venv/bin/activate && python3 tracker.py
```

Terminal 2 — dashboard:
```bash
source .venv/bin/activate && python3 dashboard.py
```

Open `http://127.0.0.1:7070`

### Windows setup (PowerShell)

```powershell
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip mss Pillow
```

Download Ollama from [ollama.com/download/windows](https://ollama.com/download/windows), then `ollama pull llama3`

Terminal 1:
```powershell
.venv\Scripts\Activate.ps1; python tracker.py
```

Terminal 2:
```powershell
.venv\Scripts\Activate.ps1; python dashboard.py
```

Open `http://127.0.0.1:7070`

---

## CLI usage

```bash
# Live activity stream
tail -f ~/.focusaudit/logs/$(date +%Y-%m-%d).jsonl

# Analysis without AI
~/.focusaudit/venv/bin/python3 ~/.focusaudit/analyze.py --no-ai

# Analysis with OpenAI
~/.focusaudit/venv/bin/python3 ~/.focusaudit/analyze.py --provider openai --model gpt-4o-mini --api-key YOUR_KEY
```

---

## Cloud backup

In the dashboard Backup section:

1. Pick scope: **Today**, **All time**, or **Custom date range**
2. Click **Download** — saves a `.jsonl` file to your machine
3. Optional: select **GitHub Gist** or **Webhook** and click **Upload**

---

## Troubleshooting (Linux)

```bash
systemctl --user status focusaudit
systemctl --user status flowtrack-dashboard
journalctl --user -u flowtrack-dashboard -f
curl http://localhost:11434/api/tags    # check Ollama
```

---

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

- Bugs → [GitHub Issues](https://github.com/saroj479/Flowtrack/issues) with the bug report template
- Features → [GitHub Issues](https://github.com/saroj479/Flowtrack/issues) with the feature request template
- Code → fork, branch, PR targeting `master`

All PRs are reviewed and merged by [@saroj479](https://github.com/saroj479).

---

## Roadmap

- [ ] Google Drive backup
- [ ] Vision AI for screenshot analysis
- [ ] Auto-start on boot option in dashboard
- [ ] Incognito window detection
- [ ] CSV export

---

## License

[MIT](LICENSE) — Copyright (c) 2026 saroj479
