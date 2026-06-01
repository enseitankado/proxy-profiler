# Filters and metrics

> ← [Back to README](../../README.md)

Three extra filters beyond anonymity (`--tunnel-test`, `-a`, speed) and two extra metrics (timing percentiles, country distribution) — all to push a raw proxine list to real production quality.

## HTTPS tunnel test (default on)

**Why:** An HTTP proxy may forward plain HTTP requests just fine but not support the `CONNECT` command HTTPS needs. Today almost every site is HTTPS, so a proxy without CONNECT is practically useless.

**What it does:** One extra request per HTTP/HTTPS proxy: `https://www.gstatic.com/generate_204`. A 204 means CONNECT is supported. SOCKS proxies tunnel by design; automatically skipped.

**Cost:** Roughly doubles scan time (2 requests per HTTP/HTTPS proxy). Bump `--concurrency` to compensate.

**Usage:** Default **on**. Disable with `--no-tunnel-test`.

**Result:**
- Only proxies passing the tunnel test land in stdout / `-o`
- `TUNNEL` column in the live table: `✓` / `×` / `—`
- Summary box: `tunnel │ 118 CONNECT-capable (of 197 good)`

## Multi-gatekeeper access test (`--access-test`)

**Why:** A proxy may pass Cloudflare but get a Google CAPTCHA, or vice versa. One gatekeeper isn't enough to find "works everywhere" proxies.

**What it does:** Probes **all** URLs in the list through the proxy. Even one failure marks the proxy "blocked".

**Two modes:**

```bash
# Auto: 3 random sites from the built-in CF list (different per scan)
proxyprof http -f raw.lst --access-test

# Manual: your own gatekeepers (comma-separated, each http(s)://)
proxyprof http -f raw.lst \
  --access-test https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org
```

Built-in CF list: cloudflare.com, discord.com, reddit.com, medium.com, udemy.com, patreon.com, kickstarter.com, upwork.com, zendesk.com, shopify.com — each uses its `/cdn-cgi/trace` endpoint (present on every CF site, returns 200, no UA filtering).

**Result:**
- Only proxies that reach every URL land in stdout
- `ACCESS` column in the live table: `✓` / `×` / `—`
- Summary box: `blocked │ 24 access denied`

## Speed metrics (automatic)

**Why:** A "working" proxy isn't necessarily fast. Mean is often misleading: one or two slow outliers inflate it. **Percentiles** describe the distribution better.

**Percentile primer:**
- **p50** (median): half the data is below this value.
- **p95**: 95% of the data is below this value; only 5% is slower — the worst-case threshold.

**Concrete example.** Scan 10 proxies, times:
```
0.4, 0.6, 0.8, 0.9, 1.1, 1.3, 1.5, 2.0, 3.5, 8.0
```
- Mean = **2.01s** — but 9 of 10 proxies are faster! Outlier (8.0) wins.
- p50 = **1.2s** — the honest middle.
- p95 = **8.0s** — the worst-5% edge.

**Result:**

```
timing │ p50 1.2s · p95 4.1s
```

**How to read:**
- **p50 ≈ p95** → consistent, fast list. Ideal.
- **p95 >> p50** → most fast, but a long slow tail.
- **p95 ≈ timeout** → the list barely fits; bumping `-T` recovers more proxies.
- **High p50** → list is generally slow.

## Geolocation (free, with CF judge)

**Why:** You usually only want proxies in certain countries (TR proxy for TR banking site, US for US streaming). Geolocation normally means downloading MaxMind DB or calling rate-limited APIs — extra complexity.

**What it does:** Cloudflare resolves the incoming IP for every proxy request and adds a `CF-IPCountry` header. The CF-aware judge (`proxyjudge.php`) catches that header and exposes it as `PROXY_COUNTRY`. proxyprof extracts it automatically. **No extra API calls, no dependencies, no files.**

**Usage:**

```bash
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php
```

**Result:**

```
country │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more
```

**Important:** Public azenv judges don't return country info — works only with CF-aware judges.

## Everything together

```bash
# Fresh HTTP proxies from proxine → tested against your CF judge → keep
# only elite + CONNECT-capable + ones that pass 3 random CF gatekeepers.
proxine -p http -s | proxyprof http \
  -j https://yours.tld/proxyjudge.php \
  --access-test \
  -o production-ready.lst
```
