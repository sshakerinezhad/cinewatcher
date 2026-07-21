# 🎬 Cinewatcher — The Odyssey IMAX 70mm ticket bot

Watches Cineplex for **The Odyssey** in **IMAX 70mm** on **August 21, 2026** at the only
two GTA theatres with 70mm IMAX projectors:

| Theatre | Cineplex location ID |
|---|---|
| Cineplex Cinemas Vaughan | 7408 |
| Cineplex Cinemas Mississauga Square One | 7420 |

**Status website:** https://sshakerinezhad.github.io/cinewatcher/
(append `?demo` to preview what the alert state looks like)

## How it works

[`checker.py`](checker.py) calls the same Cineplex showtimes API the cineplex.com website
uses. A GitHub Actions workflow ([`.github/workflows/watch.yml`](.github/workflows/watch.yml))
runs it **every 5 minutes**. The moment IMAX 70mm sessions appear for Aug 21 it fires, in
order:

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

An independent **Claude watchdog routine** also checks hourly and sends a Claude
push/email if tickets appear or if the workflow has stalled.

## Configuration

- Repo secrets (Settings → Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. If they're absent the Telegram step is skipped.
- The Cineplex API key in `checker.py` is Cineplex's own public frontend key, not a secret.
- A workflow run fails only when (a) tickets are found — intentional, see above — or
  (b) **both** theatre lookups error, which means the API or key changed and the bot
  needs fixing.

## Testing

Run the full alert path against a date that already has showtimes (today, for instance):
Actions → *Watch Odyssey IMAX 70mm* → *Run workflow* → set *test_date* to e.g.
`2026-07-21`. Test runs prefix alerts with `[TEST]` and never commit state, so the
status site keeps showing the real Aug 21 situation. Close the `[TEST]` issue afterwards
so a real alert opens a fresh issue.

Locally: `CINEWATCHER_DATE=2026-07-21 python3 checker.py && cat alert.txt`

## After August 21

Disable the workflow (Actions → *Watch Odyssey IMAX 70mm* → ⋯ → *Disable workflow*) and
delete the "Odyssey IMAX 70mm watchdog" routine in Claude.
