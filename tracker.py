#!/usr/bin/env python3
"""
FocusAudit - High-Frequency Window Activity Tracker (Modules 1 & 2)

Polls the active window title every second. Logs an entry on every title
change OR every 30 seconds if the title is unchanged. Captures a
compressed grayscale screenshot alongside each log entry.
Purges screenshots older than 48 hours on startup.

Storage layout:
    ~/.focusaudit/
        logs/          YYYY-MM-DD.jsonl  (one line per event, kept forever)
        screenshots/   YYYY-MM-DD_HH-MM-SS.jpg
        service.log    systemd / runtime log
        tracker.log    Python logger output
"""

import os
import sys
import json
import time
import signal
import logging
import datetime
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# ── Ensure DISPLAY is available for X11 tools launched from systemd ────────────
os.environ.setdefault("DISPLAY", ":0")
os.environ.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))

# ── Optional heavy dependencies ─────────────────────────────────────────────────
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import mss  # type: ignore
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

# ── Configuration ────────────────────────────────────────────────────────────────
BASE_DIR        = Path.home() / ".focusaudit"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOG_DIR         = BASE_DIR / "logs"
SYSTEM_LOG      = BASE_DIR / "tracker.log"

LOG_INTERVAL    = 30     # seconds between "still-here" interval log entries
POLL_INTERVAL   = 1      # seconds between window-title polls
PURGE_HOURS     = 48     # screenshots older than this are deleted
IMG_WIDTH       = 1000   # resize screenshots to this width (px)
IMG_QUALITY     = 60     # JPEG quality (0-100)

# ── Directory setup ──────────────────────────────────────────────────────────────
BASE_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(SYSTEM_LOG),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════════
#  Window title detection  (X11 primary → Wayland/GNOME fallback → xprop)
# ════════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, timeout: int = 2) -> Optional[str]:
    """Run a subprocess, return stdout or None on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _app_name_from_pid(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "unknown"


def get_active_window_info() -> Tuple[str, str]:
    """Return (window_title, app_name) for the currently focused window."""

    # ── xdotool (X11) ────────────────────────────────────────────────────────
    title = _run(["xdotool", "getactivewindow", "getwindowname"])
    if title:
        app = "unknown"
        win_id = _run(["xdotool", "getactivewindow"])
        if win_id:
            pid = _run(["xdotool", "getwindowpid", win_id])
            if pid:
                app = _app_name_from_pid(pid)
        return title, app

    # ── gdbus / GNOME Shell (Wayland) ─────────────────────────────────────────
    gdbus_out = _run([
        "gdbus", "call", "--session",
        "--dest", "org.gnome.Shell",
        "--object-path", "/org/gnome/Shell",
        "--method", "org.gnome.Shell.Eval",
        "global.display.focus_window ? global.display.focus_window.get_title() : 'Unknown'",
    ], timeout=3)
    if gdbus_out and "true" in gdbus_out:
        parts = gdbus_out.split("'")
        if len(parts) >= 2:
            return parts[1].strip(), "unknown"

    # ── xprop fallback (X11 without xdotool) ─────────────────────────────────
    net_win = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if net_win:
        win_id = net_win.split()[-1]
        if win_id and win_id != "0x0":
            wm_name = _run(["xprop", "-id", win_id, "WM_NAME"])
            if wm_name and '"' in wm_name:
                return wm_name.split('"')[1], "unknown"

    return "Unknown", "unknown"


# ════════════════════════════════════════════════════════════════════════════════
#  Screenshot capture  (mss primary → scrot fallback)
# ════════════════════════════════════════════════════════════════════════════════

def take_screenshot() -> Optional[str]:
    """
    Capture the primary monitor, resize to IMG_WIDTH, convert to grayscale,
    save as JPEG with IMG_QUALITY compression.
    Returns the filename (not full path), or None on failure.
    """
    ts       = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:23]
    filename = f"{ts}.jpg"
    dst      = SCREENSHOTS_DIR / filename

    img: Optional["Image.Image"] = None  # type: ignore[name-defined]

    # ── mss (pure Python, very low overhead) ─────────────────────────────────
    if HAS_MSS and HAS_PIL:
        try:
            import mss as _mss
            with _mss.mss() as sct:
                monitor = sct.monitors[1]      # primary monitor
                raw     = sct.grab(monitor)
                img     = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception as exc:
            log.warning("mss capture failed: %s", exc)
            img = None

    # ── scrot fallback ────────────────────────────────────────────────────────
    if img is None:
        tmp = SCREENSHOTS_DIR / f"{ts}_raw.png"
        ok  = _run(["scrot", str(tmp)], timeout=5)
        if ok is not None or tmp.exists():
            if HAS_PIL and tmp.exists():
                try:
                    img = Image.open(str(tmp))
                    tmp.unlink(missing_ok=True)
                except Exception as exc:
                    log.warning("PIL open after scrot failed: %s", exc)
                    tmp.unlink(missing_ok=True)
                    return None
            elif tmp.exists():
                # No PIL — keep the raw PNG, rename to match expected filename
                tmp.rename(dst.with_suffix(".png"))
                return filename.replace(".jpg", ".png")
            else:
                return None

    if img is None:
        return None

    # ── Process: resize → grayscale → JPEG ───────────────────────────────────
    try:
        ratio      = IMG_WIDTH / img.width
        new_height = int(img.height * ratio)
        img        = img.resize((IMG_WIDTH, new_height), Image.LANCZOS)
        img        = img.convert("L")                           # grayscale
        img.save(str(dst), "JPEG", quality=IMG_QUALITY, optimize=True)
        return filename
    except Exception as exc:
        log.warning("Image processing failed: %s", exc)
        return None


# ════════════════════════════════════════════════════════════════════════════════
#  Module 2 — 48-hour purge
# ════════════════════════════════════════════════════════════════════════════════

def purge_old_screenshots() -> None:
    """Delete .jpg (and .png fallback) files older than PURGE_HOURS hours."""
    cutoff  = time.time() - PURGE_HOURS * 3600
    purged  = 0
    for f in SCREENSHOTS_DIR.glob("*"):
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                purged += 1
        except OSError as exc:
            log.warning("Could not delete %s: %s", f, exc)
    if purged:
        log.info("Purged %d screenshot(s) older than %d hours.", purged, PURGE_HOURS)


# ════════════════════════════════════════════════════════════════════════════════
#  Logging
# ════════════════════════════════════════════════════════════════════════════════

def _daily_log_path() -> Path:
    return LOG_DIR / f"{datetime.date.today().isoformat()}.jsonl"


def append_log_entry(
    title: str,
    app: str,
    event: str,            # "change" | "interval"
    screenshot: Optional[str],
    duration: float,       # seconds spent on the previous window
) -> None:
    """Append one JSON line to today's JSONL log. Never truncates the file."""
    entry = {
        "ts":         datetime.datetime.now().isoformat(timespec="seconds"),
        "title":      title,
        "app":        app,
        "event":      event,
        "duration":   round(duration, 1),
        "screenshot": screenshot,
    }
    with open(_daily_log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ════════════════════════════════════════════════════════════════════════════════
#  Main tracking loop
# ════════════════════════════════════════════════════════════════════════════════

_running = True


def _handle_signal(signum, frame):   # noqa: ARG001
    global _running
    log.info("Signal %d received — shutting down.", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


def main() -> None:
    log.info("FocusAudit tracker starting. DISPLAY=%s", os.environ.get("DISPLAY", "unset"))

    # Module 2: purge old screenshots on every startup
    purge_old_screenshots()

    last_title:    str   = ""
    last_log_time: float = time.monotonic()
    window_start:  float = time.monotonic()

    while _running:
        try:
            title, app = get_active_window_info()
            now        = time.monotonic()

            title_changed    = title != last_title
            interval_elapsed = (now - last_log_time) >= LOG_INTERVAL

            if title_changed or interval_elapsed:
                duration   = now - window_start
                event_type = "change" if title_changed else "interval"
                screenshot = take_screenshot()

                append_log_entry(title, app, event_type, screenshot, duration)

                if title_changed:
                    window_start = now          # reset timer for new window
                last_title    = title
                last_log_time = now

        except Exception as exc:                # never crash the daemon
            log.error("Tracker loop error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)

    log.info("FocusAudit tracker stopped.")


if __name__ == "__main__":
    main()
