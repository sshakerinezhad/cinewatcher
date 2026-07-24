#!/usr/bin/env python3
"""Poll the Cineplex showtimes API for The Odyssey in IMAX 70mm.

Watches Cineplex Cinemas Vaughan (7408) and Cineplex Cinemas Mississauga
Square One (7420) for showtimes on the target date. Writes docs/status.json
(for the status website), state.json (change-detection state committed to the
repo), and alert.md (notification body) when new IMAX 70mm sessions appear.

Outputs for GitHub Actions (via $GITHUB_OUTPUT):
  changed  - "true" if state.json / status.json should be committed
  new70    - "true" if brand-new IMAX 70mm sessions were detected
  found    - "true" if any IMAX 70mm sessions currently exist
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes"
# Public subscription key embedded in the cineplex.com web frontend.
KEY = os.environ.get("CINEPLEX_API_KEY", "dcdac5601d864addbc2675a2e96cb1f8")

TARGET_DATE = os.environ.get("CINEWATCHER_DATE") or "2026-08-21"  # YYYY-MM-DD
MOVIE_MATCH = os.environ.get("CINEWATCHER_MOVIE", "odyssey").lower()

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

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
STATUS_PATH = os.path.join(ROOT, "docs", "status.json")
ALERT_MD_PATH = os.path.join(ROOT, "alert.md")      # GitHub issue body
ALERT_TXT_PATH = os.path.join(ROOT, "alert.txt")    # Telegram message body
RESULT_PATH = os.path.join(ROOT, "result.json")     # this run's outcome, for CI

TEST_MODE = bool(os.environ.get("CINEWATCHER_DATE"))
PREFIX = "[TEST] " if TEST_MODE else ""


def api_date(iso_date):
    y, m, d = (int(x) for x in iso_date.split("-"))
    return f"{m}/{d}/{y}"


def fetch(theatre_id, attempts=4):
    """Fetch showtimes, retrying transient failures.

    Runner-side network blips are common enough that a single failed request
    must not be reported as an outage: a theatre marked errored is a theatre
    we are not actually watching.
    """
    url = f"{API}?language=en&locationId={theatre_id}&date={api_date(TARGET_DATE)}"
    req = urllib.request.Request(
        url,
        headers={
            "Ocp-Apim-Subscription-Key": KEY,
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            return json.loads(body) if body else []
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            if attempt == attempts:
                raise
            print(f"  theatre {theatre_id} attempt {attempt}/{attempts} failed "
                  f"({type(e).__name__}), retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)


def is_70mm(experience_types):
    types = {t.lower() for t in experience_types}
    return any("imax" in t for t in types) and any("70" in t for t in types)


def is_target_movie(name):
    # Match Nolan's "The Odyssey" (incl. variants like "The Odyssey: The IMAX
    # Experience") without false-positives on "2001: A Space Odyssey" reruns.
    n = name.lower()
    return MOVIE_MATCH in n and "2001" not in n and "space odyssey" not in n


def collect_sessions(payload, theatre_id):
    """Return (imax70mm, other) session lists for the target date."""
    imax, other = [], []
    for theatre in payload:
        if theatre.get("theatreId") != theatre_id:
            continue
        for date in theatre.get("dates", []):
            for movie in date.get("movies", []):
                if not is_target_movie(movie.get("name", "")):
                    continue
                for exp in movie.get("experiences", []):
                    types = exp.get("experienceTypes", [])
                    for s in exp.get("sessions", []):
                        start = s.get("showStartDateTime", "")
                        if not start.startswith(TARGET_DATE):
                            continue
                        entry = {
                            "id": str(s.get("vistaSessionId")),
                            "theatreId": theatre_id,
                            "theatre": THEATRES[theatre_id]["name"],
                            "movie": movie.get("name"),
                            "start": start,
                            "experience": " ".join(types),
                            "soldOut": bool(s.get("isSoldOut")),
                            "seatsRemaining": s.get("seatsRemaining"),
                            "ticketingUrl": s.get("ticketingUrl"),
                            "seatMapUrl": s.get("seatMapUrl"),
                        }
                        (imax if is_70mm(types) else other).append(entry)
    return imax, other


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except ValueError:
        return iso


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_imax, all_other, errors = [], [], []

    for tid in THEATRES:
        try:
            payload = fetch(tid)
            imax, other = collect_sessions(payload, tid)
            all_imax += imax
            all_other += other
        except Exception as e:  # noqa: BLE001 - record and keep checking others
            errors.append({"theatreId": tid, "error": f"{type(e).__name__}: {e}"})

    all_imax.sort(key=lambda s: (s["theatreId"], s["start"]))
    all_other.sort(key=lambda s: (s["theatreId"], s["start"]))

    try:
        with open(STATE_PATH) as f:
            old_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old_state = None

    # State used for change detection: session ids, sold-out flags and owning
    # theatre only, so routine seat-count fluctuations don't commit every run.
    errored = {e["theatreId"] for e in errors}
    new_state = {
        "imax70mm": {s["id"]: {"soldOut": s["soldOut"], "theatreId": s["theatreId"]} for s in all_imax},
        "other": {s["id"]: {"soldOut": s["soldOut"], "theatreId": s["theatreId"]} for s in all_other},
    }
    # A theatre we failed to reach tells us nothing — carry its last known
    # sessions forward. Otherwise they'd look deleted now and brand new (a
    # duplicate alert) the moment the theatre comes back.
    for key in ("imax70mm", "other"):
        for sid, entry in (old_state or {}).get(key, {}).items():
            if entry.get("theatreId") in errored and sid not in new_state[key]:
                new_state[key][sid] = entry

    changed = new_state != old_state
    known_70mm = set((old_state or {}).get("imax70mm", {}))
    known_other = set((old_state or {}).get("other", {}))
    new_70mm = [s for s in all_imax if s["id"] not in known_70mm]
    # Also alert on non-70mm Odyssey sessions appearing for the target date.
    # We can't assume Cineplex tags the 70mm screening exactly "IMAX"+"70mm"
    # on day one — if the label differs or lands late, an alert that says
    # "the date went live, go look" still gets you there in time. Missing the
    # drop is unrecoverable; an extra notification is not.
    new_other = [s for s in all_other if s["id"] not in known_other]

    status = {
        "generatedAt": now,
        "targetDate": TARGET_DATE,
        "movie": "The Odyssey",
        "format": "IMAX 70mm",
        "theatres": [
            {"id": tid, **info, "error": next((e["error"] for e in errors if e["theatreId"] == tid), None)}
            for tid, info in THEATRES.items()
        ],
        "found": bool(all_imax),
        "imax70mm": all_imax,
        "otherFormats": all_other,
    }

    if changed:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=1)
        with open(STATE_PATH, "w") as f:
            json.dump(new_state, f, indent=1, sort_keys=True)

    alert_sessions = new_70mm + new_other
    if alert_sessions:
        by_theatre = {}
        for s in alert_sessions:
            by_theatre.setdefault(s["theatre"], []).append(s)

        if new_70mm:
            head_md = f"@sshakerinezhad **The Odyssey — IMAX 70mm showtimes for {TARGET_DATE} are UP!** \U0001f3ac"
            head_txt = f"{PREFIX}\U0001f6a8 The Odyssey — IMAX 70mm showtimes for {TARGET_DATE} are UP! BOOK NOW."
        else:
            head_md = (f"@sshakerinezhad **The Odyssey listings for {TARGET_DATE} just went live** "
                       "— no IMAX 70mm sessions tagged yet, but the date is open. Check now; "
                       "70mm often appears within minutes.")
            head_txt = (f"{PREFIX}\U0001f440 The Odyssey — {TARGET_DATE} listings just went live at your theatres. "
                        "No IMAX 70mm tagged yet, but CHECK NOW — 70mm often follows within minutes.")
        md = [head_md, ""]
        txt = [head_txt, ""]
        for theatre, sessions in by_theatre.items():
            md.append(f"### {theatre}")
            txt.append(f"{theatre}:")
            for s in sessions:
                t = fmt_time(s["start"])
                if s["soldOut"]:
                    avail = "SOLD OUT"
                elif s["seatsRemaining"] is not None:
                    avail = f"{s['seatsRemaining']} seats left"
                else:
                    avail = "on sale"
                md.append(f"- **{t}** ({s['experience']}) — {avail} — "
                          f"[Buy tickets]({s['ticketingUrl']}) · [Seat map]({s['seatMapUrl']})")
                txt.append(f"  {t} — {avail} — {s['ticketingUrl']}")
            md.append("")
            txt.append("")
        md += ["Book fast — first drops sold out within hours.", "",
               "Movie page: https://www.cineplex.com/movie/the-odyssey"]
        txt.append("Movie page: https://www.cineplex.com/movie/the-odyssey")
        with open(ALERT_MD_PATH, "w") as f:
            f.write("\n".join(md))
        with open(ALERT_TXT_PATH, "w") as f:
            f.write("\n".join(txt))

    # The workflow reads this file rather than $GITHUB_OUTPUT: the check step
    # runs this script up to three times, and relying on which duplicate
    # output key wins would risk silently dropping an alert found on a later
    # pass. The workflow accumulates these results explicitly instead.
    with open(RESULT_PATH, "w") as f:
        json.dump({"changed": changed, "alert": bool(alert_sessions),
                   "has70mm": bool(new_70mm), "found": bool(all_imax)}, f)

    print(f"[{now}] target={TARGET_DATE} imax70mm={len(all_imax)} "
          f"other={len(all_other)} new70mm={len(new_70mm)} "
          f"newOther={len(new_other)} changed={changed} errors={errors}")

    # Fail the run only if every theatre errored (likely API/key breakage).
    if errors and len(errors) == len(THEATRES):
        print("All theatre lookups failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
