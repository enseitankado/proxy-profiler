# Architecture

> ← [Back to README](../../README.md)

```
proxy-profiler/
├── proxyprof.py    # Async scanner + CLI (single file)
├── judges.py       # Judge list + response parser + level + distorting + country
├── reputation.py   # SQLite-based reputation store + bucket classification + probation
├── i18n.py         # Multilingual message module (stdlib-only)
├── i18n/
│   ├── en.json     # Canonical English (reference)
│   └── tr.json     # Türkçe
├── proxyjudge.php  # Optional CF-aware judge — host on your own domain
├── pyproject.toml  # aiohttp + aiohttp-socks + aiodns dependencies
├── docs/           # Detailed documentation
└── README.md
```

- **`judges.py`** holds the judge URL list, parses both judge response formats (`<pre>KEY=VALUE</pre>` and plain JSON), and derives anonymity level from `(public IP, header dict)`. CF pre-filter uses aiodns for non-blocking DNS.
- **`reputation.py`** is a single-file SQLite schema, bucket classification (HOT/WARM/NEW/COLD), exponential probation logic, and weighted-interleave dispatch helpers. WAL mode is on, so parallel proxyprof processes are safe.
- **`proxyprof.py`** opens one `aiohttp_socks.ProxyConnector` per proxy, bounds concurrency with `asyncio.Semaphore(N)`; when reputation is on, tasks are interleaved by bucket priority; results are gathered into a single batch and upserted into the DB at the end.
