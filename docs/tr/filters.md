# Süzgeçler ve metrikler

> ← [Ana README'ye dön](../../README.tr.md)

Anonimlik dışındaki üç ekstra süzgeç (`--tunnel-test`, `-a`, hız) ve iki ekstra metrik (timing percentiles, ülke dağılımı) — hepsi proxine boru hattının ötesinde ham listeyi gerçek üretim kalitesine indirgemeye yarar.

## HTTPS tünel testi (default açık)

**Neden:** Bir HTTP proxy düz HTTP isteklerini iletiyor olabilir ama HTTPS için gereken `CONNECT` komutunu desteklemiyor olabilir. Bugün neredeyse her site HTTPS olduğundan, CONNECT-yetkisi olmayan HTTP proxy pratik olarak çoğu hedefe işe yaramaz.

**Ne yapar:** Her HTTP/HTTPS proxy için ek bir istek atar: `https://www.gstatic.com/generate_204`. 204 dönerse CONNECT destekleniyor demektir. SOCKS proxy'leri doğası gereği tünel kurar; otomatik geçilir, ek istek yapılmaz.

**Maliyet:** Tarama süresi yaklaşık 2 katına çıkar (HTTP/HTTPS proxy'leri için proxy başına 2 istek). Concurrency artırılarak telafi edilebilir.

**Kullanım:** Default **açık**. Kapatmak için `--no-tunnel-test`.

**Sonuç:**
- Stdout'a (veya `-o` dosyasına) sadece tünel testini geçen proxy'ler yazılır
- Canlı tabloda `TUNNEL` sütunu: `✓` / `×` / `—`
- Özet kutuda: `tunnel │ 118 CONNECT-capable (of 197 good)`

## Çoklu gatekeeper erişim testi (`--access-test`)

**Neden:** Bir proxy Cloudflare'i geçebilir ama Google CAPTCHA gösterebilir, veya tersine. "Her yerden çalışan" proxy'leri ayıklamak için tek bir gatekeeper yeterli değil.

**Ne yapar:** Verdiğiniz URL listesinin **hepsine** proxy üzerinden istek atar. Tek bir URL bile fail ederse proxy "blocked" sayılır.

**Kullanım — iki mod:**

```bash
# Otomatik: dahili CF listesinden 3 rastgele site (her tarama farklı seçim)
proxyprof http -f raw.lst --access-test

# Manuel: kendi gatekeeper'larını ver (virgülle, hepsi http(s):// ile)
proxyprof http -f raw.lst \
  --access-test https://www.cloudflare.com,https://www.google.com,https://www.wikipedia.org
```

Dahili CF listesi: cloudflare.com, discord.com, reddit.com, medium.com, udemy.com, patreon.com, kickstarter.com, upwork.com, zendesk.com, shopify.com — hepsinin `/cdn-cgi/trace` endpoint'i kullanılır (her CF site'da mevcut, 200 döner, UA filtrelemez).

**Sonuç:**
- Sadece tüm URL'lere ulaşan proxy'ler stdout'a düşer
- Canlı tabloda `ACCESS` sütunu: `✓` / `×` / `—`
- Özet kutuda: `blocked │ 24 access denied`

## Hız metrikleri (otomatik)

**Neden:** Bir proxy "çalışıyor" demek hızlı olduğu anlamına gelmez. Ortalama (mean) çoğu zaman yanıltıcıdır: bir-iki yavaş outlier ortalamayı şişirir. Bunun yerine **yüzdelik** (percentile) kullanılır.

**Yüzdelik ne demek?**
- **p50** (medyan): verinin yarısı bu değerden küçük.
- **p95**: verinin %95'i bu değerden küçük, sadece %5'i daha yavaş — worst-case eşiği.

**Somut bir örnek.** 10 proxy taradın ve süreleri:
```
0.4, 0.6, 0.8, 0.9, 1.1, 1.3, 1.5, 2.0, 3.5, 8.0
```
- Ortalama = **2.01s** — ama 9 proxy bu değerden hızlı! Outlier'a (8.0) yenildi.
- p50 = **1.2s** — gerçek tablo.
- p95 = **8.0s** — en kötü %5'in eşiği.

**Sonuç:**

```
timing │ p50 1.2s · p95 4.1s
```

**Nasıl okunur:**
- **p50 ≈ p95** → tutarlı, hızlı liste. İdeal.
- **p95 >> p50** → çoğu hızlı ama uzun bir yavaş kuyruk var.
- **p95 ≈ timeout** → liste zar zor sığıyor; `-T` artırırsan daha çok proxy "good" olur.
- **p50 yüksek** → liste genel olarak yavaş.

## Geolokasyon (CF judge ile, ücretsiz)

**Neden:** Çoğunlukla sadece belirli ülkelerdeki proxy'ler işe yarar. Geolokasyon genellikle MaxMind DB indirmek veya rate-limited API çağırmak demektir — ek karmaşıklık.

**Ne yapar:** Cloudflare her proxy'den gelen isteğin IP'sini çözer ve `CF-IPCountry` header'ı ekler. CF-aware judge (`proxyjudge.php`) bu header'ı yakalayıp `PROXY_COUNTRY` alanı olarak yansıtır. Proxyprof bunu otomatik çıkartır. **Ek API çağrısı, ek bağımlılık, ek dosya yok.**

**Kullanım:**

```bash
proxyprof http -f raw.lst -j https://yours.tld/proxyjudge.php
```

**Sonuç:**

```
country │ TR=42 US=28 DE=21 RU=18 BR=14  +74 more
```

**Önemli:** Public azenv judge'larında country bilgisi yok — sadece CF-aware judge ile çalışır.

## Hepsini birlikte

```bash
# proxine'den taze HTTP proxy → kendi CF judge'una karşı test → sadece elite +
# CONNECT-yetkili + 3 rastgele CF gatekeeper'a geçen proxy'leri al.
proxine -p http -s | proxyprof http \
  -j https://yours.tld/proxyjudge.php \
  --access-test \
  -o production-ready.lst
```
