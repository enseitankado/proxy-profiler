# Output — stdout, stderr, boxes

> ← [Back to README](../../README.md)

## Stdout

Sorted, deduped, filtered `IP:PORT` lines:

```
1.2.3.4:8080
1.2.3.4:8443
5.6.7.8:3128
```

With `-o FILE`, stdout stays empty and lines go to the file.

## Stderr — live table + progress + CONFIG/RESULT boxes

While scanning, stderr looks like:

```
┌───────┬────────┬───────┬───────────────────────┬────────┬─────────────────┬─────────┬────────┬────────┬──────┬────────┐
│     # │ STATUS │ BUCKET│ PROXY                 │ LEVEL  │ OUTBOUND        │ COUNTRY │   TIME │ TUNNEL │ MITM │ ACCESS │
├───────┼────────┼───────┼───────────────────────┼────────┼─────────────────┼─────────┼────────┼────────┼──────┼────────┤
│  3/30 │ ok     │ HOT   │ 8.211.194.85:4444     │ L1     │ 8.211.194.85    │ US      │   1.2s │ ✓      │ ✓    │ ✓      │
│  7/30 │ ok     │ NEW   │ 5.6.7.8:1080          │ L2d    │ 5.6.7.8         │ DE      │   0.8s │ ✓      │ ✓    │ ✓      │
│ 12/30 │ filter │ WARM  │ 9.10.11.12:3128       │ L1     │ 9.10.11.12      │ —       │   2.1s │ ✓      │ ×    │ ✓      │
└───────┴────────┴───────┴───────────────────────┴────────┴─────────────────┴─────────┴────────┴────────┴──────┴────────┘
[████████████████░░░░]  80%  24/30  ok:3     fail:21    skip:0     elapsed:  9.4s
 LEVEL: L1=Elite · L2=Anonymous · L2d=Anonymous+Distorting · L3=Transparent   ·   — = data unavailable
```

`MITM=×` on the third row: proxy opened the CONNECT tunnel (`TUNNEL=✓`) but TLS cert validation failed — the proxy is breaking the TLS chain with its own certificate, a **MITM signature**. STATUS drops to `filter` and the row is not written to stdout.

## Table behavior

- Only **successful** proxies become rows. Failures don't show but progress `fail:N` counter increments.
- Rows arrive in completion order (fastest first).
- The bottom line is the live progress: completed/total, ok/fail counts, elapsed. In pipe mode (stderr not TTY), only the final progress is written.

## Columns

| Column | Meaning |
|---|---|
| `#` | Completion order |
| `STATUS` | `ok` (all checks passed) · `filter` (judge passed but tunnel/access/mitm failed) |
| `BUCKET` | Reputation bucket: `HOT` / `WARM` / `NEW` / `COLD` |
| `PROXY` | IP:PORT |
| `PROTO` | Scan protocol (`http`/`https`/`socks4`/`socks5`) |
| `LEVEL` | `L1` elite · `L2` anonymous · `L2d` distorting · `L3` transparent |
| `OUTBOUND` | Exit IP as seen by the judge |
| `COUNTRY` | ISO country code (requires CF judge) |
| `TIME` | Single judge round-trip time |
| `TUNNEL` | `✓` CONNECT opened · `×` failed · `—` no test |
| `MITM` | `✓` clean · `×` MITM detected · `—` no test |
| `ACCESS` | `✓` reached all gatekeepers · `×` at least one failed · `—` no test |

Columns for disabled tests (e.g. `-p http` disables tunnel/mitm/access by default) are **hidden entirely** — no column means no test.

## What does `mitm` mean in the `ACCESS` column?

Seeing `mitm` in the `ACCESS` cell means the TLS handshake during the access probe found a **self-signed cert** in the chain and refused to validate. The proxy is returning its own cert instead of the real `cloudflare.com` cert — textbook MITM signature.

Patterns observed:
- These IPs are mostly on port `:4145` (SOCKS4 daemon convention port), in `67.x / 68.71.x / 70.166.x / 72.x / 74.x / 98.x` blocks — public-list honeypots that decrypt HTTPS for credential skim / ad injection.
- `to` / `err` / `?` codes are **not** MITM — they're timeout, network error, unexpected exception.

20-30% mitm rate in SOCKS4 lists is normal. `--no-mitm-test` keeps them in output (filter off) but the column still flags them.

## CONFIG and RESULT boxes

**CONFIG** — scan parameter reference:

```
┌ CONFIG ─────┬──────────────────────────────────────────────┐
│ protocol    │ http                                         │
│ input       │ raw.lst                                      │
│ output      │ working.lst                                  │
│ judge       │ https://yours.tld/proxyjudge.php             │
│ publicIP    │ 78.180.x.x                                   │
│ level       │ ≤1                                           │
│ concurrency │ 500                                          │
│ timeout     │ 5.0s                                         │
│ tunnel-test │ on                                           │
│ access-test │ 3 URLs  (https://www.cloudflare.com/...)     │
│ identity    │ on                                           │
└─────────────┴──────────────────────────────────────────────┘
```

**RESULT** — scan summary:

```
┌ RESULT ──┬───────────────────────────────────────────────────────────┐
│ scanned  │ 1,000 proxies                                             │
│ good     │ 142 elite, 38 anon (10 distorting), 17 transparent        │
│ bad      │ 803 (timeout/error)                                       │
│ blocked  │ 24 access denied                                          │
│ tunnel   │ 118 CONNECT-capable (of 197 good)                         │
│ timing   │ p50 1.2s · p95 4.1s                                       │
│ country  │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more                   │
│ elapsed  │ 12.4s                                                     │
└──────────┴───────────────────────────────────────────────────────────┘
```

Some RESULT lines are conditional:
- `blocked` → if `--access-test` is on
- `tunnel` → if `--tunnel-test` is on
- `country` → if the judge returns `PROXY_COUNTRY` (CF judge)

## Output mode matrix

| Command | stdout | stderr |
|---|---|---|
| `proxyprof http` | filtered list | live table → summary boxes |
| `proxyprof http -o f.lst` | (empty) | live table → summary boxes |
| `proxyprof http -s` | filtered list | (empty) |
| `proxyprof http -o f.lst -s` | (empty) | (empty) |
