# Çıktı — stdout, stderr, kutular

> ← [Ana README'ye dön](../../README.tr.md)

## Stdout

Sıralı, dedupe edilmiş, süzgeçten geçen `IP:PORT` satırları:

```
1.2.3.4:8080
1.2.3.4:8443
5.6.7.8:3128
```

`-o FILE` verilirse stdout boş kalır; satırlar dosyaya yazılır.

## Stderr — canlı tablo + progress + CONFIG/RESULT kutuları

Tarama akarken stderr şu yapıdadır:

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

`MITM=×` üçüncü satırda: proxy CONNECT tüneli kurabildi (`TUNNEL=✓`) ama TLS sertifika doğrulama başarısız oldu — proxy TLS chain'i kendi sertifikasıyla kırıyor, **MITM imzası**. STATUS `filter`'a düşer ve stdout'a yazılmaz.

## Tablo davranışı

- Yalnız **başarılı** proxy'ler tabloda satır olur. Fail'ler görünmez ama progress satırındaki `fail:N` sayımı artar.
- Satırlar tamamlanma sırasıyla gelir (en hızlı önce).
- En alt satır canlı progress: tamamlanan/toplam, ok/fail sayımları, elapsed. Pipe ortamında (stderr TTY değilken) sadece final progress yazılır.

## Sütunlar

| Sütun | Anlamı |
|---|---|
| `#` | Tamamlanma sırası |
| `STATUS` | `ok` (her şey geçti) · `filter` (judge geçti, tunnel/access/mitm düştü) |
| `BUCKET` | Reputation grubu: `HOT` / `WARM` / `NEW` / `COLD`. Türkçe: `SICAK` / `ILIK` / `YENİ` / `SOĞUK`. |
| `PROXY` | IP:PORT |
| `PROTO` | Tarama protokolü (`http`/`https`/`socks4`/`socks5`) |
| `LEVEL` | `L1` elite · `L2` anonymous · `L2d` distorting · `L3` transparent |
| `OUTBOUND` | Judge'ın gördüğü çıkış IP'si |
| `COUNTRY` | ISO ülke kodu (CF judge gerekir) |
| `TIME` | Tek judge round-trip süresi |
| `TUNNEL` | `✓` CONNECT açıldı · `×` kapalı · `—` test yok |
| `MITM` | `✓` temiz · `×` MITM tespit · `—` test yok |
| `ACCESS` | `✓` tüm gatekeeper'lara ulaştı · `×` en az biri fail · `—` test yok |

Test yapılmayan sütunlar (örn. `-p http` default'ta tunnel/mitm/access kapalı) **tamamen gizlenir** — sütun yoksa test de yok.

## `ACCESS` sütunundaki `mitm` ne demek?

`ACCESS` hücresinde `mitm` görürsen, access probe sırasında TLS handshake'in zincirde **self-signed bir cert** bulduğu için doğrulamayı reddetmesi demek. Proxy gerçek `cloudflare.com` cert'ini değil, **kendi ürettiği** cert'i geri veriyor — ders kitabı MITM imzası.

Pattern olarak gözlenenler:
- Bu IP'lerin çoğu `:4145` portunda (SOCKS4 daemon konvansiyon portu), `67.x / 68.71.x / 70.166.x / 72.x / 74.x / 98.x` bloklarında — public-listelerde dolaşan ve HTTPS'i decrypt edip credential skim / reklam injection için **kasıtlı kurulmuş** honeypot'lar.
- `to` / `err` / `?` kodları MITM **değildir** — sırasıyla timeout, ağ hatası, beklenmedik istisna.

SOCKS4 listelerinde %20-30 oranında mitm çıkması olağan. `--no-mitm-test` ile bu proxy'leri yine de çıktıya almak istersen filtre kapatılır ama tabloda işaret kalır.

## CONFIG ve RESULT kutuları

**CONFIG** — tarama parametrelerinin key=value referansı:

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

**RESULT** — tarama sonuçlarının özeti:

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

RESULT'taki bazı satırlar koşullu:
- `blocked` → `--access-test` verildiyse
- `tunnel` → `--tunnel-test` aktifse
- `country` → judge `PROXY_COUNTRY` döndürüyorsa (CF judge)

## Çıktı modu tablosu

| Komut | stdout | stderr |
|---|---|---|
| `proxyprof http` | süzülmüş liste | canlı tablo → özet kutu |
| `proxyprof http -o f.lst` | (boş) | canlı tablo → özet kutu |
| `proxyprof http -s` | süzülmüş liste | (boş) |
| `proxyprof http -o f.lst -s` | (boş) | (boş) |
