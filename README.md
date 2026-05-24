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

| Uzun | Kısa | Varsayılan | Açıklama |
|---|---|---|---|
| `--file` | `-f` | stdin | Proxy listesi dosyası. `-` veya bayrak yok = stdin. |
| `--output` | `-o` | — | Süzülmüş listeyi bu dosyaya yaz; stdout boş kalır. |
| `--level` | `-l` | `1` | Kabul edilen maks. anonimlik seviyesi. `1`=elite, `2`=elite+anon, `3`=hepsi. |
| `--concurrency` | `-c` | `500` | Eşzamanlı sonda sayısı. |
| `--timeout` | `-T` | `5` | Proxy başına timeout (saniye). |
| `--retries` | `-r` | `1` | Başarısız proxy başına tekrar deneme. |
| `--judge` | `-j` | otomatik | Özel azenv.php-uyumlu judge URL'i. CF judge önerilir. |
| `--access` | `-a` | — | Proxy bu URL'(ler)e erişebiliyor mu? Virgülle çoklu URL; hepsi pass etmek zorunda. |
| `--tunnel-test` | — | kapalı | HTTP/HTTPS proxy'lerde CONNECT tüneli sınanır. |
| `--verbose` | `-v` | — | Her proxy için satır log; progress kapatılır. |
| `--silent` | `-s` | — | Yalnız stdout (proxy listesi); tüm stderr susturulur. |

### Örnekler

```bash
# Proxine ile zincir: HTTP proxy'leri topla, sadece elite olanları çıkar
proxine http -s | proxyprof http -l 1 -o elite.lst

# Dosyadan oku, elite + anonymous tut, dosyaya yaz
proxyprof http -f raw.lst -l 2 -o filtered.lst

# SOCKS5 listesi, 1000 eşzamanlı, 8s timeout, satır satır log
proxyprof socks5 -f socks5.lst -c 1000 -T 8 -v

# Cloudflare erişim testi: proxy CF korumalı hedefe ulaşabiliyor mu?
proxyprof http -f raw.lst -a https://example.cloudflare.com

# Çoklu gatekeeper: proxy hem CF'e hem Google'a ulaşıyor olsun
proxyprof http -f raw.lst -a https://cloudflare.com,https://google.com

# HTTPS tünel desteği şartı (CONNECT yetkin proxy'leri süz)
proxyprof http -f raw.lst --tunnel-test

# Kendi Cloudflare-korumalı judge'ınla: ülke bilgisi otomatik gelir
proxyprof http -f raw.lst -j https://yourdomain.tld/proxyjudge.php

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

### Stderr

Çalışma sırasında tek satırlık ilerleme:

```
[████████████░░░░░░░░]  60%  600/1000  ✓ 5.6.7.8:3128         good   142
```

Sonda özet kutusu (tüm özellikler aktif):

```
┌──────────┬───────────────────────────────────────────────────────────┐
│ protocol │ http                                                      │
│ judge    │ https://yours.tld/proxyjudge.php                          │
│ publicIP │ 78.180.x.x                                                │
│ scanned  │ 1,000 proxies                                             │
│ good     │ 142 elite, 38 anon (10 distorting), 17 transparent  →  out.lst│
│ bad      │ 803 (timeout/error)                                       │
│ blocked  │ 24 access denied                                          │
│ tunnel   │ 118 CONNECT-capable (of 197 good)                         │
│ timing   │ p50 1.2s · p95 4.1s                                       │
│ country  │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more                   │
│ elapsed  │ 12.4s                                                     │
└──────────┴───────────────────────────────────────────────────────────┘
```

Satırların görünmesi koşula bağlı:
- `blocked` → `-a` verildiyse
- `tunnel` → `--tunnel-test` aktifse
- `country` → judge `PROXY_COUNTRY` / `CF-IPCountry` döndürüyorsa (yani CF judge kullanılıyorsa)
- `distorting` → en az 1 distorting proxy yakalandıysa

### Çıktı modu tablosu

| Komut | stdout | stderr |
|---|---|---|
| `proxyprof http` | süzülmüş liste | progress → özet |
| `proxyprof http -v` | süzülmüş liste | satır log → özet |
| `proxyprof http -o f.lst` | (boş) | progress → özet |
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

### HTTPS tünel testi (`--tunnel-test`)

**Neden:** Bir HTTP proxy düz HTTP isteklerini iletiyor olabilir ama HTTPS için
gereken `CONNECT` komutunu desteklemiyor olabilir. Bugün neredeyse her site
HTTPS olduğundan, CONNECT-yetkisi olmayan HTTP proxy pratik olarak çoğu hedefe
işe yaramaz.

**Ne yapar:** Her HTTP/HTTPS proxy için ek bir istek atar: `https://www.gstatic.com/generate_204`.
204 dönerse CONNECT destekleniyor demektir. SOCKS proxy'leri doğası gereği
tünel kurar; otomatik geçilir, ek istek yapılmaz.

**Maliyet:** Tarama süresi yaklaşık 2 katına çıkar (HTTP/HTTPS proxy'leri için
proxy başına 2 istek). Concurrency artırılarak telafi edilebilir.

**Kullanım:**

```bash
proxyprof http -f raw.lst --tunnel-test -o tunneled.lst
```

**Sonuç:**
- Stdout'a (veya `-o` dosyasına) sadece tünel testini geçen proxy'ler yazılır
- Özet kutuda: `tunnel │ 118 CONNECT-capable (of 197 good)`
- Verbose log'da satır sonuna `tun` / `no-tun` etiketi düşer

### Çoklu gatekeeper erişim testi (`-a`)

**Neden:** Bir proxy Cloudflare'i geçebilir ama Google CAPTCHA gösterebilir,
veya tersine. "Her yerden çalışan" proxy'leri ayıklamak için tek bir gatekeeper
yeterli değil.

**Ne yapar:** Verdiğiniz URL listesinin **hepsine** proxy üzerinden istek atar.
Tek bir URL bile fail ederse proxy "blocked" sayılır.

**Kullanım:** Virgülle ayır, hepsi `http://` veya `https://` ile başlasın:

```bash
proxyprof http -f raw.lst \
  -a https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org
```

**Sonuç:**
- Sadece tüm URL'lere ulaşan proxy'ler stdout'a düşer
- Özet kutuda: `blocked │ 24 access denied`

### Hız metrikleri (otomatik)

**Neden:** Bir proxy "çalışıyor" demek hızlı olduğu anlamına gelmez. 10 saniyede
yanıt veren bir proxy teknik olarak iyi ama pratikte yavaş. Bir taramanın
genel hız resmini görmeden kalite değerlendirmesi yapılamaz.

**Ne yapar:** Tüm başarılı sondajların yanıt sürelerinin **medyan** (p50) ve
**%95'inci yüzdelik** (p95) değerlerini hesaplar.

**Kullanım:** Otomatik. Hiçbir bayrak gerekmez; her tarama sonunda görünür.

**Sonuç:**

```
timing   │ p50 1.2s · p95 4.1s
```

**Nasıl okunur:**
- **p50 1.2s** → proxy'lerin yarısı 1.2 saniyeden hızlı (medyan latency)
- **p95 4.1s** → proxy'lerin %95'i 4.1 saniyeden hızlı (worst-case'in alt sınırı)
- p95 ile p50 arasındaki büyük fark "outlier'lar var" demektir
- p95 timeout'a yakınsa (`-T 5` iken p95 4.5s gibi) liste zar zor sığıyor demektir

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
# CONNECT-yetkili + üç gatekeeper'a geçen proxy'leri al
proxine http -s | proxyprof http \
  -j https://yours.tld/proxyjudge.php \
  --tunnel-test \
  -a https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org \
  -l 1 \
  -o production-ready.lst
```

Bu komut size production'a koymadan önce her açıdan elenmiş bir proxy listesi
verir: elite anonimlik + HTTPS tüneli + 3 farklı gatekeeper'a erişim + ülke
dağılımı raporu.

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

------------------------------------------------------------

## Mimari

```
proxy-profiler/
├── proxyprof.py    # Async scanner + CLI (tek dosya, ~600 satır)
├── judges.py       # Judge listesi + response parser + seviye + distorting + country
├── proxyjudge.php  # Opsiyonel CF-aware judge — kendi domain'inde host et
├── pyproject.toml  # aiohttp + aiohttp-socks bağımlılıkları
└── README.md
```

- **`judges.py`** judge URL listesi, judge yanıtının iki olası formatını
  (`<pre>KEY=VALUE</pre>` ve düz JSON) ayrıştırır, public IP + header
  sözlüğünden seviye çıkarır.
- **`proxyprof.py`** her proxy için tek bir `aiohttp_socks.ProxyConnector`
  açar, `asyncio.Semaphore(N)` ile eşzamanlılığı sınırlar; sonuçlar tek bir
  `gather` ile toplanır.

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
