#!/usr/bin/env python3
"""Watch Cineplex for Dune: Part 3 IMAX 70mm — new showtimes AND freed good seats.

Two things trigger an alert, checked in a sweep that repeats every
SWEEP_INTERVAL_SECONDS for RUN_MINUTES per workflow run:

1. A brand-new IMAX 70mm session appearing anywhere in the schedule (new dates
   opening, or extra showtimes added to existing dates). One schedule call per
   theatre with no date parameter returns the theatre's entire calendar, so no
   date window ever has to be guessed.
2. A seat inside the "good zone" — rows GOOD_ROWS, within GOOD_RADIUS columns
   of the auditorium's centre — flipping from not-purchasable to Available on
   any tracked session (a refund, or held inventory being released).

Alerts go Telegram -> ntfy -> GitHub issue, all sent directly from here so a
failure in one channel can never block the others. The workflow adds a fourth
channel by failing the run on purpose (GitHub's run-failed email).

State lives in state.json (schema 2):
  sessions  - id -> {theatreId, start, soldOut, auditorium}
  goodSeats - id -> sorted list of currently-Available good-zone seat labels
  goodZone  - the zone config the snapshot was taken with; if the config
              changes, seat snapshots are rebuilt without alerting
  alertLog  - "sessionId:seatLabel" -> last alert time, for the re-alert
              cooldown (a freed seat gets carted and released repeatedly;
              each release is real but repeats within the cooldown stay quiet)

Modes (env):
  CINEWATCHER_TEST=1 - one sweep, then a synthetic [TEST] alert through every
        real channel using live data; no state commit, no loop.
  CINEWATCHER_DRY=1  - print sends instead of sending, never touch git.
  CINEWATCHER_ONCE=1 - single sweep (used for seeding state locally).

A run only exits non-zero when every sweep in it failed outright — that means
the API or key broke and the failure email is the correct outcome.
"""

import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

API_BASE = "https://apis.cineplex.com"
SHOWTIMES = f"{API_BASE}/prod/cpx/theatrical/api/v1/showtimes"
TICKETING = f"{API_BASE}/prod/ticketing/api/v1/theatre"
# Public subscription key embedded in the cineplex.com web frontend, not a
# secret. If Cineplex rotates it, pull the new one from any request the
# cineplex.com showtimes page makes and set it as a CINEPLEX_API_KEY secret.
KEY = os.environ.get("CINEPLEX_API_KEY", "dcdac5601d864addbc2675a2e96cb1f8")

MOVIE_MATCH = os.environ.get("CINEWATCHER_MOVIE", "dune").lower()
# Re-releases of the first two films would otherwise match a bare "dune".
MOVIE_EXCLUDE = ("part one", "part two", "part 1", "part 2")

THEATRES = {
    7408: {
        "name": "Cineplex Cinemas Vaughan",
        "url": "https://www.cineplex.com/theatre/cineplex-cinemas-vaughan",
    },
    7420: {
        "name": "Cineplex Cinemas Mississauga Square One",
        "url": "https://www.cineplex.com/theatre/cineplex-cinemas-mississauga-square-one",
    },
}

# The good zone. Row demand at these two houses runs front-to-back: the seat
# scan across all 196+ showtimes found rows F-J nearly stripped while A-D sat
# empty, and nothing within 4 columns of centre available anywhere. Radius 6
# means "as good as or better than anything currently buyable".
GOOD_ROWS = os.environ.get("GOOD_ROWS", "FGHIJ").upper()
GOOD_RADIUS = int(os.environ.get("GOOD_RADIUS", "6"))

SWEEP_INTERVAL = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "120"))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "25"))
COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MINUTES", "90"))

TEST_MODE = bool(os.environ.get("CINEWATCHER_TEST"))
DRY_RUN = bool(os.environ.get("CINEWATCHER_DRY"))
RUN_ONCE = bool(os.environ.get("CINEWATCHER_ONCE")) or TEST_MODE
PREFIX = "[TEST] " if TEST_MODE else ""

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "sshakerinezhad/cinewatcher")
ISSUE_LABEL = "dune3-alert"

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
STATUS_PATH = os.path.join(ROOT, "docs", "status.json")
RESULT_PATH = os.path.join(ROOT, "result.json")

MOVIE_PAGE = "https://www.cineplex.com/Movie/dune-part-3"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------- HTTP layer

def decompress(body, encoding):
    """Undo Content-Encoding; urllib never does. Cineplex's CDN gzips whether
    or not it's asked to, varying by edge, so sniff the magic bytes rather
    than trusting the header — and keep unwrapping, because some edges have
    been observed serving DOUBLE-gzipped bodies (one pass still left
    b"\\x1f\\x8b", which is what killed the runs of 2026-08-31 23:20 and
    2026-09-01 02:00 despite the single-pass sniff)."""
    if not body:
        return body
    for _ in range(5):
        if body[:2] != b"\x1f\x8b":
            break
        body = gzip.decompress(body)
    else:
        raise ValueError("body still gzipped after 5 decompress passes")
    if encoding == "deflate" and body[:1] not in (b"{", b"["):
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def api_get(url, attempts=3, timeout=30):
    """GET a Cineplex API URL, retrying transient failures with backoff."""
    req = urllib.request.Request(url, headers={
        "Ocp-Apim-Subscription-Key": KEY,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": UA,
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = decompress(r.read(), (r.headers.get("Content-Encoding") or "").lower())
            return json.loads(body) if body and body.strip() else None
        except Exception as e:  # noqa: BLE001 - any transient failure retries
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)


def now_utc():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------- schedule scan

def is_70mm(types):
    t = {x.lower() for x in types}
    return any("imax" in x for x in t) and any("70" in x for x in t)


def is_target_movie(name):
    n = name.lower()
    return MOVIE_MATCH in n and not any(x in n for x in MOVIE_EXCLUDE)


def fetch_schedule(theatre_id):
    """All IMAX 70mm sessions for the target movie at one theatre, every date.

    Omitting the date parameter makes the showtimes API return the theatre's
    complete calendar (verified 2026-09-01: 93 dates, ~900KB), which is what
    lets one call catch new dates the moment they open.
    """
    payload = api_get(f"{SHOWTIMES}?language=en&locationId={theatre_id}") or []
    sessions = {}
    for theatre in payload:
        if theatre.get("theatreId") != theatre_id:
            continue
        for date in theatre.get("dates", []):
            for movie in date.get("movies", []):
                if not is_target_movie(movie.get("name", "")):
                    continue
                for exp in movie.get("experiences", []):
                    types = exp.get("experienceTypes", [])
                    if not is_70mm(types):
                        continue
                    for s in exp.get("sessions", []):
                        if s.get("isInThePast"):
                            continue
                        sid = str(s.get("vistaSessionId"))
                        sessions[sid] = {
                            "id": sid,
                            "theatreId": theatre_id,
                            "theatre": THEATRES[theatre_id]["name"],
                            "movie": movie.get("name"),
                            "start": s.get("showStartDateTime", ""),
                            "experience": " ".join(types),
                            "soldOut": bool(s.get("isSoldOut")),
                            "seatsRemaining": s.get("seatsRemaining"),
                            "ticketingUrl": s.get("ticketingUrl"),
                            "seatMapUrl": s.get("seatMapUrl"),
                            "auditorium": s.get("auditorium") or "",
                        }
    return sessions


# --------------------------------------------------------------- seat layer

_layout_cache = {}  # (theatreId, auditorium) -> {"seats": {id: (row,label,col)}, "colCount": int}


def _parse_layout(layout):
    seats, col_count = {}, layout["standardSeats"].get("columnCount") or 0
    for row in layout["standardSeats"]["rows"]:
        label = row.get("label")
        if not label:
            continue  # aisle rows
        for seat in row["seats"]:
            if seat.get("type") != "Standard":
                continue  # wheelchair / companion seats are not bookable wins
            seats[seat["id"]] = (label, seat["label"], seat["column"])
    return {"seats": seats, "colCount": col_count}


def get_layout(theatre_id, session_id, auditorium):
    key = (theatre_id, auditorium)
    if key not in _layout_cache:
        layout = api_get(f"{TICKETING}/{theatre_id}/showtime/{session_id}/seat-layout")
        _layout_cache[key] = _parse_layout(layout)
    return _layout_cache[key]


def good_seats_available(session):
    """Sorted list of {label,row,col,offCentre} for Available good-zone seats."""
    tid, sid = session["theatreId"], session["id"]
    layout = get_layout(tid, sid, session["auditorium"])
    avail = api_get(f"{TICKETING}/{tid}/showtime/{sid}/seat-availability?preview=true")
    statuses = avail["seatAvailabilities"]
    if any(k not in layout["seats"] for k in statuses) and \
            len(set(statuses) - set(layout["seats"])) > 8:
        # More unknown ids than the handful of non-Standard seats: this
        # session is in a different auditorium than the cached layout. Refetch.
        layout = _parse_layout(api_get(f"{TICKETING}/{tid}/showtime/{sid}/seat-layout"))
        _layout_cache[(tid, session["auditorium"])] = layout
    centre = (layout["colCount"] - 1) / 2
    out = []
    for seat_id, status in statuses.items():
        if status != "Available" or seat_id not in layout["seats"]:
            continue
        row, label, col = layout["seats"][seat_id]
        off = abs(col - centre)
        if row in GOOD_ROWS and off <= GOOD_RADIUS:
            out.append({"label": label, "row": row, "col": col, "offCentre": off})
    out.sort(key=lambda s: (s["row"], s["col"]))
    return out


# ------------------------------------------------------------ notifications

def _send(name, fn, attempts=3):
    """Run one channel send with retries; never raise. Returns status string."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt == attempts:
                print(f"::error::{name} delivery FAILED — {type(e).__name__}: {e}")
                return f"FAILED ({type(e).__name__})"
            time.sleep(2 ** attempt)


def send_telegram(text):
    if DRY_RUN:
        print(f"--- DRY telegram ---\n{text}\n---")
        return "dry-run"
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("::error::TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — no Telegram alert sent")
        return "FAILED (secrets missing)"

    def send_chunk(chunk):
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": chunk,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.load(r)
        if not resp.get("ok"):
            raise RuntimeError(f"telegram not ok: {resp.get('description')}")
        return resp["result"]["message_id"]

    ids = []
    for chunk in chunk_text(text, 3500):
        def one(c=chunk):
            return send_chunk(c)
        result = _send("Telegram", one)
        if isinstance(result, str) and result.startswith("FAILED"):
            return result
        ids.append(result)
    return f"delivered (message_id {', '.join(map(str, ids))})"


def send_ntfy(title, text):
    if DRY_RUN:
        print(f"--- DRY ntfy [{title}] ---")
        return "dry-run"
    if not NTFY_TOPIC:
        return "skipped"

    def post():
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode(),
            headers={"Title": title.encode("ascii", "ignore").decode(),
                     "Priority": "urgent", "Tags": "rotating_light,clapper"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return "delivered"
    return _send("ntfy", post)


def gh_api(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "cinewatcher"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        if method == "POST" and path.endswith("/labels") and e.code == 422:
            return None  # label already exists
        raise


def send_issue(title, md_body):
    if DRY_RUN:
        print(f"--- DRY issue [{title}] ---")
        return "dry-run"
    if not GH_TOKEN:
        return "skipped"

    def post():
        gh_api("POST", f"/repos/{GH_REPO}/labels",
               {"name": ISSUE_LABEL, "color": "FF0000"})
        open_issues = gh_api(
            "GET", f"/repos/{GH_REPO}/issues?state=open&labels={ISSUE_LABEL}") or []
        if open_issues:
            n = open_issues[0]["number"]
            gh_api("POST", f"/repos/{GH_REPO}/issues/{n}/comments", {"body": md_body})
            return f"commented on #{n}"
        created = gh_api("POST", f"/repos/{GH_REPO}/issues",
                         {"title": title, "labels": [ISSUE_LABEL], "body": md_body})
        return f"opened #{created['number']}"
    return _send("GitHub issue", post)


def chunk_text(text, limit):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line[:limit] + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return [c for c in chunks if c.strip()]


# ----------------------------------------------------------- message builder

def fmt_dt(iso):
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%a %b %-d"), d.strftime("%-I:%M %p")
    except ValueError:
        return iso, iso


def short_theatre(name):
    return "Vaughan" if "Vaughan" in name else "Square One" if "Square One" in name else name


def build_new_sessions_alert(new_sessions):
    txt = [f"{PREFIX}\U0001f6a8 NEW Dune: Part 3 IMAX 70mm showtimes are UP! BOOK NOW.", ""]
    md = [f"@sshakerinezhad **{PREFIX}NEW Dune: Part 3 IMAX 70mm showtimes!** \U0001f3ac", ""]
    by_date = {}
    for s in sorted(new_sessions, key=lambda x: (x["start"], x["theatreId"])):
        by_date.setdefault(s["start"][:10], []).append(s)
    summarize = len(new_sessions) > 30
    for date_key in sorted(by_date):
        day, _ = fmt_dt(by_date[date_key][0]["start"])
        txt.append(f"{day}:")
        md.append(f"### {day}")
        if summarize:
            counts = {}
            for s in by_date[date_key]:
                counts[short_theatre(s["theatre"])] = counts.get(short_theatre(s["theatre"]), 0) + 1
            line = " · ".join(f"{k}: {v} showtimes" for k, v in counts.items())
            txt.append(f"  {line}")
            md.append(f"- {line}")
        else:
            for s in by_date[date_key]:
                _, t = fmt_dt(s["start"])
                seats = ("SOLD OUT" if s["soldOut"]
                         else f"{s['seatsRemaining']} seats" if s["seatsRemaining"] is not None
                         else "on sale")
                txt.append(f"  {t} {short_theatre(s['theatre'])} — {seats} — {s['ticketingUrl']}")
                md.append(f"- **{t}** {s['theatre']} — {seats} — "
                          f"[Buy tickets]({s['ticketingUrl']}) · [Seat map]({s['seatMapUrl']})")
        txt.append("")
        md.append("")
    txt.append(f"Movie page: {MOVIE_PAGE}")
    md.append(f"Movie page: {MOVIE_PAGE}")
    return "\n".join(txt), "\n".join(md)


def build_freed_seats_alert(freed):
    """freed: list of (session, [seat dicts], all_available_labels_set)."""
    zone = f"rows {GOOD_ROWS[0]}-{GOOD_ROWS[-1]}, within {GOOD_RADIUS} of centre"
    txt = [f"{PREFIX}\U0001f39f GOOD SEATS JUST FREED UP ({zone}) — someone cancelled. GO.", ""]
    md = [f"@sshakerinezhad **{PREFIX}Good seats freed up** \U0001f39f ({zone})", ""]
    for session, seats, avail_by_col in freed:
        day, t = fmt_dt(session["start"])
        head = f"{day} {t} — {short_theatre(session['theatre'])}"
        txt.append(head)
        md.append(f"### {head}")
        shown = seats[:20]
        for seat in shown:
            off = seat["offCentre"]
            pos = "dead centre" if off < 1 else f"{off:.0f} off centre"
            pair = ""
            neighbours = [avail_by_col.get((seat["row"], seat["col"] - 1)),
                          avail_by_col.get((seat["row"], seat["col"] + 1))]
            mates = [n for n in neighbours if n and n != seat["label"]]
            if mates:
                pair = f" — PAIR with {'/'.join(mates)}"
            txt.append(f"  {seat['label']} ({pos}){pair}")
            md.append(f"- **{seat['label']}** ({pos}){pair}")
        if len(seats) > len(shown):
            txt.append(f"  …and {len(seats) - len(shown)} more")
            md.append(f"- …and {len(seats) - len(shown)} more")
        txt.append(f"  Buy: {session['ticketingUrl']}")
        md.append(f"- [Buy tickets]({session['ticketingUrl']}) · [Seat map]({session['seatMapUrl']})")
        txt.append("")
        md.append("")
    return "\n".join(txt), "\n".join(md)


def dispatch_alert(headline, txt, md):
    tg = send_telegram(txt)
    ntfy = send_ntfy(PREFIX + headline, txt)
    issue = send_issue(PREFIX + headline, md)
    print(f"alert dispatched — telegram: {tg} | ntfy: {ntfy} | issue: {issue}")
    return tg


# ------------------------------------------------------------------- git ops

def commit_state():
    if DRY_RUN or TEST_MODE:
        return "skipped"
    if os.environ.get("GITHUB_REF_NAME", "") != "main":
        print("state changed but not on main — not committing")
        return "skipped (not main)"
    def git(*args, check=True):
        return subprocess.run(["git", "-C", ROOT, *args], check=check,
                              capture_output=True, text=True)
    try:
        git("config", "user.name", "cinewatcher-bot")
        git("config", "user.email", "actions@users.noreply.github.com")
        git("add", "state.json", "docs/status.json")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return "nothing to commit"
        git("commit", "-m", "status: update showtime state")
        for attempt in range(1, 4):
            git("pull", "--rebase", "origin", "main", check=False)
            if git("push", "origin", "HEAD:main", check=False).returncode == 0:
                return "pushed"
            time.sleep(2 ** attempt)
        print("::error::state commit could not be pushed after 3 attempts")
        return "push FAILED"
    except subprocess.CalledProcessError as e:
        print(f"::error::git failure: {e.stderr}")
        return "git FAILED"


# --------------------------------------------------------------------- sweep

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sweep(old_state, totals):
    """One full pass. Returns the new state (or None if the sweep failed)."""
    now = now_utc().isoformat(timespec="seconds")
    baseline = old_state.get("schema") != 2
    zone_cfg = {"rows": GOOD_ROWS, "radius": GOOD_RADIUS}
    zone_changed = old_state.get("goodZone") != zone_cfg

    sessions, errors = {}, {}
    for tid in THEATRES:
        try:
            sessions.update(fetch_schedule(tid))
        except Exception as e:  # noqa: BLE001
            errors[tid] = f"{type(e).__name__}: {e}"
    if len(errors) == len(THEATRES):
        print(f"[{now}] sweep failed — all theatres errored: {errors}")
        return None

    # A theatre we failed to reach tells us nothing: carry its last known
    # sessions forward so they don't churn as deleted-then-new.
    old_sessions = old_state.get("sessions", {})
    old_good = old_state.get("goodSeats", {})
    for sid, entry in old_sessions.items():
        if entry.get("theatreId") in errors and sid not in sessions:
            sessions[sid] = {**entry, "id": sid,
                             "theatre": THEATRES[entry["theatreId"]]["name"],
                             "carried": True}

    new_ids = [sid for sid in sessions if sid not in old_sessions]

    # Seat phase: current good-zone availability for every live session.
    seat_results, seat_errors = {}, {}

    def check(sid):
        s = sessions[sid]
        if s.get("carried") or s["theatreId"] in errors:
            raise RuntimeError("theatre unreachable")
        return good_seats_available(s)

    live = [sid for sid in sessions]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {sid: pool.submit(check, sid) for sid in live}
    for sid, fut in futures.items():
        try:
            seat_results[sid] = fut.result()
        except Exception as e:  # noqa: BLE001
            seat_errors[sid] = f"{type(e).__name__}: {e}"

    good_seats = {}
    for sid in sessions:
        if sid in seat_results:
            good_seats[sid] = sorted(s["label"] for s in seat_results[sid])
        elif sid in old_good:
            good_seats[sid] = old_good[sid]  # carry forward, no false diffs
        else:
            good_seats[sid] = []

    # Freed-seat detection: only for sessions whose seat check succeeded and
    # that already had a snapshot under the same zone config.
    alert_log = dict(old_state.get("alertLog", {}))
    cutoff = (now_utc() - timedelta(minutes=COOLDOWN_MIN)).isoformat(timespec="seconds")
    alert_log = {k: v for k, v in alert_log.items()
                 if v >= cutoff and k.split(":", 1)[0] in sessions}

    freed = []
    if not baseline and not zone_changed:
        for sid, current in seat_results.items():
            if sid not in old_good or sid in new_ids:
                continue
            previous = set(old_good[sid])
            newly = [s for s in current if s["label"] not in previous]
            newly = [s for s in newly
                     if alert_log.get(f"{sid}:{s['label']}", "") < cutoff]
            if newly:
                avail_by_col = {(s["row"], s["col"]): s["label"] for s in current}
                freed.append((sessions[sid], newly, avail_by_col))
                for s in newly:
                    alert_log[f"{sid}:{s['label']}"] = now

    # Alerts.
    telegram_status = None
    if baseline:
        print(f"[{now}] BASELINE established — {len(sessions)} sessions recorded, "
              "no alerts this sweep")
    elif zone_changed:
        print(f"[{now}] good-zone config changed — seat snapshots rebuilt without alerting")
    else:
        if new_ids:
            txt, md = build_new_sessions_alert([sessions[i] for i in new_ids])
            telegram_status = dispatch_alert(
                "Dune: Part 3 — NEW IMAX 70mm showtimes are UP", txt, md)
            totals["alerts"] += 1
        if freed:
            txt, md = build_freed_seats_alert(freed)
            telegram_status = dispatch_alert(
                "Dune: Part 3 — good IMAX 70mm seats just FREED UP", txt, md)
            totals["alerts"] += 1

    new_state = {
        "schema": 2,
        "goodZone": zone_cfg,
        "sessions": {sid: {"theatreId": s["theatreId"], "start": s["start"],
                           "soldOut": s["soldOut"], "auditorium": s.get("auditorium", "")}
                     for sid, s in sessions.items()},
        "goodSeats": good_seats,
        "alertLog": alert_log,
    }

    stripped_old = {k: v for k, v in old_state.items() if k != "generatedAt"}
    if new_state != stripped_old:
        status = {
            "schema": 2,
            "generatedAt": now,
            "movie": "Dune: Part 3",
            "format": "IMAX 70mm",
            "goodZone": zone_cfg,
            "theatres": [{"id": tid, **info, "error": errors.get(tid)}
                         for tid, info in THEATRES.items()],
            "sessions": sorted(
                ({**{k: s.get(k) for k in ("id", "theatreId", "theatre", "start",
                                           "soldOut", "seatsRemaining", "experience",
                                           "ticketingUrl", "seatMapUrl")},
                  "goodSeatsFree": good_seats.get(s["id"], []),
                  "seatCheckError": seat_errors.get(s["id"])}
                 for s in sessions.values()),
                key=lambda s: (s["start"], s["theatreId"])),
        }
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=1)
        with open(STATE_PATH, "w") as f:
            json.dump(new_state, f, indent=1, sort_keys=True)
        print(f"[{now}] state changed — commit: {commit_state()}")

    if telegram_status:
        totals["telegram"] = telegram_status
    n_dates = len({s["start"][:10] for s in sessions.values()})
    print(f"[{now}] sweep ok — sessions={len(sessions)} dates={n_dates} "
          f"new={len(new_ids)} freedAlerts={len(freed)} "
          f"seatErrors={len(seat_errors)} theatreErrors={errors or 'none'}"
          + (" [BASELINE]" if baseline else ""))
    return new_state


def synthetic_test_alert():
    """Exercise every channel end-to-end with live data, marked [TEST]."""
    picks = []
    try:
        for tid in THEATRES:
            sched = fetch_schedule(tid)
            picks += list(sched.values())[:1]
    except Exception as e:  # noqa: BLE001
        print(f"test fetch failed: {e}")
    if not picks:
        print("::error::[TEST] could not fetch any live session to build the test alert")
        return False
    txt, md = build_new_sessions_alert(picks)
    txt += "\n\nThis is a TEST of the alert pipeline — no real change was detected."
    md += "\n\nThis is a TEST of the alert pipeline — no real change was detected."
    tg = dispatch_alert("Dune: Part 3 — alert pipeline test", txt, md)
    return not str(tg).startswith("FAILED")


# ---------------------------------------------------------------------- main

def main():
    totals = {"alerts": 0, "telegram": None, "sweeps_ok": 0, "sweeps_failed": 0}
    state = load_state()
    deadline = time.monotonic() + RUN_MINUTES * 60

    while True:
        started = time.monotonic()
        try:
            result = sweep(state, totals)
        except Exception as e:  # noqa: BLE001 - a sweep bug must not kill the run
            import traceback
            traceback.print_exc()
            print(f"::warning::sweep crashed: {type(e).__name__}: {e}")
            result = None
        if result is None:
            totals["sweeps_failed"] += 1
        else:
            totals["sweeps_ok"] += 1
            state = result
        if RUN_ONCE:
            break
        remaining = deadline - time.monotonic()
        if remaining < 20:
            break
        time.sleep(max(5, min(SWEEP_INTERVAL - (time.monotonic() - started), remaining - 10)))

    if TEST_MODE:
        ok = synthetic_test_alert()
        totals["alerts"] = 0  # a test never triggers the run-failed email
        totals["test_ok"] = ok

    with open(RESULT_PATH, "w") as f:
        json.dump(totals, f)
    print(f"run summary: {totals}")

    if totals["sweeps_ok"] == 0:
        print("every sweep in this run failed — the API, key, or network is broken",
              file=sys.stderr)
        return 1
    if TEST_MODE and not totals.get("test_ok"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
