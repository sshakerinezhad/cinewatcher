# 🎬 Cinewatcher — Dune: Part 3 IMAX 70mm ticket bot

Watches Cineplex for **Dune: Part 3** in **IMAX 70mm** on **December 21, 2026** at the
only two GTA theatres with 70mm IMAX projectors:

| Theatre | Cineplex location ID |
|---|---|
| Cineplex Cinemas Vaughan | 7408 |
| Cineplex Cinemas Mississauga Square One | 7420 |

**Status website:** https://sshakerinezhad.github.io/cinewatcher/
(append `?demo` to preview what the alert state looks like)

## What it's waiting for

Cineplex has IMAX 70mm sessions on sale for **Dec 17-20** (Vaughan only, 2 per day);
Dec 21 onward has no Dune listing yet. Note that Dec 21 is *not* a closed booking date —
other films are already listed on it — so the signal is the film appearing on the board
for the 21st, not the date opening up. Once the 21st drops, the dates after it follow.

## How it works

[`checker.py`](checker.py) calls the same Cineplex showtimes API the cineplex.com website
uses. A GitHub Actions workflow ([`.github/workflows/watch.yml`](.github/workflows/watch.yml))
runs it **every 30 minutes**, at :07 and :37 — deliberately off the hour, since GitHub
delays scheduled runs most at :00/:30. It alerts on **IMAX 70mm only** — Cineplex tags those
sessions `["IMAX", "70mm"]`, and a Dune session on Dec 21 in any other format (plain
IMAX, standard digital) is tracked and shown on the status site but never notifies. The
moment IMAX 70mm sessions appear for Dec 21 it fires, in order:

1. **Telegram message** (primary) — full showtime list with seat availability and buy
   links, sent by [@odyssey_watcher_bot](https://t.me/odyssey_watcher_bot)
2. **ntfy push** (secondary) — topic `odyssey-imax-70mm-88955e30`
3. **GitHub issue** — showtimes, seats remaining, buy links, and seat-map links
4. **Status site update** — `docs/status.json` is committed, flipping the site to the
   alert view with per-session *Buy tickets* buttons and live seat counts
5. **Intentional run failure** — GitHub's "Run failed" email doubles as an email alert
   (CI-failure emails aren't suppressed the way @mention notifications are)

New sessions in a later wave are detected individually and alert again (as a comment on
the existing alert issue). Seat-count fluctuations alone don't re-alert; sold-out
transitions update the site.

The bot name and ntfy topic still say "odyssey" — they're just identifiers carried over
from the previous watch, and renaming them would mean re-pairing the Telegram bot and
re-subscribing the ntfy topic for no benefit.

## Configuration

- Repo secrets (Settings → Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. If they're absent the Telegram step is skipped.
- The Cineplex API key in `checker.py` is Cineplex's own public frontend key, not a secret.
- A workflow run fails only when (a) tickets are found — intentional, see above — or
  (b) **both** theatre lookups error, which means the API or key changed and the bot
  needs fixing.

## Testing

Run the full alert path against a date that already has showtimes:
Actions → *Watch Dune Part 3 IMAX 70mm* → *Run workflow* → set *test_date* to
`2026-12-17` (Vaughan has two IMAX 70mm sessions that day). Test runs prefix alerts with
`[TEST]` and never commit state, so the status site keeps showing the real Dec 21
situation. Close the `[TEST]` issue afterwards so a real alert opens a fresh issue.

Locally: `CINEWATCHER_DATE=2026-12-17 python3 checker.py && cat alert.txt`

## After the drop

Disable the workflow (Actions → *Watch Dune Part 3 IMAX 70mm* → ⋯ → *Disable workflow*),
or remove the `schedule:` trigger from the workflow. There is no self-chaining dispatch in
this version, so stopping the cron is sufficient.

To retarget at a different film or date afterwards: `TARGET_DATE` and `MOVIE_MATCH` at the
top of [`checker.py`](checker.py), reset [`state.json`](state.json) to
`{"imax70mm": {}, "other": {}}`, and update the headline strings in `checker.py` and the
workflow.
