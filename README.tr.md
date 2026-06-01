# 🛰️ Proxy Profiler

> **Dil:** [English](README.md) · **Türkçe**

Bir proxy listesini saniyeler içinde **canlılık**, **anonimlik seviyesi** ve isteğe bağlı **erişim testi** açısından profilleyen async Python aracı.

```bash
# proxine listeyi toplar, proxyprof eler — saniyeler içinde elite proxy'ler hazır
proxine http -s | proxyprof http -l 1 -o working.lst
```

------------------------------------------------------------

## Neden proxyprof?

- 🚀 **Hızlı** — async (1.000+ eşzamanlı sonda), aiodns ile non-blocking DNS, thread şişmesi yok
- 🧠 **Akıllı** — SQLite reputation + üstel probation: 100k listede ölü kuyruğu otomatik atlar (%85 daha az iş)
- 🎯 **Doğru sınıflandırma** — Elite / Anonymous / Distorting / Transparent, HTTPS CONNECT testi, MITM tespiti
- 🌍 **Ücretsiz geolokasyon** — kendi CF-korumalı judge'ınla, ek API/DB yok
- 🛡️ **MITM-suspected proxy filtresi** — TLS chain'i kırık honeypot'ları otomatik eler
- 🔄 **proxine boru hattı uyumlu** — stdin/stdout default, ek konfig yok
- 🌐 **Çok dilli** — `tr` ve `en`; sistem locale otomatik algılanır

------------------------------------------------------------

## Kurulum

Python ≥ 3.10 gerekir.

```bash
git clone https://github.com/enseitankado/proxy-profiler.git
cd proxy-profiler
python3 -m venv .venv && source .venv/bin/activate
pip install .
proxyprof --help
```

<details>
<summary>Bağımlılık yoksa otomatik kurulum</summary>

`aiohttp` / `aiohttp-socks` / `aiodns` yüklü değilken TTY'de çalıştırırsan tek soru sorulur: *"Auto-setup will create ./.venv and install … Proceed? [Y/n]"*. `Y` denirse yerel `.venv` oluşturulur, bağımlılıklar yüklenir, proxyprof venv'in Python'uyla yeniden başlatılır. Sudo veya sistem paket değişikliği yok, PEP 668 kısıtlamasına takılmaz.

Boru hatlarında (TTY değilken) prompt çıkmaz; statik hata mesajıyla çıkar.
</details>

------------------------------------------------------------

## 60 saniye Hızlı Başlangıç

```bash
# 1) Bir liste topla (proxine, kendi kaynağın, ya da public liste)
proxine http -s > raw.lst

# 2) Sadece elite + canlı olanları çek
proxyprof http -f raw.lst -l 1 -o working.lst

# 3) İstersen daha sert: gatekeeper testinden de geçenler kalsın
proxyprof http -f raw.lst --access-test -o production.lst
```

Tarama akarken stderr'de canlı tablo + progress + sonda iki Unicode özet kutusu görünür:

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

## Dokümantasyon

| Konu | Detay |
|---|---|
| 📖 [Kullanım — bayraklar ve örnekler](docs/tr/usage.md) | Tüm CLI bayrakları, default değerleri, kullanım örnekleri |
| 📊 [Çıktı — stdout, stderr, kutular](docs/tr/output.md) | Tablo sütunları, STATUS kodları, CONFIG/RESULT kutuları |
| 🎭 [Anonimlik seviyeleri](docs/tr/anonymity.md) | L1/L2/L2d/L3 tespit kuralları, distorting limitleri |
| 🔬 [Süzgeçler ve metrikler](docs/tr/filters.md) | Tunnel test, access test, hız percentile'ları, geolokasyon |
| 💾 [Reputation & probation](docs/tr/reputation.md) | HOT/WARM/NEW/COLD bucket'lar, üstel probation, tipik tasarruf |
| ☁️ [Cloudflare-aware judge](docs/tr/cloudflare-judge.md) | `proxyjudge.php` kurulumu, ülke bilgisi, ziyaret log'u, kimlik whitelist |
| 🌐 [Yerelleştirme](docs/tr/i18n.md) | Dil seçimi, yeni dil ekleme |
| 🏗️ [Mimari](docs/tr/architecture.md) | Dosya yapısı, modüller, eşzamanlılık modeli |

------------------------------------------------------------

## İlgili araçlar

- **[Proxine](https://github.com/enseitankado/proxine)** — 60+ açık kaynaktan ham proxy listesi toplayan aggregator. Proxyprof'un asıl girdi kaynağı.
- **[EliteProxySwitcher](https://www.eliteproxyswitcher.com/)** — Windows GUI.
- **[Open Proxy Checker](https://openproxy.space/software/proxy-checker/)** — Windows liste doğrulayıcı.

------------------------------------------------------------

## Lisans

MIT. Türev çalışmalarda orijinal yazar (Özgür Koca) atıfını koruyun. Yazılım "olduğu gibi" sunulur.

## Yazar

**Özgür Koca** — meslek lisesinde [öğretmenlik](https://samsuneml.meb.k12.tr/) yapıyor.
GitHub: [enseitankado](https://github.com/enseitankado) · Blog: [tankado.com](https://www.tankado.com)
