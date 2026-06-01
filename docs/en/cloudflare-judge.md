# Cloudflare-aware judge (recommended)

> ← [Back to README](../../README.md)

Public azenv judges go down occasionally and answer slowly. If you have your own Cloudflare-protected domain, drop the repo's `proxyjudge.php` anywhere and point `-j` at it:

```bash
# Local sanity check
curl https://yours.tld/proxyjudge.php

# With proxyprof
proxyprof http -j https://yours.tld/proxyjudge.php -f raw.lst
```

This judge:
- Normalizes `CF-Connecting-IP` as `REMOTE_ADDR` → anonymity detection runs against the actual client (proxy exit) IP.
- Exposes `CF-IPCountry` as the `PROXY_COUNTRY` field → proxyprof picks it up automatically and adds country distribution to the summary box. **No extra GeoIP DB or API call.**
- Strips all `CF-*` headers from the output → doesn't bias anonymity detection.

> **Important:** The domain must be in **"Proxied"** (orange cloud) mode on Cloudflare. In "DNS only" (grey cloud), CF headers don't arrive and the judge falls back to a plain azenv (no country info).

## Visit log (opt-in)

The judge can write every incoming request to a JSONL file. Default **off**. Enable by editing the top of `proxyjudge.php`:

```php
// Empty = no logging. Set a path to enable.
$LOG_FILE = '/var/log/proxyjudge.log';
```

> ⚠️ **Security:** Do not put the log file **inside web root**. Either path it fully outside (e.g. `/var/log/...`), or if you keep it inside web root, block HTTP access with `.htaccess`/nginx rules. Otherwise the list of every proxy hitting your judge becomes publicly downloadable.

Each line contains:

| Field | Source | Description |
|---|---|---|
| `ts` | Server | ISO-8601 UTC timestamp |
| `seen_ip` | CF-Connecting-IP | Proxy's real exit IP (trustworthy — CF sets it) |
| `seen_port` | TCP peer | Ephemeral source port the proxy used for this request |
| `country` | CF-IPCountry | ISO country code |
| `client_type` | `X-Proxyprof-Proxy` | Proxy type (`http`/`https`/`socks4`/`socks5`) — **spoofable** |
| `client_ip` | `X-Proxyprof-Proxy` | Proxy's listening IP |
| `client_port` | `X-Proxyprof-Proxy` | Proxy's listening port |
| `ua` | User-Agent | Truncated at 200 chars |
| `cf_ray` | CF-Ray | CF edge trace ID |

A typical line:

```json
{"ts":"2026-05-24T13:47:21+00:00","seen_ip":"45.83.122.10","seen_port":54231,"country":"TR","client_type":"socks5","client_ip":"45.83.122.10","client_port":1080,"ua":"Mozilla/5.0 ...","cf_ray":"8a1b2c3d4e5f6789-IST"}
```

**Why two IP fields?** `seen_ip` is what Cloudflare sees as the TCP peer — proxies can't spoof it. `client_ip` is what proxyprof wrote in the header — anyone can forge that. If the two **differ**, either a proxy chain (proxyprof → proxy A → proxy B → judge) or fake-header traffic. If they **match**, direct connection, trustworthy record.

## Identity sending — hardcoded domain whitelist

The `X-Proxyprof-Proxy: <type>://<ip>:<port>` header is sent **only** to judges on hardcoded trusted domains. No CLI flag or env var.

The trusted list is in `proxyprof.py` as the `_TRUSTED_JUDGE_DOMAINS` constant:

```python
_TRUSTED_JUDGE_DOMAINS: tuple[str, ...] = (
    "tankado.com",
)
```

If you fork the repo for your own judge, change one line:

```python
_TRUSTED_JUDGE_DOMAINS = ("mydomain.net", "altdomain.com")
```

**Match rules:**
- `tankado.com` → `tankado.com` and `*.tankado.com` (any subdomain) **trusted**
- `eviltankado.com` or near-look-alikes **NOT** trusted
- Port-independent: `tankado.com:8443` matches too

| judge URL | Header sent? |
|---|---|
| `https://tankado.com/projects/proxy_detect/proxyjudge.php` | ✅ |
| `https://judge.tankado.com/anything` | ✅ (subdomain) |
| `https://tankado.com:8443/p.php` | ✅ (port-independent) |
| `https://eviltankado.com/x` | ❌ |
| `http://httpheader.net/azenv.php` | ❌ |

The end-of-scan CONFIG box shows this as `identity = on/off`.

## Reading the log

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
