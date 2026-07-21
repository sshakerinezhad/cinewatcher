# 🎬 Cinewatcher — The Odyssey IMAX 70mm ticket bot

Watches Cineplex for **The Odyssey** in **IMAX 70mm** on **August 21, 2026** at:

| Theatre | Cineplex location ID |
|---|---|
| Cineplex Cinemas Vaughan | 7408 |
| Cineplex Cinemas Mississauga Square One | 7420 |

(These are the only two GTA theatres with 70mm IMAX projectors — Courtney Park's IMAX is digital-only.)

## How it works

A GitHub Actions workflow ([`.github/workflows/watch.yml`](.github/workflows/watch.yml)) runs
**every 5 minutes** and calls the same Cineplex showtimes API the cineplex.com website uses
([`checker.py`](checker.py)). The moment IMAX 70mm sessions appear for Aug 21, it:

1. **📱 Pushes an instant phone notification** via [ntfy.sh](https://ntfy.sh) to topic
   **`odyssey-imax-70mm-88955e30`**
2. **📧 Opens a GitHub issue** @mentioning you (triggers a GitHub email notification)
3. **🌐 Updates the status website** in [`docs/`](docs/) with the showtimes and direct
   *Buy tickets* links

If a *second wave* of showtimes drops later, new sessions are detected individually and a
comment is added to the same issue (plus another push notification).

## ⚠️ One-time setup (do these!)

1. **Get instant phone alerts:** install the [ntfy app](https://ntfy.sh/) (iOS/Android),
   tap *Add subscription*, and subscribe to the topic `odyssey-imax-70mm-88955e30`.
   No account needed. (Or just open <https://ntfy.sh/odyssey-imax-70mm-88955e30> in a browser tab.)
2. **Make this repo public** (Settings → General → Danger Zone → Change visibility).
   Strongly recommended — there's nothing sensitive here, and:
   - Public repos get **unlimited free** Actions minutes. Private repos get 2,000/month,
     which a 5-minute cron burns through in ~1 week (then the bot silently stops).
   - GitHub Pages (the status website) is free on public repos.
3. **Enable the status website:** Settings → Pages → *Deploy from a branch* →
   branch `main`, folder `/docs`.
   The site will be at `https://sshakerinezhad.github.io/cinewatcher/`.
4. **Check your GitHub notification settings** (Settings → Notifications) so that
   *Participating / @mentions* delivers email — the alert issue @mentions you.

If you'd rather keep the repo private, edit the `cron` line in
`.github/workflows/watch.yml` to `*/30 * * * *` so free minutes last the month
(and skip the Pages site, or open `docs/index.html` locally).

## Status website

`docs/index.html` reads `docs/status.json` (committed by the bot whenever showtime state
changes) and auto-refreshes every 60 seconds. It shows a big **SHOWTIMES ARE UP** banner
with per-session *Buy tickets* buttons once tickets drop, and also lists any *other*
Odyssey formats (UltraAVX, regular, etc.) that appear for Aug 21 — often a leading
indicator that the 70mm drop is imminent.

## Notes

- The API key in `checker.py` is Cineplex's own public key, embedded in their website
  frontend for anonymous use — not a secret.
- The checker only commits when state actually changes (new sessions or sold-out
  transitions), so history stays clean.
- A workflow run fails only if *both* theatre lookups error — a signal the API or key
  changed and the bot needs fixing.
- After August 21, disable the workflow (Actions → Watch Odyssey IMAX 70mm → ⋯ → Disable).

## Test it

Run a manual check any time: Actions → *Watch Odyssey IMAX 70mm* → *Run workflow*.
To test the full alert path, run locally with today's date (showtimes exist now):

```bash
CINEWATCHER_DATE=2026-07-21 python3 checker.py && cat alert.md
```
