#!/usr/bin/env python3
"""
proxyprof — async proxy scanner. Proxine'in boru hattı tamamlayıcısı.

stdin'den (veya -f FILE'dan) IP:PORT proxy listesi okur; her birini bir judge
URL'e yönlendirerek canlı / anonim / elite / transparent sınıflandırması yapar.
Filtreyi geçen proxy'leri stdout'a sıralı, dedupe edilmiş halde yazar.

Boru hattı örneği:
    proxine http -s | proxyprof http -l 1 -o working.lst
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import i18n
# Dil tespiti _ensure_deps'ten ÖNCE yapılır ki bootstrap mesajları da
# çevrilebilsin. --lang/-L CLI > PROXYPROF_LANG env > sistem locale > en.
i18n.set_language(i18n.pre_parse_lang(sys.argv[1:]))
from i18n import t  # noqa: E402

# PyPI paket adı modül adından bazen farklı (aiohttp_socks → aiohttp-socks).
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("aiohttp", "aiohttp"),
    ("aiohttp_socks", "aiohttp-socks"),
)
_GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def _missing_packages() -> list[str]:
    missing: list[str] = []
    for module, pkg in _REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)
    return missing


def _prompt(question: str, default_yes: bool) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    if not ans:
        return default_yes
    return ans in ("y", "yes", "e", "evet")


def _project_venv_python() -> Path:
    return Path(__file__).resolve().parent / ".venv" / "bin" / "python"


def _try_reexec_into_local_venv() -> None:
    """Yerel ./.venv'de bağımlılıklar zaten kurulu mu? Öyleyse oraya geç.

    Kullanıcı `python3 proxyprof.py` derken aktive edilmemiş yerel bir venv'in
    içinde bağımlılıklar olabilir. Sessiz bir probe ile teyit edip o
    interpreter'la yeniden başlatırız — kullanıcıya soru sorulmaz.
    """
    venv_py = _project_venv_python()
    if not venv_py.exists():
        return
    # Symlink çözmeden ham path karşılaştırması: Pardus'ta .venv/bin/python
    # genelde /usr/bin/python3.11'e resolve eder, ama venv'in sys.prefix
    # değişimi binary'nin KONUMUNA göre tetiklenir. Eşit ham path = exec'i atla.
    if Path(sys.executable) == venv_py:
        return
    probe = subprocess.run(
        [str(venv_py), "-c", "import aiohttp, aiohttp_socks"],
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        os.execv(str(venv_py), [str(venv_py), *sys.argv])


def _bootstrap_local_venv(venv_dir: Path, packages: list[str]) -> None:
    """Sıfırdan: `python3 -m venv --without-pip` → get-pip.py → pip install.

    Hiçbir adım sudo gerektirmez; tüm hedef proje klasöründeki `.venv`'dir.
    """
    venv_py = venv_dir / "bin" / "python"

    if not venv_py.exists():
        # Önce normal (pip dahil) venv oluştur; ensurepip sistemde yoksa
        # --without-pip ile dener ve sonra get-pip.py bootstrap'lar.
        sys.stderr.write(f"proxyprof: {t('deps.creating_venv', dir=venv_dir)}\n")
        rc = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            stderr=subprocess.DEVNULL,
        ).returncode
        if rc != 0 or not (venv_dir / "bin" / "pip").exists():
            rc = subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
            ).returncode
            if rc != 0 or not venv_py.exists():
                sys.stderr.write(
                    f"proxyprof: {t('deps.venv_creation_failed')}\n"
                    f"{t('deps.venv_hint')}\n"
                )
                sys.exit(1)

    if not (venv_dir / "bin" / "pip").exists():
        sys.stderr.write(f"proxyprof: {t('deps.downloading', url=_GET_PIP_URL)}\n")
        try:
            with urllib.request.urlopen(_GET_PIP_URL, timeout=30) as resp:
                get_pip_src = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            sys.stderr.write(
                f"proxyprof: {t('deps.download_failed', err=e)}\n"
                f"{t('deps.download_failed_hint')}\n"
            )
            sys.exit(1)
        sys.stderr.write(f"proxyprof: {t('deps.bootstrapping_pip')}\n")
        if subprocess.run([str(venv_py)], input=get_pip_src).returncode != 0:
            sys.stderr.write(f"proxyprof: {t('deps.pip_bootstrap_failed')}\n")
            sys.exit(1)

    sys.stderr.write(f"proxyprof: {t('deps.installing', pkgs=' '.join(packages))}\n")
    if subprocess.run(
        [str(venv_py), "-m", "pip", "install", "--quiet", *packages]
    ).returncode != 0:
        sys.stderr.write(f"proxyprof: {t('deps.pip_install_failed')}\n")
        sys.exit(1)


def _ensure_deps() -> None:
    """Eksik bağımlılıkları tek bir soruda hallet.

    Sırası:
    1. Yerel `./.venv` bağımlılıkları içeriyorsa sessizce o Python'a geç.
    2. TTY değilse → statik hata mesajıyla çık (boru hattını şaşırtma).
    3. TTY ise tek soru:
       - pip mevcut → `pip install` çalıştır (gerekirse PEP 668 fallback'i için
         ikinci bir onay sorulur).
       - pip yoksa → yerel venv + get-pip.py bootstrap + pip install.
       İki dalın da sonunda süreç `os.execv` ile temiz biçimde yeniden başlar.
    """
    if not _missing_packages():
        return

    _try_reexec_into_local_venv()  # Sessiz fast-path. Dönerse hala eksiğimiz var.

    missing = _missing_packages()
    if not missing:
        return
    pkg_str = " ".join(missing)

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        sys.stderr.write(
            f"proxyprof: {t('deps.missing', pkgs=pkg_str)}\n"
            f"{t('deps.install_with', cmd=f'{sys.executable} -m pip install {pkg_str}')}\n"
        )
        sys.exit(1)

    in_venv = sys.prefix != sys.base_prefix

    if in_venv:
        # Aktif venv'deyiz — sistem Python'unu kirletme riski yok, direkt pip.
        sys.stderr.write(
            f"proxyprof: {t('deps.missing', pkgs=pkg_str)}\n"
            f"{t('deps.active_venv_note', prefix=sys.prefix)}\n"
        )
        if not _prompt(
            t("deps.install_prompt", pkgs=pkg_str), default_yes=True,
        ):
            sys.exit(1)
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing]
        sys.stderr.write(f"proxyprof: {t('deps.running', cmd=' '.join(cmd))}\n")
        if subprocess.run(cmd).returncode != 0:
            sys.stderr.write(f"proxyprof: {t('deps.install_failed')}\n")
            sys.exit(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    # Sistem Python'dayız: tek doğru yol yerel venv. PEP 668'i baştan bypass
    # eder, sudo gerektirmez, sistem paketlerini kirletmez. pip yoksa
    # get-pip.py ile bootstrap edilir; varsa direkt kullanılır.
    venv_dir = Path(__file__).resolve().parent / ".venv"
    sys.stderr.write(
        f"proxyprof: {t('deps.missing', pkgs=pkg_str)}\n"
        f"{t('deps.system_python_note', python=sys.executable)}\n"
        f"{t('deps.auto_setup_intro', dir=venv_dir, pkgs=pkg_str)}\n"
    )
    if not _prompt(t("deps.proceed_prompt"), default_yes=True):
        sys.exit(1)

    _bootstrap_local_venv(venv_dir, missing)
    venv_py = venv_dir / "bin" / "python"
    os.execv(str(venv_py), [str(venv_py), *sys.argv])


_ensure_deps()

import aiohttp  # noqa: E402
from aiohttp_socks import ProxyConnector, ProxyType  # noqa: E402

from judges import (  # noqa: E402
    JudgeUnavailable,
    detect_level,
    extract_country,
    judges_for,
    parse_judge_response,
    pick_judge,
    remote_addr,
)

from reputation import (  # noqa: E402
    BUCKET_COLD,
    BUCKET_HOT,
    BUCKET_NEW,
    BUCKET_WARM,
    BUCKETS,
    DEFAULT_DEAD_THRESHOLD,
    DEFAULT_PROBATION_MAX_SKIP,
    DEFAULT_WEIGHTS,
    Reputation,
    classify,
    default_db_path,
    now_epoch,
    should_test_now,
    weighted_interleave,
)


DEFAULT_LEVEL = 1
DEFAULT_CONCURRENCY = 500
DEFAULT_TIMEOUT = 3.0
DEFAULT_RETRIES = 1
# COLD bucket için kısa timeout: zaten %90+'ı timeout'a düşecek, beklemeye
# gerek yok. HOT/WARM/NEW normal --timeout'u kullanır.
DEFAULT_COLD_TIMEOUT = 2.0

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"
)

# HTTPS CONNECT tünel testi için kullanılır. Google'ın generate_204 endpoint'i:
# 204 No Content döner, body sıfır byte. Hızlı, kararlı, header'lerde
# bilgi sızdırmaz. SOCKS proxy'leri zaten tünel'er, sadece http/https
# proxy'lerde anlamlı bir sınav.
TUNNEL_TEST_URL = "https://www.gstatic.com/generate_204"

# `--access-test` bayrağı değer almadan kullanılırsa bu listeden rastgele 3 URL
# seçilir. Hepsi /cdn-cgi/trace endpoint'i — her Cloudflare-korumalı sitede
# mevcuttur, 200 döner, UA filtresi uygulamaz, ~200B body sıfır cost'a yakın.
# Birden fazla farklı CF zone'una karşı test etmek "her CF sitede çalışıyor"
# güvencesi verir.
CF_GATEKEEPERS: tuple[str, ...] = (
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.discord.com/cdn-cgi/trace",
    "https://www.reddit.com/cdn-cgi/trace",
    "https://www.medium.com/cdn-cgi/trace",
    "https://www.udemy.com/cdn-cgi/trace",
    "https://www.patreon.com/cdn-cgi/trace",
    "https://www.kickstarter.com/cdn-cgi/trace",
    "https://www.upwork.com/cdn-cgi/trace",
    "https://www.zendesk.com/cdn-cgi/trace",
    "https://www.shopify.com/cdn-cgi/trace",
)
ACCESS_AUTO_COUNT = 3
ACCESS_AUTO_SENTINEL = "AUTO"


# X-Proxyprof-Proxy header'ının gönderileceği SABİT domain listesi. Hardcoded;
# CLI/env override DESTEKLENMEZ — yanlışlıkla başka bir judge'a kimlik
# sızdırma riskini fiziksel olarak imkânsız kılar. Bu repoyu fork edenler
# kendi domain'lerini buraya ekleyebilir.
_TRUSTED_JUDGE_DOMAINS: tuple[str, ...] = (
    "tankado.com",
)


def _judge_accepts_proxyprof_header(url: str) -> bool:
    """Judge URL'inin domain'i hardcoded güvenilir listede mi?

    Match: `host == d` veya `host.endswith('.' + d)` (subdomain). Port
    önemsizdir. Domain sahipliği güveni belirler — başkasının kendi
    domain'ine proxyjudge.php deploy etmesi yine de header alamaz.
    """
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    if not netloc:
        return False
    host = netloc.split("@", 1)[-1].split(":", 1)[0].rstrip(".")
    for d in _TRUSTED_JUDGE_DOMAINS:
        if host == d or host.endswith("." + d):
            return True
    return False

# IPv4 oktet (0–255) + port (1–65535). Hem doğrulama hem ayıklama için.
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IP_PORT_RE = re.compile(rf"\b({_OCTET}(?:\.{_OCTET}){{3}}):([1-9]\d{{0,4}})\b")
_IPV4_RE = re.compile(rf"^{_OCTET}(?:\.{_OCTET}){{3}}$")

PROXY_TYPE = {
    "http":   ProxyType.HTTP,
    "https":  ProxyType.HTTP,   # "HTTPS proxy" = HTTP CONNECT tunneling
    "socks4": ProxyType.SOCKS4,
    "socks5": ProxyType.SOCKS5,
}


@dataclass
class ScanResult:
    proxy: str                  # "IP:PORT"
    ok: bool                    # bağlantı + judge yanıtı geldi mi
    level: int | None           # 1=elite, 2=anon, 3=transparent (ok=False → None)
    distorting: bool = False    # level==2 + sahte IP enjekte ediyor
    outbound_ip: str | None = None   # judge'ın gördüğü proxy çıkış IP'si
    country: str | None = None       # ISO-3166-1 alpha-2 (CF judge'tan)
    elapsed: float = 0.0        # saniye
    error: str | None = None    # ok=False ise hata özeti
    access_ok: bool | None = None    # -a verildiyse: tüm URL'lere ulaşıyor mu
    tunnel_ok: bool | None = None    # --tunnel-test: CONNECT testi geçti mi
    mitm_suspected: bool | None = None  # --mitm-test: TLS chain kırık mı (MITM)
    skipped: bool = False            # probe çalıştırılmadan kısayolla skip edildi
    bucket: str | None = None        # reputation bucket (HOT/WARM/NEW/COLD/None)


@dataclass
class ScanTask:
    """Tarama planının atomik birimi.

    Reputation entegrasyonuyla birlikte her proxy'nin kendi `timeout`'u (COLD
    bucket kısa, diğerleri normal) ve bir `bucket` etiketi (UI + sonuç
    metaverisi) olabilir. --no-reputation modunda hepsi aynı timeout'la girer
    ve bucket=None olur."""
    proxy: str
    timeout: float
    bucket: str | None = None


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def parse_proxies(text: str) -> list[str]:
    """Metni tara, geçerli IP:PORT'ları sırasıyla dedupe ederek döndür."""
    seen: set[str] = set()
    out: list[str] = []
    for m in IP_PORT_RE.finditer(text):
        ip, port_str = m.group(1), m.group(2)
        port = int(port_str)
        if not (1 <= port <= 65535):
            continue
        key = f"{ip}:{port}"
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def read_proxies(file_arg: str | None) -> list[str]:
    if file_arg in (None, "-", "STDIN"):
        if sys.stdin.isatty():
            sys.exit(f"proxyprof: {t('input.no_input')}")
        text = sys.stdin.read()
    else:
        try:
            with open(file_arg, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            sys.exit(f"proxyprof: {t('input.cannot_read', file=file_arg, err=e)}")
    proxies = parse_proxies(text)
    if not proxies:
        sys.exit(f"proxyprof: {t('input.no_valid_pairs')}")
    return proxies


def _looks_like_ipv4(s: str) -> bool:
    return bool(_IPV4_RE.match(s))


# ---------------------------------------------------------------------------
# Network primitives
# ---------------------------------------------------------------------------

async def get_public_ip(session: aiohttp.ClientSession, timeout: float) -> str:
    """canhazip.com'dan istemcinin gerçek public IP'sini al. Hata → boş string."""
    try:
        async with session.get(
            "https://canhazip.com/",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = (await resp.text()).strip()
            if _looks_like_ipv4(text):
                return text
    except (aiohttp.ClientError, TimeoutError, OSError):
        pass
    return ""


async def probe(
    proxy: str,
    protocol: str,
    judge_url: str,
    timeout: float,
    retries: int,
    public_ip: str,
    access_urls: list[str],
    tunnel_test: bool,
    send_identity: bool,
) -> ScanResult:
    """Bir proxy'yi judge'a yönlendirerek profile et.

    Args:
        access_urls: hepsi pass etmesi gereken hedef URL listesi (boş = test yok).
        tunnel_test: HTTPS CONNECT testi çalıştırılsın mı? SOCKS proxy'lerinde
            otomatik True olarak işaretlenir (test yapılmadan).
    """
    proxy_type = PROXY_TYPE[protocol]
    host, _, port_str = proxy.partition(":")
    port = int(port_str)

    last_err: str | None = None
    started = time.monotonic()

    for attempt in range(retries + 1):
        connector = ProxyConnector(
            proxy_type=proxy_type, host=host, port=port, rdns=True,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                # X-Proxyprof-Proxy: judge'a "ben bu protokolden, bu IP:PORT'tan
                # geliyorum" der. SADECE kullanıcının trusted listesindeki
                # domain'lerde olan judge'lara gönderilir (send_identity).
                # Default güvenli: trusted listesi boşsa header gönderilmez.
                extra_headers: dict[str, str] = {}
                if send_identity:
                    extra_headers["X-Proxyprof-Proxy"] = f"{protocol}://{proxy}"
                async with session.get(
                    judge_url, headers=extra_headers,
                ) as resp:
                    body = await resp.text(errors="replace")

            elapsed = time.monotonic() - started
            headers = parse_judge_response(body)
            if not headers:
                return ScanResult(
                    proxy=proxy, ok=False, level=None,
                    elapsed=elapsed,
                    error="judge returned unparseable body",
                )
            level, distorting = detect_level(headers, public_ip)
            outbound = remote_addr(headers)
            country = extract_country(headers)

            # Access test: tüm URL'ler pass etmek zorunda. Tek bir hata
            # access_ok'ı False'a düşürür.
            access_ok: bool | None = None
            if access_urls:
                access_ok = await _access_check(
                    proxy, proxy_type, access_urls, timeout,
                )

            # HTTPS probe: tek istek, iki sonuç (tunnel_ok + mitm_suspected).
            # SOCKS proxy'leri zaten tünel'er → CONNECT testi gereksiz; ama MITM
            # için yine HTTPS probe atmamız gerek (SOCKS proxy de MITM yapabilir).
            tunnel_ok: bool | None = None
            mitm_suspected: bool | None = None
            if tunnel_test:
                if protocol in ("socks4", "socks5"):
                    # SOCKS daima tünel'er, ama MITM olabilir → yine probe at,
                    # tunnel_ok'u True olarak işaretle.
                    probe_result = await _https_probe(
                        proxy, proxy_type, timeout,
                    )
                    tunnel_ok = True
                    mitm_suspected = probe_result.mitm_suspected
                else:
                    probe_result = await _https_probe(
                        proxy, proxy_type, timeout,
                    )
                    tunnel_ok = probe_result.tunnel_ok
                    mitm_suspected = probe_result.mitm_suspected

            return ScanResult(
                proxy=proxy, ok=True, level=level, distorting=distorting,
                outbound_ip=outbound, country=country,
                elapsed=time.monotonic() - started,
                access_ok=access_ok, tunnel_ok=tunnel_ok,
                mitm_suspected=mitm_suspected,
            )

        # aiohttp_socks/python_socks kendi istisna hiyerarşisini fırlatır
        # (ProxyError, ProxyConnectionError, ReplyError) — hiçbiri
        # aiohttp.ClientError'ın altında değildir. Geniş Exception yakalaması
        # her proxy hatasını "bu proxy çalışmıyor" olarak işaretler; BaseException
        # alt sınıfları (KeyboardInterrupt, SystemExit) etkilenmez.
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}".strip(": ") or type(e).__name__
            if attempt < retries:
                continue

    return ScanResult(
        proxy=proxy, ok=False, level=None,
        elapsed=time.monotonic() - started, error=last_err,
    )


async def _access_check(
    proxy: str, proxy_type: ProxyType, urls: list[str], timeout: float,
) -> bool:
    """Tüm URL'lere ulaşıyor mu? Tek bir başarısızlık → False."""
    host, _, port_str = proxy.partition(":")
    for url in urls:
        connector = ProxyConnector(
            proxy_type=proxy_type, host=host, port=int(port_str), rdns=True,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if not (200 <= resp.status < 400):
                        return False
        except Exception:  # noqa: BLE001
            return False
    return True


@dataclass
class HttpsProbeResult:
    """HTTPS CONNECT probe'unun iki ayrı sonucu.

    Tek bir HTTPS isteği, iki ortogonal bilgi üretir:
      - tunnel_ok:        proxy CONNECT tüneli kurabildi mi?
      - mitm_suspected:   proxy TLS chain'i kendi sertifikasıyla kırıyor mu?

    Olası kombinasyonlar:
      tunnel_ok=True,  mitm_suspected=False → temiz HTTPS, TLS chain bozulmamış
      tunnel_ok=True,  mitm_suspected=True  → CONNECT açıldı AMA cert doğrulama
                                              fail etti = MITM imzası
      tunnel_ok=False, mitm_suspected=False → CONNECT bile kurulmadı (refused,
                                              timeout, network error)
    """
    tunnel_ok: bool
    mitm_suspected: bool
    error_class: str | None = None


async def _https_probe(
    proxy: str, proxy_type: ProxyType, timeout: float,
) -> HttpsProbeResult:
    """Tek HTTPS request ile hem CONNECT-tunnel hem MITM testi.

    Strateji:
      - Default aiohttp davranışı TLS doğrulama AÇIK → MITM proxy'nin fake
        sertifikası SSL cert hatasıyla yakalanır.
      - Cert hatası → CONNECT tüneli açıldı (proxy yanıt verdi), TLS başarısız
        (MITM). Yani: tunnel_ok=True, mitm_suspected=True.
      - Diğer SSL hataları (protocol mismatch vs.) MITM imzası SAYILMAZ —
        bunlar proxy'nin TLS implementasyon sorunları olabilir.
      - Bağlantı/timeout hataları → tunnel_ok=False, mitm_suspected=False.
    """
    host, _, port_str = proxy.partition(":")
    connector = ProxyConnector(
        proxy_type=proxy_type, host=host, port=int(port_str), rdns=True,
    )
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(TUNNEL_TEST_URL) as resp:
                if resp.status == 204:
                    return HttpsProbeResult(True, False)
                return HttpsProbeResult(
                    False, False, error_class=f"HTTP{resp.status}",
                )
    except aiohttp.ClientConnectorCertificateError:
        # Sertifika doğrulama fail oldu: CONNECT açıldı (proxy 200 dönmüş)
        # ama TLS chain proxy tarafından kırılıyor. MITM imzası.
        return HttpsProbeResult(True, True, error_class="CertError")
    except aiohttp.ClientSSLError as e:
        msg = str(e).lower()
        # aiohttp bazen CertVerificationError'ı ClientSSLError olarak
        # sarmalıyor — mesaja bakarak ayırt et.
        if "certificate_verify_failed" in msg or "cert" in msg:
            return HttpsProbeResult(True, True, error_class="CertError")
        return HttpsProbeResult(False, False, error_class="SSL")
    except Exception as e:  # noqa: BLE001
        return HttpsProbeResult(False, False, error_class=type(e).__name__)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class LiveTable:
    """Tarama sırasında her **başarılı** sonuç satır olarak akan canlı tablo
    + en alt satırda canlı progress.

    Sütunlar (sadece OK satırlar): #, STATUS, PROXY, LVL, OUT, CC, TIME, TUN, ACC.
    Fail satırlar tabloda görünmez; sayım progress satırında tutulur.

    Render düzeni:
      ┌─ header ─┐
      │  row 1   │   ← OK proxy
      │  row 2   │
      │   ...    │
      └──────────┘
      [████░░] 60%  18/30  ok:6 fail:12 elapsed:5.2s   ← canlı, ANSI ile refresh

    Tarama bitince:
      - en alt progress satırı temizlenir
      - tablo bottom border yazılır
      - final progress satırı statik olarak bir kez daha yazılır
    """

    BAR_WIDTH = 20
    # Internal kod → (i18n anahtar, minimum genişlik). Genişlik runtime'da
    # gerçek (çevrilmiş) etiketin uzunluğuyla max'lanır — örn. Türkçe'de "ÜLK"
    # 3 char, mevcut min 2'den geniştir, sütun otomatik genişler.
    _FIXED: dict[str, tuple[str, int]] = {
        "#":      ("table.header.num",    5),
        "STATUS": ("table.header.status", 6),
        "BKT":    ("table.header.bkt",    4),
        "PROXY":  ("table.header.proxy",  21),
        "LVL":    ("table.header.lvl",    3),
        "OUT":    ("table.header.out",    15),
        "CC":     ("table.header.cc",     2),
        "TIME":   ("table.header.time",   6),
        "TUN":    ("table.header.tun",    3),
        "MITM":   ("table.header.mitm",   4),
        "ACC":    ("table.header.acc",    3),
    }

    # Bucket isminin tek-karakter UI temsili. Sütun dar; özet bilgi yeterli.
    _BUCKET_SHORT: dict[str, str] = {
        BUCKET_HOT: "H", BUCKET_WARM: "W", BUCKET_NEW: "N", BUCKET_COLD: "C",
    }

    def __init__(self, enabled: bool, total: int, file=sys.stderr) -> None:
        self.file = file
        self.enabled = enabled
        # ANSI cursor manipülasyonu yalnız TTY'de güvenli; pipe/file'a yazarken
        # progress satırını çizmeyiz (sadece header + OK rows + final summary).
        self.use_ansi = enabled and file.isatty()
        self.total = total
        self.count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self._headered = False
        self._progress_drawn = False
        # Sütun adı → genişlik (etiket uzunluğunu da hesaba kat).
        self._cols: dict[str, int] = {}
        for code, (key, min_w) in self._FIXED.items():
            label = t(key)
            self._cols[code] = max(min_w, len(label))
        self._cols["#"] = max(self._cols["#"], len(f"{total}/{total}"))
        # Sütun adı → çevrilmiş etiket (header satırında basılır).
        self._labels: dict[str, str] = {
            code: t(key) for code, (key, _) in self._FIXED.items()
        }
        self._started = time.monotonic()

    def _all_widths(self) -> list[int]:
        return list(self._cols.values())

    def _border(self, left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in self._all_widths()) + right

    def _row(self, cells: list[str]) -> str:
        widths = self._all_widths()
        parts = []
        for i, (c, w) in enumerate(zip(cells, widths)):
            s = c if len(c) <= w else c[: w - 1] + "…"
            # # ve TIME sağa yaslı, geri kalanlar sola
            col_name = list(self._cols.keys())[i]
            if col_name in ("#", "TIME"):
                parts.append(f" {s:>{w}} ")
            else:
                parts.append(f" {s:<{w}} ")
        return "│" + "│".join(parts) + "│"

    def _emit_header(self) -> None:
        labels = [self._labels[code] for code in self._cols.keys()]
        print(self._border("┌", "┬", "┐"), file=self.file)
        print(self._row(labels), file=self.file)
        print(self._border("├", "┼", "┤"), file=self.file)
        self.file.flush()

    def _progress_line(self) -> str:
        pct = self.count / self.total if self.total else 1.0
        filled = int(self.BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)
        digits = len(str(self.total))
        elapsed = time.monotonic() - self._started
        return t(
            "progress.format",
            bar=bar, pct=pct * 100,
            done=self.count, digits=digits, total=self.total,
            ok=self.ok_count, fail=self.fail_count, skip=self.skip_count,
            elapsed=elapsed,
        )

    def update(self, r: ScanResult) -> None:
        self.count += 1
        if r.ok:
            self.ok_count += 1
        elif r.skipped:
            # IP-poison erken-atlama; sayım gerçek fail'lerden ayrı tutulur
            # ki kullanıcı "kaç port'u test bile etmediğimi" görebilsin.
            self.skip_count += 1
        else:
            self.fail_count += 1

        if not self.enabled:
            return
        if not self._headered:
            self._emit_header()
            self._headered = True

        # Mevcut progress satırını temizle (varsa) — sadece TTY'de.
        if self.use_ansi and self._progress_drawn:
            self.file.write("\r\033[K")

        # Sadece OK satırlarını tabloya ekle. Fail'ler sayıma katıldı ama
        # tablo gürültüsünü artırmasın.
        if r.ok:
            # STATUS: judge'ı geçti ama tunnel/access/mitm düştüyse "filter".
            if (
                (r.access_ok is False)
                or (r.tunnel_ok is False)
                or (r.mitm_suspected is True)
            ):
                status = t("table.status.filter")
            else:
                status = t("table.status.ok")
            if r.level == 1:
                lvl = "L1"
            elif r.level == 2:
                lvl = "L2d" if r.distorting else "L2"
            elif r.level == 3:
                lvl = "L3"
            else:
                lvl = "—"

            def _mark(v: bool | None) -> str:
                if v is None:
                    return "—"
                return "✓" if v else "×"

            bkt = self._BUCKET_SHORT.get(r.bucket or "", "—")
            # MITM kolonu: True = TLS chain kırık (kırmızı bayrak). Mantıken
            # ters: ✓ = MITM YOK (güvenli), × = MITM şüphesi. _mark'a
            # `not mitm_suspected` veriyoruz ki ✓ = iyi semantiği kalsın.
            mitm_mark = (
                "—" if r.mitm_suspected is None
                else ("✓" if not r.mitm_suspected else "×")
            )
            cells = [
                f"{self.count}/{self.total}",
                status,
                bkt,
                r.proxy,
                lvl,
                r.outbound_ip or "—",
                r.country or "—",
                f"{r.elapsed:.1f}s",
                _mark(r.tunnel_ok),
                mitm_mark,
                _mark(r.access_ok),
            ]
            self.file.write(self._row(cells) + "\n")

        # Progress satırını en altta yeniden çiz (TTY varsa).
        if self.use_ansi:
            self.file.write(self._progress_line())
            self._progress_drawn = True

        self.file.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        # Canlı progress satırını temizle.
        if self.use_ansi and self._progress_drawn:
            self.file.write("\r\033[K")
        # Tablo varsa bottom border'ı kapat.
        if self._headered:
            self.file.write(self._border("└", "┴", "┘") + "\n")
        # Statik final progress satırı (her zaman, TTY olsun olmasın).
        self.file.write(self._progress_line() + "\n")
        self.file.flush()


def _percentile(data: list[float], p: float) -> float:
    """Linear-interpolation percentile (NumPy uyumlu, küçük listelerde de stabil)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def _print_keyval_box(
    title: str, rows: list[tuple[str, str]], file,
) -> None:
    """Generic etiketli kutu yazıcı. CONFIG ve RESULT için ortak.

    Başlık sol kutucuğa gömülür; üst ve alt sınır `┬`/`┴` ile aynı yerde
    bölünür:
       ┌ TITLE ──────┬─────────────┐
       │ key         │ value       │
       └─────────────┴─────────────┘
    """
    if not rows:
        return
    w_key = max(len(k) for k, _ in rows)
    w_val = max(len(v) for _, v in rows)
    key_box_width = w_key + 2   # " key " (padding hem solda hem sağda)
    val_box_width = w_val + 2

    title_text = f" {title} "
    if len(title_text) <= key_box_width:
        title_seg = title_text + "─" * (key_box_width - len(title_text))
    else:
        # Başlık key kutucuğuna sığmıyor — kısalt.
        title_seg = f" {title[: key_box_width - 3]}…"[:key_box_width]

    print("┌" + title_seg + "┬" + "─" * val_box_width + "┐", file=file)
    for k, v in rows:
        print(f"│ {k:<{w_key}} │ {v:<{w_val}} │", file=file)
    print(
        "└" + "─" * key_box_width + "┴" + "─" * val_box_width + "┘",
        file=file,
    )


def print_config_box(
    args: argparse.Namespace,
    judge_url: str,
    public_ip: str,
    access_urls: list[str],
    send_identity: bool,
    reputation_enabled: bool = False,
    run_index: int = 0,
    bucket_groups: dict[str, list[str]] | None = None,
    probation_skipped: int = 0,
    file=sys.stderr,
) -> None:
    """Taramanın TÜM ayarlarını key=value olarak göster.

    Parametre listesi tarama bittikten sonra okunabilir bir referans; tekrar
    çalıştırılması gerektiğinde hangi parametrelerle yapıldığını net gösterir.
    """
    on, off, unknown = t("value.on"), t("value.off"), t("value.unknown")
    rows: list[tuple[str, str]] = [
        (t("row.protocol"),     args.protocol),
        (t("row.input"),        args.file or t("value.stdin")),
        (t("row.output"),       args.output or t("value.stdout")),
        (t("row.judge"),        judge_url),
        (t("row.public_ip"),    public_ip or unknown),
        (t("row.level"),        f"≤{args.level}"),
        (t("row.concurrency"),  str(args.concurrency)),
        (t("row.timeout"),      t("value.elapsed_seconds", elapsed=args.timeout)),
        (t("row.retries"),      str(args.retries)),
        (t("row.tunnel_test"),  on if args.tunnel_test else off),
        (t("row.mitm_test"),    on if args.mitm_test else off),
        (t("row.lang"),         i18n.current_language()),
    ]
    if access_urls:
        samples = ", ".join(access_urls[:3]) + ("..." if len(access_urls) > 3 else "")
        rows.append((t("row.access_test"),
                     t("value.access_n_urls", n=len(access_urls), samples=samples)))
    else:
        rows.append((t("row.access_test"), off))
    # Output filtreler — sadece set edilmişse göster (kapalı default'lar
    # CONFIG kutusunu şişirmesin).
    if getattr(args, "country", None):
        rows.append((t("row.country_filter"), args.country))
    if getattr(args, "exclude_distorting", False):
        rows.append((t("row.exclude_distorting"), on))
    rows.append((t("row.identity"), on if send_identity else off))
    if reputation_enabled:
        rows.append((t("row.reputation"),
                     t("value.reputation_on", run=run_index, db=args.reputation)))
        if bucket_groups is not None:
            hot = len(bucket_groups.get(BUCKET_HOT, []))
            warm = len(bucket_groups.get(BUCKET_WARM, []))
            new = len(bucket_groups.get(BUCKET_NEW, []))
            cold = len(bucket_groups.get(BUCKET_COLD, []))
            rows.append((t("row.buckets"),
                         t("value.buckets_breakdown",
                           hot=hot, warm=warm, new=new, cold=cold)))
        if probation_skipped:
            rows.append((t("row.probation"),
                         t("value.probation_skipped", n=probation_skipped)))
        rows.append((t("row.cold_timeout"),
                     t("value.elapsed_seconds", elapsed=args.cold_timeout)))
    else:
        rows.append((t("row.reputation"), t("value.reputation_off")))
    _print_keyval_box(t("box.title.config"), rows, file)


def print_result_box(
    scanned: int,
    counts: dict,
    timings: list[float],
    countries: Counter,
    output_path: str | None,
    elapsed: float,
    tunnel_test: bool,
    mitm_test: bool = False,
    file=sys.stderr,
) -> None:
    """Tarama sonuçları — sayım, dağılım, hız, ülke, süre."""
    elite = counts.get(1, 0)
    anon = counts.get(2, 0)
    trans = counts.get(3, 0)
    distorting = counts.get("distorting", 0)
    mitm = counts.get("mitm", 0)
    bad = counts.get("bad", 0)
    skipped = counts.get("skipped", 0)
    blocked = counts.get("blocked")
    tunneled = counts.get("tunneled")
    mitm_filtered = counts.get("mitm_filtered", 0)
    country_filtered = counts.get("country_filtered", 0)
    distort_filtered = counts.get("distort_filtered", 0)
    dest = t("value.dest_arrow", path=output_path) if output_path else ""

    if distorting:
        anon_text = t("value.anon_distorting", anon=anon, distorting=distorting)
    else:
        anon_text = t("value.anon_simple", anon=anon)

    rows: list[tuple[str, str]] = [
        (t("row.scanned"), t("value.proxies", n=scanned)),
        (t("row.good"),    t("value.good_breakdown",
                             elite=elite, anon_text=anon_text,
                             trans=trans, dest=dest)),
        (t("row.bad"),     t("value.bad_count", n=bad)),
    ]
    if skipped:
        rows.append((t("row.skipped"), t("value.skipped_count", n=skipped)))
    if blocked is not None:
        rows.append((t("row.blocked"), t("value.blocked_count", n=blocked)))
    if tunnel_test and tunneled is not None:
        good_total = elite + anon + trans
        rows.append((t("row.tunnel"),
                     t("value.tunnel_count", n=tunneled, good=good_total)))
    if mitm_test and (mitm or mitm_filtered):
        rows.append((t("row.mitm"),
                     t("value.mitm_breakdown", n=mitm, filtered=mitm_filtered)))
    if country_filtered:
        rows.append((t("row.country_drop"),
                     t("value.country_drop_count", n=country_filtered)))
    if distort_filtered:
        rows.append((t("row.distort_drop"),
                     t("value.distort_drop_count", n=distort_filtered)))
    if timings:
        p50 = _percentile(timings, 50)
        p95 = _percentile(timings, 95)
        rows.append((t("row.timing"), t("value.timing", p50=p50, p95=p95)))
    if countries:
        top = countries.most_common(5)
        country_str = " ".join(f"{c}={n}" for c, n in top)
        others = sum(countries.values()) - sum(n for _, n in top)
        if others:
            country_str += t("value.country_more", n=others)
        rows.append((t("row.country"), country_str))
    rows.append((t("row.elapsed"), t("value.elapsed_seconds", elapsed=elapsed)))
    _print_keyval_box(t("box.title.result"), rows, file)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_POISON_THRESHOLD = 3
_POISONING_CLASSES = frozenset({"tls-intercept", "tls-junk", "timeout"})


def _classify_error(err: str | None) -> str:
    """Coarse classification for IP-poison tracking.

    Returns a short label. Same class on many ports of one IP is strong
    evidence the IP itself is bad (TLS intercept gateway, captive portal,
    blackhole route), not each individual port — so we can skip the rest.
    """
    if not err:
        return "other"
    e = err.lower()
    if "certificate_verify_failed" in e:
        return "tls-intercept"     # MITM gateway terminating TLS with its own cert
    if "wrong_version_number" in e or "unknown_protocol" in e:
        return "tls-junk"          # plaintext bytes where TLS handshake expected
    if "timeouterror" in e or "timed out" in e or "asyncio.timeouterror" in e:
        return "timeout"           # firewalled / dropped / unreachable
    if "connection refused" in e or "connectionreseterror" in e:
        return "refused"
    if "no route to host" in e or "network is unreachable" in e:
        return "unreachable"
    return "other"


class IPPoison:
    """Per-IP consecutive-failure tracker for short-circuit skipping.

    After THRESHOLD consecutive failures of the SAME error class on one IP,
    mark the IP as poisoned; subsequent ports of that IP return immediately
    without probing. Only TLS-level and timeout classes poison — these are
    IP-wide symptoms, not per-port. Per-port errors (refused, other) are
    tracked but never trigger poisoning.

    Async-safe under a single event loop: all mutations come from inside
    worker coroutines; no lock needed.
    """

    def __init__(self, threshold: int = _POISON_THRESHOLD) -> None:
        self.threshold = threshold
        self._streak: dict[str, tuple[str, int]] = {}  # ip -> (class, count)
        self._poisoned: dict[str, str] = {}            # ip -> reason label
        self.skipped = 0

    def reason(self, ip: str) -> str | None:
        return self._poisoned.get(ip)

    def record_failure(self, ip: str, error_class: str) -> None:
        last = self._streak.get(ip)
        if last and last[0] == error_class:
            count = last[1] + 1
        else:
            count = 1
        self._streak[ip] = (error_class, count)
        if (
            count >= self.threshold
            and error_class in _POISONING_CLASSES
            and ip not in self._poisoned
        ):
            self._poisoned[ip] = f"{error_class} x{count}"

    def record_success(self, ip: str) -> None:
        # A working port on this IP proves it's reachable; clear streak.
        self._streak.pop(ip, None)


async def scan(
    tasks: list[ScanTask],
    protocol: str,
    judge_url: str,
    public_ip: str,
    concurrency: int,
    retries: int,
    access_urls: list[str],
    tunnel_test: bool,
    send_identity: bool,
    table: LiveTable | None,
) -> list[ScanResult]:
    """Verilen ScanTask listesini async olarak tara.

    Her task'ın kendi `timeout` ve `bucket` etiketi vardır. Çağıran taraf
    task'ları zaten istenen dispatch order'da (weighted-interleaved) verir;
    tek shared semafor + asyncio.gather doğal olarak ilk task'ları ilk
    dispatch eder, böylece HOT bucket öncelik kazanır.
    """
    sem = asyncio.Semaphore(concurrency)
    poison = IPPoison()

    async def worker(t: ScanTask) -> ScanResult:
        async with sem:
            ip = t.proxy.partition(":")[0]
            poisoned_reason = poison.reason(ip)
            if poisoned_reason is not None:
                poison.skipped += 1
                r = ScanResult(
                    proxy=t.proxy, ok=False, level=None, elapsed=0.0,
                    error=f"skipped: IP poisoned ({poisoned_reason})",
                    skipped=True,
                    bucket=t.bucket,
                )
            else:
                r = await probe(
                    proxy=t.proxy, protocol=protocol, judge_url=judge_url,
                    timeout=t.timeout, retries=retries,
                    public_ip=public_ip, access_urls=access_urls,
                    tunnel_test=tunnel_test, send_identity=send_identity,
                )
                r.bucket = t.bucket
                if r.ok:
                    poison.record_success(ip)
                else:
                    poison.record_failure(ip, _classify_error(r.error))
            if table is not None:
                table.update(r)
            return r

    return await asyncio.gather(*(worker(t) for t in tasks))


def _ip_port_sort_key(s: str) -> tuple[tuple[int, int, int, int], int]:
    ip, _, port = s.partition(":")
    a, b, c, d = (int(o) for o in ip.split("."))
    return (a, b, c, d), int(port)


def _parse_access_urls(arg: str | None) -> list[str]:
    """Comma-separated URL listesini ayrıştır + validate et."""
    if not arg:
        return []
    out: list[str] = []
    for raw in arg.split(","):
        u = raw.strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            sys.exit(f"proxyprof: {t('input.access_url_invalid', url=u)}")
        out.append(u)
    return out


def _resolve_access_test(arg: str | None) -> list[str]:
    """args.access_test → URL listesi.

    None        → boş liste (test yok)
    "AUTO"      → CF_GATEKEEPERS'tan rastgele 3 site
    "url1,url2" → kullanıcı verdiği URL'ler (validate edilir)
    """
    if arg is None:
        return []
    if arg == ACCESS_AUTO_SENTINEL:
        k = min(ACCESS_AUTO_COUNT, len(CF_GATEKEEPERS))
        return random.sample(CF_GATEKEEPERS, k=k)
    return _parse_access_urls(arg)


async def amain(args: argparse.Namespace) -> int:
    proxies = read_proxies(args.file)
    # --no-access-test her durumda --access-test'in üzerine yazar; AUTO da
    # özel URL listesi de iptal edilir.
    if args.no_access_test:
        access_urls: list[str] = []
    else:
        access_urls = _resolve_access_test(args.access_test)

    # ---- Reputation (opt-out via --no-reputation) ------------------------
    #
    # 100k+ proxy'lik düzenli taramalarda input'un %80–90'ı önceki run'lardan
    # tanıdık ve sürekli fail. SQLite-tabanlı bir state DB ile her proxy'nin
    # geçmişini tutuyoruz; tarama başında HOT/WARM/NEW/COLD bucket'larına
    # ayırıyoruz; COLD bucket'a üstel probation uyguluyoruz (her run değil,
    # her 2^k run'da bir test). Bu sayede ölü kuyruk iş yükünden çıkar.
    reputation: Reputation | None = None
    bucket_records: dict[str, object] = {}      # proxy → Record (or absent)
    bucket_map: dict[str, str] = {}             # proxy → bucket name
    run_index = 0
    probation_skipped: list[str] = []           # this run'da test bile edilmedi
    if not args.no_reputation:
        reputation = Reputation(Path(args.reputation))
        run_index = reputation.increment_run_index()
        bucket_records = reputation.get_records(proxies)
        reputation.mark_seen(proxies, now_epoch())
        now = now_epoch()
        for p in proxies:
            rec = bucket_records.get(p)
            bucket = classify(rec, now, args.dead_threshold)
            bucket_map[p] = bucket
            if not should_test_now(
                rec, bucket, run_index,
                args.dead_threshold, args.probation_max_skip,
            ):
                probation_skipped.append(p)

    # Bucket gruplarını oluştur (probation'dan geçemeyenler hariç).
    if reputation is not None:
        skip_set = set(probation_skipped)
        bucket_groups: dict[str, list[str]] = {
            BUCKET_HOT: [], BUCKET_WARM: [], BUCKET_NEW: [], BUCKET_COLD: [],
        }
        for p in proxies:
            if p in skip_set:
                continue
            bucket_groups[bucket_map[p]].append(p)
        ordered_proxies = weighted_interleave(bucket_groups, DEFAULT_WEIGHTS)
    else:
        ordered_proxies = list(proxies)

    # ScanTask listesini kur — COLD bucket için kısa timeout, diğerleri için
    # kullanıcı timeout'u. Reputation kapalıysa hepsine kullanıcı timeout'u.
    tasks: list[ScanTask] = []
    for p in ordered_proxies:
        b = bucket_map.get(p) if reputation is not None else None
        t = args.cold_timeout if b == BUCKET_COLD else args.timeout
        tasks.append(ScanTask(proxy=p, timeout=t, bucket=b))

    # Tek bir proxysiz HTTP session ile public IP + judge tespit. Proxy başına
    # ayrı connector açacağımız için bu session sadece bootstrap içindir.
    # Bootstrap timeout proxy-başına timeout'tan ayrı tutulur — kullanıcı agresif
    # bir `-T 2` verirse canhazip.com'un TLS handshake'ini kesmesin.
    bootstrap_timeout = max(args.timeout, 10.0)
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
    ) as bootstrap:
        public_ip = await get_public_ip(bootstrap, timeout=bootstrap_timeout)

        if args.judge:
            judge_url = args.judge
        else:
            try:
                judge_url, _ = await pick_judge(
                    bootstrap, judges_for(args.protocol),
                    timeout=bootstrap_timeout,
                )
            except JudgeUnavailable as e:
                print(f"proxyprof: {e}", file=sys.stderr)
                if reputation is not None:
                    reputation.close()
                return 1

    send_identity = _judge_accepts_proxyprof_header(judge_url)

    # CONFIG kutusu taramanın BAŞINDA basılır — progress satırı altında akar,
    # OK satırları sonra eklenir. Kullanıcı tarama bittiğinde tekrar görmesin
    # diye sonda yeniden basılmaz. silent modda hiç basılmaz.
    if not args.silent:
        print_config_box(
            args=args,
            judge_url=judge_url,
            public_ip=public_ip,
            access_urls=access_urls,
            send_identity=send_identity,
            reputation_enabled=reputation is not None,
            run_index=run_index,
            bucket_groups=(bucket_groups if reputation is not None else None),
            probation_skipped=len(probation_skipped),
        )

    # LiveTable total = aslında test edilecek proxy sayısı (probation skipped'lar
    # hariç). Probation skipped'lar tabloda görünmez ama özet kutuda raporlanır.
    table = LiveTable(enabled=not args.silent, total=len(tasks))

    started = time.monotonic()
    results = await scan(
        tasks=tasks,
        protocol=args.protocol,
        judge_url=judge_url,
        public_ip=public_ip,
        concurrency=args.concurrency,
        retries=args.retries,
        access_urls=access_urls,
        tunnel_test=args.tunnel_test,
        send_identity=send_identity,
        table=table,
    )
    table.finish()
    elapsed = time.monotonic() - started

    # Reputation'ı güncelle — IP-poison ile pre-skipped olanlar (r.skipped=True
    # AND r.bucket reputation'dan geliyor) DB'ye değişiklik yapmamalı; onlar
    # için consecutive_failures artırılmamalı çünkü gerçek probe çalışmadı.
    if reputation is not None:
        real_results = [r for r in results if not r.skipped]
        reputation.record_results(real_results, run_index, now_epoch())
        reputation.close()

    # Sayım: counts[1..3] iyi proxy seviye dağılımı (filtre öncesi gerçek).
    # distorting, blocked, tunneled, mitm_blocked, country_filtered, distort_filtered
    # iyi proxy alt türleri. timings sadece ok proxy'leri içerir (percentile için).
    counts = {1: 0, 2: 0, 3: 0,
              "bad": 0, "skipped": 0, "blocked": 0,
              "distorting": 0, "tunneled": 0, "mitm": 0,
              "mitm_filtered": 0, "country_filtered": 0,
              "distort_filtered": 0}
    timings: list[float] = []
    countries: Counter = Counter()
    kept: list[str] = []
    country_filter = {c.strip().upper() for c in (args.country or "").split(",") if c.strip()}
    for r in results:
        if not r.ok:
            if r.skipped:
                counts["skipped"] += 1
            else:
                counts["bad"] += 1
            continue
        counts[r.level] += 1
        if r.distorting:
            counts["distorting"] += 1
        if r.tunnel_ok is True:
            counts["tunneled"] += 1
        if r.mitm_suspected is True:
            counts["mitm"] += 1
        timings.append(r.elapsed)
        if r.country:
            countries[r.country] += 1

        # Filter zinciri — her atlamayı kategori olarak say.
        if r.level > args.level:
            continue
        if access_urls and not r.access_ok:
            counts["blocked"] += 1
            continue
        if args.tunnel_test and r.tunnel_ok is False:
            continue
        if args.mitm_test and r.mitm_suspected is True:
            counts["mitm_filtered"] += 1
            continue
        if args.exclude_distorting and r.distorting:
            counts["distort_filtered"] += 1
            continue
        if country_filter and (r.country or "").upper() not in country_filter:
            counts["country_filtered"] += 1
            continue
        kept.append(r.proxy)

    kept_sorted = sorted(set(kept), key=_ip_port_sort_key)

    if args.output:
        try:
            out_fh = open(args.output, "w", encoding="utf-8")
        except OSError as e:
            print(
                f"proxyprof: {t('misc.cannot_open_output', path=args.output, err=e)}",
                file=sys.stderr,
            )
            return 1
    else:
        out_fh = sys.stdout
    try:
        for line in kept_sorted:
            print(line, file=out_fh)
    finally:
        if args.output:
            out_fh.close()

    if not args.silent:
        summary_counts = dict(counts)
        if not access_urls:
            summary_counts.pop("blocked", None)
        if not args.tunnel_test:
            summary_counts.pop("tunneled", None)
        # CONFIG taramanın başında basıldı; sonda tekrar gösterilmez.
        print_result_box(
            scanned=len(results),
            counts=summary_counts,
            timings=timings,
            countries=countries,
            output_path=args.output,
            elapsed=elapsed,
            tunnel_test=args.tunnel_test,
            mitm_test=args.mitm_test,
        )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    supported_langs = i18n.available_languages()

    # Epilog'u t() ile kur — örnekler de çevrilebilir.
    epilog = (
        f"{t('cli.epilog_header')}\n"
        "  proxine http -s | proxyprof http"
        "                            "
        f"{t('cli.example.pipe')}\n"
        "  proxyprof http -f list.lst -l 2 -o ok.lst"
        "                   "
        f"{t('cli.example.file_in')}\n"
        "  proxyprof socks5 -f - -c 1000 -T 8"
        "                          "
        f"{t('cli.example.stdin_socks5')}\n"
        "  proxyprof http -f l.lst --access-test https://a.com,https://b.com"
        "  "
        f"{t('cli.example.access_test_custom')}\n"
        "  proxyprof http -f l.lst --no-tunnel-test"
        "                    "
        f"{t('cli.example.no_tunnel')}\n"
        "  proxyprof http -j https://yours.tld/proxyjudge.php"
        "          "
        f"{t('cli.example.cf_judge')}\n"
    )
    p = argparse.ArgumentParser(
        prog="proxyprof",
        description=t("cli.description"),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "protocol",
        choices=("http", "https", "socks4", "socks5"),
        help=t("cli.help.protocol"),
    )

    # --- scan & probes --------------------------------------------------
    g_scan = p.add_argument_group(
        t("cli.group.scan_title"),
        t("cli.group.scan_desc"),
    )
    g_scan.add_argument(
        "-f", "--file", metavar="FILE",
        help=t("cli.help.file"),
    )
    g_scan.add_argument(
        "-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=t("cli.help.concurrency", default=DEFAULT_CONCURRENCY),
    )
    g_scan.add_argument(
        "-T", "--timeout", type=float, default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=t("cli.help.timeout", default=DEFAULT_TIMEOUT),
    )
    g_scan.add_argument(
        "-r", "--retries", type=int, default=DEFAULT_RETRIES,
        metavar="N",
        help=t("cli.help.retries", default=DEFAULT_RETRIES),
    )
    g_scan.add_argument(
        "-j", "--judge", metavar="URL",
        help=t("cli.help.judge", domains=", ".join(_TRUSTED_JUDGE_DOMAINS)),
    )
    g_scan.add_argument(
        "--access-test", nargs="?", const=ACCESS_AUTO_SENTINEL,
        default=ACCESS_AUTO_SENTINEL, metavar="URLS",
        help=t("cli.help.access_test", count=ACCESS_AUTO_COUNT),
    )
    g_scan.add_argument(
        "--no-access-test", action="store_true", dest="no_access_test",
        help=t("cli.help.no_access_test"),
    )
    g_scan.add_argument(
        "--tunnel-test", action=argparse.BooleanOptionalAction, default=True,
        dest="tunnel_test",
        help=t("cli.help.tunnel_test", url=TUNNEL_TEST_URL),
    )
    g_scan.add_argument(
        "--mitm-test", action=argparse.BooleanOptionalAction, default=True,
        dest="mitm_test",
        help=t("cli.help.mitm_test"),
    )
    g_scan.add_argument(
        "--reputation", metavar="PATH", default=str(default_db_path()),
        help=t("cli.help.reputation", default=default_db_path()),
    )
    g_scan.add_argument(
        "--no-reputation", action="store_true",
        help=t("cli.help.no_reputation"),
    )
    g_scan.add_argument(
        "--dead-threshold", type=int, default=DEFAULT_DEAD_THRESHOLD,
        metavar="N",
        help=t("cli.help.dead_threshold", default=DEFAULT_DEAD_THRESHOLD),
    )
    g_scan.add_argument(
        "--probation-max-skip", type=int, default=DEFAULT_PROBATION_MAX_SKIP,
        metavar="N",
        help=t("cli.help.probation_max_skip", default=DEFAULT_PROBATION_MAX_SKIP),
    )
    g_scan.add_argument(
        "--cold-timeout", type=float, default=DEFAULT_COLD_TIMEOUT,
        metavar="SECONDS",
        help=t("cli.help.cold_timeout", default=DEFAULT_COLD_TIMEOUT),
    )

    # --- output filters -------------------------------------------------
    g_filter = p.add_argument_group(
        t("cli.group.filter_title"),
        t("cli.group.filter_desc"),
    )
    g_filter.add_argument(
        "-l", "--level", type=int, choices=(1, 2, 3),
        default=DEFAULT_LEVEL,
        help=t("cli.help.level", default=DEFAULT_LEVEL),
    )
    g_filter.add_argument(
        "--country", metavar="CC[,CC...]", default=None,
        help=t("cli.help.country"),
    )
    g_filter.add_argument(
        "--exclude-distorting", action="store_true",
        help=t("cli.help.exclude_distorting"),
    )

    # --- output destination --------------------------------------------
    g_out = p.add_argument_group(
        t("cli.group.output_title"),
        t("cli.group.output_desc"),
    )
    g_out.add_argument(
        "-o", "--output", metavar="FILE",
        help=t("cli.help.output"),
    )
    g_out.add_argument(
        "-v", "--verbose", action="store_true",
        help=t("cli.help.verbose"),
    )
    g_out.add_argument(
        "-s", "--silent", action="store_true",
        help=t("cli.help.silent"),
    )

    # --- misc -----------------------------------------------------------
    g_misc = p.add_argument_group(
        t("cli.group.misc_title"),
        t("cli.group.misc_desc"),
    )
    g_misc.add_argument(
        "-L", "--lang", metavar="CODE",
        choices=supported_langs, default=i18n.current_language(),
        help=t(
            "cli.help.lang",
            supported=", ".join(supported_langs),
            default="en",
        ),
    )

    args = p.parse_args(argv)

    if args.judge and not (
        args.judge.startswith("http://") or args.judge.startswith("https://")
    ):
        p.error(t("input.judge_must_be_http"))
    # --access-test validation _parse_access_urls içinde yapılıyor.

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print(f"\nproxyprof: {t('misc.interrupted')}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
