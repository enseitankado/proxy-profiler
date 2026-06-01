# Mimari

> ← [Ana README'ye dön](../../README.tr.md)

```
proxy-profiler/
├── proxyprof.py    # Async scanner + CLI (tek dosya)
├── judges.py       # Judge listesi + response parser + seviye + distorting + country
├── reputation.py   # SQLite-tabanlı proxy reputation store + bucket sınıflandırma + probation
├── i18n.py         # Çok dilli mesaj destek modülü (stdlib-only)
├── i18n/
│   ├── en.json     # Canonical English (referans)
│   └── tr.json     # Türkçe
├── proxyjudge.php  # Opsiyonel CF-aware judge — kendi domain'inde host et
├── pyproject.toml  # aiohttp + aiohttp-socks + aiodns bağımlılıkları
├── docs/           # Detay dokümantasyon
└── README.md
```

- **`judges.py`** judge URL listesi, judge yanıtının iki olası formatını (`<pre>KEY=VALUE</pre>` ve düz JSON) ayrıştırır, public IP + header sözlüğünden seviye çıkarır. CF-pre-filter için aiodns ile non-blocking DNS yapar.
- **`reputation.py`** tek dosyalık SQLite şeması, bucket sınıflandırma (HOT/WARM/NEW/COLD), üstel probation kararı ve ağırlıklı interleave dispatch yardımcılarını sağlar. WAL modu açık → paralel proxyprof süreçleri güvenli.
- **`proxyprof.py`** her proxy için tek bir `aiohttp_socks.ProxyConnector` açar, `asyncio.Semaphore(N)` ile eşzamanlılığı sınırlar; reputation açıkken task'lar bucket önceliğine göre interleave edilir; sonuçlar tek bir `gather` ile toplanır ve sonunda DB'ye batch upsert ile yazılır.
