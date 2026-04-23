#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  FocusAudit — one-shot installer
#
#  What it does:
#    1. Installs system packages (xdotool, scrot) if missing
#    2. Creates ~/.focusaudit directory layout
#    3. Creates a Python venv and installs mss + Pillow
#    4. Copies tracker.py and analyze.py to ~/.focusaudit/
#    5. Installs and enables the systemd *user* service
#
#  Usage:  bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FOCUSAUDIT_HOME="$HOME/.focusaudit"
VENV_DIR="$FOCUSAUDIT_HOME/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="focusaudit.service"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✔]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✘]${NC} $*"; exit 1; }

echo ""
echo "══════════════════════════════════════════════════════"
echo "  FocusAudit Installer"
echo "══════════════════════════════════════════════════════"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
info "Checking system dependencies …"

MISSING=""
command -v python3 &>/dev/null || MISSING="$MISSING python3 python3-venv"
command -v xdotool &>/dev/null || MISSING="$MISSING xdotool"
command -v scrot   &>/dev/null || MISSING="$MISSING scrot"

if [[ -n "$MISSING" ]]; then
    warn "Installing missing packages:$MISSING"
    sudo apt-get install -y $MISSING
else
    info "All system packages present."
fi

# ── 2. Directory layout ───────────────────────────────────────────────────────
info "Creating ~/.focusaudit directory structure …"
mkdir -p "$FOCUSAUDIT_HOME"/{screenshots,logs,reports}

# ── 3. Python virtual environment ────────────────────────────────────────────
info "Setting up Python virtual environment at $VENV_DIR …"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet mss Pillow
info "Python dependencies installed (mss, Pillow)."

# ── 4. Copy scripts ───────────────────────────────────────────────────────────
info "Installing tracker and analyzer scripts …"

for script in tracker.py analyze.py; do
    src="$SCRIPT_DIR/$script"
    dst="$FOCUSAUDIT_HOME/$script"
    if [[ ! -f "$src" ]]; then
        error "Source file not found: $src"
    fi
    cp "$src" "$dst"
    chmod +x "$dst"
done

# Convenience wrapper so you can run  focusaudit-analyze  from anywhere
WRAPPER="$HOME/.local/bin/focusaudit-analyze"
mkdir -p "$HOME/.local/bin"
cat > "$WRAPPER" << WRAPPER_EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python3" "$FOCUSAUDIT_HOME/analyze.py" "\$@"
WRAPPER_EOF
chmod +x "$WRAPPER"
info "Analyzer wrapper installed at $WRAPPER"

# ── 5. systemd user service ───────────────────────────────────────────────────
info "Installing systemd user service …"
mkdir -p "$SERVICE_DIR"

# Generate the service file (inline so install.sh is self-contained)
cat > "$SERVICE_DIR/$SERVICE_NAME" << SERVICE_EOF
[Unit]
Description=FocusAudit — Window Activity Tracker
Documentation=file://%h/.focusaudit/tracker.log
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python3 $FOCUSAUDIT_HOME/tracker.py
Restart=on-failure
RestartSec=15
PassEnvironment=DISPLAY XAUTHORITY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS
Environment="DISPLAY=:1"
Environment="PYTHONUNBUFFERED=1"
StandardOutput=append:$FOCUSAUDIT_HOME/service.log
StandardError=append:$FOCUSAUDIT_HOME/service.log
MemoryMax=150M
MemoryHigh=80M
NoNewPrivileges=true

[Install]
WantedBy=graphical-session.target
SERVICE_EOF

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

# Start (best-effort — may fail if graphical-session.target is not yet active)
if systemctl --user start "$SERVICE_NAME" 2>/dev/null; then
    info "Service started successfully."
else
    warn "Could not start service immediately (no active graphical session?)."
    warn "It will start automatically on your next login."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════"
echo "  Installation complete!"
echo "══════════════════════════════════════════════════════"
echo ""

systemctl --user status "$SERVICE_NAME" --no-pager 2>/dev/null || true

echo ""
echo "  Useful commands"
echo "  ───────────────────────────────────────────────────"
echo "  Live log   :  tail -f $FOCUSAUDIT_HOME/logs/\$(date +%Y-%m-%d).jsonl"
echo "  AI report  :  focusaudit-analyze"
echo "  AI report  :  $VENV_DIR/bin/python3 $FOCUSAUDIT_HOME/analyze.py"
echo "  Stop       :  systemctl --user stop $SERVICE_NAME"
echo "  Restart    :  systemctl --user restart $SERVICE_NAME"
echo "  Service log:  journalctl --user -u $SERVICE_NAME -f"
echo "  Screenshots:  ls $FOCUSAUDIT_HOME/screenshots/"
echo ""
