# 🎬 Cinewatcher — Dune: Part 3 IMAX 70mm ticket bot

Watches Cineplex for **Dune: Part 3** in **IMAX 70mm** — **every showtime on every
date** — at the only two GTA theatres with 70mm IMAX projectors:

| Theatre | Cineplex location ID |
|---|---|
| Cineplex Cinemas Vaughan | 7408 |
| Cineplex Cinemas Mississauga Square One | 7420 |

**Status website:** https://sshakerinezhad.github.io/cinewatcher/

**Picking up this project?** Read [HANDOFF.md](HANDOFF.md) for history and gotchas that
are not obvious from the code.

## What triggers an alert

Checked in a sweep every **~2 minutes**, around the clock:

1. **New showtimes / new dates.** One schedule call per theatre (no `date` parameter)
   returns the theatre's *entire* calendar, so a run extension or a fresh on-sale date is
   caught on the next sweep, whatever date it lands on.
2. **A good seat freeing up** — someone cancelling, or held inventory being released.
   Every tracked session's seat map is checked seat-by-seat; a seat flipping to
   `Available` inside the good zone (rows `F`–`J`, within 6 columns of the auditorium
   centre — as good as or better than anything that was still buyable when this was
   built) alerts with the exact seat labels, how far off centre they are, whether they
   form a pair, and the buy link. A re-freed seat re-alerts after a 90-minute cooldown
   (seats bounce when carts expire). Wheelchair/companion spots don't count.

`seatsRemaining` from the showtimes API counts the whole auditorium and is misleading —
sessions report "78 seats left" with the entire rear half gone. That's why the watch is
seat-level. [`tools/seatmap.py`](tools/seatmap.py) renders any auditorium for a manual look.

## Alert channels (in order, each independent)

1. **Telegram** (primary) — [@odyssey_watcher_bot](https://t.me/odyssey_watcher_bot)
2. **ntfy push** — topic `odyssey-imax-70mm-88955e30`
3. **GitHub issue** — full details, buy links (label `dune3-alert`)
4. **Run-failed email** — the run fails on purpose after an alert; GitHub's email is the
   fourth channel

The bot/topic names still say "odyssey" — identifiers inherited from the previous watch;
renaming would mean re-pairing for no benefit.

## Cadence: the self-chain

GitHub's cron is unreliable here (measured 53–212 min between scheduled runs, median 98),
so [`watch.yml`](.github/workflows/watch.yml) uses the mechanism the Odyssey watch proved
over 2,780 runs: each run **dispatches its own successor** (via the `DISPATCH_PAT`
secret, `github.token` as fallback), then sweeps every ~2 minutes for ~25 minutes. The
successor is dispatched at the *start* of the run and waits in the concurrency queue, so
the chain survives even a killed runner. The `7,37 * * * *` cron is only a backstop that
revives a dead chain.

Consequences to know about:

- **Most *scheduled* runs show as "cancelled".** While the chain is healthy, a queued
  cron run is displaced by the chain's own successor. By design; not a failure.
- A **crash-loop guard** stops the chain (falling back to cron-only) if 3 consecutive
  runs fail in under 5 minutes — a broken watcher must not spin forever.
- **To stop everything:** disable the workflow (Actions → *Watch Dune Part 3 IMAX 70mm*
  → ⋯ → *Disable workflow*) — dispatches to a disabled workflow fail, so this kills the
  chain too. Cancel any queued run, and disable the *Daily health check* workflow and
  the Claude watchdog routine while you're at it.

## Daily health check

[`health.yml`](.github/workflows/health.yml) runs [`tools/health.py`](tools/health.py)
every morning (~08:23 Toronto). It verifies the chain's real cadence over the last 24h,
that a run is in progress right now, that the Cineplex API still parses, and that
`state.json` is sane — then **always sends a Telegram heartbeat** with the verdict. The
heartbeat is the point: the Aug 18 drop was alerted but reached nobody, so the daily
message is a standing end-to-end proof that Telegram delivery works. **If the morning
heartbeat stops arriving, the system is broken — go look.** Failures also go to ntfy, a
`dune3-health` issue, and a run-failed email.

A disabled-able Claude watchdog routine ("Dune IMAX 70mm daily watchdog") independently
re-checks all of this daily from outside GitHub, in case GitHub's cron silently skips
the health check itself.

## Configuration

- Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISPATCH_PAT`
  (fine-grained PAT, Actions: read+write on this repo), optional `CINEPLEX_API_KEY`
  (defaults to Cineplex's public frontend key; set it if they rotate the key — grab the
  new one from any API request the cineplex.com showtimes page makes).
- Watch tuning lives in the `env:` block at the top of `watch.yml`: `GOOD_ROWS`,
  `GOOD_RADIUS`, `SWEEP_INTERVAL_SECONDS`, `RUN_MINUTES`, `ALERT_COOLDOWN_MINUTES`.
  Changing the good zone re-baselines seat snapshots without alerting.
- `state.json` (schema 2) is the change-detection state. If it's ever reset or the
  schema bumps, the next sweep records a baseline **without alerting**, and alerts
  resume on the sweep after.

## Testing

Actions → *Watch Dune Part 3 IMAX 70mm* → *Run workflow* → set **test** to `true`.
That does one sweep, then pushes a synthetic `[TEST]` alert through Telegram, ntfy, and
the issue — no state commit, no chain, no run-failed email. Close/ignore the `[TEST]`
issue comment afterwards.

Locally (no secrets needed, nothing sent):

```
CINEWATCHER_DRY=1 CINEWATCHER_ONCE=1 python3 watcher.py   # one sweep, prints would-be alerts
python3 tools/health.py                                    # health checks, prints heartbeat
python3 tools/seatmap.py map 7420 388032                   # render one auditorium
```

## Failure modes and what covers them

| Failure | Covered by |
|---|---|
| CDN gzip / **double-gzip** responses | magic-byte loop in `decompress()` (single-pass sniff was not enough — killed runs on 2026-08-31) |
| One theatre unreachable | per-theatre carry-forward; no phantom "new session" alerts |
| Seat endpoint fails for one session | per-session carry-forward; no phantom "freed seat" alerts |
| Every sweep fails (key rotated, API moved) | run fails → email; daily health check → Telegram/ntfy/issue |
| `DISPATCH_PAT` expires | fallback dispatch with `github.token`; health check flags dropped cadence |
| Chain dies (runner lost, Actions outage) | successor pre-queued at run start; cron backstop; health check |
| Watcher code broken, fails fast repeatedly | crash-loop guard stops the chain; cron keeps sparse coverage |
| Telegram silently broken | daily heartbeat is the delivery test; alerts also check `ok:true` and fall through to ntfy/issue/email |
| GitHub cron skips the health check | Claude watchdog routine checks daily from outside GitHub |
| Alert spam from seat churn | 90-min per-seat cooldown; >30 new sessions collapse to per-date summaries |
| State corrupted / schema change | baseline sweep, loud in logs, never a mass alert |
