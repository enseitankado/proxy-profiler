# Reputation & probation

> ← [Ana README'ye dön](../../README.tr.md)

Cron benzeri düzenli (saatlik/günlük) çalıştırmalarda input listesi tipik olarak **100k+ proxy** içerir, **%80–90'ı önceki çalıştırmalardan tanıdıktır**, ve büyük çoğunluğu **sürekli fail verir** (kaynak agregatörler aynı bayat listeyi günlerce sunar). Stateless modda her run baştan herkesi test eder — zamanın çoğu zaten ölü olduğunu bildiğin proxy'lere harcanır.

Reputation katmanı bu israfı çözer. SQLite tabanlı bir state DB (`--reputation PATH`, default `~/.config/proxyprof/state.db`) her proxy'nin geçmişini tutar ve her tarama başında listeyi dört **bucket**'a ayırır:

| Bucket | Tanım | Davranış |
|---|---|---|
| **HOT**  | Son 24 saatte başarılı | Önce dispatch edilir, normal `--timeout`. |
| **WARM** | Geçmişte başarılı ama 24sa+ önce | İkinci sırada dispatch. |
| **NEW**  | DB'de hiç görülmemiş | Üçüncü sırada. |
| **COLD** | `--dead-threshold` (default 3) kez üst üste fail | En son, kısa `--cold-timeout` ve **üstel probation** ile. |

## Ağırlıklı paralel dispatch

Bucket'lar **sıralı değil ağırlıklı paralel** taranır. Tek bir `asyncio.Semaphore(--concurrency)` altında, dispatch sıralaması `HOT*8 → WARM*4 → NEW*2 → COLD*1 → HOT*8 → …` döngüsüyle interleave edilir. Sonuç: HOT proxy'ler ilk dalganın çoğunluğunu kapar (output erken akar), ama COLD'lar da paralel ilerler.

## Üstel probation (asıl tasarruf)

COLD bucket'taki bir proxy **her run'da değil**, üstel olarak seyrelen bir takvimle test edilir:

| consecutive_failures | Test sıklığı |
|---|---|
| 3 (=dead_threshold) | 2 run'da bir |
| 4                   | 4 run'da bir |
| 5                   | 8 run'da bir |
| 6                   | 16 run'da bir |
| 7                   | 32 run'da bir |
| 8+                  | **64 run'da bir** (tavan, `--probation-max-skip`) |

Tavan, ölü proxy'nin tamamen unutulmasını engeller — bir gün geri gelirse yakalanır.

Sadece **judge'a hiç ulaşamayan** (`status=fail`) sonuçlar bu sayacı artırır; `status=filter` (judge geçti ama tunnel/access süzgecinden düştü) proxy'nin canlı olduğunu gösterir — fail sayılmaz.

## Tipik tasarruf

100k proxy'lik input, günlük cron:

| Run | Stateless | Reputation+probation | Açıklama |
|---|---|---|---|
| #1 (boş DB) | 100k test | 100k test | Hepsi NEW. |
| #5 | 100k test | ~30k test | 70k ölü kuyruk farklı probation kademelerinde. |
| #30 | 100k test | ~12k test | Eski ölüler 32–64 run'da bir test ediliyor. |
| Steady state | 100k test | **~10–15k test** | HOT/WARM + yeni gelen NEW + COLD'un seyrek örnekleri. |

## CONFIG kutusunda dağılım

```
│ reputation   │ on  (run #42, db=/home/u/.config/proxyprof/state.db) │
│ buckets      │ HOT 5,234 · WARM 3,128 · NEW 2,400 · COLD 89,238     │
│ probation    │ 73,455 COLD proxy skipped                            │
│ cold-timeout │ 2.0s                                                 │
```

## Tamamen kapatmak

```bash
proxyprof http -f raw.lst --no-reputation
```

## Bakım

State şeması basit (tek `proxy` tablosu + `meta`). Dosya tamamen self-contained — kopyalayıp taşıyabilirsiniz. SQLite WAL modu açık olduğundan paralel proxyprof süreçleri aynı DB'ye güvenle yazar.

Manuel inceleme:

```bash
sqlite3 ~/.config/proxyprof/state.db \
  "SELECT proxy, consecutive_failures, total_attempts,
          datetime(last_success,'unixepoch') AS last_ok
   FROM proxy
   ORDER BY consecutive_failures DESC
   LIMIT 20;"
```

DB'yi resetlemek için: dosyayı sil. Bir sonraki çalıştırmada otomatik yeniden oluşturulur ve hepsi NEW'den başlar.
