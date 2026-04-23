#!/usr/bin/env python3
"""
Flowtrack - AI Pattern Analyzer  (Modules 3 & 4)

Usage:
    python3 analyze.py              # full analysis + Ollama (if running)
    python3 analyze.py --no-ai      # skip Ollama, print stats only
    python3 analyze.py --days N     # analyse last N days (default 7)

Outputs:
    ~/.focusaudit/reports/analysis_YYYY-MM-DD.txt      raw stats
    ~/.focusaudit/reports/ai_analysis_YYYY-MM-DD.txt   Ollama response (if available)
    ~/.focusaudit/reports/ai_prompt_YYYY-MM-DD.txt     prompt for manual paste
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────────
BASE_DIR   = Path.home() / ".focusaudit"
LOG_DIR    = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"

REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── Ollama ───────────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"

# ── Pattern constants ─────────────────────────────────────────────────────────────
RAPID_SWITCH_THRESHOLD = 5    # tab changes …
RAPID_SWITCH_WINDOW    = 120  # … within this many seconds

RABBIT_HOLE_THRESHOLD  = 20   # unique browser-tab titles in a rolling window
RABBIT_HOLE_WINDOW_MIN = 30   # rolling window size in minutes

DEEP_WORK_SECONDS      = 25 * 60   # 25 minutes
LOG_INTERVAL           = 30        # default duration fallback (seconds)

BROWSER_APPS = {
    "chrome", "chromium", "chromium-browser", "google-chrome",
    "firefox", "firefox-esr", "brave", "brave-browser",
    "opera", "vivaldi", "microsoft-edge", "epiphany", "midori",
}

SOCIAL_PATTERNS: dict[str, str] = {
    "YouTube":    r"youtube\.com|YouTube",
    "Instagram":  r"instagram\.com|Instagram",
    "Reddit":     r"reddit\.com|Reddit",
    "TikTok":     r"tiktok\.com|TikTok",
    "Twitter/X":  r"twitter\.com|x\.com|\bTwitter\b",
    "Facebook":   r"facebook\.com|Facebook",
    "LinkedIn":   r"linkedin\.com|LinkedIn",
    "Reels":      r"\bReels?\b",
}

# ════════════════════════════════════════════════════════════════════════════════
#  Data loading
# ════════════════════════════════════════════════════════════════════════════════

def load_entries(days: int = 7) -> list[dict[str, Any]]:
    """Load and sort all log entries from the last *days* JSONL files."""
    today   = datetime.date.today()
    entries = []
    for delta in range(days):
        date     = today - datetime.timedelta(days=delta)
        log_file = LOG_DIR / f"{date.isoformat()}.jsonl"
        if not log_file.exists():
            continue
        with open(log_file, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                    e["dt"] = datetime.datetime.fromisoformat(e["ts"])
                    entries.append(e)
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass
    entries.sort(key=lambda e: e["dt"])
    return entries


# ════════════════════════════════════════════════════════════════════════════════
#  Module 3 — Rapid Switching detector
# ════════════════════════════════════════════════════════════════════════════════

def detect_rapid_switching(entries: list[dict]) -> list[dict]:
    """
    Return periods where more than RAPID_SWITCH_THRESHOLD window changes
    occurred within RAPID_SWITCH_WINDOW seconds.
    """
    events = []
    n = len(entries)
    i = 0
    while i < n:
        window_end = entries[i]["dt"] + datetime.timedelta(seconds=RAPID_SWITCH_WINDOW)
        j = i
        while j < n and entries[j]["dt"] <= window_end:
            j += 1
        count = j - i
        if count > RAPID_SWITCH_THRESHOLD:
            apps = list({e.get("app", "unknown") for e in entries[i:j]})
            events.append({
                "start":   entries[i]["ts"],
                "end":     entries[j - 1]["ts"],
                "switches": count,
                "apps":    apps,
            })
            i = j   # jump past the burst to avoid overlap
        else:
            i += 1
    return events


# ════════════════════════════════════════════════════════════════════════════════
#  Module 4 — Pattern Finder
# ════════════════════════════════════════════════════════════════════════════════

# ── "The Rabbit Hole" ─────────────────────────────────────────────────────────

def detect_rabbit_holes(entries: list[dict]) -> list[dict]:
    """
    Detect browser sessions where many unique tab titles appear in a short window
    (one search / curiosity chain leading to many unrelated tabs).
    """
    browser_entries = [
        e for e in entries
        if e.get("app", "").lower() in BROWSER_APPS
    ]
    holes = []
    n = len(browser_entries)
    i = 0
    while i < n:
        window_end   = browser_entries[i]["dt"] + datetime.timedelta(minutes=RABBIT_HOLE_WINDOW_MIN)
        j            = i
        while j < n and browser_entries[j]["dt"] <= window_end:
            j += 1
        unique_titles = len({e["title"] for e in browser_entries[i:j]})
        if unique_titles >= RABBIT_HOLE_THRESHOLD:
            holes.append({
                "start":        browser_entries[i]["ts"],
                "tab_changes":  j - i,
                "unique_tabs":  unique_titles,
                "trigger_title": browser_entries[i]["title"][:120],
            })
            i = j
        else:
            i += 1
    return holes


# ── "The Fatigue Pattern" ─────────────────────────────────────────────────────

def detect_fatigue_pattern(entries: list[dict]) -> dict[str, Any]:
    """
    Switching rate (events / hour) by time-of-day slot.
    Fatigue is flagged when the afternoon rate is ≥50 % higher than morning.
    """
    buckets: dict[str, list] = {"morning": [], "afternoon": [], "evening": []}
    for e in entries:
        h = e["dt"].hour
        if 6 <= h < 12:
            buckets["morning"].append(e)
        elif 12 <= h < 18:
            buckets["afternoon"].append(e)
        elif 18 <= h < 22:
            buckets["evening"].append(e)

    def rate(group: list) -> float:
        if len(group) < 2:
            return 0.0
        span_h = (group[-1]["dt"] - group[0]["dt"]).total_seconds() / 3600
        return round(len(group) / max(span_h, 0.1), 1)

    m_rate = rate(buckets["morning"])
    a_rate = rate(buckets["afternoon"])
    e_rate = rate(buckets["evening"])
    delta  = round(((a_rate - m_rate) / max(m_rate, 1)) * 100, 1)

    return {
        "morning_rate":    m_rate,
        "afternoon_rate":  a_rate,
        "evening_rate":    e_rate,
        "fatigue_detected": a_rate > m_rate * 1.5,
        "pm_vs_am_pct":    delta,
    }


# ── "The Visual Bait" ─────────────────────────────────────────────────────────

def detect_visual_bait(entries: list[dict]) -> dict[str, dict]:
    """
    For each social/entertainment platform, compute visits, total minutes,
    and the maximum number of context-switches triggered in the 30 min after
    first landing on that site.
    """
    results: dict[str, dict] = {}
    for site, pattern in SOCIAL_PATTERNS.items():
        rx       = re.compile(pattern, re.IGNORECASE)
        hit_idx  = [i for i, e in enumerate(entries) if rx.search(e.get("title", ""))]
        if not hit_idx:
            continue
        total_sec = sum(entries[i].get("duration", 0) for i in hit_idx)
        max_post  = 0
        for idx in hit_idx:
            cutoff   = entries[idx]["dt"] + datetime.timedelta(minutes=30)
            post_cnt = sum(1 for e in entries[idx:] if e["dt"] <= cutoff)
            max_post = max(max_post, post_cnt)
        results[site] = {
            "visits":                     len(hit_idx),
            "total_minutes":              round(total_sec / 60, 1),
            "max_post_trigger_switches":  max_post,
        }
    return results


# ── Context-switching between apps ────────────────────────────────────────────

def analyze_context_switches(entries: list[dict]) -> dict[str, Any]:
    """
    Track explicit app-to-app transitions (Chrome→VSCode, etc.).
    This is the core "Focus Score" signal.
    """
    switches = []
    for i in range(1, len(entries)):
        prev, curr = entries[i - 1].get("app", ""), entries[i].get("app", "")
        if prev != curr:
            switches.append({"from": prev, "to": curr, "at": entries[i]["ts"]})

    pair_counts  = Counter((s["from"], s["to"]) for s in switches)
    top_pairs    = [
        {"from": p[0], "to": p[1], "count": c}
        for p, c in pair_counts.most_common(10)
    ]
    total_hours = (
        (entries[-1]["dt"] - entries[0]["dt"]).total_seconds() / 3600
        if len(entries) > 1 else 1
    )
    return {
        "total_app_switches":   len(switches),
        "switches_per_hour":    round(len(switches) / max(total_hours, 0.01), 1),
        "top_app_pairs":        top_pairs,
    }


# ── Focus Score ───────────────────────────────────────────────────────────────

def calculate_focus_score(entries: list[dict]) -> dict[str, Any]:
    """
    Per-hour and daily focus score (0–100).

    Score components (per hour):
      40 % — average time per window   (5 min avg = 100)
      40 % — switch-rate score         (0 switches = 100, 67/hr = 0)
      20 % — deep-work ratio           (% of time in ≥25 min uninterrupted blocks)
    """
    if not entries:
        return {"daily": 0, "hourly": {}}

    hourly: dict[str, list] = defaultdict(list)
    for e in entries:
        key = e["dt"].strftime("%Y-%m-%d %H:00")
        hourly[key].append(e)

    hourly_scores: dict[str, float] = {}
    for hour_key, hour_entries in hourly.items():
        n = len(hour_entries)
        if n < 2:
            hourly_scores[hour_key] = 80.0   # too little data → neutral
            continue

        durations = [e.get("duration", LOG_INTERVAL) for e in hour_entries]

        avg_dur      = sum(durations) / len(durations)
        time_score   = min(100.0, (avg_dur / 300) * 100)           # 5 min target

        switch_rate  = n                                            # events this hour
        rate_score   = max(0.0, 100.0 - switch_rate * 1.5)

        deep_secs    = sum(d for d in durations if d >= DEEP_WORK_SECONDS)
        total_secs   = sum(durations)
        deep_score   = (deep_secs / max(total_secs, 1)) * 100

        score        = 0.4 * time_score + 0.4 * rate_score + 0.2 * deep_score
        hourly_scores[hour_key] = round(score, 1)

    daily = round(sum(hourly_scores.values()) / len(hourly_scores), 1) if hourly_scores else 0.0
    return {"daily": daily, "hourly": hourly_scores}


# ════════════════════════════════════════════════════════════════════════════════
#  Reporting
# ════════════════════════════════════════════════════════════════════════════════

def _top_apps(entries: list[dict], n: int = 10) -> list[tuple[str, float]]:
    """Return (app_name, minutes) for the top-n apps by time."""
    totals: dict[str, float] = defaultdict(float)
    for e in entries:
        totals[e.get("app", "unknown")] += e.get("duration", 0) / 60
    return sorted(totals.items(), key=lambda x: x[1], reverse=True)[:n]


def generate_text_report(
    entries:        list[dict],
    rapid:          list[dict],
    holes:          list[dict],
    fatigue:        dict,
    visual_bait:    dict,
    ctx_switches:   dict,
    focus:          dict,
) -> str:
    today = datetime.date.today().isoformat()
    lines = [
        "══════════════════════════════════════════════════════════════",
        f"  Flowtrack Analysis  —  generated {today}",
        "══════════════════════════════════════════════════════════════",
        "",
        f"TOTAL EVENTS (last 7 days): {len(entries)}",
        "",
        "┌─ FOCUS SCORE ──────────────────────────────────────────────",
        f"│  Daily average : {focus['daily']} / 100",
        "│  (40% avg window time · 40% switch rate · 20% deep work)",
        "│",
        "│  Worst hours:",
    ]
    worst = sorted(focus["hourly"].items(), key=lambda x: x[1])[:5]
    for h, s in worst:
        lines.append(f"│    {h}  →  {s}/100")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ CONTEXT SWITCHING (app-to-app) ───────────────────────────",
        f"│  Total app switches   : {ctx_switches['total_app_switches']}",
        f"│  Rate                 : {ctx_switches['switches_per_hour']} switches/hr",
        "│  Top transitions:",
    ]
    for pair in ctx_switches["top_app_pairs"][:6]:
        lines.append(f"│    {pair['from']:20s} → {pair['to']:20s}  ({pair['count']}×)")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ TOP APPLICATIONS BY TIME ─────────────────────────────────",
    ]
    for app, mins in _top_apps(entries):
        bar = "█" * int(mins / max(1, _top_apps(entries)[0][1]) * 30)
        lines.append(f"│  {app:22s} {mins:7.1f} min  {bar}")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        f"┌─ RAPID SWITCHING  ({len(rapid)} events) ──────────────────────────",
    ]
    for ev in rapid[:8]:
        lines.append(
            f"│  {ev['start']}  {ev['switches']} switches  "
            f"apps: {', '.join(ev['apps'][:4])}"
        )
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        f"┌─ THE RABBIT HOLE  ({len(holes)} sessions) ─────────────────────────",
    ]
    for h in holes[:5]:
        lines.append(f"│  {h['start']}  {h['tab_changes']} changes · {h['unique_tabs']} unique tabs")
        lines.append(f"│    triggered by: {h['trigger_title'][:80]}")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ THE FATIGUE PATTERN ───────────────────────────────────────",
        f"│  Morning   (06-12) : {fatigue['morning_rate']} switches/hr",
        f"│  Afternoon (12-18) : {fatigue['afternoon_rate']} switches/hr",
        f"│  Evening   (18-22) : {fatigue['evening_rate']} switches/hr",
        f"│  PM vs AM increase : {fatigue['pm_vs_am_pct']}%",
        f"│  Fatigue detected  : {'⚠ YES' if fatigue['fatigue_detected'] else 'NO'}",
        "└────────────────────────────────────────────────────────────",
        "",
        "┌─ THE VISUAL BAIT ──────────────────────────────────────────",
    ]
    if visual_bait:
        for site, data in sorted(visual_bait.items(), key=lambda x: x[1]["total_minutes"], reverse=True):
            lines.append(
                f"│  {site:15s}  {data['visits']:3d} visits  "
                f"{data['total_minutes']:6.1f} min  "
                f"{data['max_post_trigger_switches']} post-trigger switches"
            )
    else:
        lines.append("│  No social / entertainment activity detected.")
    lines.append("└────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def build_ai_prompt(report: str) -> str:
    return f"""You are a productivity analyst specialising in attention and deep work.

Below is a structured activity report from a user's Ubuntu desktop for the past 7 days.
Analyse it and respond with:

1. OVERALL ASSESSMENT — two sentences on their current focus pattern.
2. TOP 3 PROBLEMS — the three most harmful patterns, with the specific numbers from the data.
3. FOCUS TRAPS ACTIVE — which of the following apply and how severely:
   • The Rabbit Hole (one search → 20+ unrelated tabs)
   • The Fatigue Pattern (PM switching rate > AM)
   • The Visual Bait (social/video triggers extended distraction)
4. FOCUS SCORE BREAKDOWN — explain what is dragging their score down most.
5. 3-STEP IMPROVEMENT PLAN — concrete, implementable actions tied to the data.

Be direct, data-driven, and specific. Reference actual numbers.

─── REPORT ───────────────────────────────────────────────────────────────────
{report}
──────────────────────────────────────────────────────────────────────────────
"""


# ════════════════════════════════════════════════════════════════════════════════
#  Ollama integration
# ════════════════════════════════════════════════════════════════════════════════

def query_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str | None:
    payload = json.dumps({
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "options": {"num_ctx": 4096},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


# ════════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="FocusAudit AI Pattern Analyzer")
    parser.add_argument("--days",   type=int, default=7, help="Days of data to analyse (default: 7)")
    parser.add_argument("--no-ai",  action="store_true", help="Skip Ollama query")
    parser.add_argument("--model",  default=OLLAMA_MODEL, help=f"Ollama model (default: {OLLAMA_MODEL})")
    args = parser.parse_args()

    print("Flowtrack — AI Pattern Analyzer")
    print("=" * 60)

    entries = load_entries(args.days)
    if not entries:
        print(f"No log data found in {LOG_DIR}")
        print("Ensure the tracker is running:  systemctl --user status focusaudit")
        sys.exit(0)

    print(f"Loaded {len(entries)} events from the last {args.days} day(s).\n")

    rapid       = detect_rapid_switching(entries)
    holes       = detect_rabbit_holes(entries)
    fatigue     = detect_fatigue_pattern(entries)
    visual_bait = detect_visual_bait(entries)
    ctx         = analyze_context_switches(entries)
    focus       = calculate_focus_score(entries)

    report = generate_text_report(entries, rapid, holes, fatigue, visual_bait, ctx, focus)
    print(report)

    today = datetime.date.today().isoformat()

    # Save raw report
    report_path = REPORT_DIR / f"analysis_{today}.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved → {report_path}")

    if args.no_ai:
        return

    # Build prompt
    ai_prompt    = build_ai_prompt(report)
    prompt_path  = REPORT_DIR / f"ai_prompt_{today}.txt"
    prompt_path.write_text(ai_prompt, encoding="utf-8")

    # Try Ollama
    print(f"\nQuerying Ollama ({args.model}) — timeout 120 s …")
    ai_response = query_ollama(ai_prompt, model=args.model)

    if ai_response:
        print("\n" + "=" * 60)
        print("AI ANALYSIS")
        print("=" * 60)
        print(ai_response)
        ai_path = REPORT_DIR / f"ai_analysis_{today}.txt"
        ai_path.write_text(ai_response, encoding="utf-8")
        print(f"\nAI report saved → {ai_path}")
    else:
        print("\nOllama unavailable or timed out.")
        print("Paste the prompt below into any web AI (ChatGPT, Claude, Gemini …)")
        print(f"\nPrompt also saved → {prompt_path}\n")
        print("─" * 60)
        print(ai_prompt)


if __name__ == "__main__":
    main()
