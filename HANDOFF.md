# Cinewatcher — handoff

> **2026-09-01 (later):** largely superseded. The watcher was rebuilt on the user's
> instruction: it now watches **all dates** (not just Dec 21), adds the **seat-level
> good-seat watch** proposed in *Unresolved #3*, runs at ~2-minute cadence on the
> revived self-chain, and has a daily health check + heartbeat. See `README.md`.
> Still accurate below: the seat-availability findings, the API reference, the
> Aug 18 alert mystery (*Unresolved #1* — the daily Telegram heartbeat now bounds it
> to 24h), and the squash-merge gotcha. The gzip fix described below turned out to be
> incomplete — some CDN edges **double-gzip** (killed the Aug 31 23:20 and Sep 1 02:00
> runs); `decompress()` now unwraps in a loop.

Written 2026-09-01. Everything described here is merged to `main` (`94ec096` + this commit).

## TL;DR

The watcher is **live and correct**, but its job is already done and the outcome was
missed: **Dune: Part 3 IMAX 70mm tickets for Dec 21 went on sale on 2026-08-18 and were
not bought.** The good seats are now gone. Nothing is pending in git; the open work is a
decision, not a merge.

---

## What it does

`checker.py` polls the Cineplex showtimes API for **Dune: Part 3** in **IMAX 70mm** on
**2026-12-21** at the only two GTA theatres with 70mm IMAX projectors — Vaughan (7408)
and Mississauga Square One (7420). Verified empirically: across all 51 Cineplex theatres
within 200 km of Toronto, only these two ever run IMAX 70mm.

`.github/workflows/watch.yml` runs it on cron `7,37 * * * *`. On a **new** IMAX 70mm
session appearing it fires, in order: Telegram → ntfy → GitHub issue → deliberate `exit 1`
(the "Run failed" email is itself an alert channel).

- Alerts on **IMAX 70mm only**. Cineplex tags these `["IMAX", "70mm"]`. Sessions in any
  other format are tracked in `state.json` and shown on the status site but never notify.
- Change detection is by `vistaSessionId` in `state.json`. Seat-count changes alone do
  not re-alert.
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (both present and working as of
  2026-09-01). `DISPATCH_PAT` still exists but is unused — it drove the old self-chain.
- Bot is [@odyssey_watcher_bot](https://t.me/odyssey_watcher_bot); ntfy topic is
  `odyssey-imax-70mm-88955e30`. Both names are inherited from the previous Odyssey watch
  and are just identifiers — renaming would mean re-pairing the bot and re-subscribing
  the topic for no benefit.

---

## Live situation as of 2026-09-01

**Tickets have been on sale since 2026-08-18 17:35 UTC.** The bot detected it correctly,
filed [issue #13](https://github.com/sshakerinezhad/cinewatcher/issues/13) (still open),
pushed ntfy, sent Telegram, and failed the run for the email. The user reports receiving
none of it. See *Unresolved* below.

All 8 Dec 21 sessions still show 78–97 seats remaining and none are sold out — but that
number is deeply misleading (see next section).

---

## Seat availability findings

`seatsRemaining` counts the whole auditorium. Both houses are 10 rows (A front → J back)
× 29 columns, ~265 seats. Demand is heavily concentrated at the back, so a session
reporting "78 seats left" can have **zero** seats in the rear half.

Scanned all **196** IMAX 70mm showtimes, 2026-12-15 → 2027-01-11, both theatres. Percentage
of seats free, aggregated over every showtime:

| Row | A | B | C | D | E | F | G | H | I | J |
|---|---|---|---|---|---|---|---|---|---|---|
| free | 92.8% | 93.1% | 84.3% | 73.6% | 52.8% | 18.2% | 15.2% | 12.6% | 11.3% | 9.8% |

- **Nothing in rows F–J is dead centre on any showtime in the entire run.** Every rear row
  has a contiguous occupied block through the middle; only the flanks are free.
- Closest to centre available anywhere: **4 seats off centre**, January only, almost all
  at Square One. Best pair: **G19+G20, Square One, Thu 2027-01-07 22:00**.
- December is far worse. Best rear-half seat in December is F22 at Square One on Dec 31
  22:45, 7 seats off centre. Dec 15–24 tops out at 12–14 off centre (against the wall).
- **Dec 21** — the original target — has exactly one free seat in the whole rear half
  across both theatres: G28.
- Square One beats Vaughan consistently: 27/99 of its showtimes have zero rear-half
  availability, versus 39/97 at Vaughan.

Use `tools/seatmap.py` to re-check any of this; the numbers move as seats sell.

---

## What was fixed (both merged, `94ec096`)

**gzip responses broke ~1 run in 3.** Cineplex's CDN began returning
`content-encoding: gzip` regardless of the request. `urllib` does not decompress, so
`json.loads()` died on the magic bytes with `UnicodeDecodeError`. It varied by CDN edge,
so failures looked random. Every failed run was a blind window. `checker.decompress()`
now handles it by header *and* by magic-byte sniff.

**Telegram failures were invisible.** The send ended in `|| true`; a revoked token,
blocked bot or wrong chat id was indistinguishable from success. It now checks for
`ok:true`, records the `message_id`, emits `::error::` with Telegram's own description on
failure, and threads the delivery status into the run-failed email.

---

## Unresolved

1. **Why the Aug 18 alert reached nobody.** All four channels fired. Telegram was
   re-tested on 2026-09-01 and returned `{"ok":true,"message_id":18}` — delivered. The id
   was 15 on Aug 3, so two messages passed through that chat in between, one almost
   certainly the Aug 18 alert. **The user was asked to check their Telegram history around
   Aug 18 13:35 EDT and has not yet answered.** If the message is there, this is
   notification routing (muted chat), not code. If absent, Telegram lied about delivery
   and needs deeper investigation.
   - Contributing factor worth noting: the gzip bug had runs failing repeatedly for weeks,
     each sending a "Run failed" email shaped exactly like the real alert. Plausible alarm
     fatigue, but unverified for the pre-Aug-18 window.

2. **Is the rear-row occupied block real?** It is a clean contiguous rectangle in every
   rear row of every showtime, which does not look like organic sales (those are lumpier).
   It may be held inventory that gets released later. The seat-availability count matches
   the official `seatsRemaining` exactly on 187/196 showtimes, so those seats are
   genuinely not purchasable right now — but "not purchasable" is not the same as "sold".

3. **Proposed but not built:** a seat-level watch — alert when a seat within N columns of
   centre in rows F–H frees up on chosen dates, rather than only on new showtimes
   appearing. `tools/seatmap.py` already has the endpoints. The user was asked and has not
   yet answered.

---

## Gotchas

- **GitHub's cron is unreliable on this repo.** Measured: with `*/5` requested, actual
  delivery was 53–212 minutes apart, median 98. The user decided (explicitly) to accept
  cron-only and live with 1–3 hour gaps. Do not "fix" this without asking. The proven
  alternative is the self-chaining PAT dispatch the Odyssey watch used — see the `README`
  Cadence section and git history around `838de70`.
- **Scheduled runs used to be cancelled silently.** 28 of 30 historical scheduled runs
  concluded `cancelled` — the concurrency group killed them while the self-chain occupied
  it. The chain is gone, so this no longer applies, but be careful reintroducing it.
- **Squash merges rewrite history.** PRs here are squash-merged, so after a merge the
  local branch's original commit conflicts with `main`. Restart the branch from
  `origin/main` and cherry-pick anything unmerged rather than trying to merge across.
- **Retargeting requires resetting `state.json`** to `{"imax70mm": {}, "other": {}}`,
  otherwise the first run treats every session as new and alerts immediately. Also update
  `TARGET_DATE`/`MOVIE_MATCH` in `checker.py` plus the headline strings there and in the
  workflow.
- **Test mode**: any `CINEWATCHER_DATE` / `test_date` input prefixes alerts with `[TEST]`
  and skips the state commit. A test alert **comments on the existing open alert issue**
  rather than opening a new one — issue #13 currently carries a `[TEST]` comment from the
  Sep 1 diagnostics.
- `.bot-alive` is a stale artifact from July and is not used by anything.
- The Claude watchdog routine (`trig_01HPsuvY4xS4tNqSYxCRNcV3`, "Odyssey IMAX 70mm
  watchdog") is **disabled, not deleted** — re-arming is a toggle. It was the independent
  hourly check that would have caught the bot stalling.

---

## API reference

All endpoints take the same public subscription key embedded in the cineplex.com
frontend (`dcdac5601d864addbc2675a2e96cb1f8`) — not a secret.

| Purpose | Endpoint |
|---|---|
| Showtimes | `GET /prod/cpx/theatrical/api/v1/showtimes?language=en&locationId={id}&date=M/D/YYYY` |
| Theatres near a point | `GET /prod/cpx/theatrical/api/v1/theatres?language=en&range={km}&latitude=&longitude=` |
| Seat layout | `GET /prod/ticketing/api/v1/theatre/{tid}/showtime/{sid}/seat-layout` |
| Seat availability | `GET /prod/ticketing/api/v1/theatre/{tid}/showtime/{sid}/seat-availability?preview=true` |

Base is `https://apis.cineplex.com`. Seat statuses are `Available` / `Occupied` /
`Broken`; `preview=true` and `preview=false` return identical data. An empty response body
means the date has no showtimes at all. `www.cineplex.com` itself is a client-rendered
Next.js app and is not scrapable without a browser — and the sandbox proxy blocks
Chromium, so don't plan on Playwright here.

---

## Turning it off

Remove the `schedule:` trigger from the workflow, or Actions → *Watch Dune Part 3 IMAX
70mm* → ⋯ → *Disable workflow*. There is no self-chaining dispatch in this version, so
stopping the cron is sufficient. Also close issue #13 and, if it was ever re-armed, disable
the Claude routine.
