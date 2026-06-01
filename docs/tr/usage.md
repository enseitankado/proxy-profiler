# Kullanım — Bayraklar ve Örnekler

> ← [Ana README'ye dön](../../README.tr.md)

```bash
proxyprof <http|https|socks4|socks5> [seçenekler]
```

## Bayraklar

`--help` çıktısı üç gruba ayrılır: **scan & probes**, **output filters**, **output destination**.

### scan & probes (network davranışı, ek istek maliyeti olabilir)

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--file` | `-f` | stdin | Proxy listesi dosyası. `-` veya bayrak yok = stdin. |
| `--concurrency` | `-c` | `500` | Eşzamanlı sonda sayısı. |
| `--timeout` | `-T` | `5` | Proxy başına timeout (saniye). |
| `--retries` | `-r` | `1` | Başarısız proxy başına tekrar deneme. |
| `--judge` | `-j` | otomatik | Özel azenv.php-uyumlu judge URL'i. CF judge önerilir. Kimlik header'ı yalnız hardcoded güvenilir domain'lere gider — bkz. [Cloudflare-aware judge](cloudflare-judge.md). |
| `--access-test [URLS]` | — | kapalı | Çoklu gatekeeper süzgeci. Değer verilmezse dahili CF-listesinden 3 rastgele site, virgüllü URL listesi verilirse o URL'ler kullanılır. |
| `--tunnel-test` / `--no-tunnel-test` | — | **açık** | HTTPS CONNECT testi. SOCKS için de probe tetikleyici (MITM testi probe'a bağlı). `--no-tunnel-test` HTTPS probe'unu tamamen kapatır. |
| `--mitm-test` / `--no-mitm-test` | — | **açık** | MITM tespiti: TLS cert doğrulama başarısız olur ama CONNECT açıldıysa proxy MITM-suspected sayılır. Aynı HTTPS probe kullanılır — ek istek maliyeti yoktur. |
| `--reputation PATH` | — | `~/.config/proxyprof/state.db` | SQLite reputation DB. HOT/WARM/NEW/COLD bucket + üstel probation. Detay: [Reputation & probation](reputation.md). |
| `--no-reputation` | — | — | Reputation'ı tamamen kapat — stateless. |
| `--dead-threshold N` | — | `3` | COLD bucket'a girmek için gereken üst üste fail sayısı. |
| `--probation-max-skip N` | — | `64` | COLD probation'ın atlama tavanı. |
| `--cold-timeout SECONDS` | — | `2.0` | COLD bucket için per-proxy timeout. |

### output filters (post-scan, ek istek maliyeti yok)

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--level` | `-l` | `1` | Kabul edilen maks. anonimlik seviyesi. `1`=elite, `2`=elite+anon (distorting dahil), `3`=hepsi. |
| `--country CC[,CC...]` | — | — | Yalnızca verilen ISO ülke kodlarındaki proxy'leri tut. CF judge gerekir. |
| `--exclude-distorting` | — | kapalı | Distorting proxy'leri çıkar. SOCKS4'te default açık. |

### output destination

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--output` | `-o` | stdout | Süzülmüş listeyi bu dosyaya yaz; stdout boş kalır. |
| `--silent` | `-s` | — | Yalnız stdout (proxy listesi); tüm stderr susturulur. |

### misc

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--lang` | `-L` | sistem locale | UI dili. Mevcut: `en`, `tr`. `PROXYPROF_LANG` env de geçerli. Detay: [Yerelleştirme](i18n.md). |

## Örnekler

```bash
# Proxine ile zincir: HTTP proxy'leri topla, sadece elite olanları çıkar
proxine -p http -s | proxyprof http -l 1 -o elite.lst

# Dosyadan oku, elite + anonymous tut, dosyaya yaz
proxyprof http -f raw.lst -l 2 -o filtered.lst

# SOCKS5 listesi, 1000 eşzamanlı, 8s timeout
proxyprof socks5 -f socks5.lst -c 1000 -T 8

# Cloudflare gatekeeper süzgeci (3 random CF sitesine erişim şart)
proxyprof http -f raw.lst --access-test

# Kendi belirlediğin gatekeeper'larla
proxyprof http -f raw.lst --access-test https://www.cloudflare.com,https://www.google.com

# Tünel testini kapat (daha hızlı, daha az kaliteli sonuç)
proxyprof http -f raw.lst --no-tunnel-test

# Output filter'ları: yalnızca TR ve US elite, distorting yok
proxyprof http -f raw.lst --country TR,US --exclude-distorting

# MITM filtresini kapat (proxy MITM yapsa da kabul et — debug için)
proxyprof http -f raw.lst --no-mitm-test

# Kendi CF-korumalı judge'ınla: ülke bilgisi + ziyaret log'u
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php

# Tamamen sessiz; başka bir script'e besleme
proxine -p socks5 -s | proxyprof socks5 -s | head -20
```
