# Contributing to Flowtrack

Thank you for your interest in Flowtrack! Contributions are welcome.

This document explains how to get started.

---

## Project overview

Flowtrack is a local-first productivity tracker. It logs active windows, captures screenshots, and provides an AI-powered browser dashboard — all running on your own machine with no cloud dependency.

Key facts for contributors:
- **Single-file web app**: `dashboard.py` serves the full REST API and embedded HTML/JS/CSS UI
- **Pure stdlib**: No Flask, Django, or `requests` — only Python standard library + optional PIL and `xdotool`
- **Cross-platform**: Linux (primary, with systemd), Windows (native PowerShell tracker), macOS (native AppleScript tracker)
- **Local only**: Dashboard binds to `127.0.0.1:7070` — never exposed to the network

---

## Good first issues

Look for issues labelled `good first issue`. Some areas that always welcome help:

- Improving error messages and edge case handling
- Adding tests
- Improving cross-platform compatibility (Windows/macOS)
- Documentation and README improvements

---

## How to contribute

1. **Fork** the repository on GitHub
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/Flowtrack.git
   cd Flowtrack
   ```
3. **Create a branch** for your change:
   ```bash
   git checkout -b fix/your-change-name
   ```
4. **Make your changes**
5. **Test locally** (see below)
6. **Push** your branch to your fork
7. **Open a Pull Request** targeting the `master` branch of the main repo

Your PR will be reviewed before being merged. Only the maintainer (@saroj479) merges PRs.

---

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\Activate.ps1       # Windows PowerShell

pip install -r requirements.txt

# Run dashboard locally (no systemd needed)
python3 dashboard.py
# Open http://127.0.0.1:7070
```

---

## Testing your changes

Run the syntax check:

```bash
python3 -m py_compile dashboard.py tracker.py analyze.py
```

Manual checklist before submitting a PR:
- Dashboard loads at http://127.0.0.1:7070
- AI chat works (use Ollama with a model installed, or use any provider with a real key)
- Backup download triggers a file download in browser
- No Python errors in `~/.focusaudit/dashboard.log`

---

## Code style

- Follow the existing style in each file — no PEP8 reformatting of unchanged lines
- Keep everything in stdlib where possible (no new external dependencies)
- Do not hardcode paths outside of `~/.focusaudit/`
- Do not log or store API keys
- Security: this is a local trust-boundary app — `127.0.0.1` binding is intentional, but no path traversal or injection bugs please

---

## Pull request rules

- **One concern per PR** — don't bundle unrelated changes
- **Link the issue** your PR addresses
- **Fill in the PR template** — incomplete PRs will not be reviewed
- PRs that add dependencies without a strong reason will be rejected

---

## Reporting bugs

Use [GitHub Issues](https://github.com/saroj479/Flowtrack/issues) with the bug report template. Include your OS, Python version, and the relevant log file contents.

---

## License

By contributing, you agree your contributions will be licensed under the same [MIT License](LICENSE) as the project.
