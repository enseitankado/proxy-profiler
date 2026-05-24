# 🛰️ Proxy Profiler

Bir proxy listesini saniyeler içinde **canlılık**, **anonimlik seviyesi** ve
isteğe bağlı **erişim testi** açısından profilleyen async Python aracı.
[Proxine](https://github.com/enseitankado/proxine) tarafından toplanan ham
listeyi süzmek için tasarlandı.

> **Boru hattındaki yeri:** proxine toplar, proxyprof eler.
> ```
> proxine http -s | proxyprof http -l 1 -o working.lst
> ```

------------------------------------------------------------

## Özellikler

- **Async (asyncio)**. 1.000+ proxy'yi eşzamanlı test eder; threading'in tipik
  RAM şişmesi olmadan.
- **HTTP / HTTPS / SOCKS4 / SOCKS5** desteği — `aiohttp-socks` üzerinden tek
  arabirim.
- **Anonimlik sınıflandırması** (4 alt kategori):
  - **Elite (L1)** — IP ve proxy varlığı gizli
  - **Anonymous (L2)** — IP gizli, proxy belli (`Via` / `X-Forwarded-*` ekler)
  - **Anonymous + Distorting** — IP gizli, **sahte** bir IP enjekte ediliyor
  - **Transparent (L3)** — gerçek IP sızıyor
- **HTTPS tünel testi** (`--tunnel-test`). HTTP/HTTPS proxy'ler için CONNECT
  desteği ölçülür (gstatic.com/generate_204'e 204 yanıtı şart). SOCKS doğası
  gereği tünel'er, otomatik geçilir.
- **Multi-URL erişim testi** (`-a https://a,https://b`). Hepsi geçmek
  zorunda — birden çok gatekeeper'a karşı süzgeç.
- **Hız metrikleri.** Tüm başarılı sondajların p50/p95 latency'si özette
  raporlanır.
- **Geolokasyon** (CF judge ile, ücretsiz). Kendi Cloudflare-korumalı
  judge'ınızı kullanıyorsanız `CF-IPCountry` header'ı otomatik çıkarılıp her
  proxy'nin çıkış ülkesi rapor edilir — ekstra API çağrısı yok.
- **Yaşayan judge seçimi.** İçeride 9 HTTP + 3 HTTPS judge listesi var; ilk
  yanıt veren kullanılır. `-j` ile özel judge geçilebilir.
- **Proxine-uyumlu boru hattı.** Default girdi stdin, default çıktı stdout
  (sadece `IP:PORT` satırları); progress/özet stderr'e gider.
- **Tek-satır TTY progress** + sonda Unicode kutu özet — proxine ile aynı
  görsel dil.

------------------------------------------------------------

## Kurulum

Python ≥ 3.10 gerekir.

```bash
git clone https://github.com/enseitankado/proxy-profiler.git
cd proxy-profiler

# venv önerilir
python3 -m venv .venv
source .venv/bin/activate

pip install .
proxyprof --help
```

Veya bağımlılıkları doğrudan yükleyip script'i çalıştırabilirsiniz:

```bash
pip install aiohttp aiohttp-socks
./proxyprof.py --help
```

> **Eksik bağımlılık:** proxyprof TTY'de çalışırken `aiohttp` veya
> `aiohttp-socks` yüklü değilse **tek bir soru** sorar:
> *"Auto-setup will create ./.venv and install aiohttp aiohttp-socks there… Proceed? [Y/n]"*.
> `Y` denirse yerel `.venv` oluşturulur, pip gerekirse `get-pip.py` ile
> bootstrap edilir, paketler yüklenir, proxyprof venv'in Python'uyla yeniden
> başlatılır. Sudo veya sistem-paket değişikliği yoktur, PEP 668 kısıtlamasına
> takılmaz. Sonraki çalıştırmalarda `python3 proxyprof.py` komutu yerel
> `.venv`'i sessizce tespit eder; ek soru çıkmaz.
>
> Boru hatlarında (TTY değilken) prompt çıkmaz; statik hata mesajıyla çıkar ki
> script'iniz yanıltıcı bir cevap beklemesine takılmasın.

------------------------------------------------------------

## Kullanım

```bash
proxyprof <http|https|socks4|socks5> [seçenekler]
```

### Bayraklar

`--help` çıktısı üç gruba ayrılır: **scan & probes**, **output filters**,
**output destination**. Aşağıdaki tablo da bu sıraya göre düzenlenmiş.

**scan & probes** (network davranışı, probe seçimi — ekstra istek maliyeti olabilir):

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--file` | `-f` | stdin | Proxy listesi dosyası. `-` veya bayrak yok = stdin. |
| `--concurrency` | `-c` | `500` | Eşzamanlı sonda sayısı. |
| `--timeout` | `-T` | `5` | Proxy başına timeout (saniye). |
| `--retries` | `-r` | `1` | Başarısız proxy başına tekrar deneme. |
| `--judge` | `-j` | otomatik | Özel azenv.php-uyumlu judge URL'i. CF judge önerilir. Kimlik header'ı yalnız hardcoded güvenilir domain'lere gider — bkz. *Cloudflare-aware judge*. |
| `--access-test [URLS]` | — | kapalı | Çoklu gatekeeper süzgeci. Değer verilmezse dahili CF-listesinden 3 rastgele site, virgüllü URL listesi verilirse o URL'ler kullanılır. |
| `--tunnel-test` / `--no-tunnel-test` | — | **açık** | HTTPS CONNECT testi. SOCKS için de probe tetikleyici (MITM testi probe'a bağlı). `--no-tunnel-test` HTTPS probe'unu tamamen kapatır (MITM testi de implicit olarak devre dışı). |
| `--mitm-test` / `--no-mitm-test` | — | **açık** | MITM tespiti: TLS cert doğrulama başarısız olur ama CONNECT açıldıysa proxy MITM-suspected sayılır. Aynı HTTPS probe kullanılır — ek istek maliyeti yoktur. `--no-mitm-test` MITM filtresini kapatır (probe yine çalışırsa metrik raporlanır). |
| `--reputation PATH` | — | `~/.config/proxyprof/state.db` | SQLite reputation DB. HOT/WARM/NEW/COLD bucket sınıflandırması + üstel probation için kullanılır. Detay: [Reputation & probation](#reputation--probation-düzenli-tarama). |
| `--no-reputation` | — | — | Reputation'ı tamamen kapat — stateless. |
| `--dead-threshold N` | — | `3` | COLD bucket'a girmek için gereken üst üste fail sayısı. |
| `--probation-max-skip N` | — | `64` | COLD probation'ın atlama tavanı. En kötü ihtimalde her N run'da bir test edilir. |
| `--cold-timeout SECONDS` | — | `2.0` | COLD bucket için per-proxy timeout. |

**output filters** (post-scan, ek istek maliyeti yok — sadece çıktıyı süzer):

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--level` | `-l` | `1` | Kabul edilen maks. anonimlik seviyesi. `1`=elite, `2`=elite+anon (distorting dahil), `3`=hepsi. |
| `--country CC[,CC...]` | — | — | Yalnızca verilen ISO ülke kodlarındaki proxy'leri tut. CF judge gerekir (`PROXY_COUNTRY` döndürmesi için). Örn. `--country TR,US`. |
| `--exclude-distorting` | — | kapalı | Sahte IP enjekte eden (distorting) proxy'leri çıkar. Default kapalı: distorting'ler `--level 2`'yi normal geçer. |

**output destination** (çıktının nereye gideceği):

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--output` | `-o` | stdout | Süzülmüş listeyi bu dosyaya yaz; stdout boş kalır. |
| `--silent` | `-s` | — | Yalnız stdout (proxy listesi); tüm stderr susturulur. |
| `--verbose` | `-v` | — | (Deprecated, no-op) Canlı tablo artık varsayılan. |

**misc** (yerelleştirme ve yardımcı):

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--lang` | `-L` | sistem locale | UI dili. Mevcut: `en`, `tr`. `PROXYPROF_LANG` env değişkeni de geçerli. Detay: [Yerelleştirme](#yerelleştirme-localization). |

### Örnekler

```bash
# Proxine ile zincir: HTTP proxy'leri topla, sadece elite olanları çıkar
proxine http -s | proxyprof http -l 1 -o elite.lst

# Dosyadan oku, elite + anonymous tut, dosyaya yaz
proxyprof http -f raw.lst -l 2 -o filtered.lst

# SOCKS5 listesi, 1000 eşzamanlı, 8s timeout, satır satır log
proxyprof socks5 -f socks5.lst -c 1000 -T 8 -v

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

# Kendi Cloudflare-korumalı judge'ınla: ülke bilgisi + ziyaret log'u
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php \
  --judge-domain yours.tld          # X-Proxyprof-Proxy header'ı buraya gider

# Tamamen sessiz; başka bir script'e besleme
proxine socks5 -s | proxyprof socks5 -s | head -20
```

------------------------------------------------------------

## Çıktı

### Stdout

Sıralı, dedupe edilmiş, süzgeçten geçen `IP:PORT` satırları:

```
1.2.3.4:8080
1.2.3.4:8443
5.6.7.8:3128
```

`-o FILE` verilirse stdout boş kalır; satırlar dosyaya yazılır.

### Stderr — canlı tablo + progress + CONFIG/RESULT kutuları

Tarama akarken stderr şu yapıdadır:

```
┌───────┬────────┬──────┬───────────────────────┬─────┬─────────────────┬────┬────────┬─────┬──────┬─────┐
│     # │ STATUS │ BKT  │ PROXY                 │ LVL │ OUT             │ CC │   TIME │ TUN │ MITM │ ACC │
├───────┼────────┼──────┼───────────────────────┼─────┼─────────────────┼────┼────────┼─────┼──────┼─────┤
│  3/30 │ ok     │ H    │ 8.211.194.85:4444     │ L1  │ 8.211.194.85    │ US │   1.2s │ ✓   │ ✓    │ ✓   │
│  7/30 │ ok     │ N    │ 5.6.7.8:1080          │ L2d │ 5.6.7.8         │ DE │   0.8s │ ✓   │ ✓    │ ✓   │
│ 12/30 │ filter │ W    │ 9.10.11.12:3128       │ L1  │ 9.10.11.12      │ —  │   2.1s │ ✓   │ ×    │ ✓   │
└───────┴────────┴──────┴───────────────────────┴─────┴─────────────────┴────┴────────┴─────┴──────┴─────┘
[████████████████░░░░]  80%  24/30  ok:3     fail:21    skip:0     elapsed:  9.4s
```

`MITM=×` üçüncü satırda: proxy CONNECT tüneli kurabildi (`TUN=✓`) ama TLS
sertifika doğrulama başarısız oldu — proxy TLS chain'i kendi sertifikasıyla
kırıyor, **MITM imzası**. STATUS `filter`'a düşer ve stdout'a yazılmaz.

**Tablo davranışı:**

- Yalnız **başarılı** proxy'ler tabloda satır olur. Fail'ler görünmez ama
  progress satırındaki `fail:N` sayımı artar — gürültüyü tablodan ayırır.
- Satırlar tamamlanma sırasıyla gelir (en hızlı önce; `#` bu sırayı, total ise
  toplam hedefi gösterir → `3/30`, `7/30` arada atlamalar fail'lerin yerini
  belli eder).
- En alt satır canlı progress: tamamlanan / toplam, ok / fail sayımları,
  elapsed. ANSI cursor manipülasyonu ile yerinde güncellenir; pipe ortamında
  (stderr TTY değilken) sadece final progress yazılır.

**Sütunlar:**

| Sütun | Anlamı |
|---|---|
| `#` | Tamamlanma sırası / toplam |
| `STATUS` | `ok` (her şey geçti) · `filter` (judge geçti, tunnel/access düştü) |
| `PROXY` | IP:PORT |
| `LVL` | `L1` elite · `L2` anon · `L2d` anon+distorting · `L3` transparent |
| `OUT` | Judge'ın gördüğü çıkış IP'si (proxy'nin dış adresi) |
| `CC` | ISO ülke kodu (CF judge kullanılırsa) |
| `TIME` | Toplam test süresi |
| `TUN` | Tunnel testi: `✓` CONNECT açıldı · `×` kapalı · `—` test yok |
| `MITM` | TLS chain durumu: `✓` temiz · `×` MITM tespit edildi · `—` test yok |
| `ACC` | Access testi: `✓` tüm gatekeeper'lara ulaştı · `×` en az biri fail · `—` test yok |

**Tarama bittikten sonra** progress'in altında iki kutu sırayla yazılır.

**CONFIG** — tarama parametrelerinin key=value referansı (aynı taramayı
tekrarlamak için tüm bayraklar görünür):

```
┌ CONFIG ─────┬──────────────────────────────────────────────┐
│ protocol    │ http                                         │
│ input       │ raw.lst                                      │
│ output      │ working.lst                                  │
│ judge       │ https://tankado.com/proxyjudge.php           │
│ publicIP    │ 78.180.x.x                                   │
│ level       │ ≤1                                           │
│ concurrency │ 500                                          │
│ timeout     │ 5.0s                                         │
│ retries     │ 1                                            │
│ tunnel-test │ on                                           │
│ access-test │ 3 URLs  (https://www.cloudflare.com/...)     │
│ identity    │ on                                           │
└─────────────┴──────────────────────────────────────────────┘
```

**RESULT** — tarama sonuçlarının özeti:

```
┌ RESULT ──┬───────────────────────────────────────────────────────────┐
│ scanned  │ 1,000 proxies                                             │
│ good     │ 142 elite, 38 anon (10 distorting), 17 transparent  →  working.lst│
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
- `tunnel` → `--tunnel-test` aktifse (default açık)
- `country` → judge `PROXY_COUNTRY` / `CF-IPCountry` döndürüyorsa (yani CF judge kullanılıyorsa)
- `(N distorting)` → en az 1 distorting proxy yakalandıysa

### Çıktı modu tablosu

| Komut | stdout | stderr |
|---|---|---|
| `proxyprof http` | süzülmüş liste | canlı tablo → özet kutu |
| `proxyprof http -o f.lst` | (boş) | canlı tablo → özet kutu |
| `proxyprof http -s` | süzülmüş liste | (boş) |
| `proxyprof http -o f.lst -s` | (boş) | (boş) |

------------------------------------------------------------

## Anonimlik seviyeleri

Judge'tan geri dönen request header'larına bakılır. Üç seviye + bir alt tür:

| Seviye | İsim | Tespit kuralı | Anlam |
|---|---|---|---|
| **1** | Elite | Public IP yok, proxy header'ı yok | Hem IP'nizi hem proxy varlığını gizler. |
| **2** | Anonymous | Public IP yok, ama `via` / `x-forwarded-*` / `proxy-*` var | IP'nizi gizler ama "bir proxy kullanılıyor" der. |
| **2** + *distorting* | Distorting | L2 + `X-Forwarded-For` benzeri header'da public IP'den farklı, routable bir IPv4 var | IP'nizi gizler **ve sahte bir IP enjekte eder**. Fingerprint kaçırma için kullanılır, güven açısından risklidir. |
| **3** | Transparent | Public IP header'larda yansıyor | IP'nizi gizlemez; sadece routing yapar. |

`-l 1` (default) sadece elite proxy'leri tutar. `-l 2` elite + anonymous
(distorting dahil), `-l 3` hepsi. Özet kutusunda distorting alt sayımı ayrıca
gösterilir.

### Distorting tespitinin sınırı

Header'daki sahte IP **public range'de görünen** bir IPv4 olmalı (RFC1918,
loopback, link-local elenir). Bir proxy header'a `0.0.0.0` ya da `192.168.1.1`
yazıyorsa distorting değil — sadece kötü konfigüre edilmiş bir anonymous proxy.
IPv6 ya da IP olmayan değerler de tespit kapsamı dışında.

------------------------------------------------------------

## Süzgeçler ve metrikler

Anonimlik dışındaki üç ekstra süzgeç (`--tunnel-test`, `-a`, hız) ve iki ekstra
metrik (timing percentiles, ülke dağılımı) — hepsi proxine boru hattının
ötesinde ham listeyi gerçek üretim kalitesine indirgemeye yarar.

### HTTPS tünel testi (default açık)

**Neden:** Bir HTTP proxy düz HTTP isteklerini iletiyor olabilir ama HTTPS için
gereken `CONNECT` komutunu desteklemiyor olabilir. Bugün neredeyse her site
HTTPS olduğundan, CONNECT-yetkisi olmayan HTTP proxy pratik olarak çoğu hedefe
işe yaramaz.

**Ne yapar:** Her HTTP/HTTPS proxy için ek bir istek atar:
`https://www.gstatic.com/generate_204`. 204 dönerse CONNECT destekleniyor
demektir. SOCKS proxy'leri doğası gereği tünel kurar; otomatik geçilir, ek
istek yapılmaz.

**Maliyet:** Tarama süresi yaklaşık 2 katına çıkar (HTTP/HTTPS proxy'leri için
proxy başına 2 istek). Concurrency artırılarak telafi edilebilir.

**Kullanım:** Default **açık**. Kapatmak için:

```bash
proxyprof http -f raw.lst --no-tunnel-test       # CONNECT testini atla
```

**Sonuç:**
- Stdout'a (veya `-o` dosyasına) sadece tünel testini geçen proxy'ler yazılır
- Canlı tabloda `TUN` sütunu: `✓` / `×` / `—`
- Özet kutuda: `tunnel │ 118 CONNECT-capable (of 197 good)`

### Çoklu gatekeeper erişim testi (`--access-test`)

**Neden:** Bir proxy Cloudflare'i geçebilir ama Google CAPTCHA gösterebilir,
veya tersine. "Her yerden çalışan" proxy'leri ayıklamak için tek bir gatekeeper
yeterli değil.

**Ne yapar:** Verdiğiniz URL listesinin **hepsine** proxy üzerinden istek atar.
Tek bir URL bile fail ederse proxy "blocked" sayılır.

**Kullanım — iki mod:**

```bash
# Otomatik: dahili CF listesinden 3 rastgele site (her tarama farklı seçim)
proxyprof http -f raw.lst --access-test

# Manuel: kendi gatekeeper'larını ver (virgülle, hepsi http(s):// ile)
proxyprof http -f raw.lst \
  --access-test https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org
```

Dahili CF listesi: cloudflare.com, discord.com, reddit.com, medium.com,
udemy.com, patreon.com, kickstarter.com, upwork.com, zendesk.com,
shopify.com — hepsinin `/cdn-cgi/trace` endpoint'i kullanılır (her CF site'da
mevcut, 200 döner, UA filtrelemez).

**Sonuç:**
- Sadece tüm URL'lere ulaşan proxy'ler stdout'a düşer
- Canlı tabloda `ACC` sütunu: `✓` / `×` / `—`
- Özet kutuda: `blocked │ 24 access denied`

### Hız metrikleri (otomatik)

**Neden:** Bir proxy "çalışıyor" demek hızlı olduğu anlamına gelmez. "Liste
iyi mi?" sorusuna ortalama (`mean`) çoğu zaman yanıltıcı bir cevaptır:
bir-iki çok yavaş proxy ortalamayı şişirir ya da çok hızlı bir proxy
kötü dağılımı saklar. Bunun yerine **yüzdelik** (percentile) kullanılır.

**Yüzdelik (percentile) ne demek?**
Bir veri kümesini en küçükten büyüğe sıralarsın, "%X" değeri verinin
**ilk %X'inin** ne kadar küçük olduğunu söyler.
- **p50** (medyan): verinin yarısı bu değerden küçük, yarısı büyük.
- **p95**: verinin %95'i bu değerden küçük, sadece %5'i daha yavaş — yani
  worst-case'in eşiği.

**Somut bir örnek.** Diyelim 10 proxy taradın ve süreleri (saniye):
```
0.4, 0.6, 0.8, 0.9, 1.1, 1.3, 1.5, 2.0, 3.5, 8.0
```
- Ortalama (mean) = (0.4+0.6+…+8.0)/10 = **2.01s** — ama 9 proxy'nin 9'u bu değerden hızlı! Outlier'a (8.0) yenildi.
- Medyan (p50) = 5. ve 6. değerlerin ortası = **1.2s** — "yarısı bu kadar hızlı" gerçek tablo.
- p95 = listenin yukarı uçuna doğru, **8.0s** — "en kötü %5'i ne kadar yavaş?"

**Ne yapar:** Tarama sonunda **başarılı** sondajların (judge'a ulaşabilenlerin)
sürelerinden p50 ve p95'i çıkarır. Fail olanlar dışarıda — zaman/dışarıda
kalmış sayıların ortalaması anlamsız olurdu.

**Kullanım:** Otomatik. Hiçbir bayrak gerekmez.

**Sonuç:**

```
timing   │ p50 1.2s · p95 4.1s
```

**Nasıl okunur:**
- **p50 ≈ p95** (örn. p50 1.0s · p95 1.4s) → tutarlı, hızlı liste. İdeal.
- **p95 >> p50** (örn. p50 0.8s · p95 6.2s) → çoğu hızlı ama uzun bir
  "yavaş kuyruk" var. Üretimde bu kuyruktaki proxy'lerin timeout'a takılma
  ihtimali yüksek.
- **p95 ≈ timeout** (örn. `-T 5` iken p95 4.7s) → liste zar zor sığıyor;
  `-T` değerini artırırsan büyük olasılıkla daha çok proxy "good" olur.
- **p50 yüksek** (örn. p50 4.5s) → liste genel olarak yavaş; başka bir
  kaynak denemeye değer.

### Geolokasyon (CF judge ile, ücretsiz)

**Neden:** Çoğunlukla sadece belirli ülkelerdeki proxy'ler işe yarar (örn. TR
banka sitesi için TR proxy, US streaming için US proxy). Geolokasyon
genellikle MaxMind DB indirmek veya rate-limited API'ler çağırmak demektir —
ek karmaşıklık.

**Ne yapar:** Cloudflare her proxy'den gelen isteğin IP'sini çözer ve
`CF-IPCountry` header'ı ekler. CF-aware judge (`proxyjudge.php`) bu header'ı
yakalayıp `PROXY_COUNTRY` alanı olarak yansıtır. Proxyprof bunu otomatik
çıkartır. Ek API çağrısı, ek bağımlılık, ek dosya yok.

**Kullanım:** Sadece CF-protected domain'inizde host ettiğiniz judge'u
gösterin:

```bash
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php
```

**Sonuç:**
- Özet kutuda en kalabalık 5 ülke + diğerlerinin toplamı:
  ```
  country  │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more
  ```
- Verbose log'da her satıra ülke kodu düşer: `[ ok ]  L1  1.2.3.4:8080  1.2s  out=1.2.3.4  TR tun`

**Önemli:** Public azenv judge'larında country bilgisi yok — sadece CF-aware
judge ile çalışır.

### Hepsini birlikte

```bash
# proxine'den taze HTTP proxy → kendi CF judge'una karşı test → sadece elite +
# CONNECT-yetkili + 3 rastgele CF gatekeeper'a geçen proxy'leri al.
# (PROXYPROF_JUDGE_DOMAIN bir kere export edilmişse --judge-domain gereksiz.)
proxine http -s | proxyprof http \
  -j https://yours.tld/proxyjudge.php \
  --judge-domain yours.tld \
  --access-test \
  -o production-ready.lst
```

Bu komut production'a koymadan önce her açıdan elenmiş bir proxy listesi
verir: elite anonimlik (default `-l 1`) + HTTPS tüneli (default `--tunnel-test`)
+ 3 farklı CF gatekeeper'a erişim + ülke dağılımı raporu.

------------------------------------------------------------

## Reputation & probation (düzenli tarama)

Cron benzeri düzenli (saatlik/günlük) çalıştırmalarda input listesi tipik
olarak **100k+ proxy** içerir, **%80–90'ı önceki çalıştırmalardan tanıdıktır**,
ve büyük çoğunluğu **sürekli fail verir** (kaynak agregatörler aynı bayat
listeyi günlerce sunar). Stateless modda her run baştan herkesi test eder —
zamanın çoğu zaten ölü olduğunu bildiğin proxy'lere harcanır.

Reputation katmanı bu israfı çözer. SQLite tabanlı bir state DB (`--reputation
PATH`, default `~/.config/proxyprof/state.db`) her proxy'nin geçmişini tutar
ve her tarama başında listeyi dört **bucket**'a ayırır:

| Bucket | Tanım | Davranış |
|---|---|---|
| **HOT**  | Son 24 saatte başarılı | Önce dispatch edilir, normal `--timeout`. |
| **WARM** | Geçmişte başarılı ama 24sa+ önce | İkinci sırada dispatch. |
| **NEW**  | DB'de hiç görülmemiş | Üçüncü sırada. |
| **COLD** | `--dead-threshold` (default 3) kez üst üste fail | En son, kısa `--cold-timeout` ve **üstel probation** ile. |

### Ağırlıklı paralel dispatch

Bucket'lar **sıralı değil ağırlıklı paralel** taranır. Tek bir
`asyncio.Semaphore(--concurrency)` altında, dispatch sıralaması
`HOT*8 → WARM*4 → NEW*2 → COLD*1 → HOT*8 → …` döngüsüyle interleave edilir.
Sonuç: HOT proxy'ler ilk dalganın çoğunluğunu kapar (yani output erken akar),
ama COLD'lar da paralel ilerler — tek bir bucket diğerlerini bloklamaz.

### Üstel probation (asıl tasarruf)

COLD bucket'taki bir proxy **her run'da değil**, üstel olarak seyrelen bir
takvimle test edilir:

| consecutive_failures | Test sıklığı |
|---|---|
| 3 (=dead_threshold) | 2 run'da bir |
| 4                   | 4 run'da bir |
| 5                   | 8 run'da bir |
| 6                   | 16 run'da bir |
| 7                   | 32 run'da bir |
| 8+                  | **64 run'da bir** (tavan, `--probation-max-skip`) |

Tavan, ölü proxy'nin tamamen unutulmasını engeller — bir gün geri gelirse
yakalanır. Ama günlük cron'da bir proxy 8 kere üst üste fail vermişse,
ortalama her **iki ayda bir** test edilir; %90'lık ölü kuyruk efektif olarak
iş yükünden çıkar.

Sadece **judge'a hiç ulaşamayan** (`status=fail`) sonuçlar bu sayacı artırır;
`status=filter` (judge geçti ama tunnel/access süzgecinden düştü) proxy'nin
canlı olduğunu gösterir — fail sayılmaz.

### Tipik tasarruf

Diyelim 100k proxy'lik input'unuz var ve günlük cron çalıştırıyorsunuz:

| Run | Stateless | Reputation+probation | Açıklama |
|---|---|---|---|
| #1 (boş DB) | 100k test | 100k test | Hepsi NEW; aynı iş. |
| #5 | 100k test | ~30k test | 70k ölü kuyruk farklı probation kademelerinde. |
| #30 | 100k test | ~12k test | Eski ölüler 32–64 run'da bir test ediliyor. |
| Steady state | 100k test | **~10–15k test** | HOT/WARM + yeni gelen NEW + COLD'un seyrek örnekleri. |

### Canlı tabloda BUCKET sütunu

Her satırın `BKT` sütunu proxy'nin bu run'daki bucket'ını gösterir: `H` (hot),
`W` (warm), `N` (new), `C` (cold), `—` (stateless mod).

### CONFIG kutusunda dağılım

Tarama başında bucket dağılımı + atlanan probation sayısı raporlanır:

```
│ reputation   │ on  (run #42, db=/home/u/.config/proxyprof/state.db) │
│ buckets      │ HOT 5,234 · WARM 3,128 · NEW 2,400 · COLD 89,238     │
│ probation    │ 73,455 COLD proxy skipped                            │
│ cold-timeout │ 2.0s                                                 │
```

### Tamamen kapatmak

Eski stateless davranışı geri getirmek için:

```bash
proxyprof http -f raw.lst --no-reputation
```

State dosyası okunmaz/yazılmaz; tüm proxy'ler eşit önceliklendirilmemiş
şekilde, tek `--timeout` ile taranır.

### Bakım

State şeması basit (tek `proxy` tablosu + `meta`). Dosya tamamen
self-contained — kopyalayıp taşıyabilirsiniz. SQLite WAL modu açık
olduğundan paralel proxyprof süreçleri aynı DB'ye güvenle yazar.

Manuel inceleme:

```bash
sqlite3 ~/.config/proxyprof/state.db \
  "SELECT proxy, consecutive_failures, total_attempts,
          datetime(last_success,'unixepoch') AS last_ok
   FROM proxy
   ORDER BY consecutive_failures DESC
   LIMIT 20;"
```

DB'yi resetlemek için: dosyayı sil. Bir sonraki çalıştırmada otomatik yeniden
oluşturulur ve hepsi NEW'den başlar.

------------------------------------------------------------

## Cloudflare-aware judge (önerilir)

Public azenv judge'ları zaman zaman ölür ve yavaş yanıt verir. Kendi
Cloudflare-korumalı domain'iniz varsa repodaki `proxyjudge.php` dosyasını
herhangi bir yere koyup `-j` ile gösterebilirsiniz:

```bash
# Yerel test
curl https://yours.tld/proxyjudge.php

# proxyprof ile
proxyprof http -j https://yours.tld/proxyjudge.php -f raw.lst
```

Bu judge:
- `CF-Connecting-IP`'yi `REMOTE_ADDR` olarak normalize eder → anonimlik
  tespiti gerçek istemci (proxy çıkış) IP'sine karşı çalışır.
- `CF-IPCountry`'yi `PROXY_COUNTRY` field'ı olarak expose eder → proxyprof
  otomatik çekip özet kutusuna country dağılımı ekler. **Ekstra GeoIP DB
  veya API çağrısı yok.**
- Tüm `CF-*` header'larını çıktıdan strip eder → anonimlik tespitini
  saptırmaz, judge'ın CF arkasında olduğu belli olmaz.

> **Önemli:** Domain Cloudflare'de **"Proxied"** (turuncu bulut) modda olmalı.
> "DNS only" (gri bulut) modunda CF header'ları gelmez, judge sıradan bir
> azenv gibi çalışır (country bilgisi yok).

### Ziyaret log'u (opt-in)

Judge gelen her isteği JSONL formatında yan tarafa kaydedebilir. Default olarak
**kapalı**. Açmak için `proxyjudge.php`'nin tepesindeki tek satırı düzenle:

```php
// Boş = log yok. Path verirsen log açılır.
$LOG_FILE = '/var/log/proxyjudge.log';
```

> ⚠️ **Güvenlik:** Log dosyasını **web root içine koyma**. Path'i ya tamamen
> dışarı (örn. `/var/log/...`) yaz, ya da web root içinde tutuyorsan
> `.htaccess`/nginx kuralıyla HTTP erişimini engelle. Aksi takdirde judge'ını
> ziyaret eden tüm proxy'lerin listesi internete açık olur.

Her satır şu alanları içerir:

| Alan | Kaynak | Açıklama |
|---|---|---|
| `ts` | Server | ISO-8601 UTC timestamp |
| `seen_ip` | CF-Connecting-IP | Proxy'nin gerçek çıkış IP'si (güvenilir — CF set eder) |
| `seen_port` | TCP peer | Proxy'nin O istek için kullandığı **ephemeral** kaynak port (dinleme portu DEĞİL) |
| `country` | CF-IPCountry | ISO ülke kodu (`TR`, `US`, …) |
| `client_type` | `X-Proxyprof-Proxy` header | Proxy tipi (`http` / `https` / `socks4` / `socks5`) — **spoof edilebilir** |
| `client_ip` | `X-Proxyprof-Proxy` header | Proxy'nin **dinleme** IP'si — `seen_ip` ile cross-reference yapılabilir |
| `client_port` | `X-Proxyprof-Proxy` header | Proxy'nin **dinleme** portu (örn. `1080`, `8080`) |
| `ua` | User-Agent | 200 karakterle kısaltılmış |
| `cf_ray` | CF-Ray | CF edge trace ID — sorun ayıklama için |

Tipik bir satır:

```json
{"ts":"2026-05-24T13:47:21+00:00","seen_ip":"45.83.122.10","seen_port":54231,"country":"TR","client_type":"socks5","client_ip":"45.83.122.10","client_port":1080,"ua":"Mozilla/5.0 ...","cf_ray":"8a1b2c3d4e5f6789-IST"}
```

**Neden iki ayrı IP alanı?** `seen_ip` Cloudflare'in TCP peer olarak gördüğü
adres — proxy bunu sahteleyemez. `client_ip` ise proxyprof'un header'a yazdığı
değer — herkes bu header'ı uydurabilir. İkisinin **farklı** olması ya proxy
chain'i (proxyprof → proxy A → proxy B → judge) ya da fake-header trafiği
demek. **Aynı** olması = direkt bağlantı, güvenilir kayıt.

#### proxyprof tarafı — kimlik gönderimi hardcoded domain whitelist

`X-Proxyprof-Proxy: <type>://<ip>:<port>` header'ı **yalnızca** kodda
sabitlenmiş güvenilir domain'lerdeki judge'lara gönderilir. CLI bayrağı veya
env var yok — yanlış kullanım fiziksel olarak imkânsız.

Güvenilir domain listesi `proxyprof.py` içinde `_TRUSTED_JUDGE_DOMAINS`
sabitidir:

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
- `eviltankado.com` ya da `tankado.com.evil.tld` gibi yakın isimler **DEĞİL**
- Port önemsizdir: `tankado.com:8443` de eşleşir

**Davranış matrisi (sabit `tankado.com` ile):**

| judge URL | Header gönderilir mi? |
|---|---|
| `https://tankado.com/proxyjudge.php` | ✅ |
| `https://judge.tankado.com/anything` | ✅ (subdomain) |
| `https://tankado.com:8443/p.php` | ✅ (port önemsiz) |
| `https://eviltankado.com/x` | ❌ (yakın isim, farklı domain) |
| `http://httpheader.net/azenv.php` | ❌ (public judge) |
| `http://proxyjudge.biz/` | ❌ (alakasız) |

Tarama sonu CONFIG kutusunda `identity = on/off` olarak görünür. Public
azenv'lara, otomatik seçilen judge'lara veya yanlış yere belirttiğin custom
judge'a kimlik header'ı sızmaz. Path/script adına bakılmaz — başkasının kendi
domain'ine `proxyjudge.php` deploy etmesi sizin kimliğinizin oraya gitmesini
sağlamaz; tek belirleyici **domain sahipliği**dir.

#### Log'u okuma

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

------------------------------------------------------------

## Yerelleştirme (Localization)

proxyprof'un tüm kullanıcıya yönelik metinleri (yardım ekranı, runtime
mesajları, tablo başlıkları, CONFIG/RESULT kutuları, progress satırı) çok
dilli destek altyapısına sahiptir. Çeviriler `i18n/` dizininde tek-dosyalık
JSON olarak tutulur.

### Dil seçimi (öncelik sırası)

1. `-L tr` / `--lang tr` CLI bayrağı
2. `PROXYPROF_LANG=tr` ortam değişkeni
3. Sistem locale'i (`LC_ALL`, `LC_MESSAGES`, `LANG`)
4. İngilizce (her zaman mevcut fallback)

Desteklenmeyen bir dil istenirse otomatik olarak İngilizce'ye düşülür — uyarı
mesajı verilmez.

```bash
# Türkçe arayüz
proxyprof http -L tr -f raw.lst

# Sistem locale Türkçe ise otomatik (env değişkeni gerekmez)
LANG=tr_TR.UTF-8 proxyprof http -f raw.lst

# Tek seferlik İngilizce'ye geçiş
proxyprof http -L en -f raw.lst
```

Aktif dil tarama sonu **CONFIG** kutusunda `dil` satırında görünür.

### Mevcut diller

| Kod | Dil | Çeviren |
|---|---|---|
| `en` | English | proxyprof core team |
| `tr` | Türkçe | Özgür Koca |

### Yeni dil ekleme (contributor için)

Çeviriler basit JSON'dur. Üç adımda PR atabilirsiniz:

```bash
# 1) Canonical İngilizce dosyayı kopyala
cp i18n/en.json i18n/de.json

# 2) JSON içindeki "key": "value" çiftlerinin DEĞER'ini hedef dile çevir.
#    Anahtarlar (sol taraf) ve {placeholder}'lar AYNEN korunmalı.
$EDITOR i18n/de.json

# 3) Test et
./proxyprof.py -L de --help
./proxyprof.py -L de http -f /tmp/sample.lst --no-reputation
```

**Çeviri ipuçları:**

- Eksik bir anahtar runtime'da otomatik olarak İngilizce'ye fallback olur.
  Yani çeviriye yavaş yavaş başlayıp kademeli PR atılabilir.
- `{placeholder}`'lı dizgelerde placeholder isimleri kelimesi kelimesine
  korunmalı (örn. `{pkgs}`, `{n}`, `{elapsed:.1f}s`).
- `meta.lang_name` alanı dili kendi alfabesinde yazın (örn. `"meta.lang_name": "Deutsch"`).
- `meta.translator_credit` alanına kendi adınızı yazabilirsiniz — README'deki
  "Mevcut diller" tablosuna eklenir.
- Çok kısa kelimeler (örn. tablo başlıkları "TUN", "ACC") karakter sayısı
  hassas — sütun genişliği otomatik ayarlanır ama makul tutmaya çalışın.

PR açtıktan sonra mevcut dil setine eklenir.

------------------------------------------------------------

## Mimari

```
proxy-profiler/
├── proxyprof.py    # Async scanner + CLI (tek dosya, ~1000 satır)
├── judges.py       # Judge listesi + response parser + seviye + distorting + country
├── reputation.py   # SQLite-tabanlı proxy reputation store + bucket sınıflandırma + probation
├── i18n.py         # Çok dilli mesaj destek modülü (stdlib-only)
├── i18n/
│   ├── en.json     # Canonical English (referans)
│   └── tr.json     # Türkçe
├── proxyjudge.php  # Opsiyonel CF-aware judge — kendi domain'inde host et
├── pyproject.toml  # aiohttp + aiohttp-socks bağımlılıkları
└── README.md
```

- **`judges.py`** judge URL listesi, judge yanıtının iki olası formatını
  (`<pre>KEY=VALUE</pre>` ve düz JSON) ayrıştırır, public IP + header
  sözlüğünden seviye çıkarır.
- **`reputation.py`** tek dosyalık SQLite şeması, bucket sınıflandırma
  (HOT/WARM/NEW/COLD), üstel probation kararı ve ağırlıklı interleave dispatch
  yardımcılarını sağlar. WAL modu açık → paralel proxyprof süreçleri güvenli.
- **`proxyprof.py`** her proxy için tek bir `aiohttp_socks.ProxyConnector`
  açar, `asyncio.Semaphore(N)` ile eşzamanlılığı sınırlar; reputation açıkken
  task'lar bucket önceliğine göre interleave edilir; sonuçlar tek bir
  `gather` ile toplanır ve sonunda DB'ye batch upsert ile yazılır.

------------------------------------------------------------

## İlgili araçlar

- **[Proxine](https://github.com/enseitankado/proxine)** — 60+ açık kaynaktan
  ham proxy listesi toplayan aggregator. Proxyprof'un asıl girdi kaynağı.
- **[EliteProxySwitcher](https://www.eliteproxyswitcher.com/)** — Windows GUI.
- **[Open Proxy Checker](https://openproxy.space/software/proxy-checker/)** —
  Windows liste doğrulayıcı.

------------------------------------------------------------

## Lisans

MIT. Türev çalışmalarda orijinal yazar (Özgür Koca) atıfını koruyun. Yazılım
"olduğu gibi" sunulur; kullanım riski tamamen kullanıcıya aittir.

## Yazar

**Özgür Koca** — meslek lisesinde
[öğretmenlik](https://samsuneml.meb.k12.tr/) yapıyor.
GitHub: [enseitankado](https://github.com/enseitankado) · Blog:
[tankado.com](https://www.tankado.com)
