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
ALERT_PATH = os.path.join(ROOT, "alert.md")


def api_date(iso_date):
    y, m, d = (int(x) for x in iso_date.split("-"))
    return f"{m}/{d}/{y}"


def fetch(theatre_id):
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
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    if not body:
        return []
    return json.loads(body)


def is_70mm(experience_types):
    types = {t.lower() for t in experience_types}
    return any("imax" in t for t in types) and any("70" in t for t in types)


def collect_sessions(payload, theatre_id):
    """Return (imax70mm, other) session lists for the target date."""
    imax, other = [], []
    for theatre in payload:
        if theatre.get("theatreId") != theatre_id:
            continue
        for date in theatre.get("dates", []):
            for movie in date.get("movies", []):
                if MOVIE_MATCH not in movie.get("name", "").lower():
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

    # State used for change detection: session ids + sold-out flags only, so
    # routine seat-count fluctuations don't generate a commit every run.
    new_state = {
        "imax70mm": {s["id"]: {"soldOut": s["soldOut"]} for s in all_imax},
        "other": {s["id"]: {"soldOut": s["soldOut"]} for s in all_other},
        "errorTheatres": sorted(e["theatreId"] for e in errors),
    }
    try:
        with open(STATE_PATH) as f:
            old_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old_state = None

    changed = new_state != old_state
    known_70mm = set((old_state or {}).get("imax70mm", {}))
    new_70mm = [s for s in all_imax if s["id"] not in known_70mm]

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

    if new_70mm:
        lines = [
            "@sshakerinezhad **The Odyssey — IMAX 70mm showtimes for "
            f"{TARGET_DATE} are UP!** \U0001f3ac",
            "",
        ]
        by_theatre = {}
        for s in new_70mm:
            by_theatre.setdefault(s["theatre"], []).append(s)
        for theatre, sessions in by_theatre.items():
            lines.append(f"### {theatre}")
            for s in sessions:
                sold = " — **SOLD OUT**" if s["soldOut"] else ""
                lines.append(f"- **{fmt_time(s['start'])}** ({s['experience']}){sold} — [Buy tickets]({s['ticketingUrl']})")
            lines.append("")
        lines.append("Book fast — first drops sold out within hours.")
        lines.append("")
        lines.append("Movie page: https://www.cineplex.com/movie/the-odyssey")
        with open(ALERT_PATH, "w") as f:
            f.write("\n".join(lines))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"new70={'true' if new_70mm else 'false'}\n")
            f.write(f"found={'true' if all_imax else 'false'}\n")

    print(f"[{now}] target={TARGET_DATE} imax70mm={len(all_imax)} "
          f"other={len(all_other)} new70mm={len(new_70mm)} changed={changed} errors={errors}")

    # Fail the run only if every theatre errored (likely API/key breakage).
    if errors and len(errors) == len(THEATRES):
        print("All theatre lookups failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
