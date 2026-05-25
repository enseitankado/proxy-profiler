#!/usr/bin/env python3
"""
proxyprof — async proxy scanner. Proxine'in boru hattı tamamlayıcısı.

stdin'den (veya -f FILE'dan) IP:PORT proxy listesi okur; her birini bir judge
URL'e yönlendirerek canlı / anonim / elite / transparent sınıflandırması yapar.
Filtreyi geçen proxy'leri stdout'a sıralı, dedupe edilmiş halde yazar.

Boru hattı örneği:
    proxine http -s | proxyprof -p http -l 1 -o working.lst
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
from collections.abc import Callable
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
    is_judge_behind_cf,
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
    default_db_dir,
    default_db_path,
    now_epoch,
    should_test_now,
    weighted_interleave,
)


DEFAULT_LEVEL = 1
DEFAULT_CONCURRENCY = 500
DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 1
# COLD bucket için kısa timeout: zaten %90+'ı timeout'a düşecek, beklemeye
# gerek yok. HOT/WARM/NEW normal --timeout'u kullanır.
DEFAULT_COLD_TIMEOUT = 3.0

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"
)

# HTTPS CONNECT tünel testi için kullanılan endpoint havuzu.
#
# Her probe'da bu listeden rastgele 1 URL seçilir. Tek bir URL kullanmak iki
# tür proxy'yi haksız yere "tunnel kırık" işaretler:
#   1) gstatic.com'u hostname-bazlı bloklayan operatörler (yaygın — Çin, RU)
#   2) belirli sağlayıcının CDN edge'leri tarafından IP'si banlı proxy'ler
# Listede 4 ayrı sağlayıcı/zone var; tek bir sağlayıcı bir proxy'yi blocluyorsa
# diğeri muhtemelen geçer. Hepsi:
#   - HTTPS, valid public cert (MITM tespiti için gerekli)
#   - 2xx/3xx döner, küçük body, captive-portal-style endpoint
#   - Header'lerde kullanıcı bilgisi sızdırmaz
#   - Datacenter IP'lerine karşı agresif bot-blok uygulamaz
TUNNEL_TEST_URLS: tuple[str, ...] = (
    "https://captive.apple.com/hotspot-detect.html",   # Apple captive-check; "Success" döner
    "https://detectportal.firefox.com/success.txt",    # Mozilla; "success\n" döner
    "https://www.gstatic.com/generate_204",            # Google; 204 No Content
    "https://1.1.1.1/cdn-cgi/trace",                   # Cloudflare DNS edge; trace text
)

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

# Google connectivity-check endpoint'leri. Android/Chrome captive portal
# kontrolünün kullandığı /generate_204 path'leri; CF /cdn-cgi/trace'in Google
# eşdeğeri: 204 No Content, sıfır body, UA filtresi yok, son derece düşük
# maliyet. "Proxy Google altyapısına ulaşabiliyor mu?" sorusunun en hızlı
# cevabı. Birden fazla Google subdomain'i = farklı GFE edge'leri = "her
# yerden değil sadece tek bir cluster'a erişiyor" hilesini elemek için.
GOOGLE_GATEKEEPERS: tuple[str, ...] = (
    "https://www.google.com/generate_204",
    "https://www.gstatic.com/generate_204",
    "https://connectivitycheck.gstatic.com/generate_204",
    "https://clients3.google.com/generate_204",
    "https://accounts.google.com/generate_204",
    "https://www.youtube.com/generate_204",
)

ACCESS_AUTO_COUNT = 3
ACCESS_AUTO_SENTINEL = "AUTO"

# Adlı preset değerleri — `--access-test cloudflare` ya da `--access-test
# google` ile seçilir; ayrıca değersiz `--access-test` Cloudflare default'una
# eşdeğer. Tanınmayan değerler URL listesi olarak parse edilir.
ACCESS_PRESET_CLOUDFLARE = "cloudflare"
ACCESS_PRESET_GOOGLE     = "google"
ACCESS_MODE_OFF      = "off"      # --access-test verilmedi
ACCESS_MODE_CF       = "cloudflare"
ACCESS_MODE_GOOGLE   = "google"
ACCESS_MODE_CUSTOM   = "custom"   # kullanıcı URL listesi verdi


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
    access_reason: str | None = None # access_ok=False ise neden (3 char kod)
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

# Public IP tespiti için fallback zinciri. Her biri düz metin IPv4 döner.
# Tek bir sağlayıcıya bağlı kalmamak için sıralı dene; ilk geçerli yanıt kazanır.
# Sıra rastgele değil: en hızlı/en kararlı olanlar başta.
_PUBLIC_IP_SOURCES: tuple[str, ...] = (
    "https://checkip.amazonaws.com/",   # AWS, çok stabil, plain IP
    "https://api.ipify.org/",           # ipify, popüler, plain IP
    "https://ifconfig.me/ip",           # ifconfig.me, plain IP
    "https://icanhazip.com/",           # canhazip alternatif
    "https://canhazip.com/",            # Cloudflare-arkalı, ara sıra rate-limit
)


async def get_public_ip(session: aiohttp.ClientSession, timeout: float) -> str:
    """Sırayla fallback servisleri dene; ilk geçerli IPv4'ü döndür.

    Tek bir sağlayıcı (canhazip vs.) anlık down olursa diğerleri ayakta
    kalsın. Hepsi başarısızsa public_ip="" → distorting tespiti zayıflar
    ama tarama yine de yürür.
    """
    for url in _PUBLIC_IP_SOURCES:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    continue
                text = (await resp.text()).strip()
                if _looks_like_ipv4(text):
                    return text
        except (aiohttp.ClientError, TimeoutError, OSError):
            continue
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
            # access_ok'ı False'a düşürür; access_reason ilk başarısız URL'in
            # nedeninin kısa kodu (ACC sütununda gösterilir).
            access_ok: bool | None = None
            access_reason: str | None = None
            if access_urls:
                reason = await _access_check(
                    proxy, proxy_type, access_urls, timeout,
                )
                access_ok = reason is None
                if reason is not None:
                    access_reason = reason

            # HTTPS probe: tek istek, iki sonuç (tunnel_ok + mitm_suspected).
            # Üç durum:
            #   1) Judge URL HTTPS  → judge probe ZATEN CONNECT tunnel + TLS
            #      doğrulama testidir. Başarılı olduysa (buraya geldik) tunnel
            #      kanıtlanmış ve cert chain de doğru. Ayrıca _https_probe
            #      atmak boşa istek + zaman. Doğrudan True/False ata.
            #   2) SOCKS proxy   → katman-4 tunnel daima açıktır; tunnel_ok=True
            #      kabul edilir, ama MITM olabilir → yine _https_probe at.
            #   3) HTTP judge + HTTP/HTTPS proxy → judge HTTP forwarding ile
            #      gitti; HTTPS yetisi henüz test edilmedi → _https_probe gerekli.
            tunnel_ok: bool | None = None
            mitm_suspected: bool | None = None
            judge_is_https = judge_url.lower().startswith("https://")
            if tunnel_test:
                if judge_is_https and protocol not in ("socks4", "socks5"):
                    # Durum 1: judge zaten CONNECT + TLS testini geçti.
                    tunnel_ok = True
                    mitm_suspected = False
                else:
                    # Durum 2/3: ayrı HTTPS probe.
                    probe_result = await _https_probe(
                        proxy, proxy_type, timeout,
                    )
                    # SOCKS için tunnel her zaman True; HTTP/HTTPS için
                    # probe'un kararına güven.
                    if protocol in ("socks4", "socks5"):
                        tunnel_ok = True
                    else:
                        tunnel_ok = probe_result.tunnel_ok
                    mitm_suspected = probe_result.mitm_suspected

            return ScanResult(
                proxy=proxy, ok=True, level=level, distorting=distorting,
                outbound_ip=outbound, country=country,
                elapsed=time.monotonic() - started,
                access_ok=access_ok, access_reason=access_reason,
                tunnel_ok=tunnel_ok, mitm_suspected=mitm_suspected,
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


class _DebugLogger:
    """Tarama sırasında her probe'un detayını JSONL olarak `debug.log`'a yazar.

    Format: her satır self-contained JSON; `kind` alanı probe tipini ayırır
    ("access" | "tunnel"). Üretici tarafta sadece `log(**rec)` çağrısı, satır
    her zaman gerçek zamanlı flush edilir (Ctrl+C'de bile o ana kadarki
    kayıtlar disk'te).

    Açık değilse (`_DEBUG is None`) tüm probe yolları gereksiz veri toplamaktan
    kaçınır — overhead sıfıra yakın.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # Line-buffered + her satırdan sonra flush — büyük tarama yarıda
        # kesilse bile log dolu.
        self.fh = open(path, "w", buffering=1, encoding="utf-8")
        self.fh.write(json.dumps({
            "kind": "header",
            "ts": time.time(),
            "ua": USER_AGENT,
            "tunnel_pool": list(TUNNEL_TEST_URLS),
        }, ensure_ascii=False) + "\n")
        self.fh.flush()

    def log(self, **fields) -> None:
        if self.fh is None:
            return
        rec = {"ts": time.time(), **fields}
        try:
            self.fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self.fh.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


# Module-level debug logger. None = debug kapalı. Main thread'de set edilir,
# probe'lar (aynı event loop) doğrudan okur — asyncio single-threaded olduğu
# için lock gerekmez.
_DEBUG: _DebugLogger | None = None


async def _access_check_one(
    proxy: str, proxy_type: ProxyType, url: str, timeout: float,
) -> str | None:
    """Tek bir gatekeeper URL'e proxy üzerinden istek at. None=geçti,
    str=fail reason kodu. Debug açıksa her attempt'i log'lar."""
    host, _, port_str = proxy.partition(":")
    debug = _DEBUG is not None
    started = time.monotonic()
    rec: dict = {
        "kind": "access",
        "proxy": proxy,
        "url": url,
        "ua": USER_AGENT,
    }
    fail_reason: str | None = None
    try:
        connector = ProxyConnector(
            proxy_type=proxy_type, host=host, port=int(port_str), rdns=True,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                rec["status"] = resp.status
                if debug:
                    # CF + origin teşhisi için önemli header'lar
                    rec["server"] = resp.headers.get("Server")
                    rec["cf_ray"] = resp.headers.get("CF-Ray")
                    rec["cf_cache_status"] = resp.headers.get("CF-Cache-Status")
                    rec["content_type"] = resp.headers.get("Content-Type")
                    rec["content_length"] = resp.headers.get("Content-Length")
                    rec["url_final"] = str(resp.url)
                    try:
                        body = await resp.text(errors="replace")
                        rec["body_snippet"] = body[:300]
                    except Exception as be:  # noqa: BLE001
                        rec["body_read_error"] = f"{type(be).__name__}: {be}"[:200]
                if not (200 <= resp.status < 400):
                    fail_reason = str(resp.status)
    except (asyncio.TimeoutError, TimeoutError) as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "to"
    except (
        aiohttp.ClientConnectorError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientOSError,
        aiohttp.ClientPayloadError,
        ConnectionResetError,
    ) as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "err"
    except Exception as e:  # noqa: BLE001
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "?"

    rec["elapsed"] = round(time.monotonic() - started, 3)
    rec["fail_reason"] = fail_reason
    if debug and _DEBUG is not None:
        _DEBUG.log(**rec)
    return fail_reason


async def _access_check(
    proxy: str, proxy_type: ProxyType, urls: list[str], timeout: float,
) -> str | None:
    """Tüm URL'lere erişiyor mu? None = hepsi geçti.

    Geçmedi ise ilk başarısız URL'in nedeni kısa kod olarak döner:
      "to"  → istek timeout'a çakıldı (proxy yavaş ya da CF edge'e ulaşamadı)
      "<N>" → 3 haneli HTTP status (örn. "403" = CF Bot Mgmt yasakladı,
              "503" = CF challenge / Turnstile, "429" = rate limit, "502"/
              "504" = upstream çürük). Status koddan kullanıcı captcha/yasak
              ayrımını yapabilir.
      "err" → bağlantı/proxy kaynaklı IO hatası (ServerDisconnected, TCP RST)
      "?"   → sınıflandırılamayan diğer exception

    ACC sütununda bu kod birebir basılır (3 char), kullanıcı verdict'i tek
    bakışta yorumlasın.

    Debug açıksa her URL attempt'i `debug.log`'a JSONL olarak işlenir.
    """
    for url in urls:
        reason = await _access_check_one(proxy, proxy_type, url, timeout)
        if reason is not None:
            return reason
    return None


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
    # Havuzdan rastgele 1 URL: gstatic.com'u hostname-bazlı bloklayan proxy'ler
    # apple/firefox/cloudflare endpoint'lerini geçirebilir; tek noktaya bağlı
    # olmamak false-negative oranını ciddi düşürür.
    tunnel_url = random.choice(TUNNEL_TEST_URLS)
    debug = _DEBUG is not None
    started = time.monotonic()
    rec: dict = {"kind": "tunnel", "proxy": proxy, "url": tunnel_url,
                 "ua": USER_AGENT}
    connector = ProxyConnector(
        proxy_type=proxy_type, host=host, port=int(port_str), rdns=True,
    )
    result: HttpsProbeResult
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(tunnel_url) as resp:
                rec["status"] = resp.status
                if debug:
                    rec["server"] = resp.headers.get("Server")
                    rec["cf_ray"] = resp.headers.get("CF-Ray")
                # Endpoint'ler farklı status döner: 204 (gstatic), 200 (apple,
                # firefox, cloudflare). 2xx/3xx kabul; aksi durum tunnel açıldı
                # ama upstream sorunlu demek.
                if 200 <= resp.status < 400:
                    result = HttpsProbeResult(True, False)
                else:
                    result = HttpsProbeResult(
                        False, False, error_class=f"HTTP{resp.status}",
                    )
    except aiohttp.ClientConnectorCertificateError as e:
        # Sertifika doğrulama fail oldu: CONNECT açıldı (proxy 200 dönmüş)
        # ama TLS chain proxy tarafından kırılıyor. MITM imzası.
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        result = HttpsProbeResult(True, True, error_class="CertError")
    except aiohttp.ClientSSLError as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        msg = str(e).lower()
        # aiohttp bazen CertVerificationError'ı ClientSSLError olarak
        # sarmalıyor — mesaja bakarak ayırt et.
        if "certificate_verify_failed" in msg or "cert" in msg:
            result = HttpsProbeResult(True, True, error_class="CertError")
        else:
            result = HttpsProbeResult(False, False, error_class="SSL")
    except Exception as e:  # noqa: BLE001
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        result = HttpsProbeResult(False, False, error_class=type(e).__name__)

    rec["elapsed"] = round(time.monotonic() - started, 3)
    rec["tunnel_ok"] = result.tunnel_ok
    rec["mitm_suspected"] = result.mitm_suspected
    if debug and _DEBUG is not None:
        _DEBUG.log(**rec)
    return result


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _self_cpu_time() -> float:
    """Bu Python süreci için kullanıcı+sistem CPU süresi (saniye).

    `os.times()` tüm threadleri (ve asyncio task'larını taşıyan event-loop
    thread'ini) toplam CPU saatleri olarak verir; multi-core sistemde
    elapsed'den yüksek olabilir (ör. 4 core %100 → 400% gibi).
    """
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _self_mem_mb() -> float:
    """Bu sürecin RSS (Resident Set Size) — MB cinsinden.

    Linux'ta `/proc/self/status` → `VmRSS:` satırını kullanır (KB cinsinden).
    Diğer platformlarda 0.0 döner (progress'te "—" gibi sade kalır).
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB → MB
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


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
    # Uzun taramalarda kullanıcı tablo başlığını ekran dışına kaydırdıktan
    # sonra hangi sütunun ne olduğunu unutmasın diye her N OK satırda bir
    # başlık satırı yeniden basılır.
    HEADER_REPEAT = 35
    # Internal kod → (i18n anahtar, içerik için minimum genişlik). Header
    # label'ı bu min'den uzunsa sütun otomatik genişler (örn. en "OUTBOUND"
    # 8 char > içerik IP'si zaten 15 olduğu için min 15 yine kazanır; tr
    # "ERİŞİM" 6 char > min 3 ✓/×/— olduğu için 6'ya genişler).
    _FIXED: dict[str, tuple[str, int]] = {
        "#":      ("table.header.num",    5),
        "STATUS": ("table.header.status", 7),  # "missing" 7, "elendi"/"seviye"/"eksik" sığar
        "BKT":    ("table.header.bkt",    5),   # HOT/WARM/NEW/COLD 4, SICAK 5
        "PROXY":  ("table.header.proxy",  21),
        "LVL":    ("table.header.lvl",    3),
        "OUT":    ("table.header.out",    15),
        "CC":     ("table.header.cc",     16),  # tam isim sığsın ("Birleşik Krallık" 16, "United Kingdom" 14)
        "TIME":   ("table.header.time",   6),
        "TUN":    ("table.header.tun",    3),
        "MITM":   ("table.header.mitm",   8),   # "MITM YOK" 8, "NO MITM" 7
        "ACC":    ("table.header.acc",    3),
    }

    # Bucket internal name → i18n key. t() ile çevrilir; HOT/WARM/NEW/COLD
    # ya da SICAK/ILIK/YENİ/SOĞUK olarak görünür. Tek harf kısaltma yok.
    _BUCKET_KEY: dict[str, str] = {
        BUCKET_HOT:  "table.bucket.hot",
        BUCKET_WARM: "table.bucket.warm",
        BUCKET_NEW:  "table.bucket.new",
        BUCKET_COLD: "table.bucket.cold",
    }

    def __init__(
        self, enabled: bool, total: int, level_max: int = 3,
        access_mode: str = "off", access_count: int = 0,
        file=sys.stderr,
    ) -> None:
        self.file = file
        self.enabled = enabled
        # ANSI cursor manipülasyonu yalnız TTY'de güvenli; pipe/file'a yazarken
        # progress satırını çizmeyiz (sadece header + OK rows + final summary).
        self.use_ansi = enabled and file.isatty()
        self.total = total
        # CLI'dan gelen --level filtresi. STATUS hesaplaması için lazım:
        # tüm probe testleri geçilse de level_max'tan yüksek seviye "level"
        # status'üne düşer (stdout'a yazılmayacağını kullanıcı bilsin).
        self.level_max = level_max
        # Aktif --access-test modu (off / cloudflare / google / custom). Legend'in
        # ERİŞİM satırını dinamik basmak için kullanılır — kullanıcı tam olarak
        # ne testi yapıldığını sürekli görür.
        self.access_mode = access_mode
        self.access_count = access_count
        self.count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self._headered = False
        self._progress_drawn = False
        # Son header'dan beri kaç OK satır yazıldı — HEADER_REPEAT'e ulaşınca
        # bir alt-header bloğu (separator + label row + separator) yeniden
        # basılır ve sayaç sıfırlanır.
        self._rows_since_header = 0
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
        # Progress satırı için CPU kümül başlangıcı — sürekli ortalama göster.
        self._start_cpu_time = _self_cpu_time()

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
        # Anlık throughput: toplam proxy / geçen süre. İlk birkaç ms'de
        # elapsed≈0 olduğu için sıfıra bölme: max(elapsed, 0.001).
        wall = max(elapsed, 0.001)
        rate = self.count / wall
        # CPU yüzdesi — tarama başından beri kümülatif. Multi-core sistemde
        # 100%'u aşabilir (örn. 4 core'da %100 verim → 400%); bu doğru ve
        # gerçek paralelizmı gösterir.
        cpu_pct = ((_self_cpu_time() - self._start_cpu_time) / wall) * 100
        mem_mb = _self_mem_mb()
        return t(
            "progress.format",
            bar=bar, pct=pct * 100,
            done=self.count, digits=digits, total=self.total,
            ok=self.ok_count, fail=self.fail_count, skip=self.skip_count,
            rate=rate, cpu=cpu_pct, mem=mem_mb, elapsed=elapsed,
        )

    def _progress_legend(self) -> str:
        """Progress'in ALTINDA her güncellemede yazılan açıklama satırı.

        Seviye kodlarının (L1/L2/L2d/L3) tam karşılıklarını ve sütun
        değerlerinin anlamını sürekli görünür tutar — kullanıcı tarama
        sırasında tabloya bakarak hatırlamak zorunda kalmaz.

        ERİŞİM satırı dinamik: hangi gatekeeper preset'inin aktif olduğuna
        göre (CloudFlare WAF/Bot, Google connectivity, kullanıcı listesi, ya da
        kapalı) farklı bir i18n string'i basılır."""
        base = t("progress.legend")
        access_key = {
            ACCESS_MODE_OFF:    "progress.legend_access_off",
            ACCESS_MODE_CF:     "progress.legend_access_cloudflare",
            ACCESS_MODE_GOOGLE: "progress.legend_access_google",
            ACCESS_MODE_CUSTOM: "progress.legend_access_custom",
        }.get(self.access_mode, "progress.legend_access_off")
        return base + "\n" + t(access_key, n=self.access_count)

    def _clear_progress_block(self) -> None:
        """En alttaki çok-satırlı progress block'u ANSI ile temizle.

        Block = 1 (top padding) + 1 (bar) + N (legend) + 1 (bottom padding).
        Legend i18n dize'sinde `\\n` sayısı + 1 kadar satır kapsar.

        Cursor bottom padding satırının başında varsayılır (block yazıldıktan
        sonra son `\\n` cursor'u oraya bırakır):
          \\r\\033[K          → mevcut (bot pad) satırını sil
          (\\033[A\\r\\033[K)*k → k kez "bir satır yukarı, sil"
        Sonuç: cursor top padding satırı başında, tüm block boş.
        """
        legend_lines = self._progress_legend().count("\n") + 1
        # bar + legend + 3 padding (top + mid + bottom blank)
        total_lines = 1 + legend_lines + 3
        parts = ["\r\033[K"]
        parts.extend(["\033[A\r\033[K"] * (total_lines - 1))
        self.file.write("".join(parts))

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

        # Mevcut 2-satırlık progress block'u temizle (varsa) — sadece TTY'de.
        if self.use_ansi and self._progress_drawn:
            self._clear_progress_block()

        # Sadece OK satırlarını tabloya ekle. Fail'ler sayıma katıldı ama
        # tablo gürültüsünü artırmasın.
        if r.ok:
            # STATUS önceliği (yüksekten düşüğe):
            #   1) outbound_ip yok → "missing"/"eksik" — judge yanıtı parse
            #      edildi ama REMOTE_ADDR alanı boş (CF challenge / DNS hijack
            #      / kesik body). L1 sınıflandırması güvenilmez; çıktıya
            #      yazılmaz ama tabloda görünmesi nedeni kullanıcıya anlatır.
            #   2) tunnel/mitm/access testlerinden biri düştü → "filter"
            #   3) testler geçti AMA anonimlik seviyesi level_max'tan yüksek
            #      → "level" (stdout'a yazılmayacak, kullanıcı bilmeli)
            #   4) hepsi tamam → "ok"
            if r.outbound_ip is None:
                status = t("table.status.unknown")
            elif (
                (r.access_ok is False)
                or (r.tunnel_ok is False)
                or (r.mitm_suspected is True)
            ):
                status = t("table.status.filter")
            elif r.level is not None and r.level > self.level_max:
                status = t("table.status.level")
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

            bkt_key = self._BUCKET_KEY.get(r.bucket or "")
            bkt = t(bkt_key) if bkt_key else "—"
            # MITM kolonu: True = TLS chain kırık (kırmızı bayrak). Mantıken
            # ters: ✓ = MITM YOK (güvenli), × = MITM şüphesi. _mark'a
            # `not mitm_suspected` veriyoruz ki ✓ = iyi semantiği kalsın.
            mitm_mark = (
                "—" if r.mitm_suspected is None
                else ("✓" if not r.mitm_suspected else "×")
            )
            # ACC: ✓ (geçti) / 3 char reason kod (403, 503, to, err, ?) / —
            # kod = _access_check'in ilk başarısız URL için döndürdüğü neden.
            if r.access_ok is None:
                acc_cell = "—"
            elif r.access_ok:
                acc_cell = "✓"
            else:
                acc_cell = r.access_reason or "×"
            cells = [
                f"{self.count}/{self.total}",
                status,
                bkt,
                r.proxy,
                lvl,
                r.outbound_ip or "—",
                i18n.country_name(r.country) if r.country else "—",
                f"{r.elapsed:.1f}s",
                _mark(r.tunnel_ok),
                mitm_mark,
                acc_cell,
            ]
            # HEADER_REPEAT OK satırı geçtiyse, bu satırdan önce başlık
            # bloğunu yeniden bas. Kullanıcı uzun taramalarda terminal
            # scroll'ladıktan sonra sütun adlarına tekrar bakabilir.
            if self._rows_since_header >= self.HEADER_REPEAT:
                labels = [self._labels[code] for code in self._cols.keys()]
                self.file.write(self._border("├", "┼", "┤") + "\n")
                self.file.write(self._row(labels) + "\n")
                self.file.write(self._border("├", "┼", "┤") + "\n")
                self._rows_since_header = 0
            self.file.write(self._row(cells) + "\n")
            self._rows_since_header += 1

        # Progress block'u en altta yeniden çiz (TTY varsa):
        #   [üst boşluk] · bar+sayım · [boşluk] · legend · [alt boşluk]
        # Üç boşluk: block'u OK satırlarından ayır, bar'ı legend'den ayır,
        # block'u terminal alt kenarından ayır.
        if self.use_ansi:
            self.file.write("\n")                              # top padding
            self.file.write(self._progress_line() + "\n")      # bar+count
            self.file.write("\n")                              # mid padding (bar↔legend)
            self.file.write(self._progress_legend() + "\n")    # legend + bot padding
            self._progress_drawn = True

        self.file.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        # Canlı progress block'u (üst pad + bar + legend + alt pad) temizle.
        if self.use_ansi and self._progress_drawn:
            self._clear_progress_block()
        # Tablo varsa bottom border'ı kapat.
        if self._headered:
            self.file.write(self._border("└", "┴", "┘") + "\n")
        # Statik final progress block — canlı block ile aynı padding
        # düzeni: üst + bar + orta + legend + alt boşluk.
        self.file.write("\n")
        self.file.write(self._progress_line() + "\n")
        self.file.write("\n")
        self.file.write(self._progress_legend() + "\n")
        self.file.write("\n")
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


_KEYVAL_BOX_MAX_VALUE = 80  # tek value satırı maks. karakter (sığmazsa wrap)


def _wrap_value(value: str, width: int) -> list[str]:
    """Uzun bir value'yu `width` sınırına göre satırlara böl.

    Öncelik sırası:
      1. Virgüllerden böl (URL listeleri, ülke listeleri için doğal)
      2. Boşluklardan böl
      3. Çaresizse hard-break (kelime ortasında)
    """
    if len(value) <= width:
        return [value]
    # Önce virgüllerden bölmeyi dene — `--access-test` URL listesi gibi.
    if "," in value:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        lines: list[str] = []
        current = ""
        for i, p in enumerate(parts):
            piece = p + ("," if i < len(parts) - 1 else "")
            candidate = (current + " " + piece).strip() if current else piece
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = piece
        if current:
            lines.append(current)
        if lines and all(len(ln) <= width for ln in lines):
            return lines
    # Boşluk-tabanlı wrap (textwrap.wrap)
    import textwrap
    wrapped = textwrap.wrap(value, width=width, break_long_words=True,
                            break_on_hyphens=False)
    return wrapped or [value[:width]]


def _print_keyval_box(
    title: str, rows: list[tuple[str, str]], file,
) -> None:
    """Generic etiketli kutu yazıcı. CONFIG ve RESULT için ortak.

    Başlık sol kutucuğa gömülür; üst ve alt sınır `┬`/`┴` ile aynı yerde
    bölünür. _KEYVAL_BOX_MAX_VALUE'dan uzun value'lar otomatik olarak
    satırlara wrap edilir; ek satırlar key sütunu boş olarak gözükür:
       ┌ TITLE ──────┬─────────────────────────────┐
       │ key         │ uzun bir değer ilk parça    │
       │             │ devamı...                   │
       └─────────────┴─────────────────────────────┘
    """
    if not rows:
        return
    # Önce value'ları wrap'le; sonra max genişlikleri hesapla.
    wrapped_rows: list[tuple[str, list[str]]] = [
        (k, _wrap_value(v, _KEYVAL_BOX_MAX_VALUE)) for k, v in rows
    ]
    w_key = max(len(k) for k, _ in wrapped_rows)
    w_val = max(
        max(len(line) for line in vlines)
        for _, vlines in wrapped_rows
    )
    key_box_width = w_key + 2   # " key " (padding hem solda hem sağda)
    val_box_width = w_val + 2

    title_text = f" {title} "
    if len(title_text) <= key_box_width:
        title_seg = title_text + "─" * (key_box_width - len(title_text))
    else:
        # Başlık key kutucuğuna sığmıyor — kısalt.
        title_seg = f" {title[: key_box_width - 3]}…"[:key_box_width]

    print("┌" + title_seg + "┬" + "─" * val_box_width + "┐", file=file)
    for k, vlines in wrapped_rows:
        # İlk satırda key görünür; takip eden wrap satırlarında key alanı boş.
        for i, line in enumerate(vlines):
            key_cell = k if i == 0 else ""
            print(f"│ {key_cell:<{w_key}} │ {line:<{w_val}} │", file=file)
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
    if getattr(args, "exclude_country", None):
        rows.append((t("row.country_exclude"), args.exclude_country))
    if getattr(args, "exclude_distorting", False):
        rows.append((t("row.exclude_distorting"), on))
    # --allow-* override'ları: default'tan saparsa CONFIG'te göster ki
    # kullanıcı "neden MITM × output'ta?" gibi sürprize düşmesin.
    if getattr(args, "allow_tunnel_fail", False):
        rows.append((t("row.allow_tunnel_fail"), on))
    if getattr(args, "allow_mitm", False):
        rows.append((t("row.allow_mitm"), on))
    if getattr(args, "allow_access_fail", False):
        rows.append((t("row.allow_access_fail"), on))
    # --user-agent override edildiyse CONFIG'te göster (default Firefox UA
    # uzun ve gürültülü; sadece override anlamlı bilgi taşır).
    if getattr(args, "user_agent", None):
        rows.append((t("row.user_agent"), args.user_agent))
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


def _apply_protocol_defaults(args: argparse.Namespace) -> None:
    """Protokole bağımlı default'ları çöz.

    -p http: HTTP proxy'ler genelde basit forwarding yapar; HTTPS CONNECT
      tüneli kuramayan ya da CF gatekeeper'lara HTTPS ile erişmesi gerek-
      meyen proxy'ler default'ta yanlışlıkla elenmesin. tunnel/mitm/access
      testlerini OFF default'la.
    -p https/socks4/socks5: tam güvenlik bataryasını koştur (mevcut davranış).

    Kullanıcı flag'i açıkça verdiyse (örn. `--tunnel-test`) argparse zaten
    None'dan başka bir değer üretir; sentinel kontrolüyle override edilmez.
    """
    http_only = (args.protocol == "http")
    if args.tunnel_test is None:
        args.tunnel_test = not http_only
    if args.mitm_test is None:
        args.mitm_test = not http_only
    if args.access_test is None:
        args.access_test = None if http_only else ACCESS_AUTO_SENTINEL


def _show_db_stats(args: argparse.Namespace) -> int:
    """--db-stats: reputation DB(leri) özetle ve çık. Scan yok.

    Hangi DB(leri) gösterilir:
      1. --reputation PATH explicit verildiyse → sadece o
      2. -p PROTOCOL verildiyse → state-<protocol>.db
      3. Hiçbiri yoksa → varsayılan dizindeki tüm state*.db dosyalarını
         enumerate et, her biri için ayrı box bas
    """
    from pathlib import Path as _Path

    paths: list[_Path] = []
    if args.reputation:
        paths.append(_Path(args.reputation))
    elif args.protocol:
        paths.append(default_db_path(args.protocol))
    else:
        # Otomatik enumerasyon — kullanıcı hangi protokollerin DB'si var
        # bilmek zorunda olmasın. Glob `state*.db` legacy state.db'yi de
        # toplar; alfabetik sıralı bas.
        db_dir = default_db_dir()
        if db_dir.exists():
            paths = sorted(db_dir.glob("state*.db"))
        if not paths:
            print(
                f"proxyprof: {t('misc.db_missing', path=db_dir)}",
                file=sys.stderr,
            )
            return 1

    rc = 0
    for db_path in paths:
        if not db_path.exists():
            print(
                f"proxyprof: {t('misc.db_missing', path=db_path)}",
                file=sys.stderr,
            )
            rc = 1
            continue
        _print_one_db_stats(db_path, args.dead_threshold)
    return rc


def _print_one_db_stats(db_path, dead_threshold: int) -> None:
    """Tek bir DB için stats kutusunu bas. Çoklu enumerasyonda her DB
    için ayrı çağrılır."""
    rep = Reputation(db_path)
    try:
        info = rep.summary(dead_threshold=dead_threshold)
    finally:
        rep.close()

    total = info["total"]

    def _pct(n: int) -> str:
        if not total:
            return "—"
        return f"{100.0 * n / total:.1f}%"

    rows: list[tuple[str, str]] = [
        (t("row.db_path"),    info["db_path"]),
        (t("row.run_index"),  f"#{info['run_index']}"),
        (t("row.db_total"),   f"{total:,}"),
        (t("row.db_hot"),     f"{info['hot']:,}  ({_pct(info['hot'])})"),
        (t("row.db_warm"),    f"{info['warm']:,}  ({_pct(info['warm'])})"),
        (t("row.db_cold"),    f"{info['cold']:,}  ({_pct(info['cold'])})"),
    ]
    for status_key in ("ok", "filter", "fail"):
        n = info["statuses"].get(status_key, 0)
        if n:
            rows.append((
                t("row.db_status", status=status_key),
                f"{n:,}  ({_pct(n)})",
            ))
    for age_label, n in info["ages"].items():
        if n:
            rows.append((
                t("row.db_age", age=age_label),
                f"{n:,}  ({_pct(n)})",
            ))
    if info["probation_factors"]:
        parts = [
            f"cf={cf}:{n}"
            for cf, n in info["probation_factors"][:8]
        ]
        rows.append((t("row.db_probation"), "  ".join(parts)))
    if info["countries"]:
        parts = [f"{cc}={n:,}" for cc, n in info["countries"][:8]]
        rows.append((t("row.db_countries"), "  ".join(parts)))

    _print_keyval_box(t("box.title.db_stats"), rows, sys.stderr)


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
    judge_incomplete = counts.get("judge_incomplete", 0)
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
    if judge_incomplete:
        rows.append((t("row.judge_incomplete"),
                     t("value.judge_incomplete_count", n=judge_incomplete)))
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


def _passes_output_filters(
    r: ScanResult,
    args: argparse.Namespace,
    access_urls: list[str],
    country_whitelist: set[str],
    country_blacklist: set[str],
) -> bool:
    """Çıktı dosyasına yazılır mı?

    Tüm post-scan filtre kapılarını tek noktada uygular. Test flag'leri
    (`--tunnel-test`, vb.) testi koşturur; bu fonksiyondaki kapılar testin
    sonucuna göre output kararı verir. `--allow-*` flag'leri ilgili kapıyı
    devre dışı bırakır — test yine çalışır, sonuç tabloda görünür ama
    output'ta filtrelenmez.
    """
    if not r.ok or r.level is None:
        return False
    # Judge yanıtı parse edildi ama REMOTE_ADDR alanı boş → L1 sınıflandırması
    # güvenilmez (CF challenge / DNS hijack / kesik body). Tabloda görünür
    # ama output'a yazılmaz.
    if r.outbound_ip is None:
        return False
    if r.level > args.level:
        return False
    if (access_urls and not getattr(args, "allow_access_fail", False)
            and not r.access_ok):
        return False
    if (args.tunnel_test and not getattr(args, "allow_tunnel_fail", False)
            and r.tunnel_ok is False):
        return False
    if (args.mitm_test and not getattr(args, "allow_mitm", False)
            and r.mitm_suspected is True):
        return False
    if args.exclude_distorting and r.distorting:
        return False
    cc = (r.country or "").upper()
    if country_whitelist and cc not in country_whitelist:
        return False
    if country_blacklist and cc in country_blacklist:
        return False
    return True


class _StreamWriter:
    """Tarama sırasında her filtre-geçen proxy'yi dosyaya akıt + dedupe.

    Plan B: yarıda kesilse bile dosyada bulunmuş proxy'ler kalır (Ctrl+C,
    OOM, terminal kapanması = sıfır kayıp). Tarama başarıyla tamamlanırsa
    `finalize()` dosyayı sort+dedupe edilmiş haliyle atomic-replace eder.

    Stdout output için (path=None) stream yapılmaz; eskisi gibi finalize'da
    toplu yazılır — stdout'ta zaten satır-satır akış görmenin değeri yok,
    pipe alıcısı genelde bütün listeyi bekler.
    """

    def __init__(
        self,
        path: str | None,
        passes: Callable[[ScanResult], bool],
    ) -> None:
        self.path = path
        self.passes = passes
        self.seen: set[str] = set()
        self._fh = None
        if path:
            # Line-buffered + her satırdan sonra flush: Ctrl+C anında disk'te
            # mevcut tüm yazımlar kalır (kernel page cache değil, dosyaya).
            self._fh = open(path, "w", buffering=1, encoding="utf-8")

    def on_result(self, r: ScanResult) -> None:
        if r.proxy in self.seen:
            return
        if not self.passes(r):
            return
        self.seen.add(r.proxy)
        if self._fh is not None:
            self._fh.write(r.proxy + "\n")
            self._fh.flush()

    def finalize(self) -> list[str]:
        """Sort+dedupe edilmiş kept listesini döndür.

        File output: atomic replace ile dosyayı sıralı haliyle değiştir
        (`.tmp` + os.replace). Hata olursa ham (sırasız) stream dosyası
        yerinde kalır — kayıp yok.
        Stdout: sadece sıralı listeyi döndür, çağıran print eder.
        """
        ordered = sorted(self.seen, key=_ip_port_sort_key)
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            if self.path:
                tmp = self.path + ".tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        for line in ordered:
                            f.write(line + "\n")
                    os.replace(tmp, self.path)
                except OSError:
                    # Sort/replace başarısız → ham stream dosyası kalır,
                    # kullanıcı sıralanmamış ama tam listeye sahip olur.
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        return ordered


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
    writer: _StreamWriter | None = None,
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
            if writer is not None:
                writer.on_result(r)
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


def _resolve_access_test(arg: str | None) -> tuple[list[str], str]:
    """args.access_test → (URL listesi, mode).

    Mode değerleri ACCESS_MODE_* sabitleri; UI legend'inde "ne testi yapılıyor"
    bilgisini göstermek için kullanılır.

    None                    → ([], OFF)
    "AUTO" / "cloudflare"   → CF_GATEKEEPERS'tan rastgele {AUTO_COUNT} site, CF
    "google"                → GOOGLE_GATEKEEPERS'tan rastgele {AUTO_COUNT} site, GOOGLE
    "url1,url2"             → kullanıcı verdiği URL'ler (validate edilir), CUSTOM
    """
    if arg is None:
        return [], ACCESS_MODE_OFF
    norm = arg.strip().lower()
    if norm in (ACCESS_AUTO_SENTINEL.lower(), ACCESS_PRESET_CLOUDFLARE):
        k = min(ACCESS_AUTO_COUNT, len(CF_GATEKEEPERS))
        return random.sample(CF_GATEKEEPERS, k=k), ACCESS_MODE_CF
    if norm == ACCESS_PRESET_GOOGLE:
        k = min(ACCESS_AUTO_COUNT, len(GOOGLE_GATEKEEPERS))
        return random.sample(GOOGLE_GATEKEEPERS, k=k), ACCESS_MODE_GOOGLE
    return _parse_access_urls(arg), ACCESS_MODE_CUSTOM


def _status(msg: str, silent: bool) -> None:
    """Bootstrap fazında 'şu an X yapıyorum' satırı.

    Tarama başlamadan önce sırayla input okuma → reputation yükleme → public
    IP + judge tespiti aşamaları çalışır; tek başına 3-5 saniyeyi bulabilir
    (özellikle judge listesinin ilkleri ölü ise). Bu sessiz boşluk yerine her
    aşamanın başında ne yapıldığını yazdırırız; bir önceki adımın tamamlandığı
    bir sonraki satırın görünmesinden anlaşılır."""
    if not silent:
        sys.stderr.write(f"proxyprof: {msg}\n")
        sys.stderr.flush()


def _print_cf_judge_warning(judge_url: str, evidence: str) -> None:
    """Kullanıcının `-j` ile verdiği judge CF arkasında tespit edildiğinde basılır.

    Üç ana etkiyi açıkça yaz: bot management false-negative, residential bias,
    HTTPS-only sınırlama. Çıktı stderr'e gider; `--silent` modunda uyarı (ve
    onayı) atlanır — script kullanımında kullanıcıya soru sorma şansı yok zaten.
    """
    sys.stderr.write("\n")
    sys.stderr.write(t("judge.cf_warn_header", url=judge_url) + "\n")
    sys.stderr.write(t("judge.cf_warn_evidence", evidence=evidence) + "\n\n")
    sys.stderr.write(t("judge.cf_warn_intro") + "\n\n")
    sys.stderr.write(t("judge.cf_warn_effect1") + "\n\n")
    sys.stderr.write(t("judge.cf_warn_effect2") + "\n\n")
    sys.stderr.write(t("judge.cf_warn_effect3") + "\n\n")
    sys.stderr.flush()


async def amain(args: argparse.Namespace) -> int:
    _status(t("bootstrap.reading"), args.silent)
    proxies = read_proxies(args.file)
    # --no-access-test her durumda --access-test'in üzerine yazar; AUTO,
    # cloudflare, google preset'i, ya da özel URL listesi — hepsi iptal edilir.
    if args.no_access_test:
        access_urls: list[str] = []
        access_mode = ACCESS_MODE_OFF
    else:
        access_urls, access_mode = _resolve_access_test(args.access_test)

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
        _status(t("bootstrap.loading_reputation", n=len(proxies)), args.silent)
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
    # Not: `task_timeout` adı bilinçli; `t` adı modül-level i18n fonksiyonunu
    # gölgeler ve fonksiyon başındaki `t(...)` çağrılarını UnboundLocalError
    # ile patlatır.
    tasks: list[ScanTask] = []
    for p in ordered_proxies:
        b = bucket_map.get(p) if reputation is not None else None
        task_timeout = args.cold_timeout if b == BUCKET_COLD else args.timeout
        tasks.append(ScanTask(proxy=p, timeout=task_timeout, bucket=b))

    # Tek bir proxysiz HTTP session ile public IP + judge tespit. Proxy başına
    # ayrı connector açacağımız için bu session sadece bootstrap içindir.
    # Bootstrap timeout proxy-başına timeout'tan ayrı tutulur — kullanıcı agresif
    # bir `-T 2` verirse canhazip.com'un TLS handshake'ini kesmesin.
    _status(t("bootstrap.preparing"), args.silent)
    bootstrap_timeout = max(args.timeout, 10.0)
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
    ) as bootstrap:
        public_ip = await get_public_ip(bootstrap, timeout=bootstrap_timeout)

        if args.judge:
            # Kullanıcı explicit judge verdi. CF arkasında mı kontrol et —
            # arkasındaysa tarama biasını uyar ve E/h onayı al.
            judge_url = args.judge
            is_cf, evidence = await is_judge_behind_cf(
                judge_url, bootstrap, timeout=bootstrap_timeout,
            )
            if is_cf and not args.silent:
                _print_cf_judge_warning(judge_url, evidence)
                if not _prompt(t("judge.cf_continue_prompt"), default_yes=True):
                    sys.stderr.write(f"proxyprof: {t('judge.cf_aborted')}\n")
                    if reputation is not None:
                        reputation.close()
                    return 1
        else:
            # Default: CF-dışı judge'lardan rastgele bir sıralama ile dene.
            # `is_judge_behind_cf` ile CF'e geçenleri pre-filter et; geriye
            # kalanları shuffle ile her oturumda farklı sıra dene → tek bir
            # public judge'ın yükünü bizim taramamız üstüne yıkmaz.
            candidates = list(judges_for(args.protocol))
            non_cf: list[str] = []
            for url in candidates:
                is_cf, _ = await is_judge_behind_cf(
                    url, session=None, timeout=bootstrap_timeout,
                )
                if not is_cf:
                    non_cf.append(url)
            if not non_cf:
                # Beklenmedik durum: tüm default'lar CF'e geçmiş. Fail-safe:
                # orijinal listeyi shuffle edip kullan, bias riskini logla.
                sys.stderr.write(f"proxyprof: {t('judge.all_defaults_cf')}\n")
                non_cf = candidates
            random.shuffle(non_cf)
            try:
                judge_url, _ = await pick_judge(
                    bootstrap, non_cf,
                    timeout=bootstrap_timeout,
                )
            except JudgeUnavailable as e:
                print(f"proxyprof: {e}", file=sys.stderr)
                if reputation is not None:
                    reputation.close()
                return 1

    send_identity = _judge_accepts_proxyprof_header(judge_url)

    # HTTP proxy + HTTPS judge uyumsuzluğu uyarısı.
    # HTTPS judge'a giden trafik CONNECT tunnel + TLS içinden geçer, proxy
    # header inject EDEMEZ. Bu yüzden anonimlik tespiti (L1/L2/L2d) bu
    # senaryoda HTTP forwarding gözlemine bağlıdır ve yanıltıcı olabilir:
    # CONNECT-yetkin proxy hep L1 görünür, CONNECT-yetkinsiz proxy plain
    # forwarding'e düşerse L2/L2d görünebilir. HTTP judge auto-seçimi (-j
    # vermezsen) bu sorunu otomatik elimine eder.
    if (
        args.protocol == "http"
        and judge_url.lower().startswith("https://")
        and not args.silent
    ):
        print(f"proxyprof: {t('warn.http_proxy_https_judge')}",
              file=sys.stderr)

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

    # Çalışma modu:
    #   - interactive: ne -o ne -s → kullanıcı tabloyu izleyerek tarama yapıyor.
    #     stdout'a tekrar ip:port basmak gereksiz (zaten tabloda OUT sütununda
    #     görünüyor); filtreler de uygulanmaz (kullanıcı tüm probelanan
    #     proxy'leri görmek ister). Bu mod "keşif" amaçlıdır.
    #   - producer (-o veya -s): kullanıcı veri üretiyor (dosya veya pipe).
    #     Filtreler aktif, çıktı ilgili yere yazılır.
    interactive_mode = not args.output and not args.silent

    # LiveTable total = aslında test edilecek proxy sayısı (probation skipped'lar
    # hariç). Probation skipped'lar tabloda görünmez ama özet kutuda raporlanır.
    # Interactive mod'da level filtresi yokmuş gibi davran (level_max=3) →
    # "seviye" status'ü hiç tetiklenmez; L2d'ler "iyi" görünür.
    table = LiveTable(
        enabled=not args.silent, total=len(tasks),
        level_max=3 if interactive_mode else args.level,
        access_mode=access_mode, access_count=len(access_urls),
    )

    # Country filter set'leri scan başlamadan inşa edilir; stream-writer
    # her sonuçta filtre kararı verebilsin. Whitelist + blacklist mutex
    # (argparse zorunlu kılıyor) — en fazla biri dolu.
    country_whitelist = {
        c.strip().upper()
        for c in (args.country or "").split(",")
        if c.strip()
    }
    country_blacklist = {
        c.strip().upper()
        for c in (getattr(args, "exclude_country", None) or "").split(",")
        if c.strip()
    }
    # Stream-writer: --output dosyaya açıldıysa her filtre-geçen sonuç
    # gerçek zamanlı yazılır. Tarama yarıda kesilse bile dosyada bulunmuş
    # proxy'ler kalır. Stdout output'ta finalize'da toplu yazılır.
    # Interactive mod'da çıktı tamamen kapatılır — `passes` her zaman False.
    if interactive_mode:
        passes_fn: Callable[[ScanResult], bool] = lambda r: False
    else:
        passes_fn = lambda r: _passes_output_filters(
            r, args, access_urls, country_whitelist, country_blacklist,
        )
    try:
        writer = _StreamWriter(path=args.output, passes=passes_fn)
    except OSError as e:
        print(
            f"proxyprof: {t('misc.cannot_open_output', path=args.output, err=e)}",
            file=sys.stderr,
        )
        return 1

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
        writer=writer,
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
              "distort_filtered": 0, "judge_incomplete": 0}
    timings: list[float] = []
    countries: Counter = Counter()
    # Filtre KARARI stream-writer'da verildi; bu loop sadece sayım/istatistik
    # üretir. Her sonuç önce kategori (level/distort/tunnel/mitm) sayımlarına
    # işlenir, sonra "neden filtrelendi" sayaçları için filtre zincirinin
    # aynısı çalıştırılır.
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

        # Filtre sayaçları — neden output'a girmediğini RESULT kutusunda göster.
        # outbound_ip None: judge response eksik (CF challenge / DNS hijack /
        # kesik body). _passes_output_filters bunu zaten False döndürür.
        if r.outbound_ip is None:
            counts["judge_incomplete"] += 1
            continue
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
        cc = (r.country or "").upper()
        if country_whitelist and cc not in country_whitelist:
            counts["country_filtered"] += 1
            continue
        if country_blacklist and cc in country_blacklist:
            counts["country_filtered"] += 1
            continue

    # Writer dosyaya yazdıysa atomic-replace ile sort+dedupe haline çevirir
    # ve seen listesini döndürür. Stdout mod'da seen'i sıralı döner.
    kept_sorted = writer.finalize()

    # Çıktı dökümü:
    #   - `-o FILE`: stream-writer dosyaya zaten yazdı; burada hiçbir şey
    #     yapma.
    #   - `-s` (silent, -o yok): sıralı liste stdout'a basılır → pipe için.
    #   - Interactive (ne -o ne -s): stdout BOŞ kalır; kullanıcı tabloda
    #     zaten OUT sütununu görür, ek liste gereksiz tekrar olurdu.
    if not args.output and not interactive_mode:
        for line in kept_sorted:
            print(line)

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

# ---------------------------------------------------------------------------
# --help renklendirme
# ---------------------------------------------------------------------------
# argparse format_help() çıktısı düz metindir; ANSI renkleri post-process ile
# enjekte ediyoruz. Bu sayede argparse'ın kolon hizalama mantığı (raw uzunluk
# üzerinden) bozulmaz — renk kodları çıktıyı üretildikten SONRA eklenir.
#
# Renkler:
#   • bölüm başlıkları (örn. "options:", "tarama & sondalar:", "Örnekler:") → bold cyan
#   • uzun + kısa opsiyon bayrakları (örn. --reputation, -f) → green
#   • metavar'lar (FILE, PATH, SECONDS, CC[,CC...]) + choices ({http,https,...}) → yellow
#   • (varsayılan: X) / (default: X) → dim
#   • örnek komut satırları (`  $ ...`) → bold green
#
# Devre dışı: NO_COLOR env var (standart, https://no-color.org/) ya da stderr
# bir TTY değilse renksiz çıkar — pipe/file/CI ortamında gürültü yok.

_C_RESET   = "\033[0m"
_C_BOLD    = "\033[1m"
_C_DIM     = "\033[2m"
_C_GREEN   = "\033[32m"
_C_YELLOW  = "\033[33m"
_C_CYAN    = "\033[36m"

# Tek satırlık "header: rest" yapısı: satır non-space ile başlar, içinde ':' YOK,
# ama satır sonunda ':' var. Argparse section başlıkları (`options:`,
# `positional arguments:`, custom grup başlıkları, `Örnekler:`) bu desene
# girer; "usage: ..." satırı GİRMEZ çünkü ':' satırın ortasında.
_RE_SECTION = re.compile(r"^(\S[^\n:]{0,80}:)$", re.MULTILINE)
# "usage:" prefix'i ayrı — satır sonu beklenmez.
_RE_USAGE_PREFIX = re.compile(r"^(usage:)", re.MULTILINE)
# Bayrak + metavar combo: ARGPARSE'ın ürettiği bilinen metavar'lar bayrağın
# hemen ardından gelir. Yalnız bu konum'da boyarsak description'daki "URL",
# "CF", "HTTPS" gibi büyük harfli normal kelimeler false-positive olmaz.
# Whitelist sabit: argparse bu metavar isimlerini koddan alıyor, çevirisi yok.
_METAVAR_NAMES = (
    "FILE", "PATH", "URL", "URLS", "CODE", "SECONDS",
    r"CC\[,CC\.\.\.\]",   # --country için özel metavar
    "N",                   # tek karakter; SADECE bayraktan sonra eşleşir
)
_RE_FLAG_WITH_METAVAR = re.compile(
    r"(--[a-z][\w-]*|-[a-zA-Z])"   # uzun veya kısa bayrak
    r"(\s+\[?)"                     # boşluk(lar), opsiyonel `[` (örn. [URLS])
    r"(" + "|".join(_METAVAR_NAMES) + r")"
    r"(?=[\s,\]\n])"
)
# Tek başına bayraklar (metavar'sız). Yukarıdaki combo ile boyanmamış
# (ANSI escape ile başlayan) eşleşmeleri DIŞLAMAK için lookbehind kullan;
# aksi halde `\x1b[32m--xxx` içindeki `--xxx` tekrar match olur, iç içe
# ANSI sarması bozar.
_RE_LONG_OPT  = re.compile(r"(?<!\x1b\[32m)(--[a-z][\w-]*)")
# Kısa opsiyon `-X`: önünde kelime/tire olmamalı (kelime ortasındaki tireleri
# yakalama); sonunda boşluk/virgül/`]`. ANSI tekrar boyamayı engellemek için
# ayrı bir fixed-width lookbehind ekleniyor. `[` lookbehind'te YASAK DEĞİL —
# `[-h]`, `[-v]` gibi usage'taki bracket'lı kısa bayraklar da boyanmalı.
_RE_SHORT_OPT = re.compile(
    r"(?<![\w-])(?<!\x1b\[32m)(-[a-zA-Z])(?=[\s,\]])"
)
# Choices: argparse `{a,b,c}` formatında verir.
_RE_CHOICES = re.compile(r"(\{[^{}\n]+\})")
# Default değer parantezi — TR ve EN için tek desen.
_RE_DEFAULT = re.compile(
    r"(\((?:varsayılan|default):\s*[^)]+\))", re.IGNORECASE,
)
# Örnek komut satırı: epilog'da `  $ ...` formatında basılır.
_RE_EXAMPLE = re.compile(r"^(  \$ )(.+)$", re.MULTILINE)


def _color_enabled() -> bool:
    """Help çıktısında ANSI renkleri kullanılsın mı?

    https://no-color.org/ konvansiyonu: `NO_COLOR` env var'ı set (boş bile
    olsa) ise renkleri kapat. Ayrıca stderr TTY değilse (örn. `... 2>file`
    veya `... | less`) renksiz çıkar — pipe/CI'a ANSI bulaştırma."""
    if "NO_COLOR" in os.environ:
        return False
    return sys.stderr.isatty()


def _colorize_help(text: str) -> str:
    """argparse format_help() çıktısına ANSI renk ekler.

    Sıra önemli:
      1. DEFAULT — iç metnindeki kelimeleri ileri regex'ler boyamasın diye önce.
      2. CHOICES — bağımsız, her yerde geçerli.
      3. FLAG+METAVAR combo — bayrak yeşil, metavar sarı, TEK pass'te.
      4. Standalone uzun/kısa bayraklar — combo'da yakalanmamış olanlar.
         Lookbehind sayesinde combo'da zaten boyanmışları tekrar boyamaz.
      5. SECTION ve USAGE başlıkları — bold cyan.
      6. Örnek komut satırları — bold green."""
    text = _RE_DEFAULT.sub(lambda m: f"{_C_DIM}{m.group(1)}{_C_RESET}", text)
    text = _RE_CHOICES.sub(lambda m: f"{_C_YELLOW}{m.group(1)}{_C_RESET}", text)
    text = _RE_FLAG_WITH_METAVAR.sub(
        lambda m: (
            f"{_C_GREEN}{m.group(1)}{_C_RESET}"
            f"{m.group(2)}"
            f"{_C_YELLOW}{m.group(3)}{_C_RESET}"
        ),
        text,
    )
    text = _RE_LONG_OPT.sub(lambda m: f"{_C_GREEN}{m.group(1)}{_C_RESET}", text)
    text = _RE_SHORT_OPT.sub(lambda m: f"{_C_GREEN}{m.group(1)}{_C_RESET}", text)
    text = _RE_USAGE_PREFIX.sub(
        lambda m: f"{_C_BOLD}{_C_CYAN}{m.group(1)}{_C_RESET}", text,
    )
    text = _RE_SECTION.sub(
        lambda m: f"{_C_BOLD}{_C_CYAN}{m.group(1)}{_C_RESET}", text,
    )
    text = _RE_EXAMPLE.sub(
        lambda m: f"{m.group(1)}{_C_BOLD}{_C_GREEN}{m.group(2)}{_C_RESET}",
        text,
    )
    return text


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Geniş yardım sütunu + terminal genişliğine uyumlu kaydırma + renkler.

    argparse default `max_help_position=24` uzun bayrak isimlerinin (örn.
    `--probation-max-skip N`) yardım metnini ikinci satıra atmasına yol açar.
    Burada 32'ye çekiyoruz; çoğu bayrak tek satırda kalıyor. Genişliği de
    terminal'e göre 80-110 arasında klipliyoruz; aşırı geniş ekranlarda satırlar
    okunamayacak kadar uzamasın.

    `format_help()` üzerine ANSI renkleri post-process ile enjekte edilir;
    `NO_COLOR` env var veya TTY-değilse renksiz."""

    def format_help(self) -> str:
        text = super().format_help()
        if _color_enabled():
            text = _colorize_help(text)
        return text

    def __init__(self, prog: str) -> None:
        try:
            cols = shutil.get_terminal_size().columns
        except OSError:
            cols = 100
        width = max(80, min(cols, 110))
        super().__init__(prog, max_help_position=32, width=width)


def main(argv: list[str] | None = None) -> int:
    supported_langs = i18n.available_languages()

    # Epilog'u t() ile kur — her örnek iki satır: komutun altında açıklama.
    # Tek satır + fixed-width padding biçiminin yerine; dar terminal'de taşmaz,
    # göz hızlı tarar.
    _examples = [
        ("proxine http -s | proxyprof -p http",                                  "cli.example.pipe"),
        ("proxyprof -p http -f list.lst -l 2 -o ok.lst",                         "cli.example.file_in"),
        ("proxyprof -p socks5 -f - -c 1000 -T 8",                                "cli.example.stdin_socks5"),
        ("proxyprof -p http -f l.lst --access-test https://a.com,https://b.com", "cli.example.access_test_custom"),
        ("proxyprof -p http -f l.lst --no-tunnel-test",                          "cli.example.no_tunnel"),
        ("proxyprof -p http -j https://yours.tld/proxyjudge.php",                "cli.example.cf_judge"),
    ]
    epilog_lines = [t("cli.epilog_header")]
    for cmd, key in _examples:
        epilog_lines.append(f"  $ {cmd}")
        epilog_lines.append(f"      {t(key)}")
    epilog = "\n".join(epilog_lines) + "\n"
    p = argparse.ArgumentParser(
        prog="proxyprof",
        description=t("cli.description"),
        epilog=epilog,
        formatter_class=_HelpFormatter,
    )
    p.add_argument(
        "-p", "--protocol",
        choices=("http", "https", "socks4", "socks5"),
        metavar="PROTO",
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
        "--user-agent", metavar="UA", default=None, dest="user_agent",
        help=t("cli.help.user_agent"),
    )
    # --access-test, --tunnel-test, --mitm-test default'ları protokole bağımlı:
    #   -p http  → üçü de OFF (HTTP forwarding-only proxy'ler CONNECT/HTTPS
    #              gatekeeper testlerine takılırdı; bunlar HTTP proxy'nin
    #              doğal işine bakmaz)
    #   -p https/socks4/socks5 → üçü de ON (mevcut davranış)
    # Kullanıcı `--tunnel-test` / `--no-tunnel-test` (vb.) ile her zaman
    # override edebilir. Resolution post-parse'de `_apply_protocol_defaults`'ta.
    g_scan.add_argument(
        "--access-test", nargs="?", const=ACCESS_AUTO_SENTINEL,
        default=None, metavar="cloudflare|google|URLS",
        help=t("cli.help.access_test", count=ACCESS_AUTO_COUNT),
    )
    g_scan.add_argument(
        "--no-access-test", action="store_true", dest="no_access_test",
        help=t("cli.help.no_access_test"),
    )
    g_scan.add_argument(
        "--tunnel-test", action=argparse.BooleanOptionalAction, default=None,
        dest="tunnel_test",
        help=t("cli.help.tunnel_test",
               url=f"{len(TUNNEL_TEST_URLS)} URL'lik havuz, rastgele seçim"),
    )
    g_scan.add_argument(
        "--mitm-test", action=argparse.BooleanOptionalAction, default=None,
        dest="mitm_test",
        help=t("cli.help.mitm_test"),
    )
    g_scan.add_argument(
        # default=None → main()'de protokol çözüldükten sonra
        # `default_db_path(protocol)` ile çözülür. Per-protocol DB.
        "--reputation", metavar="PATH", default=None,
        help=t("cli.help.reputation",
               default=str(default_db_dir() / "state-<proto>.db")),
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
    # --country (whitelist) ve --exclude-country (blacklist) birlikte
    # kullanılamaz — argparse mutex group ile zorunlu kılınır.
    g_country = g_filter.add_mutually_exclusive_group()
    g_country.add_argument(
        "--country", metavar="CC[,CC...]", default=None,
        help=t("cli.help.country"),
    )
    g_country.add_argument(
        "--exclude-country", metavar="CC[,CC...]", default=None,
        dest="exclude_country",
        help=t("cli.help.exclude_country"),
    )
    g_filter.add_argument(
        "--exclude-distorting", action="store_true",
        help=t("cli.help.exclude_distorting"),
    )
    # --allow-* flag'leri: test çalışır ama başarısızlar output'tan ATIL-
    # MAZ. Tabloda × görünür, dosyaya da yazılır. Bilgi amaçlı tarama veya
    # düşük güvenlik gereksinimi olan use-case için.
    g_filter.add_argument(
        "--allow-tunnel-fail", action="store_true", dest="allow_tunnel_fail",
        help=t("cli.help.allow_tunnel_fail"),
    )
    g_filter.add_argument(
        "--allow-mitm", action="store_true", dest="allow_mitm",
        help=t("cli.help.allow_mitm"),
    )
    g_filter.add_argument(
        "--allow-access-fail", action="store_true", dest="allow_access_fail",
        help=t("cli.help.allow_access_fail"),
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
    g_misc.add_argument(
        "--db-stats", action="store_true", dest="db_stats",
        help=t("cli.help.db_stats"),
    )
    g_misc.add_argument(
        "--debug", nargs="?", const="debug.log",
        default=None, metavar="FILE", dest="debug",
        help=t("cli.help.debug"),
    )

    args = p.parse_args(argv)

    # --user-agent override: modül-level USER_AGENT'ı değiştir. Tüm probe
    # path'leri (judge/access/tunnel/public-IP) bu adı dynamic-lookup eder.
    # Debug init'ten ÖNCE değiştir ki log header'a resolved UA düşsün.
    if args.user_agent:
        global USER_AGENT
        USER_AGENT = args.user_agent

    # --debug açıksa modül-level logger'ı set et. Her probe path'i (_DEBUG
    # is not None) kontrolü ile bu logger'ı kullanır. Açık değilse probe'lar
    # ekstra metadata toplamaz — overhead sıfıra yakın.
    if args.debug:
        global _DEBUG
        try:
            _DEBUG = _DebugLogger(args.debug)
        except OSError as e:
            print(
                f"proxyprof: {t('misc.cannot_open_debug', path=args.debug, err=e)}",
                file=sys.stderr,
            )
            return 1
        print(
            f"proxyprof: {t('misc.debug_enabled', path=args.debug)}",
            file=sys.stderr,
        )

    # --db-stats inspeksiyon modu: tarama yok, sadece reputation DB'yi
    # özetle ve çık. -p/--protocol bu modda gerekmez (argparse'de required
    # olmadığı için kontrolü manuel yapıyoruz).
    if args.db_stats:
        return _show_db_stats(args)

    if not args.protocol:
        p.error(t("misc.protocol_required"))

    # Protokole bağımlı default'lar — `--tunnel-test`, `--mitm-test`,
    # `--access-test` flag'leri kullanıcı tarafından override edilmediyse
    # burada çözülür.
    _apply_protocol_defaults(args)

    # Reputation DB yolu protokole özel: state-<proto>.db. Kullanıcı
    # --reputation ile explicit yol vermediyse burada çözülür.
    if args.reputation is None:
        args.reputation = str(default_db_path(args.protocol))
        # Legacy state.db migrasyon ipucu: eski tek-DB sisteminden geçen
        # kullanıcı için geçmişi korumak isterse manuel rename yolu söyle.
        legacy = default_db_dir() / "state.db"
        target = Path(args.reputation)
        if legacy.exists() and not target.exists() and not args.no_reputation:
            print(
                f"proxyprof: {t('misc.legacy_db_hint', legacy=legacy, target=target)}",
                file=sys.stderr,
            )

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
    finally:
        if _DEBUG is not None:
            _DEBUG.close()


if __name__ == "__main__":
    sys.exit(main())
