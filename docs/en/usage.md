# Usage — Flags and Examples

> ← [Back to README](../../README.md)

```bash
proxyprof <http|https|socks4|socks5> [options]
```

## Flags

`--help` output is split into three groups: **scan & probes**, **output filters**, **output destination**.

### scan & probes (network behavior, may incur extra requests)

| Long | Short | Default | Description |
|---|---|---|---|
| `--file` | `-f` | stdin | Proxy list file. `-` or no flag = stdin. |
| `--concurrency` | `-c` | `500` | Max concurrent probes. |
| `--timeout` | `-T` | `5` | Per-proxy timeout (seconds). |
| `--retries` | `-r` | `1` | Retries per failed proxy. |
| `--judge` | `-j` | auto | Custom azenv.php-compatible judge URL. CF judge recommended. Identity header goes only to hardcoded trusted domains — see [Cloudflare-aware judge](cloudflare-judge.md). |
| `--access-test [URLS]` | — | off | Multi-gatekeeper filter. With no value: 3 random sites from the built-in CF list. With comma-separated URLs: those URLs. |
| `--tunnel-test` / `--no-tunnel-test` | — | **on** | HTTPS CONNECT test. Also triggers MITM probe. `--no-tunnel-test` disables HTTPS probe entirely. |
| `--mitm-test` / `--no-mitm-test` | — | **on** | MITM detection: if TLS verification fails but CONNECT opened, the proxy is flagged MITM-suspected. Same HTTPS probe — no extra request. |
| `--reputation PATH` | — | `~/.config/proxyprof/state.db` | SQLite reputation DB. HOT/WARM/NEW/COLD bucketing + exponential probation. Details: [Reputation & probation](reputation.md). |
| `--no-reputation` | — | — | Disable reputation entirely — stateless. |
| `--dead-threshold N` | — | `3` | Consecutive failures required to enter COLD bucket. |
| `--probation-max-skip N` | — | `64` | Cap on COLD probation skip count. |
| `--cold-timeout SECONDS` | — | `2.0` | Per-proxy timeout for COLD bucket. |

### output filters (post-scan, no extra request cost)

| Long | Short | Default | Description |
|---|---|---|---|
| `--level` | `-l` | `1` | Max anonymity level accepted. `1`=elite, `2`=elite+anon (incl. distorting), `3`=all. |
| `--country CC[,CC...]` | — | — | Keep only proxies in the given ISO country codes. Requires CF judge. |
| `--exclude-distorting` | — | off | Exclude distorting proxies. Default on for SOCKS4. |

### output destination

| Long | Short | Default | Description |
|---|---|---|---|
| `--output` | `-o` | stdout | Write filtered list to this file; stdout stays empty. |
| `--silent` | `-s` | — | stdout only (proxy list); all stderr silenced. |

### misc

| Long | Short | Default | Description |
|---|---|---|---|
| `--lang` | `-L` | system locale | UI language. Available: `en`, `tr`. `PROXYPROF_LANG` env also works. Details: [Localization](i18n.md). |

## Examples

```bash
# Chain with proxine: collect HTTP proxies, keep only elite ones
proxine -p http -s | proxyprof http -l 1 -o elite.lst

# Read from file, keep elite + anonymous, write to file
proxyprof http -f raw.lst -l 2 -o filtered.lst

# SOCKS5 list, 1000 concurrent, 8s timeout
proxyprof socks5 -f socks5.lst -c 1000 -T 8

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
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php

# Fully silent; feed another script
proxine -p socks5 -s | proxyprof socks5 -s | head -20
```
