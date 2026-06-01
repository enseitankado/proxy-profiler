# Reputation & probation

> ← [Back to README](../../README.md)

For cron-style recurring runs (hourly/daily), the input list typically holds **100k+ proxies**, **80–90% of which are familiar** from previous runs, and most of which **fail consistently** (source aggregators republish the same stale list for days). In stateless mode, each run re-tests everyone — most of the time spent on proxies you already know are dead.

The reputation layer fixes this. A SQLite state DB (`--reputation PATH`, default `~/.config/proxyprof/state.db`) holds each proxy's history and splits the list into four **buckets** at the start of every scan:

| Bucket | Definition | Behavior |
|---|---|---|
| **HOT**  | Succeeded in the last 24h | Dispatched first, normal `--timeout`. |
| **WARM** | Succeeded in the past but >24h ago | Dispatched second. |
| **NEW**  | Never seen in DB | Dispatched third. |
| **COLD** | Failed `--dead-threshold` (default 3) times in a row | Last, short `--cold-timeout`, with **exponential probation**. |

## Weighted parallel dispatch

Buckets are scanned in **weighted parallel**, not sequentially. Under a single `asyncio.Semaphore(--concurrency)`, the dispatch order is interleaved with the cycle `HOT*8 → WARM*4 → NEW*2 → COLD*1 → HOT*8 → …`. Result: HOT proxies make up most of the first wave (output flows early), but COLDs also progress in parallel.

## Exponential probation (the real saving)

A proxy in the COLD bucket isn't tested **every run** — it's tested on an exponentially thinning schedule:

| consecutive_failures | Test frequency |
|---|---|
| 3 (=dead_threshold) | Every 2 runs |
| 4                   | Every 4 runs |
| 5                   | Every 8 runs |
| 6                   | Every 16 runs |
| 7                   | Every 32 runs |
| 8+                  | **Every 64 runs** (cap, `--probation-max-skip`) |

The cap prevents a dead proxy from being forgotten forever — if it comes back one day, it'll be caught.

Only **judge-unreachable** (`status=fail`) results bump this counter; `status=filter` (judge passed but tunnel/access filter rejected) shows the proxy is alive — it doesn't count as a fail.

## Typical savings

100k proxy input, daily cron:

| Run | Stateless | Reputation + probation | Note |
|---|---|---|---|
| #1 (empty DB) | 100k tested | 100k tested | All NEW. |
| #5 | 100k tested | ~30k tested | 70k dead tail at various probation levels. |
| #30 | 100k tested | ~12k tested | Old deads tested every 32–64 runs. |
| Steady state | 100k tested | **~10–15k tested** | HOT/WARM + new NEW + sparse COLD samples. |

## CONFIG box distribution

```
│ reputation   │ on  (run #42, db=/home/u/.config/proxyprof/state.db) │
│ buckets      │ HOT 5,234 · WARM 3,128 · NEW 2,400 · COLD 89,238     │
│ probation    │ 73,455 COLD proxies skipped                          │
│ cold-timeout │ 2.0s                                                 │
```

## Disabling completely

```bash
proxyprof http -f raw.lst --no-reputation
```

## Maintenance

The state schema is simple (one `proxy` table + `meta`). The file is fully self-contained — copy/move freely. SQLite WAL mode is on, so parallel proxyprof processes can safely write to the same DB.

Manual inspection:

```bash
sqlite3 ~/.config/proxyprof/state.db \
  "SELECT proxy, consecutive_failures, total_attempts,
          datetime(last_success,'unixepoch') AS last_ok
   FROM proxy
   ORDER BY consecutive_failures DESC
   LIMIT 20;"
```

To reset the DB: delete the file. It will be re-created on the next run and everyone starts at NEW.
