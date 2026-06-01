# Cloudflare-aware judge (önerilir)

> ← [Ana README'ye dön](../../README.tr.md)

Public azenv judge'ları zaman zaman ölür ve yavaş yanıt verir. Kendi Cloudflare-korumalı domain'iniz varsa repodaki `proxyjudge.php` dosyasını herhangi bir yere koyup `-j` ile gösterebilirsiniz:

```bash
# Yerel test
curl https://yours.tld/proxyjudge.php

# proxyprof ile
proxyprof http -j https://yours.tld/proxyjudge.php -f raw.lst
```

Bu judge:
- `CF-Connecting-IP`'yi `REMOTE_ADDR` olarak normalize eder → anonimlik tespiti gerçek istemci (proxy çıkış) IP'sine karşı çalışır.
- `CF-IPCountry`'yi `PROXY_COUNTRY` field'ı olarak expose eder → proxyprof otomatik çekip özet kutusuna country dağılımı ekler. **Ekstra GeoIP DB veya API çağrısı yok.**
- Tüm `CF-*` header'larını çıktıdan strip eder → anonimlik tespitini saptırmaz.

> **Önemli:** Domain Cloudflare'de **"Proxied"** (turuncu bulut) modda olmalı. "DNS only" (gri bulut) modunda CF header'ları gelmez, judge sıradan bir azenv gibi çalışır (country bilgisi yok).

## Ziyaret log'u (opt-in)

Judge gelen her isteği JSONL formatında yan tarafa kaydedebilir. Default olarak **kapalı**. Açmak için `proxyjudge.php`'nin tepesindeki tek satırı düzenle:

```php
// Boş = log yok. Path verirsen log açılır.
$LOG_FILE = '/var/log/proxyjudge.log';
```

> ⚠️ **Güvenlik:** Log dosyasını **web root içine koyma**. Path'i ya tamamen dışarı (örn. `/var/log/...`) yaz, ya da web root içinde tutuyorsan `.htaccess`/nginx kuralıyla HTTP erişimini engelle. Aksi takdirde judge'ını ziyaret eden tüm proxy'lerin listesi internete açık olur.

Her satır şu alanları içerir:

| Alan | Kaynak | Açıklama |
|---|---|---|
| `ts` | Server | ISO-8601 UTC timestamp |
| `seen_ip` | CF-Connecting-IP | Proxy'nin gerçek çıkış IP'si (güvenilir — CF set eder) |
| `seen_port` | TCP peer | Proxy'nin O istek için kullandığı ephemeral kaynak port |
| `country` | CF-IPCountry | ISO ülke kodu |
| `client_type` | `X-Proxyprof-Proxy` | Proxy tipi (`http`/`https`/`socks4`/`socks5`) — **spoof edilebilir** |
| `client_ip` | `X-Proxyprof-Proxy` | Proxy'nin dinleme IP'si |
| `client_port` | `X-Proxyprof-Proxy` | Proxy'nin dinleme portu |
| `ua` | User-Agent | 200 karakterle kısaltılmış |
| `cf_ray` | CF-Ray | CF edge trace ID |

Tipik bir satır:

```json
{"ts":"2026-05-24T13:47:21+00:00","seen_ip":"45.83.122.10","seen_port":54231,"country":"TR","client_type":"socks5","client_ip":"45.83.122.10","client_port":1080,"ua":"Mozilla/5.0 ...","cf_ray":"8a1b2c3d4e5f6789-IST"}
```

**Neden iki ayrı IP alanı?** `seen_ip` Cloudflare'in TCP peer olarak gördüğü adres — proxy bunu sahteleyemez. `client_ip` ise proxyprof'un header'a yazdığı değer — herkes bu header'ı uydurabilir. İkisinin **farklı** olması ya proxy chain'i ya da fake-header trafiği demek.

## Kimlik gönderimi — hardcoded domain whitelist

`X-Proxyprof-Proxy: <type>://<ip>:<port>` header'ı **yalnızca** kodda sabitlenmiş güvenilir domain'lerdeki judge'lara gönderilir. CLI bayrağı veya env var yok.

Güvenilir domain listesi `proxyprof.py` içinde `_TRUSTED_JUDGE_DOMAINS` sabitidir:

```python
_TRUSTED_JUDGE_DOMAINS: tuple[str, ...] = (
    "tankado.com",
)
```

Repoyu kendi judge'unuz için fork ederseniz tek satırlık değişiklik:

```python
_TRUSTED_JUDGE_DOMAINS = ("mydomain.net", "altdomain.com")
```

**Match kuralı:**
- `tankado.com` → `tankado.com` ve `*.tankado.com` (her subdomain) **trusted**
- `eviltankado.com` ya da yakın isimler **DEĞİL**
- Port önemsizdir: `tankado.com:8443` de eşleşir

| judge URL | Header gönderilir mi? |
|---|---|
| `https://tankado.com/projects/proxy_detect/proxyjudge.php` | ✅ |
| `https://judge.tankado.com/anything` | ✅ (subdomain) |
| `https://tankado.com:8443/p.php` | ✅ (port önemsiz) |
| `https://eviltankado.com/x` | ❌ |
| `http://httpheader.net/azenv.php` | ❌ |

Tarama sonu CONFIG kutusunda `identity = on/off` olarak görünür.

## Log'u okuma

```bash
# Son 10 girişi göster
tail -n 10 /var/log/proxyjudge.log

# Sadece SOCKS5 ziyaretçilerinin IP'lerini çıkar
jq -r 'select(.client_type=="socks5") | .seen_ip' < /var/log/proxyjudge.log

# Ülke dağılımı
jq -r '.country' < /var/log/proxyjudge.log | sort | uniq -c | sort -rn | head

# seen_ip ile client_ip'in farklı olduğu (chain veya spoof) girişler
jq 'select(.client_ip != null and .seen_ip != .client_ip)' < /var/log/proxyjudge.log
```
