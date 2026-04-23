# Flowtrack

Private local productivity tracker with a browser dashboard.

Flowtrack tracks active windows, captures compressed grayscale screenshots, measures context switching, and gives AI feedback on focus patterns.

## Highlights

- Hard screenshot storage cap at 3 GB
- Automatic screenshot cleanup by age (48 hours) and by size cap
- JSONL logs kept forever unless user deletes them
- Browser dashboard at http://127.0.0.1:7070
- Start, stop, restart service from dashboard
- Live log table, screenshot gallery, RAM and storage cards
- AI analysis report from dashboard
- AI chat panel with provider selection
- Optional cloud JSON backup to private GitHub Gist or webhook

## Data model

Flowtrack stores data in ~/.focusaudit/

- logs/*.jsonl: permanent activity logs
- screenshots/*.jpg: compressed screenshots, auto cleaned
- reports/*.txt: analysis outputs
- tracker.log and dashboard.log: service logs

Storage policy:

- Screenshots older than 48 hours are deleted
- Screenshots are also capped to 3 GB max
- Oldest screenshots are removed first when cap is exceeded
- JSONL logs are never deleted automatically

## Privacy and security

- Dashboard listens on 127.0.0.1 only
- No public network exposure by default
- API keys are not stored by default
- API keys sent from dashboard are used in memory for request execution
- Screenshot file access is filename validated to block path traversal
- Service actions are command whitelisted: start, stop, restart only

Important:

- Window titles may contain sensitive text
- If you open banking or password reset tabs, the title may appear in JSON logs
- Exclude sensitive apps by editing tracker.py if needed

## Install on Ubuntu or Debian

```bash
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
bash install.sh
```

After install:

- Tracker service: focusaudit.service
- Dashboard service: flowtrack-dashboard.service
- Open UI: http://127.0.0.1:7070
- Shortcut command: flowtrack

## How to use

Open dashboard:

```bash
flowtrack
```

Optional CLI commands:

```bash
# live activity JSON
tail -f ~/.focusaudit/logs/$(date +%Y-%m-%d).jsonl

# run analysis without LLM
~/.focusaudit/venv/bin/python3 ~/.focusaudit/analyze.py --no-ai

# run analysis with default Ollama model
~/.focusaudit/venv/bin/python3 ~/.focusaudit/analyze.py

# run analysis with another provider
~/.focusaudit/venv/bin/python3 ~/.focusaudit/analyze.py --provider openai --model gpt-4o-mini --api-key YOUR_KEY

# view screenshots
eog ~/.focusaudit/screenshots/
```

## Cloud JSON backup

In dashboard:

1. Choose backup provider: GitHub Gist or Webhook
2. Add token or webhook URL
3. Click Backup JSON

Notes:

- Gist backups are created as private gists
- Webhook backups send a JSON payload with all JSONL content
- This feature is optional and off by default

## AI providers

Supported in analysis and chat:

- Ollama (local, no API key)
- OpenAI
- Anthropic
- Gemini

Defaults:

- Chat provider defaults to Ollama
- Analysis provider defaults to Ollama
- Default Ollama model is llama3
- API key is not required for Ollama

You can still change provider, model, API key, and optional custom base URL at any time.

## Native Windows and macOS usage

Flowtrack dashboard and analysis run natively on Windows and macOS.

Tracker integration notes:

- Linux: full service mode via systemd
- Windows/macOS: run tracker manually from terminal

### Windows setup (native)

1. Install Python 3.10+ from python.org
2. Install Ollama from ollama.com/download/windows
3. In PowerShell:

```powershell
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip mss Pillow
```

4. Start Ollama and pull model:

```powershell
ollama pull llama3
ollama run llama3
```

5. Start tracker and dashboard (manual mode):

```powershell
python tracker.py
```

Open a second terminal:

```powershell
cd Flowtrack
.venv\Scripts\Activate.ps1
python dashboard.py
```

Open http://127.0.0.1:7070 in browser.

### macOS setup (native)

1. Install Python 3.10+
2. Install Ollama from ollama.com/download/mac
3. In Terminal:

```bash
git clone https://github.com/saroj479/Flowtrack.git
cd Flowtrack
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip mss Pillow
```

4. Start Ollama and pull model:

```bash
ollama pull llama3
ollama run llama3
```

5. Start tracker and dashboard (manual mode):

```bash
python3 tracker.py
```

Open a second terminal:

```bash
cd Flowtrack
source .venv/bin/activate
python3 dashboard.py
```

Open http://127.0.0.1:7070 in browser.

Important for Windows/macOS:

- Service buttons in dashboard use systemd and are Linux-only
- Start/stop tracker manually from terminal on Windows/macOS
- All AI chat, AI analysis, backup download, and cloud backup features work in dashboard

## Troubleshooting

Service status:

```bash
systemctl --user status focusaudit
systemctl --user status flowtrack-dashboard
```

Live service logs:

```bash
journalctl --user -u focusaudit -f
journalctl --user -u flowtrack-dashboard -f
```

## License

MIT. See LICENSE.
