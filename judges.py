"""
Judge URL'leri ve proxy üzerinden gelen yanıttan anonimlik seviyesi çıkarma.

Bir "judge" tipik olarak `azenv.php` veya `env.cgi` benzeri bir betiktir: kendisine
gelen HTTP request'in tüm header'larını geri yansıtır. Proxy üzerinden bu betiğe
istek attığımızda, header listesinde:

  - kendi public IP'mizi görüyorsak     → proxy şeffaf (TRANSPARENT, level 3)
  - via/forwarded-for benzeri header'lar → proxy anonim ama belli       (ANON, level 2)
  - hiçbiri yoksa                         → proxy elite                  (ELITE, level 1)

Public judge'lar zaman zaman ölür. `pick_judge()` listeyi sırayla dener ve ilk
yaşayanı döndürür; hiçbiri yanıt vermezse `JudgeUnavailable` fırlatır.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
import urllib.parse
from typing import Callable, Iterable

import aiodns
import aiohttp


# Modül-seviye paylaşımlı aiodns resolver. Non-blocking (c-ares tabanlı), thread
# executor'a girmez — `loop.getaddrinfo` ile yaşadığımız "wait_for cancel olsa da
# arkadaki blocking DNS thread pool'u tıkar" problemini bypass eder.
_dns_resolver: aiodns.DNSResolver | None = None


def _get_dns_resolver() -> aiodns.DNSResolver:
    global _dns_resolver
    if _dns_resolver is None:
        _dns_resolver = aiodns.DNSResolver()
    return _dns_resolver


async def _resolve_host(host: str, timeout: float) -> list[str]:
    """Host için A + AAAA kayıtlarını paralel sorgula; IP listesi döner.

    aiodns iç timeout'u tutulduğu için `asyncio.wait_for` sarması gereksiz —
    zaten gerçek async (c-ares), wait_for cancel'da arka thread bırakmıyor.
    Yine de outer guard olarak wait_for kullanıyoruz (DNS sunucusu sessiz
    drop ederse aiodns retry pattern'inde takılı kalmasın).
    """
    resolver = _get_dns_resolver()

    async def _query(qtype: str) -> list[str]:
        try:
            recs = await asyncio.wait_for(
                resolver.query(host, qtype), timeout=timeout,
            )
        except (aiodns.error.DNSError, asyncio.TimeoutError, TimeoutError):
            return []
        return [r.host for r in recs]

    a_recs, aaaa_recs = await asyncio.gather(_query("A"), _query("AAAA"))
    return a_recs + aaaa_recs


# ---------------------------------------------------------------------------
# Cloudflare detection
# ---------------------------------------------------------------------------
# Tarama sonuçları açısından CF-arkası bir judge önemli ölçüde fark yaratır
# (bot management false-fail'leri, HTTPS-only erişim, ülke bilgisi). Bu yüzden:
#   1. Kullanıcı `-j` ile CF-arkası judge verirse → uyarı + onay
#   2. Default listemizi CF-dışı olanlarla kuruyoruz; runtime'da DNS check ile
#      yine de filtreliyoruz (judge bir gün CF arkasına geçerse otomatik elenir)
#
# CF'in yayınladığı IP aralıkları: https://www.cloudflare.com/ips-v4
# Hardcoded — değişimleri nadirdir; güncelse manuel update. Stale olmaması
# istenen ortamlarda env var ile override etmek mümkün ama default OK.
_CF_IPV4_RANGES: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
_CF_IPV6_RANGES: tuple[str, ...] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)
_CF_NETWORKS: tuple = tuple(
    ipaddress.ip_network(c) for c in _CF_IPV4_RANGES + _CF_IPV6_RANGES
)


def _ip_in_cf(ip_str: str) -> bool:
    """IP, yayınlanan CF range'lerinden birinde mi?"""
    try:
        addr = ipaddress.ip_address(ip_str)
    except (ValueError, ipaddress.AddressValueError):
        return False
    return any(addr in net for net in _CF_NETWORKS)


async def is_judge_behind_cf(
    url: str,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Bir judge URL'inin CF arkasında olup olmadığını tespit et.

    İki katmanlı:
      1. DNS lookup → IP'lerin biri CF range'inde mi (offline, hızlı, %99 vakaları
         yakalar — CF kullanan domain'in A kaydı CF IP'sine düşer)
      2. (Opsiyonel, session verilirse) HEAD request → response'da
         `Server: cloudflare` veya `CF-Ray` header'ı var mı (DNS başka bir
         IP'ye düşse bile CF reverse-proxy edge'i bunları ekler)

    Returns:
        (is_cf, evidence) — evidence kullanıcıya gösterilecek kısa kanıt.
        is_cf False ise evidence boş.
    """
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return False, ""

    # 1) DNS-based check — aiodns ile non-blocking (c-ares). `loop.getaddrinfo`
    # blocking executor thread'i kullanır ve `wait_for` cancel olsa bile thread
    # arka planda sürer → thread pool tıkanır. aiodns gerçek async olduğu için
    # bu sorunu yaşamıyoruz.
    ips = await _resolve_host(host, timeout=timeout)
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except (ValueError, ipaddress.AddressValueError):
            continue
        for net in _CF_NETWORKS:
            if addr in net:
                return True, f"DNS {host} → {ip} ∈ {net}"

    # 2) Header check (varsa session ile) — CDN front yapısı veya non-CF IP'den
    #    geçen ama yanıtı CF üreten konfigürasyonlar için fallback.
    if session is not None:
        try:
            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            async with session.head(
                url, allow_redirects=True, timeout=timeout_obj,
            ) as resp:
                server = (resp.headers.get("Server") or "").lower()
                cf_ray = resp.headers.get("CF-Ray", "")
                if "cloudflare" in server:
                    return True, f"Response 'Server: {server}'"
                if cf_ray:
                    return True, f"Response 'CF-Ray: {cf_ray}'"
        except (aiohttp.ClientError, TimeoutError, OSError):
            pass

    return False, ""

# X-Forwarded-For tarzı header'lar — `Distorting` tespit edicisi sadece BUNLARI
# inceler. `Via` veya `Proxy-Connection` gibi yapısal header'ların IP yazması
# beklenmez; sahte IP enjeksiyonu yalnızca chain-tabanlı forwarded-for
# header'larında anlamlıdır.
FORWARDED_HEADERS: frozenset[str] = frozenset({
    "forwarded", "forwarded-for", "forwarded-for-ip",
    "x-forwarded", "x-forwarded-for", "x-forwarded-for-ip",
    "x-forwarded-for-host", "x-real-ip",
    "http-forwarded", "http-forwarded-for", "http-forwarded-for-ip",
    "http-x-forwarded", "http-x-forwarded-for", "http-x-forwarded-for-ip",
    "client-ip", "http-client-ip", "x-client-ip",
    "x-proxyuser-ip",
})

# Proxy varlığını ele veren header'lar — `<pre>KEY=VALUE</pre>` formatı için
# tireli, JSON formatı için underscore'lu varyantları da içerir.
PROXY_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "client-ip", "client_ip",
    "http-client-ip", "http_client_ip",
    "forwarded",
    "forwarded-for", "forwarded_for",
    "forwarded-port", "forwarded_port",
    "forwarded-for-ip", "forwarded_for_ip",
    "http-from", "http_from",
    "x-http-from", "x_http_from",
    "http-proxy-connection", "http_proxy_connection",
    "http-x-forwarded", "http_x_forwarded",
    "http-x-forwarded-for", "http_x_forwarded_for",
    "http-x-forwarded-port", "http_x_forwarded_port",
    "http-x-forwarded-proto", "http_x_forwarded_proto",
    "http-x-proxy-id", "http_x_proxy_id",
    "http-proxy-agent", "http_proxy_agent",
    "x-proxyuser-ip", "x_proxyuser_ip",
    "proxy-authorization", "proxy_authorization",
    "proxy-authenticate", "proxy_authenticate",
    "proxy-connection", "proxy_connection",
    "via",
    "http-via", "http_via",
    "x-http-via", "x_http_via",
    "x-client-ip", "x_client_ip",
    "x-forwarded", "x_forwarded",
    "x-forwarded-for", "x_forwarded_for",
    "x-forwarded-port", "x_forwarded_port",
    "x-forwarded-for-ip", "x_forwarded_for_ip",
    "x-forwarded-for-host", "x_forwarded_for_host",
    "x-real-ip", "x_real_ip",
})


HTTP_JUDGES: tuple[str, ...] = (
    # tankado.com: bu repo'nun trusted self-host judge'ı. _TRUSTED_JUDGE_DOMAINS
    # ile eşleşir → X-Proxyprof-Proxy header'ı gönderilir, judge tarafında
    # ziyaret eden proxy'lerin protocol/IP/port bilgisi loglanır.
    "https://tankado.com/projects/proxy_detect/proxyjudge.php",
    "http://httpheader.net/azenv.php",
    "http://azenv.net",
    "http://www.meow.org.uk/cgi-bin/env.pl",
    "http://proxyjudge.biz",
    "http://proxyjudge.us/",
    "http://users.on.net/~emerson/env/env.pl",
    "http://shinh.org/env.cgi",
    "http://www3.wind.ne.jp/hassii/env.cgi",
    "http://proxyjudge.info/azenv.php",
)

HTTPS_JUDGES: tuple[str, ...] = (
    "https://tankado.com/projects/proxy_detect/proxyjudge.php",
    "https://httpheader.net/azenv.php",
    "https://wfuchs.de/azenv.php",
    "https://proxyjudge.biz",
)


def judges_for(protocol: str) -> tuple[str, ...]:
    """Protokol için aday judge listesini döndürür."""
    p = protocol.lower()
    if p == "https":
        return HTTPS_JUDGES
    # http / socks4 / socks5 hepsinde HTTP judge yeterli — SOCKS sadece
    # taşıma katmanını değiştirir, judge HTTP olarak konuşur.
    return HTTP_JUDGES


class JudgeUnavailable(RuntimeError):
    """Tek bir yaşayan judge bile bulunamadığında fırlar."""


# `<pre>` blok'undan KEY=VALUE çizgileri çekmek için. azenv.php legacy çıktısı
# bu formatı kullanır (Perl env.cgi de aynı).
_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.IGNORECASE | re.DOTALL)


def parse_judge_response(body: str) -> dict[str, str]:
    """Judge yanıt gövdesini header_name → value sözlüğüne çevir.

    İki format desteklenir:
      1. `<pre>KEY: VALUE</pre>` veya `<pre>KEY=VALUE</pre>` (azenv.php, env.cgi)
      2. Düz JSON object (modern azenv varyantları)

    Anahtar isimleri lowercase'e indirilir; ":" ve "=" ayraçların ikisi de kabul.
    """
    body = body.strip()
    if not body:
        return {}

    out: dict[str, str] = {}

    # Önce JSON dene — modern azenv.php (örn. bu repodaki self-host varyantı)
    # Content-Type: application/json döner.
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            pass
        else:
            if isinstance(data, dict):
                out = {str(k).lower().strip(): str(v) for k, v in data.items()}

    if not out:
        # `<pre>...</pre>` blok'u var mı? Varsa içeriği al; yoksa tüm body'yi tara.
        m = _PRE_RE.search(body)
        raw = m.group(1) if m else body
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Önce ":" sonra "=" — env.cgi her ikisini de üretiyor.
            if ":" in line:
                key, _, val = line.partition(":")
            elif "=" in line:
                key, _, val = line.partition("=")
            else:
                continue
            key = key.strip().lower()
            val = val.strip()
            if key:
                out[key] = val

    # Sağlamlık kontrolü: gerçek env-dump judge'lar (azenv.php, env.cgi, env.pl)
    # daima REMOTE_ADDR döker — proxy'nin gördüğümüz çıkış IP'si. Yoksa parse
    # düşmüş demektir. Park-sayfa redirect HTML'i ("...window.onload=function...")
    # `=` split'iyle çöp anahtar üretir; `if not out` filtresi geçer ama bizim
    # için işe yaramaz. Bu tür "yaşıyor gibi görünen ölü" judge'ları burada
    # eleyince pick_judge sıradakine geçer, taramada outbound=None / DURUM=eksik
    # salgını olmaz.
    if "remote_addr" not in out and "remote-addr" not in out:
        return {}
    return out


# Bir string içinde geçen ilk IPv4 adresini bulmak için — `X-Forwarded-For`
# chain'leri "1.2.3.4, 5.6.7.8" veya "for=1.2.3.4;proto=http" formatlarında
# olabilir; tek tek parse etmek yerine regex extraction yapıyoruz.
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IP_FIND_RE = re.compile(rf"\b({_OCTET}(?:\.{_OCTET}){{3}})\b")


def _is_routable_ipv4(s: str) -> bool:
    """RFC1918/127.x/0.0.0.0/multicast/link-local DEĞIL → True. Bunlar fake IP
    enjekte etmek için anlamsız değerler, distorting tespiti için elemeli."""
    try:
        return ipaddress.IPv4Address(s).is_global
    except (ipaddress.AddressValueError, ValueError):
        return False


def _matches_proxy_header(key: str) -> bool:
    """Anahtar PROXY_HEADERS içinde mi (tire/underscore varyantları dahil)?"""
    return (
        key in PROXY_HEADERS
        or key.replace("_", "-") in PROXY_HEADERS
        or key.replace("-", "_") in PROXY_HEADERS
    )


def _is_forwarded_header(key: str) -> bool:
    return (
        key in FORWARDED_HEADERS
        or key.replace("_", "-") in FORWARDED_HEADERS
        or key.replace("-", "_") in FORWARDED_HEADERS
    )


def detect_level(
    headers: dict[str, str], public_ip: str,
) -> tuple[int, bool]:
    """Proxy seviyesini ve `distorting` alt türünü belirle.

    Returns:
        (level, distorting)
        level:
            1 — Elite (ne IP sızıyor ne proxy header'ı var)
            2 — Anonymous (IP sızmıyor, proxy header'ı var)
            3 — Transparent (public IP herhangi bir header'da yansıyor)
        distorting:
            True iff level==2 AND forwarded-for-style bir header'da public_ip'den
            farklı, **routable** (RFC1918/loopback DEĞİL) bir IPv4 var. Yani
            proxy gerçek IP'yi gizleyip yerine sahte ama "internet-IP gibi
            görünen" bir adres enjekte ediyor.
    """
    # Transparent: gerçek IP herhangi bir header VALUE'ında geçiyor.
    if public_ip:
        for v in headers.values():
            if public_ip in v:
                return 3, False

    proxy_header_present = False
    distorting = False
    for k, v in headers.items():
        if not _matches_proxy_header(k):
            continue
        proxy_header_present = True
        if not _is_forwarded_header(k):
            continue
        # Distorting kararı `ip != public_ip` karşılaştırmasına dayanır.
        # public_ip boşsa (canhazip.com erişilemedi vs.) bu karşılaştırma
        # anlamsız — her routable IP "bizim değil" sayılır ve TÜM L2'ler
        # L2d gibi görünür. Bu false-positive'i önlemek için sessizce atla;
        # level=2 olarak kal, distorting False kalır. Header bazında değil
        # detection bazında pasifleştirme: ilk routable IP "doğru sınıflandırma
        # yapılamıyor" anlamına gelir, "distorting değil" demez.
        if not public_ip:
            continue
        # forwarded-for-style: içerideki her IP'yi tara; biri bile routable
        # ve public_ip değilse → distorting.
        for ip_str in _IP_FIND_RE.findall(v):
            if ip_str == public_ip:
                continue
            if _is_routable_ipv4(ip_str):
                distorting = True
                break

    if proxy_header_present:
        return 2, distorting
    return 1, False


def remote_addr(headers: dict[str, str]) -> str | None:
    """Judge'ın gördüğü çıkış IP'si (proxy'nin dış adresi)."""
    for key in ("remote_addr", "remote-addr"):
        v = headers.get(key)
        if v:
            return v.strip()
    return None


def extract_country(headers: dict[str, str]) -> str | None:
    """CF-aware judge'lardan ISO ülke kodu çek. Yoksa None.

    Önce custom field `PROXY_COUNTRY` (proxyjudge.php ekler), sonra ham
    `CF-IPCountry` (kullanıcı kendi judge'unu yazarsa). Değer 2 harfli ISO
    alpha-2 olmalı; "XX" (CF'ın bilinmeyen değeri) None'a düşürülür.
    """
    for key in ("proxy_country", "cf_ipcountry", "country", "geo_country"):
        v = headers.get(key)
        if not v:
            continue
        v = v.strip().upper()
        if len(v) == 2 and v.isalpha() and v != "XX":
            return v
    return None


def country_from_trace(body: str) -> str | None:
    """Cloudflare `/cdn-cgi/trace` yanıtından `loc=XX` çek; ISO alpha-2 dön.

    Trace body formatı düz metin, satır başına `key=value`:
        fl=87f47
        h=www.cloudflare.com
        ip=88.247.X.X
        ts=1727654321.123
        ...
        loc=TR       ← ziyaretçi IP'sinin (proxy çıkışı) ülke kodu
        ...

    Bu CF-IPCountry header'ı ile aynı bilgi — CF kendi geo DB'sinden üretir.
    Proxy üzerinden CF'e giden istekte ziyaretçi = proxy çıkış IP'si, yani
    `loc=` = proxy'nin ülkesi. Main judge non-CF olsa bile bu sayede ülke
    bilgisi yakalanabilir (access-test cloudflare preset'inde her CF site
    /cdn-cgi/trace döndürür).

    "XX" (CF unknown) → None. 2-harfli alfa olmayanlar → None.
    """
    if not body:
        return None
    for line in body.splitlines():
        if line.startswith("loc="):
            v = line[4:].strip().upper()
            if len(v) == 2 and v.isalpha() and v != "XX":
                return v
            return None
    return None


async def pick_judge(
    session: aiohttp.ClientSession,
    candidates: Iterable[str],
    timeout: float,
    on_attempt: Callable[[str, bool, str, float], None] | None = None,
) -> tuple[str, str]:
    """Aday judge'ları PARALEL tara; ilk başarılı `(url, body)` döner, kalanlar
    iptal edilir.

    Eski versiyon sıralıydı: shuffle'lı listede ilk 2-3 judge ölüyse her biri
    `timeout` boyunca asılırdı (3 × 10s = 30s bootstrap). Şimdi tüm aday'lar
    aynı anda çağrılır; ilk parseable 200 alındığında diğer task'lar cancel
    edilir. Bootstrap = max(1 judge'ın süresi) ≈ tipik <1s.

    Yan etki: önyük olarak adayların hepsine birer istek gider (9 istek tek
    seferde). Bootstrap'ta bir kez olduğu için kabul edilebilir.

    `on_attempt(url, ok, reason, elapsed)`: opsiyonel callback. Her aday
    sonuçlandığında (success ya da failure) çağrılır → caller streaming log
    yazabilir. Cancel edilen task'lar için çağrılmaz (kazanan belirlenince
    kalan task'lar drop edilir).
    """
    candidates = list(candidates)
    if not candidates:
        # Caller bu durumu zaten engellemeli; defensive.
        raise JudgeUnavailable("no judge candidates supplied")

    async def _try(url: str) -> tuple[str, str]:
        t0 = time.monotonic()
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    elapsed = time.monotonic() - t0
                    if on_attempt is not None:
                        on_attempt(url, False, f"HTTP {resp.status}", elapsed)
                    raise RuntimeError(f"HTTP {resp.status}")
                body = await resp.text(errors="replace")
                elapsed = time.monotonic() - t0
                if not parse_judge_response(body):
                    if on_attempt is not None:
                        on_attempt(url, False, "unparseable body", elapsed)
                    raise RuntimeError("empty/unparseable body")
                if on_attempt is not None:
                    on_attempt(url, True, "ok", elapsed)
                return url, body
        except asyncio.CancelledError:
            raise
        except Exception as e:
            elapsed = time.monotonic() - t0
            if on_attempt is not None:
                reason = f"{type(e).__name__}: {e}".strip(": ") or type(e).__name__
                # Eğer success/HTTP/unparse path'lerinde zaten raporlandıysa,
                # caller dedup yapsın diye outer'da ikinci kez basmıyoruz.
                # Pratikte: yukarıdaki iki callback noktası `raise RuntimeError`
                # ile bu except'e düşer ama o RuntimeError'lar zaten raporlandı.
                # TimeoutError/OSError/aiohttp.ClientError ise bu noktaya
                # callback'siz gelir → burada raporla.
                if not isinstance(e, RuntimeError):
                    on_attempt(url, False, reason, elapsed)
            raise

    tasks = [asyncio.create_task(_try(u)) for u in candidates]
    last_err: BaseException | None = None
    try:
        # as_completed: task'lar tamamlandıkça yield et; ilk başarıyı yakala.
        for fut in asyncio.as_completed(tasks):
            try:
                url, body = await fut
                # Başarı — kalan task'ları iptal et ve dön.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return url, body
            except (aiohttp.ClientError, TimeoutError, OSError,
                    RuntimeError) as e:
                last_err = e
                continue
    finally:
        # Erken çıkış / exception durumunda da temizle.
        for t in tasks:
            if not t.done():
                t.cancel()
        # Cancelled task'ların exception'larını sessizce yut (event loop
        # warning'i çıkmasın).
        for t in tasks:
            if t.cancelled():
                continue
            exc = t.exception() if t.done() else None
            del exc
    # i18n burada lazy import — judges.py'yi bağımsız test edenler için.
    try:
        from i18n import t as _t
        n = len(tuple(candidates))
        if last_err is not None:
            msg = _t("input.judge_unavailable_with_err", n=n, err=last_err)
        else:
            msg = _t("input.judge_unavailable", n=n)
    except ImportError:
        msg = (
            f"no usable judge among {len(tuple(candidates))} candidates"
            + (f": {last_err}" if last_err else "")
        )
    raise JudgeUnavailable(msg)
