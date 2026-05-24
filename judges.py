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

import ipaddress
import json
import re
from typing import Iterable

import aiohttp

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

    # Önce JSON dene — modern azenv.php (örn. bu repodaki self-host varyantı)
    # Content-Type: application/json döner.
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            pass
        else:
            if isinstance(data, dict):
                return {str(k).lower().strip(): str(v) for k, v in data.items()}

    # `<pre>...</pre>` blok'u var mı? Varsa içeriği al; yoksa tüm body'yi tara.
    m = _PRE_RE.search(body)
    raw = m.group(1) if m else body

    out: dict[str, str] = {}
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


async def pick_judge(
    session: aiohttp.ClientSession,
    candidates: Iterable[str],
    timeout: float,
) -> tuple[str, str]:
    """Aday judge'ları sırayla proxysiz tara; ilk başarılı olanı `(url, body)` ver.

    `body` daha sonra `parse_judge_response` ile sözlüğe çevrilebilir biçimde
    döner; bu sayede çağıran kod judge'ı validate etmek için ekstra istek
    atmak zorunda kalmaz.
    """
    last_err: Exception | None = None
    for url in candidates:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    last_err = RuntimeError(f"HTTP {resp.status}")
                    continue
                body = await resp.text(errors="replace")
                if not parse_judge_response(body):
                    last_err = RuntimeError("empty/unparseable body")
                    continue
                return url, body
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            last_err = e
            continue
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
