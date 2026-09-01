#!/usr/bin/env python3
"""Daily health check for the Dune: Part 3 IMAX 70mm watcher.

Verifies, independently of the watch chain itself, that the system is still
doing its job, and ALWAYS sends a Telegram heartbeat with the verdict. The
heartbeat is deliberate: the Aug 18 drop was detected and alerted but reached
nobody, so the daily message is the end-to-end proof that Telegram delivery
still works. If the morning heartbeat ever stops arriving, the pipeline is
broken even though no run has "failed".

Checks:
  1. Cadence — completed watch runs in the last 24h (expect ~48 with the
     chain healthy; below MIN_RUNS_24H means the chain is limping on the cron
     backstop), and how long ago the last one finished.
  2. Chain liveness — a watch run currently in progress or queued.
  3. Cineplex API — a live schedule fetch parses and still contains sessions.
  4. State sanity — state.json is schema 2 (the watcher is on the new logic).

Any check failing => Telegram (if possible) + ntfy + GitHub issue comment +
non-zero exit so GitHub's run-failed email fires too.
"""

import gzip
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "sshakerinezhad/cinewatcher")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
# `or`, not a get() default — the workflow sets this to "" when the secret is absent.
CINEPLEX_KEY = os.environ.get("CINEPLEX_API_KEY") or "dcdac5601d864addbc2675a2e96cb1f8"

MIN_RUNS_24H = int(os.environ.get("MIN_RUNS_24H", "20"))
MAX_LAST_RUN_MIN = int(os.environ.get("MAX_LAST_RUN_MIN", "90"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_LABEL = "dune3-health"


def gh(path):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "cinewatcher-health"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def check_cadence(problems, lines):
    now = datetime.now(timezone.utc)
    runs, page = [], 1
    while page <= 3:
        batch = gh(f"/repos/{GH_REPO}/actions/workflows/watch.yml/runs"
                   f"?per_page=100&page={page}")["workflow_runs"]
        runs += batch
        if len(batch) < 100:
            break
        page += 1

    day_ago = now - timedelta(hours=24)
    completed = [r for r in runs
                 if r["status"] == "completed" and r["conclusion"] != "cancelled"
                 and datetime.fromisoformat(r["run_started_at"].replace("Z", "+00:00")) > day_ago]
    active = [r for r in runs if r["status"] in ("in_progress", "queued")]

    last_end_min = None
    done = [r for r in runs if r["status"] == "completed" and r["conclusion"] != "cancelled"]
    if done:
        last = max(datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")) for r in done)
        last_end_min = int((now - last).total_seconds() / 60)

    lines.append(f"checks (24h): {len(completed)} runs, "
                 f"last finished {last_end_min if last_end_min is not None else '?'} min ago, "
                 f"chain {'ALIVE' if active else 'DEAD'}")
    if len(completed) < MIN_RUNS_24H:
        problems.append(f"only {len(completed)} completed watch runs in 24h "
                        f"(expected ~48; chain may be limping on the cron backstop — "
                        f"check DISPATCH_PAT expiry)")
    if last_end_min is None or last_end_min > MAX_LAST_RUN_MIN:
        problems.append(f"last completed watch run was {last_end_min} min ago "
                        f"(threshold {MAX_LAST_RUN_MIN})")
    if not active and last_end_min is not None and last_end_min > 10:
        problems.append("no watch run in progress or queued — the chain is dead "
                        "(dispatch a run manually or wait for the cron backstop)")
    failures = [r for r in completed if r["conclusion"] == "failure"]
    if failures:
        lines.append(f"note: {len(failures)} of those runs concluded 'failure' "
                     f"(intentional if they were alerts — check the alert issue)")


def check_cineplex(problems, lines):
    try:
        req = urllib.request.Request(
            "https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes"
            "?language=en&locationId=7420",
            headers={"Ocp-Apim-Subscription-Key": CINEPLEX_KEY,
                     "Accept": "application/json", "Accept-Encoding": "gzip",
                     "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        # Some CDN edges double-gzip; unwrap until the magic bytes are gone.
        for _ in range(5):
            if body[:2] != b"\x1f\x8b":
                break
            body = gzip.decompress(body)
        payload = json.loads(body) if body.strip() else []
        n = 0
        for th in payload:
            for d in th.get("dates", []):
                for m in d.get("movies", []):
                    if "dune" in m.get("name", "").lower():
                        for e in m.get("experiences", []):
                            types = {t.lower() for t in e.get("experienceTypes", [])}
                            if any("imax" in t for t in types) and any("70" in t for t in types):
                                n += len(e.get("sessions", []))
        lines.append(f"Cineplex API: OK, {n} IMAX 70mm sessions listed at Square One")
        if n == 0:
            lines.append("note: zero sessions — either the run has ended "
                         "(time to retire the watch) or Cineplex changed the data shape")
    except Exception as e:  # noqa: BLE001
        problems.append(f"Cineplex API check failed: {type(e).__name__}: {e} "
                        f"(key rotated? API moved? the watcher is blind)")


def check_state(problems, lines):
    try:
        with open(os.path.join(ROOT, "state.json")) as f:
            state = json.load(f)
        if state.get("schema") != 2:
            problems.append("state.json is not schema 2 — the watcher is not on the current logic")
        else:
            free = sum(len(v) for v in state.get("goodSeats", {}).values())
            with_free = sum(1 for v in state.get("goodSeats", {}).values() if v)
            lines.append(f"state: {len(state.get('sessions', {}))} sessions tracked, "
                         f"{free} good-zone seats free across {with_free} showtimes")
    except Exception as e:  # noqa: BLE001
        problems.append(f"state.json unreadable: {e}")


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return "FAILED (secrets missing)"
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.load(r)
        if not resp.get("ok"):
            return f"FAILED ({resp.get('description')})"
        return "delivered"
    except Exception as e:  # noqa: BLE001
        return f"FAILED ({type(e).__name__}: {e})"


def send_ntfy(title, text):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode(),
            headers={"Title": title, "Priority": "urgent", "Tags": "warning"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"::error::ntfy failed too: {e}")


def post_issue(text):
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/labels", method="POST",
            data=json.dumps({"name": HEALTH_LABEL, "color": "D93F0B"}).encode(),
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "cinewatcher-health"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as e:
            if e.code != 422:
                raise
        existing = gh(f"/repos/{GH_REPO}/issues?state=open&labels={HEALTH_LABEL}")
        path = (f"/repos/{GH_REPO}/issues/{existing[0]['number']}/comments"
                if existing else f"/repos/{GH_REPO}/issues")
        body = ({"body": text} if existing
                else {"title": "🔴 Watcher health check failing", "body": text,
                      "labels": [HEALTH_LABEL]})
        req = urllib.request.Request(
            f"https://api.github.com{path}", method="POST",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "cinewatcher-health"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"::error::could not file health issue: {e}")


def main():
    problems, lines = [], []
    for check in (check_cadence, check_cineplex, check_state):
        try:
            check(problems, lines)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{check.__name__} itself crashed: {type(e).__name__}: {e}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if problems:
        head = "🔴 cinewatcher daily check — PROBLEMS FOUND"
        body = "\n".join([head, now, ""] + [f"• {p}" for p in problems] + [""] + lines)
    else:
        head = "✅ cinewatcher daily check — all good"
        body = "\n".join([head, now, ""] + lines +
                         ["", "(this heartbeat doubles as the daily Telegram delivery test)"])

    print(body)
    tg = send_telegram(body)
    print(f"telegram heartbeat: {tg}")
    if tg != "delivered":
        problems.append(f"Telegram heartbeat could not be delivered: {tg}")

    if problems:
        text = "\n".join([f"• {p}" for p in problems] + [""] + lines)
        send_ntfy("cinewatcher health check FAILING", text)
        post_issue(f"Daily health check found problems ({now}):\n\n"
                   + "\n".join(f"- {p}" for p in problems)
                   + "\n\n" + "\n".join(lines)
                   + "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
        print("::error::health check failed: " + "; ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
