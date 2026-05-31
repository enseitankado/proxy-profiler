# 🛰️ Proxy Profiler

> **Language:** **English** · [Türkçe](README.tr.md)

An async Python tool that profiles a proxy list in seconds for **reachability**,
**anonymity level**, and optional **access tests**. Designed to filter the raw
output produced by [Proxine](https://github.com/enseitankado/proxine).

> **Position in the pipeline:** proxine collects, proxyprof filters.
> ```
> proxine http -s | proxyprof http -l 1 -o working.lst
> ```

------------------------------------------------------------

## Features

- **Async (asyncio).** Tests 1,000+ proxies concurrently without the RAM bloat
  typical of threading.
- **HTTP / HTTPS / SOCKS4 / SOCKS5** support via `aiohttp-socks` — a single
  interface.
- **Anonymity classification** (4 sub-categories):
  - **Elite (L1)** — IP and proxy presence both hidden
  - **Anonymous (L2)** — IP hidden, proxy disclosed (`Via` / `X-Forwarded-*`)
  - **Anonymous + Distorting** — IP hidden, **fake** IP injected
  - **Transparent (L3)** — real IP leaks
- **HTTPS tunnel test** (`--tunnel-test`). For HTTP/HTTPS proxies the CONNECT
  capability is measured (a 204 response to gstatic.com/generate_204 is
  required). SOCKS tunnels by design and is automatically passed.
- **Multi-URL access test** (`-a https://a,https://b`). All must succeed — a
  filter against multiple gatekeepers.
- **Speed metrics.** p50/p95 latency of all successful probes is reported in
  the summary.
- **Geolocation** (with a CF judge, free). If you use your own
  Cloudflare-protected judge, the `CF-IPCountry` header is parsed automatically
  and each proxy's exit country is reported — no extra API call.
- **Live judge selection.** 9 HTTP + 3 HTTPS judges are bundled; the first to
  answer is used. `-j` overrides with a custom judge.
- **Proxine-compatible pipeline.** Default input stdin, default output stdout
  (only `IP:PORT` lines); progress/summary goes to stderr.
- **Single-line TTY progress** + Unicode summary boxes at the end — the same
  visual language as proxine.

------------------------------------------------------------

## Installation

Python ≥ 3.10 required.

```bash
git clone https://github.com/enseitankado/proxy-profiler.git
cd proxy-profiler

# venv recommended
python3 -m venv .venv
source .venv/bin/activate

pip install .
proxyprof --help
```

Or install the dependencies directly and run the script:

```bash
pip install aiohttp aiohttp-socks
./proxyprof.py --help
```

> **Missing dependency:** When running on a TTY without `aiohttp` or
> `aiohttp-socks` installed, proxyprof asks **one question**:
> *"Auto-setup will create ./.venv and install aiohttp aiohttp-socks there… Proceed? [Y/n]"*.
> Answering `Y` creates a local `.venv`, bootstraps pip via `get-pip.py` if
> needed, installs the packages, and restarts proxyprof with the venv's
> Python. No sudo, no system-package changes, no PEP 668 friction. On
> subsequent runs `python3 proxyprof.py` silently picks up the local `.venv`;
> the prompt does not reappear.
>
> In pipelines (non-TTY) the prompt is skipped; the script exits with a
> static error message so your shell pipeline does not hang waiting for an
> answer it can't give.

------------------------------------------------------------

## Usage

```bash
proxyprof <http|https|socks4|socks5> [options]
```

### Flags

`--help` output is split into three groups: **scan & probes**, **output
filters**, **output destination**. The table below follows that order.

**scan & probes** (network behavior, probe selection — may incur extra
request cost):

| Long | Short | Default | Description |
|---|---|---|---|
| `--file` | `-f` | stdin | Proxy list file. `-` or omitted = stdin. |
| `--concurrency` | `-c` | `500` | Concurrent probe count. |
| `--timeout` | `-T` | `5` | Per-proxy timeout (seconds). |
| `--retries` | `-r` | `1` | Retries per failed proxy. |
| `--judge` | `-j` | auto | Custom azenv.php-compatible judge URL. CF judge recommended. Identity header is sent only to hardcoded trusted domains — see *Cloudflare-aware judge*. |
| `--access-test [URLS]` | — | off | Multi-gatekeeper filter. With no value, 3 random sites are picked from the built-in CF list; a comma-separated URL list uses those instead. |
| `--tunnel-test` / `--no-tunnel-test` | — | **on** | HTTPS CONNECT test. Also the trigger probe for SOCKS (MITM test piggy-backs on it). `--no-tunnel-test` disables the HTTPS probe entirely (implicitly disabling MITM as well). |
| `--mitm-test` / `--no-mitm-test` | — | **on** | MITM detection: if TLS cert validation fails but CONNECT succeeded, the proxy is flagged as MITM-suspected. Same HTTPS probe — no extra request cost. `--no-mitm-test` turns the MITM filter off (the probe still runs if active and the metric is reported). |
| `--reputation PATH` | — | `~/.config/proxyprof/state.db` | SQLite reputation DB. Drives HOT/WARM/NEW/COLD bucket classification + exponential probation. See [Reputation & probation](#reputation--probation-recurring-scans). |
| `--no-reputation` | — | — | Disable reputation entirely — stateless. |
| `--dead-threshold N` | — | `3` | Consecutive failures required to enter the COLD bucket. |
| `--probation-max-skip N` | — | `64` | Upper bound on COLD probation skips. At worst a proxy is tested every N runs. |
| `--cold-timeout SECONDS` | — | `2.0` | Per-proxy timeout used for the COLD bucket. |

**output filters** (post-scan, no extra request cost — they only gate the
output list):

| Long | Short | Default | Description |
|---|---|---|---|
| `--level` | `-l` | `1` | Maximum accepted anonymity level. `1`=elite, `2`=elite+anon (distorting included), `3`=all. |
| `--country CC[,CC...]` | — | — | Keep only proxies in the given ISO country codes. Requires a CF judge (to surface `PROXY_COUNTRY`). E.g. `--country TR,US`. |
| `--exclude-distorting` | — | off | Drop proxies that inject a fake IP. Off by default: distorting proxies pass `--level 2` normally. |

**output destination** (where the output goes):

| Long | Short | Default | Description |
|---|---|---|---|
| `--output` | `-o` | stdout | Write the filtered list to this file; stdout stays empty. |
| `--silent` | `-s` | — | Stdout-only (proxy list); all stderr is muted. |
| `--verbose` | `-v` | — | (Deprecated, no-op) the live table is the default now. |

**misc** (localization and helpers):

| Long | Short | Default | Description |
|---|---|---|---|
| `--lang` | `-L` | system locale | UI language. Available: `en`, `tr`. `PROXYPROF_LANG` env var also accepted. See [Localization](#localization). |

### Examples

```bash
# Chain with proxine: collect HTTP proxies, keep only the elite ones
proxine http -s | proxyprof http -l 1 -o elite.lst

# Read from file, keep elite + anonymous, write to file
proxyprof http -f raw.lst -l 2 -o filtered.lst

# SOCKS5 list, 1000 concurrent, 8s timeout, per-line log
proxyprof socks5 -f socks5.lst -c 1000 -T 8 -v

# Cloudflare gatekeeper filter (must reach 3 random CF sites)
proxyprof http -f raw.lst --access-test

# With your own gatekeepers
proxyprof http -f raw.lst --access-test https://www.cloudflare.com,https://www.google.com

# Skip the tunnel test (faster, lower-quality results)
proxyprof http -f raw.lst --no-tunnel-test

# Output filters: keep only TR and US elite, no distorting
proxyprof http -f raw.lst --country TR,US --exclude-distorting

# Disable the MITM filter (accept MITM proxies too — for debugging)
proxyprof http -f raw.lst --no-mitm-test

# With your own Cloudflare-protected judge: country info + visit log
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php \
  --judge-domain yours.tld          # X-Proxyprof-Proxy header goes here

# Fully silent; feed another script
proxine socks5 -s | proxyprof socks5 -s | head -20
```

------------------------------------------------------------

## Output

### Stdout

Sorted, deduplicated, filtered `IP:PORT` lines:

```
1.2.3.4:8080
1.2.3.4:8443
5.6.7.8:3128
```

If `-o FILE` is set, stdout stays empty; the lines go to the file.

### Stderr — live table + progress + CONFIG/RESULT boxes

While a scan runs, stderr looks like this:

```
┌───────┬────────┬───────┬───────────────────────┬────────┬─────────────────┬─────────┬────────┬────────┬──────┬────────┐
│     # │ STATUS │ BUCKET│ PROXY                 │ LEVEL  │ OUTBOUND        │ COUNTRY │   TIME │ TUNNEL │ MITM │ ACCESS │
├───────┼────────┼───────┼───────────────────────┼────────┼─────────────────┼─────────┼────────┼────────┼──────┼────────┤
│  3/30 │ ok     │ HOT   │ 8.211.194.85:4444     │ L1     │ 8.211.194.85    │ US      │   1.2s │ ✓      │ ✓    │ ✓      │
│  7/30 │ ok     │ NEW   │ 5.6.7.8:1080          │ L2d    │ 5.6.7.8         │ DE      │   0.8s │ ✓      │ ✓    │ ✓      │
│ 12/30 │ filter │ WARM  │ 9.10.11.12:3128       │ L1     │ 9.10.11.12      │ —       │   2.1s │ ✓      │ ×    │ ✓      │
└───────┴────────┴───────┴───────────────────────┴────────┴─────────────────┴─────────┴────────┴────────┴──────┴────────┘
[████████████████░░░░]  80%  24/30  ok:3     fail:21    skip:0     elapsed:  9.4s
 LEVEL: L1=Elite · L2=Anonymous · L2d=Anonymous+Distorting · L3=Transparent   ·   — = data unavailable (test not run or judge didn't return)
```

`MITM=×` on the third row: the proxy could open a CONNECT tunnel (`TUNNEL=✓`)
but TLS certificate validation failed — the proxy breaks the TLS chain with
its own certificate, a **MITM signature**. STATUS drops to `filter` and the
proxy is not written to stdout.

A legend appears under the progress line on every update: it explains the
level codes (L1/L2/L2d/L3) and what `—` means in a cell — if a column shows
`—`, that test did not run or the judge did not return that field (e.g. a
public azenv does not include COUNTRY; with `--no-tunnel-test` the MITM
column is `—`; etc.).

**Table behavior:**

- Only **successful** proxies become rows. Failures don't appear but the
  `fail:N` counter on the progress line increments — separating noise from
  the table.
- Rows arrive in completion order (fastest first; `#` shows that order, the
  total being your target → `3/30`, `7/30` with gaps marking where failures
  fell).
- The bottom line is the live progress: completed / total, ok / fail counts,
  elapsed. Updated in place via ANSI cursor manipulation; in a pipe (stderr
  not a TTY) only the final progress is written.

**Columns:**

| Column | Meaning |
|---|---|
| `#` | Completion order / total |
| `STATUS` | `ok` (everything passed) · `filter` (judge passed, tunnel/access/mitm dropped) |
| `BUCKET` | Reputation group: `HOT` / `WARM` / `NEW` / `COLD` (`—` when reputation off). In the Turkish UI: `SICAK` / `ILIK` / `YENİ` / `SOĞUK`. |
| `PROXY` | IP:PORT |
| `LEVEL` | `L1` elite · `L2` anonymous · `L2d` anonymous + distorting (fake IP injected) · `L3` transparent |
| `OUTBOUND` | The exit IP the judge sees (the proxy's outside address) |
| `COUNTRY` | ISO country code (with a CF judge) |
| `TIME` | Total probe time |
| `TUNNEL` | Tunnel test: `✓` CONNECT opened · `×` closed · `—` not tested |
| `MITM` | TLS chain status: `✓` clean · `×` MITM detected · `—` not tested |
| `ACCESS` | Access test: `✓` reached all gatekeepers · `×` at least one failed · `—` not tested |

**What does `—` mean in a column?** The relevant test did not run or the
judge did not return that field:
- `BUCKET` → reputation off via `--no-reputation`
- `COUNTRY` → judge did not return a country (public azenv judges don't; CF judge does)
- `OUTBOUND` → judge did not return `REMOTE_ADDR`
- `TUNNEL` → disabled via `--no-tunnel-test`
- `MITM` → tunnel test off (MITM rides the same probe)
- `ACCESS` → disabled via `--no-access-test`

#### What does `mitm` mean in the `ACCESS` column?

When you see `mitm` in an `ACCESS` cell, it means the TLS handshake during
the access probe **rejected** the certificate chain because it found a
**self-signed cert** in it. In other words, the proxy returned its **own
cert** instead of the real `cloudflare.com` cert — a textbook MITM
signature. Both `_access_check_one` (`proxyprof.py:941`) and `_https_probe`
trigger `mitm_suspected` from the same signal; two independent paths arrive
at the same finding → no "coincidence" possible. Running with
`--debug log.jsonl` reveals the raw exception:
`[SSL: CERTIFICATE_VERIFY_FAILED] ... self-signed certificate in certificate chain`.

Observed patterns:
- Almost all of these IPs sit on port `:4145` (the conventional SOCKS4
  daemon port), most of them in the `67.x / 68.71.x / 70.166.x / 72.x /
  74.x / 98.x` blocks — public-listed proxies **deliberately** set up to
  decrypt HTTPS for credential skimming or ad injection ("honeypot"
  proxies).
- `to` / `err` / `?` are **not** MITM — they mean timeout, ordinary network
  error (ConnectionReset / ServerDisconnected / payload), and unexpected
  exception, respectively. `mitm` is set **only** for cert verification
  failures.

A 20–30% MITM rate is normal for SOCKS4 lists; the rate is **sector-driven**
(the `:4145` honeypot cluster dominates the access list), not a false
positive in proxyprof. `--no-mitm-test` will keep these proxies in the
output but the flag will still appear in the table.

**After the scan ends** two boxes are written below the progress.

**CONFIG** — a key=value reference of the scan parameters (all flags visible
so the same scan can be reproduced):

```
┌ CONFIG ─────┬──────────────────────────────────────────────┐
│ protocol    │ http                                         │
│ input       │ raw.lst                                      │
│ output      │ working.lst                                  │
│ judge       │ https://tankado.com/proxyjudge.php           │
│ publicIP    │ 78.180.x.x                                   │
│ level       │ ≤1                                           │
│ concurrency │ 500                                          │
│ timeout     │ 5.0s                                         │
│ retries     │ 1                                            │
│ tunnel-test │ on                                           │
│ access-test │ 3 URLs  (https://www.cloudflare.com/...)     │
└─────────────┴──────────────────────────────────────────────┘
```

**RESULT** — the scan summary:

```
┌ RESULT ──┬───────────────────────────────────────────────────────────┐
│ scanned  │ 1,000 proxies                                             │
│ good     │ 142 elite, 38 anon (10 distorting), 17 transparent  →  working.lst│
│ bad      │ 803 (timeout/error)                                       │
│ blocked  │ 24 access denied                                          │
│ tunnel   │ 118 CONNECT-capable (of 197 good)                         │
│ timing   │ p50 1.2s · p95 4.1s                                       │
│ country  │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more                   │
│ elapsed  │ 12.4s                                                     │
└──────────┴───────────────────────────────────────────────────────────┘
```

Some RESULT rows are conditional:
- `blocked` → only if `--access-test` was given
- `tunnel` → only if `--tunnel-test` is active (default on)
- `country` → only if the judge returns `PROXY_COUNTRY` / `CF-IPCountry` (i.e. a CF judge)
- `(N distorting)` → only when at least 1 distorting proxy was caught

### Output mode matrix

| Command | stdout | stderr |
|---|---|---|
| `proxyprof http` | filtered list | live table → summary box |
| `proxyprof http -o f.lst` | (empty) | live table → summary box |
| `proxyprof http -s` | filtered list | (empty) |
| `proxyprof http -o f.lst -s` | (empty) | (empty) |

------------------------------------------------------------

## Anonymity levels

Determined by inspecting the request headers reflected back by the judge.
Three levels + one sub-type:

| Level | Name | Detection rule | Meaning |
|---|---|---|---|
| **1** | Elite | No public IP, no proxy header | Hides both your IP and the fact that a proxy is in use. |
| **2** | Anonymous | No public IP, but `via` / `x-forwarded-*` / `proxy-*` is present | Hides your IP but says "a proxy is in use." |
| **2** + *distorting* | Distorting | L2 + an `X-Forwarded-For`-style header carries a routable IPv4 that is not your public IP | Hides your IP **and injects a fake one**. Used for fingerprint evasion; risky for trust. |
| **3** | Transparent | Public IP appears in some header | Doesn't hide your IP; just routes. |

`-l 1` (default) keeps only elite proxies. `-l 2` keeps elite + anonymous
(distorting included), `-l 3` keeps all. The distorting sub-count appears
separately in the summary box.

### Limits of distorting detection

The fake IP in a header must be **a routable IPv4** (RFC1918, loopback,
link-local are filtered out). A proxy that writes `0.0.0.0` or `192.168.1.1`
to a header isn't distorting — it's just a badly configured anonymous proxy.
IPv6 and non-IP values are out of scope for distorting detection.

------------------------------------------------------------

## Filters and metrics

Three extra filters beyond anonymity (`--tunnel-test`, `-a`, speed) and two
extra metrics (timing percentiles, country distribution) — together they
take the raw list past the proxine pipeline to production quality.

### HTTPS tunnel test (default on)

**Why:** An HTTP proxy may forward plain HTTP requests but lack the
`CONNECT` capability HTTPS needs. Today nearly every site is HTTPS, so a
CONNECT-less HTTP proxy is useless for most practical targets.

**What it does:** Sends one extra request per HTTP/HTTPS proxy:
`https://www.gstatic.com/generate_204`. A 204 means CONNECT is supported.
SOCKS proxies tunnel by nature; they pass automatically without an extra
request.

**Cost:** Scan time roughly doubles (2 requests per HTTP/HTTPS proxy). You
can compensate by raising concurrency.

**Use:** **On** by default. To disable:

```bash
proxyprof http -f raw.lst --no-tunnel-test       # skip the CONNECT test
```

**Result:**
- Only tunnel-passing proxies make it to stdout (or to the `-o` file)
- In the live table the `TUN` column: `✓` / `×` / `—`
- In the summary box: `tunnel │ 118 CONNECT-capable (of 197 good)`

### Multi-gatekeeper access test (`--access-test`)

**Why:** A proxy might pass Cloudflare but get a Google CAPTCHA, or vice
versa. One gatekeeper is not enough to isolate proxies that "work
everywhere."

**What it does:** Sends a request through the proxy to **each** URL you
specify. A single URL failure marks the proxy as "blocked."

**Use — two modes:**

```bash
# Auto: 3 random sites from the built-in CF list (different per scan)
proxyprof http -f raw.lst --access-test

# Manual: your own gatekeepers (comma-separated, each http(s)://)
proxyprof http -f raw.lst \
  --access-test https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org
```

Built-in CF list: cloudflare.com, discord.com, reddit.com, medium.com,
udemy.com, patreon.com, kickstarter.com, upwork.com, zendesk.com,
shopify.com — each uses the `/cdn-cgi/trace` endpoint (available on every
CF site, returns 200, doesn't filter by UA).

**Result:**
- Only proxies that reach all URLs reach stdout
- In the live table the `ACC` column: `✓` / `×` / `—`
- In the summary box: `blocked │ 24 access denied`

### Speed metrics (automatic)

**Why:** "Works" doesn't mean "fast." For "is this list good?" `mean` is
often misleading: one or two very slow proxies inflate the mean, while one
very fast proxy hides a bad distribution. Instead, **percentiles** are used.

**What is a percentile?**
Sort the data smallest-to-largest; the "Xth" value is the value below which
the first **X%** of the data fall.
- **p50** (median): half the data is below this, half above.
- **p95**: 95% of the data is below this, only 5% is slower — the
  worst-case threshold.

**A concrete example.** Say you tested 10 proxies and got these times
(seconds):
```
0.4, 0.6, 0.8, 0.9, 1.1, 1.3, 1.5, 2.0, 3.5, 8.0
```
- Mean = (0.4+0.6+…+8.0)/10 = **2.01s** — but 9 of 10 proxies are faster!
  Lost to the outlier (8.0).
- Median (p50) = average of the 5th and 6th values = **1.2s** — "half are
  this fast" — the real picture.
- p95 ≈ the upper tail, **8.0s** — "how slow is the worst 5%?"

**What it does:** At the end of the scan the durations of all **successful**
probes (those that reached the judge) are reduced to p50 and p95. Failures
are excluded — averaging timeouts is meaningless.

**Use:** Automatic. No flag.

**Result:**

```
timing   │ p50 1.2s · p95 4.1s
```

**How to read it:**
- **p50 ≈ p95** (e.g. p50 1.0s · p95 1.4s) → consistent, fast list. Ideal.
- **p95 >> p50** (e.g. p50 0.8s · p95 6.2s) → most are fast but a long "slow
  tail" exists. Those tail proxies are likely to time out in production.
- **p95 ≈ timeout** (e.g. `-T 5` with p95 4.7s) → the list barely fits;
  raising `-T` will likely produce more "good" results.
- **High p50** (e.g. p50 4.5s) → the list is generally slow; try another
  source.

### Geolocation (with CF judge, free)

**Why:** Often only proxies in specific countries are useful (e.g. a TR
proxy for a TR banking site, a US proxy for US streaming). Geolocation
usually means downloading a MaxMind DB or calling rate-limited APIs — extra
complexity.

**What it does:** Cloudflare resolves the IP of every incoming request and
adds the `CF-IPCountry` header. A CF-aware judge (`proxyjudge.php`) catches
that header and re-emits it as `PROXY_COUNTRY`. Proxyprof picks it up
automatically. No extra API call, dependency, or file.

**Use:** Just point to a judge hosted on your CF-protected domain:

```bash
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php
```

**Result:**
- Top 5 countries + the rest in the summary box:
  ```
  country  │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more
  ```
- The verbose log puts the country on each row: `[ ok ]  L1  1.2.3.4:8080  1.2s  out=1.2.3.4  TR tun`

**Important:** Public azenv judges do not return country info — only a
CF-aware judge does.

### Everything together

```bash
# Fresh HTTP proxies from proxine → tested against your CF judge → keep
# only elite + CONNECT-capable + ones that pass 3 random CF gatekeepers.
# (Once PROXYPROF_JUDGE_DOMAIN is exported, --judge-domain is optional.)
proxine http -s | proxyprof http \
  -j https://yours.tld/proxyjudge.php \
  --judge-domain yours.tld \
  --access-test \
  -o production-ready.lst
```

You get a list that has been filtered by every angle before production:
elite anonymity (default `-l 1`) + HTTPS tunnel (default `--tunnel-test`)
+ access to 3 different CF gatekeepers + country distribution report.

------------------------------------------------------------

## Reputation & probation (recurring scans)

Recurring (hourly/daily) cron runs typically work on an input list of
**100k+ proxies**, **80–90% of which are familiar from previous runs**, and
**most of which keep failing** (the source aggregators serve the same stale
list for days). In stateless mode every run tests everyone from scratch —
most of the time is spent on proxies you already know are dead.

The reputation layer fixes this waste. A SQLite-based state DB
(`--reputation PATH`, default `~/.config/proxyprof/state.db`) tracks each
proxy's history and at the start of every scan splits the list into four
**buckets**:

| Bucket | Definition | Behavior |
|---|---|---|
| **HOT**  | Succeeded in the last 24 hours | Dispatched first, normal `--timeout`. |
| **WARM** | Succeeded in the past but >24h ago | Dispatched second. |
| **NEW**  | Never seen in the DB | Dispatched third. |
| **COLD** | `--dead-threshold` (default 3) consecutive failures | Last, with a short `--cold-timeout` and **exponential probation**. |

### Weighted parallel dispatch

Buckets are dispatched **weighted-parallel**, not sequential. Under a
single `asyncio.Semaphore(--concurrency)`, dispatch order interleaves as
`HOT*8 → WARM*4 → NEW*2 → COLD*1 → HOT*8 → …`. Result: HOT proxies dominate
the first wave (output streams early), but COLDs still progress in
parallel — no bucket blocks another.

### Exponential probation (the real saving)

A proxy in the COLD bucket is **not** tested every run; an exponentially
sparse schedule is applied:

| consecutive_failures | Test frequency |
|---|---|
| 3 (=dead_threshold) | every 2 runs |
| 4                   | every 4 runs |
| 5                   | every 8 runs |
| 6                   | every 16 runs |
| 7                   | every 32 runs |
| 8+                  | **every 64 runs** (cap, `--probation-max-skip`) |

The cap prevents a dead proxy from being forgotten forever — if it comes
back one day, it's caught. But for a proxy in a daily cron that has failed
8 times in a row, the average test rate is **once every two months**; the
90% dead tail is effectively removed from the workload.

Only results that **never reached the judge** (`status=fail`) increment the
failure counter; `status=filter` (judge passed but tunnel/access filtered)
proves the proxy is alive — it does not count as a failure.

### Typical savings

Say you have a 100k-proxy input and run a daily cron:

| Run | Stateless | Reputation+probation | Description |
|---|---|---|---|
| #1 (empty DB) | 100k probes | 100k probes | All NEW; same workload. |
| #5 | 100k probes | ~30k probes | 70k of the dead tail is at various probation levels. |
| #30 | 100k probes | ~12k probes | Old corpses tested every 32–64 runs. |
| Steady state | 100k probes | **~10–15k probes** | HOT/WARM + incoming NEW + sparse COLD samples. |

### BUCKET column in the live table

Each row's `BKT` column shows the proxy's bucket for this run: `H` (hot),
`W` (warm), `N` (new), `C` (cold), `—` (stateless mode).

### Distribution in the CONFIG box

The bucket distribution + count of probation skips is reported at the start
of the scan:

```
│ reputation   │ on  (run #42, db=/home/u/.config/proxyprof/state.db) │
│ buckets      │ HOT 5,234 · WARM 3,128 · NEW 2,400 · COLD 89,238     │
│ probation    │ 73,455 COLD proxy skipped                            │
│ cold-timeout │ 2.0s                                                 │
```

### Disabling completely

To restore the old stateless behavior:

```bash
proxyprof http -f raw.lst --no-reputation
```

The state file is neither read nor written; all proxies are probed equally
under a single `--timeout`.

### Maintenance

The state schema is simple (one `proxy` table + `meta`). The file is fully
self-contained — copy/move at will. SQLite WAL mode is enabled, so parallel
proxyprof processes can write to the same DB safely.

Manual inspection:

```bash
sqlite3 ~/.config/proxyprof/state.db \
  "SELECT proxy, consecutive_failures, total_attempts,
          datetime(last_success,'unixepoch') AS last_ok
   FROM proxy
   ORDER BY consecutive_failures DESC
   LIMIT 20;"
```

To reset the DB: delete the file. The next run recreates it and everyone
starts as NEW.

------------------------------------------------------------

## Cloudflare-aware judge (recommended)

Public azenv judges occasionally die or respond slowly. If you have a
Cloudflare-protected domain, drop the repo's `proxyjudge.php` somewhere and
point `-j` at it:

```bash
# Local sanity check
curl https://yours.tld/proxyjudge.php

# With proxyprof
proxyprof http -j https://yours.tld/proxyjudge.php -f raw.lst
```

This judge:
- Normalizes `CF-Connecting-IP` to `REMOTE_ADDR` → anonymity detection runs
  against the **real** client (proxy exit) IP.
- Exposes `CF-IPCountry` as the `PROXY_COUNTRY` field → proxyprof picks it
  up automatically and adds the country breakdown to the summary box. **No
  extra GeoIP DB or API call.**
- Strips all `CF-*` headers from the dump → doesn't bias anonymity
  detection and doesn't reveal that the judge is behind CF.

> **Important:** The domain must be in Cloudflare **"Proxied"** (orange
> cloud) mode. In "DNS only" (gray cloud) mode the CF headers are not
> added and the judge behaves like a vanilla azenv (no country info).

### Visit log (opt-in)

The judge can write each incoming request to a sidecar JSONL log. **Off**
by default. To enable, edit the single line at the top of `proxyjudge.php`:

```php
// Empty = no log. Set a path to enable.
$LOG_FILE = '/var/log/proxyjudge.log';
```

> ⚠️ **Security:** Do **not** put the log file inside the web root. Use a
> path entirely outside (e.g. `/var/log/...`), or if you keep it inside,
> block HTTP access with `.htaccess` / nginx rules. Otherwise the list of
> every proxy that visited your judge is publicly downloadable.

Each line has these fields:

| Field | Source | Description |
|---|---|---|
| `ts` | Server | ISO-8601 UTC timestamp |
| `seen_ip` | CF-Connecting-IP | Proxy's real exit IP (trusted — set by CF) |
| `seen_port` | TCP peer | The **ephemeral** source port the proxy used for that request (NOT the listen port) |
| `country` | CF-IPCountry | ISO country code (`TR`, `US`, …) |
| `client_type` | `X-Proxyprof-Proxy` header | Proxy type (`http` / `https` / `socks4` / `socks5`) — **spoofable** |
| `client_ip` | `X-Proxyprof-Proxy` header | Proxy's **listen** IP — cross-reference against `seen_ip` |
| `client_port` | `X-Proxyprof-Proxy` header | Proxy's **listen** port (e.g. `1080`, `8080`) |
| `ua` | User-Agent | Truncated to 200 chars |
| `cf_ray` | CF-Ray | CF edge trace ID — for debugging |

A typical line:

```json
{"ts":"2026-05-24T13:47:21+00:00","seen_ip":"45.83.122.10","seen_port":54231,"country":"TR","client_type":"socks5","client_ip":"45.83.122.10","client_port":1080,"ua":"Mozilla/5.0 ...","cf_ray":"8a1b2c3d4e5f6789-IST"}
```

**Why two IP fields?** `seen_ip` is the TCP peer address Cloudflare sees —
the proxy cannot forge it. `client_ip` is the value proxyprof writes into a
header — anyone can fabricate it. **Different** values mean either a proxy
chain (proxyprof → proxy A → proxy B → judge) or fake-header traffic.
**Same** values mean a direct connection — a trustworthy record.

#### proxyprof side — identity gated by hardcoded domain whitelist

The `X-Proxyprof-Proxy: <type>://<ip>:<port>` header is sent **only** to
judges on domains hardcoded in the source as trusted. There is no CLI flag
or env var — misuse is physically impossible.

The trusted-domain list is the `_TRUSTED_JUDGE_DOMAINS` constant in
`proxyprof.py`:

```python
_TRUSTED_JUDGE_DOMAINS: tuple[str, ...] = (
    "tankado.com",
)
```

If you fork this repo for your own judge, one line changes:

```python
_TRUSTED_JUDGE_DOMAINS = ("mydomain.net", "altdomain.com")
```

**Match rule:**

- `tankado.com` → matches `tankado.com` and `*.tankado.com` (any subdomain) **trusted**
- `eviltankado.com` or `tankado.com.evil.tld` and other look-alikes are **not**
- Port is irrelevant: `tankado.com:8443` matches

**Behavior matrix (with `tankado.com` as the constant):**

| judge URL | Header sent? |
|---|---|
| `https://tankado.com/proxyjudge.php` | ✅ |
| `https://judge.tankado.com/anything` | ✅ (subdomain) |
| `https://tankado.com:8443/p.php` | ✅ (port irrelevant) |
| `https://eviltankado.com/x` | ❌ (look-alike, different domain) |
| `http://httpheader.net/azenv.php` | ❌ (public judge) |
| `http://proxyjudge.biz/` | ❌ (unrelated) |

No path or script name is checked — someone else deploying `proxyjudge.php`
on their domain cannot harvest your identity; the only determinant is
**domain ownership**. Public azenv judges, the auto-selected judge, or a
custom judge pointed somewhere wrong will never receive the identity
header.

#### Reading the log

```bash
# Show the last 10 entries
tail -n 10 /var/log/proxyjudge.log

# Only the IPs of SOCKS5 visitors
jq -r 'select(.client_type=="socks5") | .seen_ip' < /var/log/proxyjudge.log

# Country distribution
jq -r '.country' < /var/log/proxyjudge.log | sort | uniq -c | sort -rn | head

# Entries where seen_ip and client_ip differ (chain or spoof)
jq 'select(.client_ip != null and .seen_ip != .client_ip)' < /var/log/proxyjudge.log
```

------------------------------------------------------------

## Localization

All user-facing text in proxyprof (help screen, runtime messages, table
headers, CONFIG/RESULT boxes, the progress line) goes through a multi-
language infrastructure. Translations live as single JSON files under
`i18n/`.

### Language selection (priority order)

1. `-L tr` / `--lang tr` CLI flag
2. `PROXYPROF_LANG=tr` environment variable
3. System locale (`LC_ALL`, `LC_MESSAGES`, `LANG`)
4. English (always-available fallback)

If an unsupported language is requested, English is silently used — no
warning.

```bash
# Turkish UI
proxyprof http -L tr -f raw.lst

# System locale is Turkish — automatic, no env var needed
LANG=tr_TR.UTF-8 proxyprof http -f raw.lst

# One-off switch to English
proxyprof http -L en -f raw.lst
```

The active language appears in the **CONFIG** box at the end of the scan
under the `lang` row.

### Available languages

| Code | Language | Translator |
|---|---|---|
| `en` | English | proxyprof core team |
| `tr` | Türkçe | Özgür Koca |

### Adding a new language (for contributors)

Translations are plain JSON. Three steps to a PR:

```bash
# 1) Copy the canonical English file
cp i18n/en.json i18n/de.json

# 2) Translate the VALUES of each "key": "value" pair into the target
#    language. Keys (left side) and {placeholders} MUST be preserved.
$EDITOR i18n/de.json

# 3) Test
./proxyprof.py -L de --help
./proxyprof.py -L de http -f /tmp/sample.lst --no-reputation
```

**Translation tips:**

- A missing key falls back to English at runtime, so you can translate
  gradually and PR incrementally.
- In `{placeholder}` strings keep placeholder names character-for-character
  (e.g. `{pkgs}`, `{n}`, `{elapsed:.1f}s`).
- Write `meta.lang_name` in the language's own script (e.g.
  `"meta.lang_name": "Deutsch"`).
- Put your name in `meta.translator_credit` — it appears in this README's
  "Available languages" table.
- Very short words (e.g. table headers "TUN", "ACC") are width-sensitive —
  column widths auto-adjust but keep them reasonable.

Once your PR is merged the language joins the active set.

------------------------------------------------------------

## Architecture

```
proxy-profiler/
├── proxyprof.py    # Async scanner + CLI (single file, ~1000 lines)
├── judges.py       # Judge list + response parser + level + distorting + country
├── reputation.py   # SQLite-backed proxy reputation store + bucket classification + probation
├── i18n.py         # Multi-language message module (stdlib-only)
├── i18n/
│   ├── en.json     # Canonical English (reference)
│   └── tr.json     # Turkish
├── proxyjudge.php  # Optional CF-aware judge — host on your own domain
├── pyproject.toml  # aiohttp + aiohttp-socks dependencies
└── README.md
```

- **`judges.py`** holds the judge URL list, parses both possible judge
  response formats (`<pre>KEY=VALUE</pre>` and plain JSON), and derives the
  anonymity level from the public IP + header dictionary.
- **`reputation.py`** provides the single-file SQLite schema, the bucket
  classifier (HOT/WARM/NEW/COLD), the exponential probation decision, and
  the weighted interleave dispatch helpers. WAL mode is on → parallel
  proxyprof processes are safe.
- **`proxyprof.py`** opens one `aiohttp_socks.ProxyConnector` per proxy,
  caps concurrency with `asyncio.Semaphore(N)`; when reputation is on,
  tasks are interleaved by bucket priority; results are gathered with a
  single `gather` and batch-upserted to the DB at the end.

------------------------------------------------------------

## Related tools

- **[Proxine](https://github.com/enseitankado/proxine)** — Aggregator that
  collects raw proxy lists from 60+ open sources. Proxyprof's primary input
  source.
- **[EliteProxySwitcher](https://www.eliteproxyswitcher.com/)** — Windows GUI.
- **[Open Proxy Checker](https://openproxy.space/software/proxy-checker/)** —
  Windows list validator.

------------------------------------------------------------

## License

MIT. Preserve the original author's attribution (Özgür Koca) in derivative
works. The software is provided "as is"; use is entirely at the user's risk.

## Author

**Özgür Koca** — teaches at a [vocational high
school](https://samsuneml.meb.k12.tr/).
GitHub: [enseitankado](https://github.com/enseitankado) · Blog:
[tankado.com](https://www.tankado.com)
