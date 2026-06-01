# Anonimlik seviyeleri

> ← [Ana README'ye dön](../../README.tr.md)

Judge'tan geri dönen request header'larına bakılır. Üç seviye + bir alt tür:

| Seviye | İsim | Tespit kuralı | Anlam |
|---|---|---|---|
| **1** | Elite | Public IP yok, proxy header'ı yok | Hem IP'nizi hem proxy varlığını gizler. |
| **2** | Anonymous | Public IP yok, ama `via` / `x-forwarded-*` / `proxy-*` var | IP'nizi gizler ama "bir proxy kullanılıyor" der. |
| **2** + *distorting* | Distorting | L2 + `X-Forwarded-For` benzeri header'da public IP'den farklı, routable bir IPv4 var | IP'nizi gizler **ve sahte bir IP enjekte eder**. Fingerprint kaçırma için kullanılır, güven açısından risklidir. |
| **3** | Transparent | Public IP header'larda yansıyor | IP'nizi gizlemez; sadece routing yapar. |

`-l 1` (default) sadece elite proxy'leri tutar. `-l 2` elite + anonymous (distorting dahil), `-l 3` hepsi. Özet kutusunda distorting alt sayımı ayrıca gösterilir.

## Distorting tespitinin sınırı

Header'daki sahte IP **public range'de görünen** bir IPv4 olmalı (RFC1918, loopback, link-local elenir). Bir proxy header'a `0.0.0.0` ya da `192.168.1.1` yazıyorsa distorting değil — sadece kötü konfigüre edilmiş bir anonymous proxy. IPv6 ya da IP olmayan değerler de tespit kapsamı dışında.
