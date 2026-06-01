# 🛰️ Proxy Profiler

> **Language:** **English** · [Türkçe](README.tr.md)

An async Python tool that profiles a proxy list for **liveness**, **anonymity level**, and optional **access checks** — in seconds.

```bash
# proxine collects the list, proxyprof filters — elite proxies ready in seconds
proxine http -s | proxyprof http -l 1 -o working.lst
```

------------------------------------------------------------

## Why proxyprof?

- 🚀 **Fast** — async (1,000+ concurrent probes), non-blocking DNS via aiodns, no thread bloat
- 🧠 **Smart** — SQLite reputation + exponential probation: a 100k list skips the dead tail automatically (~85% less work)
- 🎯 **Accurate classification** — Elite / Anonymous / Distorting / Transparent, HTTPS CONNECT test, MITM detection
- 🌍 **Free geolocation** — with your own CF-protected judge, no extra API or DB
- 🛡️ **MITM-suspected filter** — TLS-tampering honeypots are flagged and dropped
- 🔄 **proxine pipeline compatible** — stdin/stdout by default, no extra config
- 🌐 **Multilingual** — `tr` and `en`; system locale auto-detected

------------------------------------------------------------

## Installation

Python ≥ 3.10 required.

```bash
git clone https://github.com/enseitankado/proxy-profiler.git
cd proxy-profiler
python3 -m venv .venv && source .venv/bin/activate
pip install .
proxyprof --help
```

<details>
<summary>Auto-bootstrap when dependencies are missing</summary>

If `aiohttp` / `aiohttp-socks` / `aiodns` aren't installed and you run on a TTY, you get a single prompt: *"Auto-setup will create ./.venv and install … Proceed? [Y/n]"*. Saying `Y` creates a local `.venv`, installs the dependencies, and restarts proxyprof inside that venv's Python. No sudo, no system package changes, no PEP 668 fight.

In pipelines (no TTY), the prompt is skipped and a static error message is returned so your script doesn't hang on a missing answer.
</details>

------------------------------------------------------------

## 60-second Quickstart

```bash
# 1) Get a list (proxine, your own source, or any public list)
proxine http -s > raw.lst

# 2) Keep only elite + live ones
proxyprof http -f raw.lst -l 1 -o working.lst

# 3) Stricter: also require gatekeeper access
proxyprof http -f raw.lst --access-test -o production.lst
```

While scanning, stderr renders a live table + progress + two Unicode summary boxes at the end:

```
┌───────┬────────┬───────┬───────────────────────┬───────┬────────┬─────────────────┬─────────┬────────┬────────┬──────┬────────┐
│     # │ STATUS │ BUCKET│ PROXY                 │ PROTO │ LEVEL  │ OUTBOUND        │ COUNTRY │   TIME │ TUNNEL │ MITM │ ACCESS │
├───────┼────────┼───────┼───────────────────────┼───────┼────────┼─────────────────┼─────────┼────────┼────────┼──────┼────────┤
│  3/30 │ ok     │ HOT   │ 8.211.194.85:4444     │ http  │ L1     │ 8.211.194.85    │ US      │   1.2s │ ✓      │ ✓    │ ✓      │
│  7/30 │ ok     │ NEW   │ 5.6.7.8:1080          │ http  │ L2d    │ 5.6.7.8         │ DE      │   0.8s │ ✓      │ ✓    │ ✓      │
│ 12/30 │ filter │ WARM  │ 9.10.11.12:3128       │ http  │ L1     │ 9.10.11.12      │ —       │   2.1s │ ✓      │ ×    │ ✓      │
└───────┴────────┴───────┴───────────────────────┴───────┴────────┴─────────────────┴─────────┴────────┴────────┴──────┴────────┘
[████████████████░░░░]  80%  24/30  ok:3  fail:21  skip:0  elapsed:  9.4s
```

------------------------------------------------------------

## Documentation

| Topic | Detail |
|---|---|
| 📖 [Usage — flags and examples](docs/en/usage.md) | All CLI flags, defaults, usage examples |
| 📊 [Output — stdout, stderr, boxes](docs/en/output.md) | Table columns, STATUS codes, CONFIG/RESULT boxes |
| 🎭 [Anonymity levels](docs/en/anonymity.md) | L1/L2/L2d/L3 detection rules, distorting limits |
| 🔬 [Filters and metrics](docs/en/filters.md) | Tunnel test, access test, speed percentiles, geolocation |
| 💾 [Reputation & probation](docs/en/reputation.md) | HOT/WARM/NEW/COLD buckets, exponential probation, typical savings |
| ☁️ [Cloudflare-aware judge](docs/en/cloudflare-judge.md) | `proxyjudge.php` setup, country info, visit log, identity whitelist |
| 🌐 [Localization](docs/en/i18n.md) | Language selection, adding a new language |
| 🏗️ [Architecture](docs/en/architecture.md) | File layout, modules, concurrency model |

------------------------------------------------------------

## Related tools

- **[Proxine](https://github.com/enseitankado/proxine)** — Aggregator that pulls raw proxy lists from 60+ open sources. proxyprof's main input source.
- **[EliteProxySwitcher](https://www.eliteproxyswitcher.com/)** — Windows GUI.
- **[Open Proxy Checker](https://openproxy.space/software/proxy-checker/)** — Windows list validator.

------------------------------------------------------------

## License

MIT. Derivative works should keep the original author (Özgür Koca) attribution. Software is provided "as is".

## Author

**Özgür Koca** — [vocational high-school teacher](https://samsuneml.meb.k12.tr/).
GitHub: [enseitankado](https://github.com/enseitankado) · Blog: [tankado.com](https://www.tankado.com)
