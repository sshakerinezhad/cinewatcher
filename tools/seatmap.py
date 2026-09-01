#!/usr/bin/env python3
"""Inspect Cineplex seat maps for the watched showtimes.

The showtimes API (used by checker.py) reports only a seatsRemaining count,
which is misleading on its own: a session can report 78 seats left while every
seat in the rear half is gone and the remainder is the front three rows. These
endpoints, reverse-engineered from the cineplex.com ticketing bundle, give the
actual per-seat picture:

  GET {TICKETING}/v1/theatre/{theatreId}/showtime/{showtimeId}/seat-layout
  GET {TICKETING}/v1/theatre/{theatreId}/showtime/{showtimeId}/seat-availability?preview=true

Both take the same public Ocp-Apim-Subscription-Key as the showtimes API.
`preview=true` and `preview=false` return identical data. Seat statuses are
Available / Occupied / Broken, and the Available count matches the showtimes
API's seatsRemaining exactly, so Occupied means genuinely not purchasable.

Usage:
  python3 tools/seatmap.py list 2026-12-21          # showtimes + ids for a date
  python3 tools/seatmap.py map 7420 388032          # render one auditorium
"""

import gzip
import json
import sys
import urllib.request

KEY = "dcdac5601d864addbc2675a2e96cb1f8"
SHOWTIMES = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes"
TICKETING = "https://apis.cineplex.com/prod/ticketing/api/v1/theatre"
THEATRES = {7408: "Vaughan", 7420: "Square One"}
HEADERS = {
    "Ocp-Apim-Subscription-Key": KEY,
    "Accept": "application/json",
    # Cineplex serves gzip whether or not it is asked to; see checker.decompress.
    "Accept-Encoding": "gzip",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
        body = r.read()
        encoding = (r.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body) if body.strip() else None


def is_70mm(types):
    lowered = [t.lower() for t in types]
    return any("imax" in t for t in lowered) and any("70" in t for t in lowered)


def cmd_list(iso_date):
    y, m, d = (int(x) for x in iso_date.split("-"))
    for tid, name in THEATRES.items():
        payload = get(f"{SHOWTIMES}?language=en&locationId={tid}&date={m}/{d}/{y}") or []
        for theatre in payload:
            for date in theatre.get("dates", []):
                for movie in date.get("movies", []):
                    for exp in movie.get("experiences", []):
                        if not is_70mm(exp.get("experienceTypes", [])):
                            continue
                        for s in exp.get("sessions", []):
                            print(f"{name:12} {s['showStartDateTime'][11:16]}  "
                                  f"showtimeId={s['vistaSessionId']:<8} "
                                  f"seatsRemaining={s.get('seatsRemaining')}")


def cmd_map(theatre_id, showtime_id):
    base = f"{TICKETING}/{theatre_id}/showtime/{showtime_id}"
    layout = get(f"{base}/seat-layout")
    avail = get(f"{base}/seat-availability?preview=true")["seatAvailabilities"]
    block = layout["standardSeats"]
    width = block["columnCount"]
    centre = (width - 1) / 2

    print(f"{THEATRES.get(int(theatre_id), theatre_id)} — showtime {showtime_id}")
    print("  . free   # taken   x broken   ^ = dead centre\n")
    print("      " + "".join(str(c % 10) for c in range(width)))
    for row in block["rows"]:
        if not row["seats"]:
            continue
        cells = [" "] * width
        near = []
        for seat in row["seats"]:
            status = avail.get(seat["id"], "Available")
            cells[seat["column"]] = {"Available": ".", "Broken": "x"}.get(status, "#")
            if status == "Available":
                near.append((abs(seat["column"] - centre), seat["label"]))
        near.sort()
        closest = f"  closest to centre: {', '.join(l for _, l in near[:4])}" if near else ""
        print(f"  {row['label'] or '?':2}  " + "".join(cells) + f"  {len(near):3} free{closest}")
    print("      " + " " * int(centre) + "^")
    free = sum(1 for v in avail.values() if v == "Available")
    print(f"\n  {free} of {len(avail)} seats available")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "list":
        cmd_list(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "map":
        cmd_map(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
