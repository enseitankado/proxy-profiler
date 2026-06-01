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
import ipaddress
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
    ("aiodns", "aiodns"),
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
        [str(venv_py), "-c", "import aiohttp, aiohttp_socks, aiodns"],
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
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.creating_venv', dir=venv_dir)}\n")
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
                    f"{_paint('proxyprof:', _C_DIM)} {t('deps.venv_creation_failed')}\n"
                    f"{t('deps.venv_hint')}\n"
                )
                sys.exit(1)

    if not (venv_dir / "bin" / "pip").exists():
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.downloading', url=_GET_PIP_URL)}\n")
        try:
            with urllib.request.urlopen(_GET_PIP_URL, timeout=30) as resp:
                get_pip_src = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            sys.stderr.write(
                f"{_paint('proxyprof:', _C_DIM)} {t('deps.download_failed', err=e)}\n"
                f"{t('deps.download_failed_hint')}\n"
            )
            sys.exit(1)
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.bootstrapping_pip')}\n")
        if subprocess.run([str(venv_py)], input=get_pip_src).returncode != 0:
            sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.pip_bootstrap_failed')}\n")
            sys.exit(1)

    sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.installing', pkgs=' '.join(packages))}\n")
    if subprocess.run(
        [str(venv_py), "-m", "pip", "install", "--quiet", *packages]
    ).returncode != 0:
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.pip_install_failed')}\n")
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
            f"{_paint('proxyprof:', _C_DIM)} {t('deps.missing', pkgs=pkg_str)}\n"
            f"{t('deps.install_with', cmd=f'{sys.executable} -m pip install {pkg_str}')}\n"
        )
        sys.exit(1)

    in_venv = sys.prefix != sys.base_prefix

    if in_venv:
        # Aktif venv'deyiz — sistem Python'unu kirletme riski yok, direkt pip.
        sys.stderr.write(
            f"{_paint('proxyprof:', _C_DIM)} {t('deps.missing', pkgs=pkg_str)}\n"
            f"{t('deps.active_venv_note', prefix=sys.prefix)}\n"
        )
        if not _prompt(
            t("deps.install_prompt", pkgs=pkg_str), default_yes=True,
        ):
            sys.exit(1)
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing]
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.running', cmd=' '.join(cmd))}\n")
        if subprocess.run(cmd).returncode != 0:
            sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('deps.install_failed')}\n")
            sys.exit(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    # Sistem Python'dayız: tek doğru yol yerel venv. PEP 668'i baştan bypass
    # eder, sudo gerektirmez, sistem paketlerini kirletmez. pip yoksa
    # get-pip.py ile bootstrap edilir; varsa direkt kullanılır.
    venv_dir = Path(__file__).resolve().parent / ".venv"
    sys.stderr.write(
        f"{_paint('proxyprof:', _C_DIM)} {t('deps.missing', pkgs=pkg_str)}\n"
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
    country_from_trace,
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


DEFAULT_LEVEL = 3   # filtre yok — çalışan tüm proxy'leri tut (kullanıcı
                    # `--level 1` veya `--level 2` ile daha sıkı süzebilir).
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
#
# **Tasarım: 2-katman**
#   1) CF kendi altyapısı — bot-block YAPMAZ, her zaman 200. False-positive
#      sıfır, ama tek "zone": CF içi → "proxy CF ağına ulaşabiliyor mu" cevabı.
#   2) Mid-tier dev/SaaS müşterileri — anti-bot agresif değil (büyük marka
#      değil, scraper hedefi değil), CF üzerinde farklı edge'lerde →
#      "proxy gerçek bir CF müşterisinin sitesine ulaşabiliyor mu" cevabı.
#
# Big-brand'lar (discord, reddit, medium, upwork...) bilinçli olarak dışlandı:
# yüksek scraping baskısı → CF Bot Fight Mode → /cdn-cgi/trace bile bloklanı-
# yor → random seçildiğinde sağlam proxy'leri haksızca eler. Discord Firefox
# TLS fingerprint'ine TCP RST ile cevap veriyor — bot-blok'un kanıtı.
#
# **Sağlık testi**: `proxyprof --verify-gatekeepers` her URL'i doğrudan test
# eder, ölü/bloklu olanları `~/.config/proxyprof/gatekeepers.txt` overlay'ine
# alive-only liste olarak yazar. Sonraki taramalar overlay varsa onu kullanır.
CF_GATEKEEPERS_DEFAULT: tuple[str, ...] = (
    # ============================================================
    # Tier 1: Cloudflare'in kendi altyapısı (12) — bedrock, ölmez
    # ============================================================
    # CF'in kendi domain'leri /cdn-cgi/trace'i hiç disable etmez, bot-block
    # uygulamaz. Bu URL'ler her zaman 200 döner; auto-verify her zaman geçer.
    "https://1.1.1.1/cdn-cgi/trace",
    "https://one.one.one.one/cdn-cgi/trace",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://workers.cloudflare.com/cdn-cgi/trace",
    "https://cdnjs.cloudflare.com/cdn-cgi/trace",
    "https://blog.cloudflare.com/cdn-cgi/trace",
    "https://developers.cloudflare.com/cdn-cgi/trace",
    "https://docs.cloudflare.com/cdn-cgi/trace",
    "https://pages.cloudflare.com/cdn-cgi/trace",
    "https://radar.cloudflare.com/cdn-cgi/trace",
    "https://speed.cloudflare.com/cdn-cgi/trace",
    "https://community.cloudflare.com/cdn-cgi/trace",

    # ============================================================
    # Tier 2: CF müşteri domain'leri (~20) — zone diversity için
    # ============================================================
    # Düzgün konfigüre edildikleri sürece /cdn-cgi/trace 200 döner.
    # NOT: CF müşterileri zamanla /cdn-cgi/trace'i disable edebilir veya auth
    # arkasına alabilir (bkz. "Çıkarılanlar" listesi). Bu yüzden:
    #   - Session başında otomatik canlılık kontrolü yapılır (oturum-içi prune)
    #   - `--verify-gatekeepers` periyodik çalıştırılırsa alive-only overlay
    #     üretir (~/.config/proxyprof/gatekeepers.txt)
    #
    # Tech / developer tools
    "https://typeform.com/cdn-cgi/trace",
    "https://calendly.com/cdn-cgi/trace",
    "https://www.discord.com/cdn-cgi/trace",
    "https://www.npmjs.com/cdn-cgi/trace",
    "https://www.algolia.com/cdn-cgi/trace",
    "https://www.replit.com/cdn-cgi/trace",
    "https://www.gitter.im/cdn-cgi/trace",
    # SaaS / CRM / analytics
    "https://www.zendesk.com/cdn-cgi/trace",
    "https://www.intercom.com/cdn-cgi/trace",
    "https://www.mailchimp.com/cdn-cgi/trace",
    "https://www.hubspot.com/cdn-cgi/trace",
    "https://www.segment.com/cdn-cgi/trace",
    "https://www.mixpanel.com/cdn-cgi/trace",
    "https://www.crisp.chat/cdn-cgi/trace",
    "https://www.statuspage.io/cdn-cgi/trace",
    # Content / community / e-commerce
    "https://www.medium.com/cdn-cgi/trace",
    "https://www.udemy.com/cdn-cgi/trace",
    "https://www.patreon.com/cdn-cgi/trace",
    "https://www.kickstarter.com/cdn-cgi/trace",
    "https://www.upwork.com/cdn-cgi/trace",
    "https://www.shopify.com/cdn-cgi/trace",
    "https://www.canva.com/cdn-cgi/trace",
    "https://www.ghost.org/cdn-cgi/trace",
    "https://www.framer.com/cdn-cgi/trace",

    # ============================================================
    # Çıkarılanlar (--verify-gatekeepers ile teyit edildi)
    # ============================================================
    # 2026-05-29:
    #   archlinux.org    → HTTP 404 (CF zone /cdn-cgi/trace'i disable etmiş)
    #   huggingface.co   → HTTP 401 (CF zone auth gerektirir hale gelmiş)
)


def _gatekeepers_overlay_path() -> Path:
    """Overlay dosyası yolu: --verify-gatekeepers tarafından yazılır,
    her tarama başında okunur. Format: # comment'ler atlanır; her satır
    bir URL."""
    from reputation import default_db_dir as _ddir
    return _ddir() / "gatekeepers.txt"


def _load_gatekeepers() -> tuple[str, ...]:
    """Overlay varsa onu döndür, yoksa default listeyi.

    Overlay --verify-gatekeepers ile yaratılır; sadece "alive" URL'leri içerir.
    Default listeyi kalıcı değiştirmez (kaynak kod stabil kalır), kullanıcı
    overlay'i silebilir veya elle düzenleyebilir."""
    overlay = _gatekeepers_overlay_path()
    if overlay.exists():
        try:
            urls: list[str] = []
            for line in overlay.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
            if urls:
                return tuple(urls)
        except OSError:
            pass
    return CF_GATEKEEPERS_DEFAULT


# Module load anında çözülür. --verify-gatekeepers overlay'i yazdıktan sonra
# bir sonraki proxyprof çağrısı yeni listeyi görür.
CF_GATEKEEPERS: tuple[str, ...] = _load_gatekeepers()

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
    judge_skipped: bool = False      # --no-judge: judge probe atlandı, anonimlik bilgisi yok


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


def _is_local_ip(ip_str: str) -> bool:
    """Routable unicast olmayan IPv4 mü? RFC1918 + loopback + link-local +
    carrier NAT + reserved (is_global=False) + multicast hepsi atılır. Python
    stdlib'inde `is_global` multicast'i global sayar (multicast = routable,
    sadece unicast değil); proxy bağlamında çağırılamaz → multicast'i de
    explicit ele. Geçersiz IP string'i için False döner (caller filtrelemeden
    geçirir; parse aşaması zaten geçersizleri eler)."""
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except (ValueError, ipaddress.AddressValueError):
        return False
    return (not addr.is_global) or addr.is_multicast


def filter_local_ips(proxies: list[str]) -> tuple[list[str], int]:
    """Yerel ağ IP'lerini ele; (kalan, atılan_sayı) döner.

    Default davranış: `--keep-local-ips` verilmediyse input listesindeki
    RFC1918 / loopback / link-local / multicast adresleri at — bir public
    proxy listesinde 192.168.x.y görmek kullanıcı hatasıdır (yanlış kopyala,
    private subnet sızıntısı) ve proxy üzerinden zaten internet'e çıkamaz.
    """
    kept: list[str] = []
    dropped = 0
    for p in proxies:
        ip = p.partition(":")[0]
        if _is_local_ip(ip):
            dropped += 1
        else:
            kept.append(p)
    return kept, dropped


def read_proxies(file_arg: str | None) -> list[str]:
    if file_arg in (None, "-", "STDIN"):
        if sys.stdin.isatty():
            sys.exit(f"{_paint('proxyprof:', _C_DIM)} {t('input.no_input')}")
        text = sys.stdin.read()
    else:
        try:
            with open(file_arg, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            sys.exit(f"{_paint('proxyprof:', _C_DIM)} {t('input.cannot_read', file=file_arg, err=e)}")
    proxies = parse_proxies(text)
    if not proxies:
        sys.exit(f"{_paint('proxyprof:', _C_DIM)} {t('input.no_valid_pairs')}")
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
    no_judge: bool = False,
    access_timeout: float | None = None,
    access_strict: bool = False,
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
        # `judge_elapsed` = bu attempt'in judge round-trip süresi (TEK HTTP
        # isteği). SÜRE sütununda gösterilen değer budur — access/tunnel/MITM
        # probe'larının zamanı dahil DEĞİL. Böylece kullanıcı "bu proxy
        # gerçekten ne kadar hızlı?" sorusuna sağlıklı cevap alır; total wall
        # time `time.monotonic() - started` (probe başından retry'lara kadar)
        # internal metric olarak ScanResult'ta yer almaz.
        judge_elapsed: float | None = None
        try:
            if no_judge:
                # --no-judge: judge probe tamamen atlanır. Anonimlik tespiti
                # (L1/L2/L2d/L3), çıkış IP'si ve ülke bilgisi yok. Sadece
                # tunnel/access/mitm testleri çalışır → "proxy ulaşılabilir
                # ve HTTPS taşıyabiliyor mu" sorusuna yanıt verir.
                level: int | None = None
                distorting: bool = False
                outbound: str | None = None
                country: str | None = None
            else:
                connector = ProxyConnector(
                    proxy_type=proxy_type, host=host, port=port, rdns=True,
                )
                judge_t0 = time.monotonic()
                async with aiohttp.ClientSession(
                    connector=connector,
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as session:
                    # X-Proxyprof-Proxy: judge'a "ben bu protokolden, bu
                    # IP:PORT'tan geliyorum" der. SADECE kullanıcının trusted
                    # listesindeki domain'lerde olan judge'lara gönderilir.
                    extra_headers: dict[str, str] = {}
                    if send_identity:
                        extra_headers["X-Proxyprof-Proxy"] = f"{protocol}://{proxy}"
                    async with session.get(
                        judge_url, headers=extra_headers,
                    ) as resp:
                        body = await resp.text(errors="replace")

                # Judge round-trip tamamlandı: bu attempt'in canonical "proxy
                # latency"si. Connector setup'ı dahil ama probe-içi normal
                # davranış (TCP handshake + HTTP request + response read).
                judge_elapsed = time.monotonic() - judge_t0
                headers = parse_judge_response(body)
                if not headers:
                    return ScanResult(
                        proxy=proxy, ok=False, level=None,
                        elapsed=judge_elapsed,
                        error="judge returned unparseable body",
                    )
                level, distorting = detect_level(headers, public_ip)
                outbound = remote_addr(headers)
                country = extract_country(headers)

            # Access test: tüm URL'ler pass etmek zorunda. Tek bir hata
            # access_ok'ı False'a düşürür; access_reason ilk başarısız URL'in
            # nedeninin kısa kodu (ACC sütununda gösterilir).
            #
            # Side-channel: `/cdn-cgi/trace` URL'leri yanıtın body'sinde
            # `loc=XX` ile ülke bilgisi taşır. _access_check bunu parse edip
            # döner; main judge non-CF olsa bile ülke yakalanır. Judge'tan
            # gelen country önceliklidir (kanonik kaynak); access'ten gelen
            # sadece judge'da yoksa kullanılır.
            access_ok: bool | None = None
            access_reason: str | None = None
            access_first_elapsed: float | None = None
            if access_urls:
                # Access için ayrı timeout: judge timeout'tan farklı; default
                # `timeout × 2`. HTTPS + CONNECT + TLS handshake için 5s tight,
                # 10s rahat. _apply_protocol_defaults'ta hesaplanır; None ise
                # fallback olarak judge timeout kullanılır.
                a_timeout = access_timeout if access_timeout is not None else timeout
                reason, access_country, access_first_elapsed = await _access_check(
                    proxy, proxy_type, access_urls, a_timeout,
                    strict=access_strict,
                )
                access_ok = reason is None
                if reason is not None:
                    access_reason = reason
                if not country and access_country:
                    country = access_country

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
            tunnel_elapsed: float | None = None
            # judge_is_https: judge probe yapıldıysa True/False, atlandıysa False
            # (no_judge mode'da CONNECT'in judge probe ile kanıtlandığı varsayımı
            # tutmaz → ayrı _https_probe gerekir).
            judge_is_https = (
                not no_judge
                and judge_url is not None
                and judge_url.lower().startswith("https://")
            )
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
                    tunnel_elapsed = probe_result.elapsed

            # Access-katmanı MITM tespitini propagate et — kritik güvenlik
            # düzeltmesi. Tunnel probe küçük bir URL havuzunda test eder ve
            # MITM proxy o URL'leri beyaz listelemiş olabilir (debug data'da
            # access fail'in %88'i bu nedenleydi). Access probe gatekeeper
            # URL'lerine (CF zone'lar) gider; orada MITM yakalanırsa tunnel
            # testinin "temiz" verdiği karara güvenmemek lazım. Override:
            # mitm_suspected=True. Tunnel hâlâ True kalır (CONNECT açılıyor,
            # sadece TLS chain bozuluyor) — eski tunnel probe MITM bulgu
            # semantiği ile aynı.
            if access_reason == "mitm":
                mitm_suspected = True

            # SÜRE sütunu için canonical değer: tek temsil edici HTTP round-trip
            # süresi (proxy'nin gerçek latency'sini yansıtsın). Öncelik sırası:
            #   1) judge_elapsed (en stabil; her başarılı tarama yapar)
            #   2) access_first_elapsed (no_judge mode'da fallback)
            #   3) tunnel_elapsed (no_judge + no_access; nadir kombinasyon)
            #   4) Total wall (hiçbir probe çalışmadıysa, teorik fallback)
            # Hiçbir durumda access'in 3 probe'unun + tunnel'ın TOPLAMı SÜRE'ye
            # konmaz — eskisi gibi 1s'lik bir proxy'yi 3s gibi gösteren kirli
            # metrikten kurtulduk.
            probe_elapsed = (
                judge_elapsed
                if judge_elapsed is not None
                else access_first_elapsed
                if access_first_elapsed is not None
                else tunnel_elapsed
                if tunnel_elapsed is not None
                else time.monotonic() - started
            )

            return ScanResult(
                proxy=proxy, ok=True, level=level, distorting=distorting,
                outbound_ip=outbound, country=country,
                elapsed=probe_elapsed,
                access_ok=access_ok, access_reason=access_reason,
                tunnel_ok=tunnel_ok, mitm_suspected=mitm_suspected,
                judge_skipped=no_judge,
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
) -> tuple[str | None, str | None, float]:
    """Tek bir gatekeeper URL'e proxy üzerinden istek at.

    Returns:
        (fail_reason, country, elapsed)
          fail_reason: None=geçti, str=hata kodu (to/err/?/<HTTP_N>)
          country:     URL `/cdn-cgi/trace` ise ve body'de `loc=XX` varsa
                       proxy'nin çıkış ülke kodu; aksi halde None.
          elapsed:     Bu probe'un tek round-trip süresi (saniye).
                       Probe'lar arası ortalama değil, sadece bu istek.

    Debug açıksa her attempt'i log'lar.
    """
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
    country: str | None = None
    # CF /cdn-cgi/trace endpoint'lerinden body parse edip `loc=XX` çekeceğiz —
    # ek istek değil, mevcut response'tan side-channel bilgi. Sadece bu pattern
    # için body okunur; diğer access URL'leri (Google /generate_204 vs custom)
    # ek bandwidth harcamaz.
    is_trace_endpoint = "/cdn-cgi/trace" in url
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
                # Body okuma kararı: debug açıksa her zaman, değilse SADECE
                # trace endpoint + 2xx için (country extraction). 4xx/5xx'te
                # body parse etmek anlamsız (CF challenge HTML olabilir).
                body_text: str | None = None
                want_body = debug or (
                    is_trace_endpoint and 200 <= resp.status < 300
                )
                if want_body:
                    try:
                        body_text = await resp.text(errors="replace")
                    except Exception as be:  # noqa: BLE001
                        rec["body_read_error"] = f"{type(be).__name__}: {be}"[:200]
                if debug:
                    rec["server"] = resp.headers.get("Server")
                    rec["cf_ray"] = resp.headers.get("CF-Ray")
                    rec["cf_cache_status"] = resp.headers.get("CF-Cache-Status")
                    rec["content_type"] = resp.headers.get("Content-Type")
                    rec["content_length"] = resp.headers.get("Content-Length")
                    rec["url_final"] = str(resp.url)
                    if body_text is not None:
                        rec["body_snippet"] = body_text[:300]
                if not (200 <= resp.status < 400):
                    fail_reason = str(resp.status)
                elif is_trace_endpoint and body_text:
                    country = country_from_trace(body_text)
                    if debug:
                        rec["country"] = country
    except (asyncio.TimeoutError, TimeoutError) as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "to"
    # MITM tespiti — ClientOSError'dan ÖNCE özel yakala. ClientSSLError
    # ClientOSError'ın alt sınıfı olduğu için sıra önemli. CERTIFICATE_
    # VERIFY_FAILED imzası varsa proxy MITM yapıyor → fail_reason="mitm"
    # (sıradan "err" değil) → probe() seviyesinde mitm_suspected=True'ya
    # propagate edilir → MITM YOK sütunu × olur.
    except aiohttp.ClientConnectorCertificateError as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "mitm"
    except aiohttp.ClientSSLError as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        if "certificate_verify_failed" in str(e).lower() or "cert" in str(e).lower():
            fail_reason = "mitm"
        else:
            fail_reason = "err"  # SSL ama cert hatası değil (protocol mismatch vb.)
    except (
        aiohttp.ClientConnectorError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientOSError,
        aiohttp.ClientPayloadError,
        ConnectionResetError,
    ) as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        # Debug log'da gözlemlenmiş kalıp: aiohttp bazen cert hatasını düz
        # ClientOSError olarak sarmalıyor (özellikle SOCKS5+CONNECT akışında
        # python-socks katmanı arası). Mesajdan ek tespit yapalım.
        if "certificate_verify_failed" in str(e).lower():
            fail_reason = "mitm"
            rec["mitm_detected_in_oserror"] = True
        else:
            fail_reason = "err"
    except Exception as e:  # noqa: BLE001
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        fail_reason = "?"

    elapsed = time.monotonic() - started
    rec["elapsed"] = round(elapsed, 3)
    rec["fail_reason"] = fail_reason
    if debug and _DEBUG is not None:
        _DEBUG.log(**rec)
    return fail_reason, country, elapsed


async def _access_check(
    proxy: str, proxy_type: ProxyType, urls: list[str], timeout: float,
    strict: bool = False,
) -> tuple[str | None, str | None, float | None]:
    """Gatekeeper URL'lerine erişiyor mu? `(reason, country, fastest_elapsed)`.

    İki mod:
      - **any-of** (default, `strict=False`): En az BİR URL geçtiyse proxy
        access-OK kabul edilir. 3 farklı CF zone'undan birine ulaşabilen
        proxy çoğu pratik senaryoda kullanılabilir; tek bir CF zone'da
        geçici 429/503/edge-yavaşlığı false-fail yaratmaz.
      - **strict** (`--access-strict`, eski davranış): URL'lerin TÜMÜ geçmek
        zorunda. Production-kalite seçim için doğru ama kümülatif fail
        olasılığını şişirir (`0.8³ ≈ %51` etkili başarı).

    PARALEL execution: asyncio.gather ile tüm probe'lar AYNI ANDA. Sequential
    yerine paralel olması proxy'ye sürekli akış yerine TEK BURST gösterir →
    per-source rate limit / connection budget tetiklenmesi azalır. Wall-time
    da `sum` yerine `max` olur.

    reason: None=geçti, str=fail nedeni:
      "to"=timeout, "<N>"=3 haneli HTTP status, "err"=IO hatası, "?"=diğer

    country: gatekeeper'lardan en az birinden `/cdn-cgi/trace`'in `loc=XX`
      satırı yakalandıysa ülke kodu. Birden çok URL aynı country verirse
      ilki kullanılır (CF geo DB tutarlı, consensus gereksiz).

    fastest_elapsed: probe'ların en hızlısının round-trip süresi. SÜRE
      sütunu için fallback (judge yoksa). Paralel olduğu için "ilk gelen"
      = "en hızlı" — gerçek minimum latency'yi gösterir.
    """
    if not urls:
        return None, None, None

    # Tüm probe'ları aynı anda fırlat — sequential overhead yok.
    results = await asyncio.gather(
        *(_access_check_one(proxy, proxy_type, u, timeout) for u in urls),
    )
    # results: list of (reason, country, elapsed)

    # Country: ilk valid olanı al (consensus gerekmez).
    country: str | None = next(
        (c for _, c, _ in results if c), None,
    )
    # Fastest elapsed: en hızlı probe.
    elapseds = [e for _, _, e in results if e is not None]
    fastest_elapsed: float | None = min(elapseds) if elapseds else None

    reasons = [r for r, _, _ in results if r is not None]
    passes = len(results) - len(reasons)

    # MITM ÖNCELİĞİ: herhangi bir probe'da cert verify failure varsa proxy
    # MITM yapıyor demektir. Bu her iki modda (strict + any-of) **fail**
    # ile sonuçlanır — bir CF zone için MITM kıran proxy diğerlerini de
    # potansiyel olarak kırar (operator beyaz liste yapmış olabilir).
    # Güvenlik kararı semantik geçişinden önce gelir: any-of'taki "1 pass
    # yeterli" kuralı MITM tespit varsa devreye girmez.
    if "mitm" in reasons:
        return "mitm", country, fastest_elapsed

    if strict:
        # Strict: tüm probe'lar geçmeli. Tek fail bile yeter.
        if reasons:
            return reasons[0], country, fastest_elapsed
        return None, country, fastest_elapsed

    # Any-of (default): en az 1 pass yeterli (MITM zaten yukarıda elendi).
    if passes > 0:
        return None, country, fastest_elapsed
    # Tümü fail — ilk fail nedenini döndür (kullanıcı pattern görsün).
    return reasons[0], country, fastest_elapsed


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
    elapsed: float = 0.0   # bu probe'un tek round-trip süresi


async def _https_probe_one(
    proxy: str, proxy_type: ProxyType, url: str, timeout: float,
) -> HttpsProbeResult:
    """Tek bir URL'e karşı HTTPS probe — _https_probe'un işçi parçası."""
    host, _, port_str = proxy.partition(":")
    debug = _DEBUG is not None
    started = time.monotonic()
    rec: dict = {"kind": "tunnel", "proxy": proxy, "url": url, "ua": USER_AGENT}
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
            async with session.get(url) as resp:
                rec["status"] = resp.status
                if debug:
                    rec["server"] = resp.headers.get("Server")
                    rec["cf_ray"] = resp.headers.get("CF-Ray")
                if 200 <= resp.status < 400:
                    result = HttpsProbeResult(True, False)
                else:
                    result = HttpsProbeResult(
                        False, False, error_class=f"HTTP{resp.status}",
                    )
    except aiohttp.ClientConnectorCertificateError as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        result = HttpsProbeResult(True, True, error_class="CertError")
    except aiohttp.ClientSSLError as e:
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        msg = str(e).lower()
        if "certificate_verify_failed" in msg or "cert" in msg:
            result = HttpsProbeResult(True, True, error_class="CertError")
        else:
            result = HttpsProbeResult(False, False, error_class="SSL")
    except aiohttp.ClientOSError as e:
        # ClientSSLError aslında ClientOSError'ın alt sınıfı — yukarıdaki
        # blok yakalamadıysa burada kalan ClientOSError varyantları (TCP
        # reset, EPIPE vb.) düşer. Mesajda "CERTIFICATE_VERIFY_FAILED" ararız
        # — aiohttp bazen düz ClientOSError olarak da sarmalıyor (özellikle
        # SOCKS5+CONNECT akışında python-socks katmanı arası).
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        if "certificate_verify_failed" in str(e).lower():
            result = HttpsProbeResult(True, True, error_class="CertError")
        else:
            result = HttpsProbeResult(False, False, error_class=type(e).__name__)
    except Exception as e:  # noqa: BLE001
        rec["error_class"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        result = HttpsProbeResult(False, False, error_class=type(e).__name__)

    elapsed = time.monotonic() - started
    rec["elapsed"] = round(elapsed, 3)
    rec["tunnel_ok"] = result.tunnel_ok
    rec["mitm_suspected"] = result.mitm_suspected
    if debug and _DEBUG is not None:
        _DEBUG.log(**rec)
    result.elapsed = elapsed
    return result


# MITM tespit için kaç URL paralel test edilir. 1 yetersiz (proxy operatörü
# captive-portal URL'lerini beyaz listeye alabiliyor → MITM kaçırılıyor).
# 2 paralel, debug verisinde gözlemlenen %68 MITM cluster'ı yakalar.
_MITM_PROBE_URL_COUNT = 2


async def _https_probe(
    proxy: str, proxy_type: ProxyType, timeout: float,
) -> HttpsProbeResult:
    """Çok-URL paralel HTTPS probe — CONNECT-tunnel + MITM testi.

    Strateji (eski tek-URL versiyonundan farkı):
      - Default aiohttp davranışı TLS doğrulama AÇIK → MITM proxy'nin fake
        sertifikası SSL cert hatasıyla yakalanır.
      - {_MITM_PROBE_URL_COUNT} farklı URL'e PARALEL probe (asyncio.gather).
        Tek URL'lik versiyon, MITM proxy'nin URL beyaz listeleyebilmesi
        nedeniyle MITM'leri kaçırıyordu (debug'da %88 access fail'in nedeni
        cert verify failure çıktı — proxy MITM ama tunnel testi temiz
        gelmişti). Birden fazla URL probe etmek bu beyaz liste atlatma
        taktiğini kırar.
      - **Karar mantığı:**
          - HERHANGİ probe'da cert verify hatası → MITM kesin
            (tunnel_ok=True, mitm_suspected=True; kalan sonuçlar tunnel_ok
            açısından değerlendirilmez — güvenlik kararı önceliklidir).
          - Aksi halde EN AZ BİR probe başarılı → tunnel_ok=True, mitm yok.
          - Hiçbiri başarısız, cert hatası da yok → tunnel_ok=False (proxy
            HTTPS taşıyamıyor).
      - Maliyet: paralel olduğu için wall-time `max(probe_süresi)` ≈ tek
        URL versiyonu. Sadece toplam bandwidth çift (~10KB → ~20KB per
        proxy). Tarama tamamında ihmal edilebilir.
    """
    if not TUNNEL_TEST_URLS:
        return HttpsProbeResult(False, False, error_class="no_tunnel_urls")

    k = min(_MITM_PROBE_URL_COUNT, len(TUNNEL_TEST_URLS))
    urls = random.sample(TUNNEL_TEST_URLS, k=k)

    started = time.monotonic()
    sub_results = await asyncio.gather(
        *(_https_probe_one(proxy, proxy_type, u, timeout) for u in urls),
    )
    total_elapsed = time.monotonic() - started

    # MITM önceliği: herhangi bir probe cert hatası verdiyse proxy MITM
    # sayılır — diğer probe'lar başarılı bile olsa (operatör URL whitelist
    # yapmış olabilir, kalanlarda yakalanır). HTTPS güvenliği açısından
    # "bazen MITM yapıyor" = "her zaman risk".
    mitm_hits = [r for r in sub_results if r.mitm_suspected]
    if mitm_hits:
        result = HttpsProbeResult(
            tunnel_ok=True, mitm_suspected=True, error_class="CertError",
        )
        result.elapsed = total_elapsed
        return result

    # MITM yok — en az 1 başarı varsa tunnel_ok=True.
    any_ok = any(r.tunnel_ok for r in sub_results)
    if any_ok:
        result = HttpsProbeResult(tunnel_ok=True, mitm_suspected=False)
        result.elapsed = total_elapsed
        return result

    # Hiçbiri başarılı değil ve MITM imzası da yok → tunnel yok.
    # En bilgi verici error_class'ı seç (ilkini al).
    err_class = next(
        (r.error_class for r in sub_results if r.error_class), None,
    )
    result = HttpsProbeResult(
        tunnel_ok=False, mitm_suspected=False, error_class=err_class,
    )
    result.elapsed = total_elapsed
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


def _default_route_iface() -> str | None:
    """Default IPv4 route'un üzerinden internete çıktığı interface adı.

    `/proc/net/route` formatı tab-separated; Destination alanı hex
    little-endian. 00000000 = 0.0.0.0/0 = default route. VPN tüneli (wg0,
    tun0) varsa onu döndürür — proxyprof trafiği o tünelden geçtiği için
    doğrudur. Modem'in fiziksel kabloyu gördüğü değerle 1:1 değildir
    (VPN encapsulation overhead'i tünel-interface yerine wan-interface'te
    görülür), ama proxyprof'un ürettiği gerçek byte sayısını verir.
    """
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            next(f, None)  # header satırı atla
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    return parts[0]
    except (OSError, StopIteration, IndexError):
        pass
    return None


def _iface_wan_bytes(iface: str) -> int:
    """`/proc/net/dev` üzerinden bir interface'in TOPLAM rx+tx byte sayısı.

    Bu ÇIPLAK kablo (wire) byte'ıdır — Ethernet header + IP header + TCP
    header + TLS records + HTTP payload + retransmissions + ACK/SYN/FIN
    paketleri hepsi dahil. Modem WAN istatistiklerinde gördüğün değerle
    aynı semantik (sistem genelinde, sadece bu süreç değil).

    Önemli sınır: aynı makinede başka aktif uygulamalar (browser, sistem
    güncelleme, bulut yedeği vs.) varsa onların trafiği de bu sayıya dahil
    edilir. Sadece proxyprof koşuyorsa fark ihmal edilebilir.

    Linux-only — diğer platformlarda 0 döner.
    """
    try:
        with open("/proc/net/dev", encoding="utf-8") as f:
            for line in f:
                head, sep, rest = line.partition(":")
                if not sep or head.strip() != iface:
                    continue
                fields = rest.split()
                if len(fields) < 16:
                    return 0
                # Sütun haritası (/proc/net/dev sırası):
                #   0=rx_bytes 1=rx_packets ... 8=tx_bytes 9=tx_packets ...
                rx_bytes = int(fields[0])
                tx_bytes = int(fields[8])
                return rx_bytes + tx_bytes
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _self_net_bytes(iface: str | None = None) -> int:
    """Network throughput ölçümü için byte sayısı döndür.

    Strateji: WAN interface byte'ı (rx+tx) — modem panel değeriyle uyumlu.
    Application-level (rchar/wchar) değil çünkü:
      - TCP/IP/TLS overhead'i kaçırır (proxy taraması: binlerce kısa
        connection, her birinde TLS handshake = 2-3KiB; ihmal edilemez)
      - Kullanıcı "modem panelinde gördüğüm hız" diyor — wire-level istiyor

    `iface` None ise default route interface'ini tespit eder. Linux dışı
    veya tespit başarısızsa 0 döner.
    """
    if iface is None:
        iface = _default_route_iface()
    if not iface:
        return 0
    return _iface_wan_bytes(iface)


def _format_throughput(bytes_per_sec: float) -> str:
    """Bytes/saniye → KiB/s veya MiB/s; sabit 9 karakter genişlikte format.

    Eşik: 1 MiB/s. Altında KiB, üstünde MiB. Birim KiB/MiB (binary 1024 tabanlı,
    KB/MB ile karıştırılmasın — network throughput için yaygın convention)."""
    mib_per_sec = bytes_per_sec / (1024 * 1024)
    if mib_per_sec >= 1.0:
        return f"{mib_per_sec:5.1f}MiB/s"
    kib_per_sec = bytes_per_sec / 1024
    return f"{kib_per_sec:5.1f}KiB/s"


def _visual_lines(text: str, width: int) -> int:
    """Text terminal'de kaç görsel satır kaplar.

    Logical satır (`\\n` ile ayrılmış) terminal genişliğinden uzunsa wrap'lanır
    ve birden fazla görsel satır olur. ANSI cursor-up komutu görsel satır
    bazında çalıştığı için clear logic'i için bu hesap gerekli.

    Empty satır 1 görsel satır sayılır. Genişlik 0 veya negatifse fallback
    olarak sadece logical satır sayar.

    ANSI escape kodları (renk vs.) terminal'de görünmez ama `len()` bunları
    sayar — strip etmezsek progress satırı genişliği aşıyormuş gibi görünür ve
    `_clear_progress_block` fazla satır temizleyip yukarıdaki tablo satırlarını
    yiyebilir.
    """
    if width <= 0:
        return text.count("\n") + 1
    total = 0
    for line in text.split("\n"):
        if not line:
            total += 1
        else:
            visible = _ANSI_RE.sub("", line)
            # Tavan bölme: len/width yukarı yuvarla; her satır en az 1 görsel
            total += max(1, (len(visible) + width - 1) // width)
    return total


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
        "PROTO":  ("table.header.proto",  6),   # "socks5" 6, header "PROTO" 5
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
        quit_event=None, force_event=None, pause_event=None,
        protocol: str = "",
        tunnel_test: bool = True, mitm_test: bool = True,
        file=sys.stderr,
    ) -> None:
        # quit_event/force_event: asyncio.Event veya None. 'q' tuşu birinciye
        # ilk basıldığında quit_event set olur (yumuşak kapanış); ikincide
        # force_event set olur (anında çıkış). amain bu event'leri scan()'e
        # geçirerek davranışı kontrol eder. None ise 'q' tuşu işlemsiz.
        # pause_event: asyncio.Event veya None. SEMANTIK INVERTED — event SET
        # iken çalışan akış (default), CLEAR iken duraklatılmış. Worker'lar
        # `await pause_event.wait()` ile devam sinyalini bekler. 'p' tuşu
        # toggle eder. None ise 'p' işlemsiz.
        self.quit_event = quit_event
        self.force_event = force_event
        self.pause_event = pause_event
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
        # Aktif tarama protokolü (http/https/socks4/socks5). Her tablo satırında
        # sabit bir sütun olarak gösterilir — kullanıcı output'u kaydedip sonra
        # baktığında hangi protokolün test edildiğini satır bazında görür.
        self.protocol = protocol
        # Footer (progress'in altındaki açıklama) toggle durumu. Default kapalı:
        # tek-satır 'd ile aç' ipucu. 'd' tuşuna basınca tam legend açılır;
        # tekrar 'd' → kapanır. Klavye listener aktif değilse (silent/non-TTY)
        # `_keyboard_attached=False` → tam legend her zaman.
        self.show_legend = False
        self._keyboard_attached = False
        self._loop = None
        self._orig_term_attrs = None
        self.count = 0
        # ok_count = DURUM="iyi" (output'a yazılacak); drop_count = probe
        # başarılı ama filtre/eksik nedeniyle output'a YAZILMAYAN (elendi/
        # eksik/seviye). Toplam: ok_count + drop_count = "probe yanıtlandı".
        self.ok_count = 0
        self.drop_count = 0
        self.fail_count = 0
        self.skip_count = 0
        # # sütunu için tablodaki satır numarası — `r.ok` her başarılı probe'
        # da artar (DURUM ne olursa olsun). ok_count'tan ayrı; o sadece
        # output'a yazılan "iyi" sayısı.
        self._table_row = 0
        self._headered = False
        self._progress_drawn = False
        # Fail/skip update'leri için redraw throttle penceresi (saniye).
        # Quit drain sırasında binlerce skipped update mikrosaniyeler içinde
        # gelirse her birinin clear+redraw yapması ekranı titretir; throttle
        # ile maksimum ~20fps render → titreşim olmadan counter ilerler.
        # OK satırları HER ZAMAN render edilir (yeni başarı görünmeli).
        self._render_throttle = 0.05
        self._last_render = 0.0
        # Son header'dan beri kaç OK satır yazıldı — HEADER_REPEAT'e ulaşınca
        # bir alt-header bloğu (separator + label row + separator) yeniden
        # basılır ve sayaç sıfırlanır.
        self._rows_since_header = 0
        # Kapalı testlerin sütunlarını gizle. Örn. `-p http` default'ta tunnel/
        # mitm/access üçünü de OFF eder → tabloda hep "—" basmak yerine sütunu
        # tamamen kaldırırız: ekran sade kalır + kullanıcı "bu test yapılmadı"
        # bilgisini görsel olarak alır (sütun yoksa test de yok).
        _skip = set()
        if not tunnel_test:
            _skip.add("TUN")
        if not mitm_test:
            _skip.add("MITM")
        if access_mode == ACCESS_MODE_OFF:
            _skip.add("ACC")
        # Sütun adı → genişlik (etiket uzunluğunu da hesaba kat).
        self._cols: dict[str, int] = {}
        for code, (key, min_w) in self._FIXED.items():
            if code in _skip:
                continue
            label = t(key)
            self._cols[code] = max(min_w, len(label))
        self._cols["#"] = max(self._cols["#"], len(f"{total}/{total}"))
        # Sütun adı → çevrilmiş etiket (header satırında basılır).
        self._labels: dict[str, str] = {
            code: t(key) for code, (key, _) in self._FIXED.items()
            if code not in _skip
        }
        self._started = time.monotonic()
        # Progress satırı için CPU kümül başlangıcı — sürekli ortalama göster.
        self._start_cpu_time = _self_cpu_time()
        # Network throughput: WAN interface bytes (modem panel ile uyumlu, tüm
        # protokol katmanları dahil). Default route interface'ini bir kez
        # tespit edip cache'liyoruz — her progress update'inde /proc/net/route
        # parse etmeye gerek yok. Tarama ortasında route değişirse (VPN
        # connect/disconnect) cached iface yanıltıcı olabilir; nadir senaryo.
        self._net_iface = _default_route_iface()
        self._last_net_bytes = _self_net_bytes(self._net_iface)
        self._last_net_time = self._started
        # Cache: sample throttling sırasında her progress update yeni ölçüm
        # YAPMADAN bu değeri döndürür. 500+ probe/sn akışında /proc'u 1ms
        # aralıkla okumak NOISE üretir; pencere açılana kadar son hesaplanan
        # bps kullanılır.
        self._cached_net_bps: float = 0.0

    def _all_widths(self) -> list[int]:
        return list(self._cols.values())

    def _border(self, left: str, mid: str, right: str) -> str:
        border = left + mid.join("─" * (w + 2) for w in self._all_widths()) + right
        return _paint(border, _C_DIM)

    def _row(self, cells: list[str]) -> str:
        widths = self._all_widths()
        col_names = list(self._cols.keys())
        parts = []
        for col_name, c, w in zip(col_names, cells, widths):
            # Truncate visible width'e göre — ANSI escape'leri saymadan kıyas.
            if _visible_len(c) > w:
                plain = _strip_ansi(c)
                c = plain[: w - 1] + "…"
            # # ve TIME sağa yaslı, geri kalanlar sola.
            if col_name in ("#", "TIME"):
                cell = _pad_right(c, w)
            else:
                cell = _pad_left(c, w)
            parts.append(f" {cell} ")
        sep = _paint("│", _C_DIM)
        return sep + sep.join(parts) + sep

    def _emit_header(self) -> None:
        labels = [
            _paint(self._labels[code], _C_BOLD, _C_CYAN)
            for code in self._cols.keys()
        ]
        print(self._border("┌", "┬", "┐"), file=self.file)
        print(self._row(labels), file=self.file)
        print(self._border("├", "┼", "┤"), file=self.file)
        self.file.flush()

    def _progress_line(self) -> str:
        pct = self.count / self.total if self.total else 1.0
        filled = int(self.BAR_WIDTH * pct)
        # Bar: dolu kısım cyan (genişledikçe ilerleme), boş dim (henüz).
        bar = (
            _paint("█" * filled, _C_CYAN)
            + _paint("░" * (self.BAR_WIDTH - filled), _C_DIM)
        )
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
        # Network throughput — sample throttling ile noise'suz hesap.
        # Wire-level (rx+tx, tüm protokol katmanları dahil) → modem panel
        # değeri ile karşılaştırılabilir.
        #
        # Sorun: 500+ probe/sn finish hızıyla _progress_line çağrıldığında her
        # çağrıda /proc okuyup delta hesaplamak bps'i mahveder — dt~1-5ms,
        # çoğu window'da kernel sayacı henüz güncellenmemiş = 0 byte = 0 bps,
        # arada sırada bir burst = milyonlarca bps. Stabil bps istiyoruz.
        #
        # Çözüm: en az NET_SAMPLE_INTERVAL aralıkla yeni ölçüm. Bu süre
        # dolmamışsa son hesaplanan bps cache'den döndürülür. 500ms = göze
        # stabil + 500+ probe'un birikmiş trafiğine yetecek pencere.
        NET_SAMPLE_INTERVAL = 0.5
        now_mono = time.monotonic()
        dt = now_mono - self._last_net_time
        if dt >= NET_SAMPLE_INTERVAL:
            now_net = _self_net_bytes(self._net_iface)
            self._cached_net_bps = max(0, now_net - self._last_net_bytes) / dt
            self._last_net_bytes = now_net
            self._last_net_time = now_mono
        net_str = _format_throughput(self._cached_net_bps)
        line = t(
            "progress.format",
            bar=bar, pct=pct * 100,
            done=self.count, digits=digits, total=self.total,
            ok=self.ok_count, drop=self.drop_count,
            fail=self.fail_count, skip=self.skip_count,
            rate=rate, cpu=cpu_pct, mem=mem_mb, net=net_str,
            elapsed=elapsed,
        )
        # Sayaçları semantik renkle vurgula: iyi/ok yeşil-bold (output'a
        # girdi), elendi/dropped sarı (probe ok ama filtre düşürdü), hata/fail
        # kırmızı, atlanan/skip dim. Sadece rakamları boyar; etiket + padding
        # aynen kalır → progress satırının kolon hizası korunur.
        if _color_enabled():
            line = re.sub(
                r"(iyi:|ok:)(\d+)",
                lambda m: m.group(1) + _paint(m.group(2), _C_BOLD, _C_GREEN),
                line,
            )
            line = re.sub(
                r"(elendi:|dropped:)(\d+)",
                lambda m: m.group(1) + _paint(m.group(2), _C_YELLOW), line,
            )
            line = re.sub(
                r"(hata:|fail:)(\d+)",
                lambda m: m.group(1) + _paint(m.group(2), _C_RED), line,
            )
            line = re.sub(
                r"(atlanan:|skip:)(\d+)",
                lambda m: m.group(1) + _paint(m.group(2), _C_DIM), line,
            )
        return line

    def _progress_legend(self) -> str:
        """Tam legend metni — seviye kodları (L1/L2/L2d/L3) + sütun anlamları.

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
        text = base + "\n" + t(access_key, n=self.access_count)
        return _colorize_legend(text)

    def _progress_footer(self) -> str:
        """Progress'in altında basılacak metin — durum bağımlı.

        Öncelik sırası:
          1. force_event set → "Anında çıkış..." (tek satır, en kritik mesaj)
          2. quit_event set  → "Yumuşak kapanış... ('q' tekrar = anında çık)"
          3. pause cleared   → "Duraklatıldı ('p' devam et)" — kullanıcı yeni
             task dispatch edilmediğini hemen görsün
          4. show_legend açık → tam legend + "'d' ile gizle" hint
          5. default → tek-satır "'d' detay, 'p' duraklat, 'q' çıkış" hint

        Quit/pause modlarında legend gizlenir; kullanıcının dikkati kritik
        durumdan dağılmasın. Klavye listener aktif değilse (silent / non-TTY
        / cbreak desteklemeyen terminal) her durumda tam legend gösterilir —
        'd'/'p'/'q' zaten çalışmaz."""
        if not self._keyboard_attached:
            return self._progress_legend()
        if self.force_event is not None and self.force_event.is_set():
            return t("progress.quit_forcing")
        if self.quit_event is not None and self.quit_event.is_set():
            return t("progress.quit_requested")
        if self.pause_event is not None and not self.pause_event.is_set():
            return t("progress.paused")
        if self.show_legend:
            return self._progress_legend() + "\n" + t("progress.hint_press_d_hide")
        return t("progress.hint_press_dpq")

    def _clear_progress_block(self) -> None:
        """En alttaki çok-satırlı progress block'u ANSI ile temizle.

        Block = 1 (top padding) + bar (görsel) + 1 (mid pad) + legend
        (görsel) + 1 (bottom padding). "Görsel satır" = wrap dahil; uzun
        legend satırı dar terminal'de wrap olduğunda logical newline
        sayısından FAZLA görsel satır kaplar.

        Eski versiyon `.count("\\n") + 1` ile logical line sayardı; wrap'lı
        satırların artığı her clear'de M+visual_lines-cleared kadar artık
        bırakırdı → ekrana boş satırlar birikirdi. Şu an `_visual_lines`
        terminal genişliğine göre gerçek görsel satır sayısını döner.

        Cursor bottom padding satırının başında varsayılır (block yazıldıktan
        sonra son `\\n` cursor'u oraya bırakır):
          \\r\\033[K          → mevcut (bot pad) satırını sil
          (\\033[A\\r\\033[K)*k → k kez "bir satır yukarı, sil"
        Sonuç: cursor top padding satırı başında, tüm block boş.
        """
        width = shutil.get_terminal_size((80, 24)).columns
        bar_visual = _visual_lines(self._progress_line(), width)
        # Footer = ya tek-satır hint ya da tam legend (toggle'a bağlı). Aynı
        # render path'inde çiziliyor; clear için aynı footer'ı kullanırız.
        footer_visual = _visual_lines(self._progress_footer(), width)
        # padding satırları boş → her zaman 1 görsel satır
        total_lines = bar_visual + footer_visual + 3
        parts = ["\r\033[K"]
        parts.extend(["\033[A\r\033[K"] * (total_lines - 1))
        self.file.write("".join(parts))

    def _classify_status(self, r: ScanResult) -> str:
        """ScanResult → DURUM kodu ("ok"/"filter"/"level"/"missing").

        update() ve emit aynı kararı kullansın diye tek nokta — sayaçlar
        ile DURUM hücresinin tutarlı olmasını garanti eder.
        Sadece `r.ok=True` için anlamlı; caller kontrol etmeli.
        """
        if r.outbound_ip is None and not r.judge_skipped:
            return "missing"
        if (
            (r.access_ok is False)
            or (r.tunnel_ok is False)
            or (r.mitm_suspected is True)
        ):
            return "filter"
        if r.level is not None and r.level > self.level_max:
            return "level"
        return "ok"

    def update(self, r: ScanResult) -> None:
        self.count += 1
        if r.ok:
            # Probe yanıt verdi — DURUM hesabına göre output'a giriyor mu,
            # yoksa filtre düşürdü mü ayır. Aksi halde "iyi:N" sayacı
            # tablodaki DURUM=iyi satır sayısıyla tutmazdı.
            status = self._classify_status(r)
            if status == "ok":
                self.ok_count += 1
            else:
                self.drop_count += 1
            self._table_row += 1
        elif r.skipped:
            # IP-poison erken-atlama; sayım gerçek fail'lerden ayrı tutulur
            # ki kullanıcı "kaç port'u test bile etmediğimi" görebilsin.
            self.skip_count += 1
        else:
            self.fail_count += 1

        if not self.enabled:
            return

        # Render throttle: OK update'leri (tabloya yeni satır ekler) her zaman
        # çiz — kullanıcı yeni başarılı proxy'yi anında görmek ister. Fail/skip
        # update'leri sadece counter'ı ilerletir; rate çok yüksek olduğunda
        # (özellikle 'q' sonrası kuyruk drain'inde binlerce skipped/saniye)
        # throttle'la → max ~20fps, titreşim olmaz. Son tick atlansa bile
        # finish() doğru final state'i yazar.
        if not r.ok and self.use_ansi:
            now = time.monotonic()
            if (now - self._last_render) < self._render_throttle:
                return
            self._last_render = now

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
            # --no-judge mode'da outbound_ip her zaman None (judge yok),
            # bu "eksik" tetiklemez; status sadece tunnel/access/mitm'e
            # dayanır.
            # STATUS rengi: ok=yeşil-bold, filter/level=sarı, missing=kırmızı.
            # `_classify_status` ile update()'in saydığı kategoriyi aynen al;
            # böylece tablodaki DURUM ile progress sayıcılar tutarlı kalır.
            status_code = self._classify_status(r)
            _STATUS_COLOR = {
                "missing": (t("table.status.unknown"), (_C_RED,)),
                "filter":  (t("table.status.filter"),  (_C_YELLOW,)),
                "level":   (t("table.status.level"),   (_C_YELLOW,)),
                "ok":      (t("table.status.ok"),      (_C_BOLD, _C_GREEN)),
            }
            _label, _codes = _STATUS_COLOR[status_code]
            status = _paint(_label, *_codes)
            # SEVİYE: L1 yeşil (elite), L2 sarı (anonim), L2d magenta (distorting),
            # L3 kırmızı (transparan), yoksa dim çizgi.
            if r.level == 1:
                lvl = _paint("L1", _C_GREEN)
            elif r.level == 2:
                if r.distorting:
                    lvl = _paint("L2d", _C_MAGENTA)
                else:
                    lvl = _paint("L2", _C_YELLOW)
            elif r.level == 3:
                lvl = _paint("L3", _C_RED)
            else:
                lvl = _paint("—", _C_DIM)

            def _mark(v: bool | None) -> str:
                if v is None:
                    return _paint("—", _C_DIM)
                return _paint("✓", _C_GREEN) if v else _paint("×", _C_RED)

            # BUCKET rengi sıcaklık-skalası: HOT kırmızı, WARM sarı, NEW cyan,
            # COLD mavi. Operatör tabloya bakıp dağılımı hızla görür.
            _BUCKET_COLOR = {
                BUCKET_HOT:  _C_RED,
                BUCKET_WARM: _C_YELLOW,
                BUCKET_NEW:  _C_CYAN,
                BUCKET_COLD: _C_BLUE,
            }
            bkt_key = self._BUCKET_KEY.get(r.bucket or "")
            if bkt_key:
                bkt = _paint(t(bkt_key), _BUCKET_COLOR.get(r.bucket or "", _C_DIM))
            else:
                bkt = _paint("—", _C_DIM)
            # MITM kolonu: True = TLS chain kırık (kırmızı bayrak). Mantıken
            # ters: ✓ = MITM YOK (güvenli), × = MITM şüphesi. _mark'a
            # `not mitm_suspected` veriyoruz ki ✓ = iyi semantiği kalsın.
            if r.mitm_suspected is None:
                mitm_mark = _paint("—", _C_DIM)
            elif not r.mitm_suspected:
                mitm_mark = _paint("✓", _C_GREEN)
            else:
                mitm_mark = _paint("×", _C_RED)
            # ACC: ✓ (geçti) / 3 char reason kod (403, 503, to, err, ?) / —
            # kod = _access_check'in ilk başarısız URL için döndürdüğü neden.
            if r.access_ok is None:
                acc_cell = _paint("—", _C_DIM)
            elif r.access_ok:
                acc_cell = _paint("✓", _C_GREEN)
            else:
                acc_cell = _paint(r.access_reason or "×", _C_RED)
            # # sütunu = tablodaki satır sırası (1, 2, 3, ...). Her r.ok
            # için 1 artar (DURUM ne olursa olsun); progress sayacındaki
            # iyi/elendi ayrımıyla karışmasın.
            _cell_map = {
                "#":      _paint(str(self._table_row), _C_DIM),
                "STATUS": status,
                "BKT":    bkt,
                "PROXY":  _paint(r.proxy, _C_CYAN),
                "PROTO":  _paint(self.protocol or "—", _C_DIM),
                "LVL":    lvl,
                "OUT":    _paint(r.outbound_ip, _C_CYAN) if r.outbound_ip else _paint("—", _C_DIM),
                "CC":     i18n.country_name(r.country) if r.country else _paint("—", _C_DIM),
                "TIME":   f"{r.elapsed:.1f}s",
                "TUN":    _mark(r.tunnel_ok),
                "MITM":   mitm_mark,
                "ACC":    acc_cell,
            }
            # Sadece aktif sütunları (_cols sırasında) bas.
            cells = [_cell_map[code] for code in self._cols.keys()]
            # HEADER_REPEAT OK satırı geçtiyse, bu satırdan önce başlık
            # bloğunu yeniden bas. Kullanıcı uzun taramalarda terminal
            # scroll'ladıktan sonra sütun adlarına tekrar bakabilir.
            if self._rows_since_header >= self.HEADER_REPEAT:
                labels = [
                    _paint(self._labels[code], _C_BOLD, _C_CYAN)
                    for code in self._cols.keys()
                ]
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
            self.file.write("\n")                              # mid padding (bar↔footer)
            self.file.write(self._progress_footer() + "\n")    # footer + bot padding
            self._progress_drawn = True
            # Throttle penceresinin başlangıcı: her gerçek render sonrası
            # kaydedilir. OK render hemen ardından gelen skip update'lerin de
            # throttle'a düşmesini sağlar (aksi halde 1ms içinde 2 redraw
            # olur ve flicker döner).
            self._last_render = time.monotonic()

        self.file.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        # Canlı progress block'u (üst pad + bar + footer + alt pad) temizle.
        if self.use_ansi and self._progress_drawn:
            self._clear_progress_block()
        # Tablo varsa bottom border'ı kapat.
        if self._headered:
            self.file.write(self._border("└", "┴", "┘") + "\n")
        # Statik final progress block — toggle ne olursa olsun en sonda
        # TAM legend göster (tarama bitti, kullanıcı detayları görebilsin).
        # `_progress_footer` yerine doğrudan `_progress_legend` çağırırız.
        self.file.write("\n")
        self.file.write(self._progress_line() + "\n")
        self.file.write("\n")
        self.file.write(self._progress_legend() + "\n")
        self.file.write("\n")
        self.file.flush()

    # ---- klavye toggle ('d' tuşu) ----------------------------------------

    def attach_keyboard_listener(self, loop) -> None:
        """stdin'i raw mode'a alıp 'd' tuşunu dinlemeye başla.

        Sadece TTY mode'da (stdin + stderr ikisi de TTY ise) çalışır. cbreak
        mode sayesinde tuş anında okunur (Enter beklemez). 'd' toggle eder;
        listener aktif değilse `_progress_footer` her zaman tam legend basar
        (kullanıcı zaten toggle yapamaz).

        Linux'ta termios; Windows / cbreak desteklemeyen platformlarda sessiz
        fallback: listener attach EDİLMEZ → legend tam görünmeye devam eder.
        """
        if not self.use_ansi:
            return
        try:
            import sys as _sys
            if not _sys.stdin.isatty():
                return  # stdin pipe/dosya — keypress okunamaz
            import termios, tty
            fd = _sys.stdin.fileno()
            self._orig_term_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            loop.add_reader(_sys.stdin, self._on_key)
            self._loop = loop
            self._keyboard_attached = True
        except (ImportError, OSError, AttributeError):
            # termios yoksa (Windows) ya da add_reader Selector loop değilse
            # sessizce fallback: legend zaten görünür kalır.
            self._keyboard_attached = False

    def detach_keyboard_listener(self) -> None:
        """Listener'ı kapat + terminal'i orijinal mode'a geri al.

        amain'in finally bloğundan ÇAĞIRILMAK ZORUNDA: aksi halde scan
        Ctrl+C ile yarıda kesilirse terminal cbreak'te kalır (echo kapalı,
        line-buffering yok) ve shell bozulur.
        """
        if not self._keyboard_attached:
            return
        try:
            import sys as _sys, termios
            if self._loop is not None:
                self._loop.remove_reader(_sys.stdin)
            if self._orig_term_attrs is not None:
                fd = _sys.stdin.fileno()
                termios.tcsetattr(fd, termios.TCSADRAIN, self._orig_term_attrs)
        except (ImportError, OSError, ValueError):
            pass
        self._keyboard_attached = False
        self._loop = None
        self._orig_term_attrs = None

    def _on_key(self) -> None:
        """stdin'den tek karakter oku; 'd' ise toggle + anında redraw.

        Sıra ÖNEMLİ: önce ESKİ state ile clear, sonra toggle, sonra YENİ
        state ile redraw. `_clear_progress_block` mevcut `show_legend`'e göre
        satır sayısı hesaplıyor; toggle önce yapılırsa yanlış sayıda satır
        siler ve ekrana artık satırlar bırakır.

        Diğer karakterler sessizce tüketilir — gelecekte (örn. 'q' = quit,
        '+/-' concurrency ayarı vb.) genişletilebilir."""
        try:
            import sys as _sys
            ch = _sys.stdin.read(1)
        except (OSError, BlockingIOError):
            return
        if not ch:
            return
        if ch in ('d', 'D'):
            if self.use_ansi and self._progress_drawn:
                # 1) ESKİ footer ile clear (mevcut ekran ne ise)
                self._clear_progress_block()
                # 2) Toggle
                self.show_legend = not self.show_legend
                # 3) YENİ footer ile yeniden çiz
                self.file.write("\n")
                self.file.write(self._progress_line() + "\n")
                self.file.write("\n")
                self.file.write(self._progress_footer() + "\n")
                self.file.flush()
            else:
                # ANSI yoksa toggle anlamsız (zaten legend her zaman görünür)
                self.show_legend = not self.show_legend
        elif ch in ('q', 'Q'):
            # İlk 'q': yumuşak kapanış (in-flight task'lar tamamlansın, gerisi
            # skipped). İkinci 'q' (quit_event zaten set): force — tüm
            # task'lar cancel + amain finalization'ı atlar.
            if self.quit_event is None:
                return
            if self.quit_event.is_set():
                # 2. basış: force
                if self.force_event is not None and not self.force_event.is_set():
                    self.force_event.set()
            else:
                # 1. basış: graceful
                self.quit_event.set()
            # Footer'ı yenile — kullanıcı 'q'ya bastığını anında görsün.
            # `_last_render` güncellenir → arkadan gelen skip update'leri
            # throttle penceresine düşer ve yeniden hemen redraw etmez,
            # böylece flicker olmaz.
            if self.use_ansi and self._progress_drawn:
                self._clear_progress_block()
                self.file.write("\n")
                self.file.write(self._progress_line() + "\n")
                self.file.write("\n")
                self.file.write(self._progress_footer() + "\n")
                self.file.flush()
                self._last_render = time.monotonic()
        elif ch in ('p', 'P'):
            # Pause/resume toggle. Semantik INVERTED: pause_event SET = çalış,
            # CLEAR = duraklat. Worker'lar `await pause_event.wait()` ile
            # devam sinyalini bekler. Quit veya force aktifse pause işlemsiz
            # (kapanış kararını ezme).
            if (self.pause_event is None or self.quit_event is None
                    or self.quit_event.is_set()):
                return
            if self.pause_event.is_set():
                self.pause_event.clear()   # duraklat
            else:
                self.pause_event.set()     # devam et
            # Footer'ı yenile — durum anında görünsün.
            if self.use_ansi and self._progress_drawn:
                self._clear_progress_block()
                self.file.write("\n")
                self.file.write(self._progress_line() + "\n")
                self.file.write("\n")
                self.file.write(self._progress_footer() + "\n")
                self.file.flush()
                self._last_render = time.monotonic()


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
    # Wrap düz string'lerle yapılır; renkler post-format aşamasında uygulanır.
    wrapped_rows: list[tuple[str, list[str]]] = [
        (k, _wrap_value(v, _KEYVAL_BOX_MAX_VALUE)) for k, v in rows
    ]
    w_key = max(_visible_len(k) for k, _ in wrapped_rows)
    w_val = max(
        max(_visible_len(line) for line in vlines)
        for _, vlines in wrapped_rows
    )
    key_box_width = w_key + 2   # " key " (padding hem solda hem sağda)
    val_box_width = w_val + 2

    # `--flag` görünümlü key'ler bayrak (yeşil), kalanlar türetilmiş bilgi
    # (dim). Value taraf semantiği call site'a göre değişir — burada düz tut.
    def _paint_key(k: str) -> str:
        plain = _strip_ansi(k)
        if plain.startswith("--"):
            return _paint(k, _C_GREEN)
        return _paint(k, _C_DIM)

    title_text = f" {title} "
    if len(title_text) <= key_box_width:
        # " TITLE " + dashes — title bold-cyan, dashes dim.
        tail_dashes = "─" * (key_box_width - len(title_text))
        title_painted = (
            " " + _paint(title, _C_BOLD, _C_CYAN) + " "
            + _paint(tail_dashes, _C_DIM)
        )
    else:
        # Başlık key kutucuğuna sığmıyor — kısalt.
        title_painted = _paint(
            f" {title[: key_box_width - 3]}…"[:key_box_width],
            _C_BOLD, _C_CYAN,
        )

    border_top = (
        _paint("┌", _C_DIM)
        + title_painted
        + _paint("┬" + "─" * val_box_width + "┐", _C_DIM)
    )
    border_bot = _paint(
        "└" + "─" * key_box_width + "┴" + "─" * val_box_width + "┘", _C_DIM,
    )
    sep = _paint("│", _C_DIM)
    print(border_top, file=file)
    for k, vlines in wrapped_rows:
        # İlk satırda key görünür; takip eden wrap satırlarında key alanı boş.
        painted_k = _paint_key(k)
        for i, line in enumerate(vlines):
            key_cell = painted_k if i == 0 else ""
            print(
                f"{sep} {_pad_left(key_cell, w_key)} "
                f"{sep} {_pad_left(line, w_val)} {sep}",
                file=file,
            )
    print(border_bot, file=file)


def print_config_box(
    args: argparse.Namespace,
    judge_url: str,
    public_ip: str,
    access_urls: list[str],
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

    # Her row'un yanına karşılık geldiği uzun CLI flag adı yazılır; kullanıcı
    # "bu değeri hangi parametre ile değiştiriyorum?" sorusuna AYAR kutusuna
    # bakarak cevap bulur (--help'e geri dönmek zorunda kalmaz). Flag None
    # ise (örn. publicIP gibi türetilen değerler) sadece i18n label görünür.
    def _row(i18n_key: str, flag: str | None, value: str) -> tuple[str, str]:
        # CLI flag varsa SADECE flag adını göster (örn. "--protocol"); kullanıcı
        # değeri değiştirmek istediğinde aynen kopyalayıp komut satırına
        # yapıştırabilir. Flag None ise türetilmiş bir değer (publicIP, kimlik,
        # bucket dağılımı vs.) — bu durumda i18n label kalır.
        if flag:
            return (flag, value)
        return (t(i18n_key), value)

    rows: list[tuple[str, str]] = [
        _row("row.protocol",     "--protocol",     args.protocol),
        _row("row.input",        "--file",         args.file or t("value.stdin")),
        _row("row.output",       "--output",       args.output or t("value.stdout")),
        _row("row.judge",        "--judge",        judge_url if judge_url else t("value.judge_skipped")),
        _row("row.public_ip",    None,             public_ip or unknown),
        _row("row.level",        "--level",        f"≤{args.level}"),
        _row("row.concurrency",  "--concurrency",  str(args.concurrency)),
        _row("row.timeout",      "--timeout",      t("value.elapsed_seconds", elapsed=args.timeout)),
        _row("row.retries",      "--retries",      str(args.retries)),
        _row("row.tunnel_test",  "--tunnel-test",  on if args.tunnel_test else off),
        _row("row.mitm_test",    "--mitm-test",    on if args.mitm_test else off),
        _row("row.lang",         "--lang",         i18n.current_language()),
    ]
    if access_urls:
        samples = ", ".join(access_urls[:3]) + ("..." if len(access_urls) > 3 else "")
        rows.append(_row("row.access_test", "--access-test",
                         t("value.access_n_urls", n=len(access_urls), samples=samples)))
    else:
        rows.append(_row("row.access_test", "--access-test", off))
    # Output filtreler — sadece set edilmişse göster (kapalı default'lar
    # CONFIG kutusunu şişirmesin).
    if getattr(args, "country", None):
        rows.append(_row("row.country_filter", "--country", args.country))
    if getattr(args, "exclude_country", None):
        rows.append(_row("row.country_exclude", "--exclude-country", args.exclude_country))
    if getattr(args, "exclude_distorting", False):
        rows.append(_row("row.exclude_distorting", "--exclude-distorting", on))
    # --allow-* override'ları: default'tan saparsa CONFIG'te göster ki
    # kullanıcı "neden MITM × output'ta?" gibi sürprize düşmesin.
    if getattr(args, "allow_tunnel_fail", False):
        rows.append(_row("row.allow_tunnel_fail", "--allow-tunnel-fail", on))
    if getattr(args, "allow_mitm", False):
        rows.append(_row("row.allow_mitm", "--allow-mitm", on))
    if getattr(args, "allow_access_fail", False):
        rows.append(_row("row.allow_access_fail", "--allow-access-fail", on))
    # --user-agent override edildiyse CONFIG'te göster (default Firefox UA
    # uzun ve gürültülü; sadece override anlamlı bilgi taşır).
    if getattr(args, "user_agent", None):
        rows.append(_row("row.user_agent", "--user-agent", args.user_agent))
    if reputation_enabled:
        rows.append(_row("row.reputation", "--reputation",
                         t("value.reputation_on", run=run_index, db=args.reputation)))
        if bucket_groups is not None:
            hot = len(bucket_groups.get(BUCKET_HOT, []))
            warm = len(bucket_groups.get(BUCKET_WARM, []))
            new = len(bucket_groups.get(BUCKET_NEW, []))
            cold = len(bucket_groups.get(BUCKET_COLD, []))
            # buckets/probation türetilmiş — DB durumundan; tek tek flag yok.
            rows.append(_row("row.buckets", None,
                             t("value.buckets_breakdown",
                               hot=hot, warm=warm, new=new, cold=cold)))
        if probation_skipped:
            rows.append(_row("row.probation", None,
                             t("value.probation_skipped", n=probation_skipped)))
        rows.append(_row("row.cold_timeout", "--cold-timeout",
                         t("value.elapsed_seconds", elapsed=args.cold_timeout)))
    else:
        rows.append(_row("row.reputation", "--no-reputation", t("value.reputation_off")))
    # Önce CLI flag'i olan satırlar (kullanıcı kopyalayıp tekrar çalıştırma
    # için), sonra türetilmiş bilgi satırları (publicIP, identity, buckets…)
    # — okurken "neyi değiştirebilirim?" ile "ne gözlemlendi?" karışmasın.
    rows = (
        [r for r in rows if r[0].startswith("--")]
        + [r for r in rows if not r[0].startswith("--")]
    )
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
    # SOCKS4 protokol seviyesinde HTTP header'larına dokunmaz → çalışan her
    # SOCKS4 ≡ L1 Elite. Bu listede "L2d distorting" görmek tanım gereği
    # şüphelidir (yanlış-etiketlenmiş HTTP-CONNECT, multi-hop chain, ya da
    # transparent corporate proxy araya girmiş). Default'ta distorting'leri
    # at; kullanıcı --no-exclude-distorting ile açıkça istemediğini söylediyse
    # saygı göster.
    if args.exclude_distorting is None:
        args.exclude_distorting = (args.protocol == "socks4")
    # Access için ayrı timeout: judge timeout'un 2x'i. HTTPS+CONNECT+TLS
    # handshake kombinasyonu judge'tan (HTTP-only) daha fazla RTT istiyor; 5s
    # tight, 10s rahat. Kullanıcı --access-timeout ile override edebilir.
    if args.access_timeout is None:
        args.access_timeout = args.timeout * 2.0


def _export_good(args: argparse.Namespace) -> int:
    """--export-good: reputation DB'den HOT + WARM proxy'leri stdout'a dök.

    Tarama yapmaz; sadece DB sorgusu. -p (protokol) zorunlu — her protokolün
    kendi DB'si var. -p verilmezse error.

    Sıralama: HOT first (recent first), sonra WARM (recent first). Bu sayede
    uygulama ilk N satırı alıp en güvenli proxy'leri kullanabilir.

    Kullanım: `proxyprof -p socks5 --export-good > working.lst`
    """
    if not args.protocol:
        sys.stderr.write(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.export_good_needs_protocol')}\n"
        )
        return 1
    db_path = (
        Path(args.reputation) if args.reputation
        else default_db_path(args.protocol)
    )
    if not db_path.exists():
        sys.stderr.write(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.db_missing', path=db_path)}\n"
        )
        return 1
    rep = Reputation(db_path)
    try:
        proxies = rep.list_good()
    finally:
        rep.close()
    for p in proxies:
        print(p)
    if not args.silent:
        sys.stderr.write(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.export_good_count', n=len(proxies), db=db_path)}\n"
        )
    return 0


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
                f"{_paint('proxyprof:', _C_DIM)} {t('misc.db_missing', path=db_dir)}",
                file=sys.stderr,
            )
            return 1

    rc = 0
    for db_path in paths:
        if not db_path.exists():
            print(
                f"{_paint('proxyprof:', _C_DIM)} {t('misc.db_missing', path=db_path)}",
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


def _verify_gatekeepers(args: argparse.Namespace) -> int:
    """Her CF_GATEKEEPERS URL'ini doğrudan (proxy'siz) test eder ve
    sonucu raporlar; yaşayanları overlay'e yazar.

    "Yaşıyor" kriteri:
      - HTTP status 2xx/3xx
      - Body /cdn-cgi/trace formatında ("fl=" işareti var)
      - Response < 10sn

    Test edilen liste: aktif `CF_GATEKEEPERS` (overlay varsa onu, yoksa
    default'u). Bir önceki overlay'in kalıntılarını tekrar test etmemek
    için her zaman `CF_GATEKEEPERS_DEFAULT`'u test edelim — kullanıcı
    listenin tam aralığını her seferinde görsün.
    """
    print(
        f"{_paint('proxyprof:', _C_DIM)} {t('misc.verify_intro', n=len(CF_GATEKEEPERS_DEFAULT))}",
        file=sys.stderr,
    )

    alive: list[str] = []
    dead: list[tuple[str, str]] = []

    async def go() -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        ) as session:
            for url in CF_GATEKEEPERS_DEFAULT:
                status, reason, elapsed = await _probe_gatekeeper(session, url)
                if status == "ok":
                    alive.append(url)
                    mark = "✓"
                else:
                    dead.append((url, reason))
                    mark = "×"
                print(
                    f"  {mark}  {elapsed:5.2f}s  {reason:<30}  {url}",
                    file=sys.stderr,
                )

    asyncio.run(go())

    print("", file=sys.stderr)
    print(
        f"{_paint('proxyprof:', _C_DIM)} {t('misc.verify_summary', alive=len(alive), dead=len(dead))}",
        file=sys.stderr,
    )

    overlay = _gatekeepers_overlay_path()
    overlay.parent.mkdir(parents=True, exist_ok=True)
    if not alive:
        print(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.verify_no_alive')}",
            file=sys.stderr,
        )
        return 1
    # Overlay'e yaz: header comment + alive URL'leri.
    header_lines = [
        f"# proxyprof gatekeepers overlay — auto-generated by --verify-gatekeepers",
        f"# generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# alive: {len(alive)} of {len(CF_GATEKEEPERS_DEFAULT)} tested",
        f"# delete this file to revert to the hardcoded default list",
        "",
    ]
    if dead:
        header_lines.append("# Dead/blocked URLs (pruned):")
        for url, reason in dead:
            header_lines.append(f"#   {reason:<30}  {url}")
        header_lines.append("")
    body = "\n".join(header_lines) + "\n".join(alive) + "\n"
    try:
        overlay.write_text(body, encoding="utf-8")
    except OSError as e:
        print(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.verify_write_failed', path=overlay, err=e)}",
            file=sys.stderr,
        )
        return 1
    print(
        f"{_paint('proxyprof:', _C_DIM)} {t('misc.verify_written', path=overlay)}",
        file=sys.stderr,
    )
    return 0


async def _probe_gatekeeper(
    session: "aiohttp.ClientSession", url: str,
) -> tuple[str, str, float]:
    """Tek gatekeeper'ı test et. Döner: (kind, reason_text, elapsed_seconds).

    kind: "ok" → alive; aksi halde "fail".
    reason: kullanıcının göreceği insan-okunabilir özet (status code, error
    class adı, "no trace marker", vb.).
    """
    started = time.monotonic()
    try:
        async with session.get(url, allow_redirects=True) as resp:
            elapsed = time.monotonic() - started
            if not (200 <= resp.status < 400):
                return ("fail", f"HTTP {resp.status}", elapsed)
            body = await resp.text(errors="replace")
            # CF trace body marker: "fl=" satırı her zaman var
            if "fl=" not in body and "ip=" not in body:
                return ("fail", "no trace marker", elapsed)
            return ("ok", f"HTTP {resp.status}", elapsed)
    except (asyncio.TimeoutError, TimeoutError):
        return ("fail", "timeout", time.monotonic() - started)
    except Exception as e:  # noqa: BLE001
        cls = type(e).__name__
        msg = str(e)[:40]
        return ("fail", f"{cls}: {msg}" if msg else cls,
                time.monotonic() - started)


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
    if not r.ok:
        return False
    # --no-judge fast path: anonimlik bilgisi YOK (level/outbound/country
    # None). Level/distort/country filtreleri uygulanamaz — atlanırlar.
    # Sadece tunnel/mitm/access kapıları kalır.
    if r.judge_skipped:
        if (access_urls and not getattr(args, "allow_access_fail", False)
                and not r.access_ok):
            return False
        if (args.tunnel_test and not getattr(args, "allow_tunnel_fail", False)
                and r.tunnel_ok is False):
            return False
        if (args.mitm_test and not getattr(args, "allow_mitm", False)
                and r.mitm_suspected is True):
            return False
        return True
    # Normal mod: judge geldi, level/outbound/country bekleniyor.
    if r.level is None:
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
    """Tarama sırasında her filtre-geçen proxy'yi BELLEKTE biriktir + dedupe.

    `-o` DOSYASINA TARAMA SIRASINDA DOKUNULMAZ — eski içerik yerinde durur.
    `finalize()` çağrıldığında (normal bitiş, graceful quit, ya da force quit
    öncesi explicit çağrı) toplu olarak atomic-replace ile yazılır.

    Bu sayede tarama yarıda kesilirse (Ctrl+C, OOM, terminal kapanması):
      - finalize çağrılmadıysa: `-o` ESKİ İÇERİĞİYLE durur, hiçbir şey kaybolmaz.
      - finalize çağrıldıysa: `-o` o anki sonuçlarla güncellenmiş olur.

    Force quit handler'ı bilinçli olarak `finalize()` çağırır → kullanıcı
    'q q' ile çıksa bile o ana kadar bulunan proxy'ler `-o`'ya yazılır.

    Stdout output için (path=None) yazma yok; çağıran liste döndürülür.
    """

    def __init__(
        self,
        path: str | None,
        passes: Callable[[ScanResult], bool],
    ) -> None:
        self.path = path
        self.passes = passes
        self.seen: set[str] = set()
        # In-memory only. Hit oranı tipik %5-10; 100k tarama = 5-10k satır =
        # birkaç yüz KB RAM — hiç sorun değil.

    def on_result(self, r: ScanResult) -> None:
        if r.proxy in self.seen:
            return
        if not self.passes(r):
            return
        self.seen.add(r.proxy)

    def finalize(self) -> list[str]:
        """Sort+dedupe edilmiş kept listesini döndür ve `-o` dosyasına yaz.

        File output: `.tmp` + `os.replace()` ile atomic replace. Eski `-o`
        içeriği ancak başarılı replace anında değişir — `.tmp` yazımı
        başarısız olursa eski `-o` dokunulmamış kalır.

        Stdout (path=None): sadece sıralı listeyi döndür, çağıran print eder.

        Birden fazla çağrı güvenli (idempotent): finalize çağrılınca `self.seen`
        boşaltılmaz; tekrar çağrılırsa aynı içeriği aynı şekilde yazar.
        """
        ordered = sorted(self.seen, key=_ip_port_sort_key)
        if self.path and ordered:
            # Sadece bulgu varsa atomic-replace yap. Boş set ile finalize
            # çağırmak (örn. force quit hemen tarama başında geldiyse) eski
            # `-o` içeriğini silecekti — bunu engelle, eski dosya yerinde kalsın.
            # Boş çıktı kullanıcı için faydasız zaten.
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    for line in ordered:
                        f.write(line + "\n")
                os.replace(tmp, self.path)
            except OSError:
                # Yazma başarısız → eski `-o` dokunulmamış kalır (atomic
                # semantic). `.tmp`'yi temizle.
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
    no_judge: bool = False,
    quit_event: asyncio.Event | None = None,
    force_event: asyncio.Event | None = None,
    pause_event: asyncio.Event | None = None,
    access_timeout: float | None = None,
    access_strict: bool = False,
) -> list[ScanResult]:
    """Verilen ScanTask listesini async olarak tara.

    Her task'ın kendi `timeout` ve `bucket` etiketi vardır. Çağıran taraf
    task'ları zaten istenen dispatch order'da (weighted-interleaved) verir;
    tek shared semafor + asyncio.gather doğal olarak ilk task'ları ilk
    dispatch eder, böylece HOT bucket öncelik kazanır.

    Quit kontrolü ('q' tuşundan):
      - `quit_event` set ise: worker semafor edinmeden önce ve aldıktan sonra
        kontrol eder; set ise probe çalıştırmaz, skipped=True ScanResult
        döner. Kuyrukta bekleyen yüzlerce task hızla drain olur.
      - `force_event` set ise: arka plandaki bir watcher tüm aktif task'ları
        cancel eder. gather CancelledError'ları synthetic skipped'a çevirir.
        Tarama anında biter.
    """
    sem = asyncio.Semaphore(concurrency)
    poison = IPPoison()

    def _quit_skip(t: ScanTask, reason_key: str) -> ScanResult:
        return ScanResult(
            proxy=t.proxy, ok=False, level=None, elapsed=0.0,
            error=reason_key, skipped=True, bucket=t.bucket,
        )

    async def _pause_wait_skip(t: ScanTask) -> ScanResult | None:
        """Pause aktifse bekle; pause sırasında quit gelirse skip döner.
        Aksi halde None (devam et)."""
        if pause_event is None or pause_event.is_set():
            return None
        while not pause_event.is_set():
            # Periyodik wake-up: quit kontrolü için. asyncio.wait_for ile
            # 0.2s tick — quit'i hızlı algılar, yine de paused-bekleyiş
            # CPU spinlemez.
            try:
                await asyncio.wait_for(pause_event.wait(), timeout=0.2)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            if quit_event is not None and quit_event.is_set():
                return _quit_skip(t, "skipped: quit during pause")
        return None

    async def worker(t: ScanTask) -> ScanResult:
        # Semafor ÖNCESİ quit check: kuyrukta bekleyenler hızlı drain olsun.
        if quit_event is not None and quit_event.is_set():
            r = _quit_skip(t, "skipped: quit requested")
            if table is not None:
                table.update(r)
            if writer is not None:
                writer.on_result(r)
            return r
        async with sem:
            # Semafor SONRASI check: edinene kadar quit gelmiş olabilir.
            if quit_event is not None and quit_event.is_set():
                r = _quit_skip(t, "skipped: quit requested")
                if table is not None:
                    table.update(r)
                if writer is not None:
                    writer.on_result(r)
                return r
            # Pause check SEMAFOR İÇİNDE: 'p' basılınca slot edinmiş ama probe'a
            # başlamamış worker'lar burada bekler. Slot meşgul kalır → diğer
            # worker'lar zaten kuyrukta. Sonuç: tüm slot'lar paused worker'larla
            # dolar, hiç probe çalışmaz. Pause öncesi probe çalıştırmaya
            # başlamış worker'lar (mid-network-IO) doğal olarak tamamlanır,
            # spec'e uygun ("in-flight olanlar tamamlanır"). Resume sonrası
            # bekleyenler aynı slot'tan probe'a geçer; ek dispatch overhead'i
            # yok.
            skip = await _pause_wait_skip(t)
            if skip is not None:
                if table is not None:
                    table.update(skip)
                if writer is not None:
                    writer.on_result(skip)
                return skip
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
                    no_judge=no_judge,
                    access_timeout=access_timeout,
                    access_strict=access_strict,
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

    worker_tasks = [asyncio.create_task(worker(t)) for t in tasks]

    # Force-quit watcher: force_event set olunca tüm aktif task'ları cancel
    # et. None ise watcher gereksiz (sleep'te asılı kalmasın).
    async def _force_canceller():
        if force_event is None:
            return
        await force_event.wait()
        for wt in worker_tasks:
            if not wt.done():
                wt.cancel()

    canceller_task = asyncio.create_task(_force_canceller())
    try:
        results = await asyncio.gather(*worker_tasks, return_exceptions=True)
    finally:
        canceller_task.cancel()

    # CancelledError'ları synthetic skipped ScanResult'a çevir; diğer beklenmeyen
    # exception'ları "fail" olarak işle.
    final: list[ScanResult] = []
    for r, task in zip(results, tasks):
        if isinstance(r, ScanResult):
            final.append(r)
        elif isinstance(r, asyncio.CancelledError):
            final.append(ScanResult(
                proxy=task.proxy, ok=False, level=None, elapsed=0.0,
                error="cancelled: force quit", skipped=True, bucket=task.bucket,
            ))
        elif isinstance(r, BaseException):
            final.append(ScanResult(
                proxy=task.proxy, ok=False, level=None, elapsed=0.0,
                error=f"{type(r).__name__}: {r}", bucket=task.bucket,
            ))
        else:
            # Defensive — gather sözleşmesinde olmasa da yine de kabul et.
            final.append(_quit_skip(task, "unknown gather result"))
    return final


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
            sys.exit(f"{_paint('proxyprof:', _C_DIM)} {t('input.access_url_invalid', url=u)}")
        out.append(u)
    return out


def _resolve_access_test(arg: str | None) -> tuple[list[str], str]:
    """args.access_test → (CANDIDATE URL listesi, mode).

    Mode değerleri ACCESS_MODE_* sabitleri; UI legend'inde "ne testi yapılıyor"
    bilgisini göstermek için kullanılır.

    None                    → ([], OFF)
    "AUTO" / "cloudflare"   → TÜM CF_GATEKEEPERS havuzu, mode=CF
    "google"                → TÜM GOOGLE_GATEKEEPERS havuzu, mode=GOOGLE
    "url1,url2"             → kullanıcı verdiği URL'ler (validate edilir), CUSTOM

    Önemli: preset modlarda artık RASTGELE 3 değil, TÜM havuz döner. amain'de
    session-start auto-verify ölü URL'leri eler, sonra alive havuzdan ACCESS_
    AUTO_COUNT kadar rastgele örnek alınır. Bu sayede 3 random pick'in 2'sinin
    ölü olması ihtimali (~%50+ vakaya) ortadan kalkar.
    """
    if arg is None:
        return [], ACCESS_MODE_OFF
    norm = arg.strip().lower()
    if norm in (ACCESS_AUTO_SENTINEL.lower(), ACCESS_PRESET_CLOUDFLARE):
        return list(CF_GATEKEEPERS), ACCESS_MODE_CF
    if norm == ACCESS_PRESET_GOOGLE:
        return list(GOOGLE_GATEKEEPERS), ACCESS_MODE_GOOGLE
    return _parse_access_urls(arg), ACCESS_MODE_CUSTOM


async def _filter_alive_gatekeepers(
    urls: list[str],
    session: "aiohttp.ClientSession",
    timeout: float = 4.0,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Verilen URL listesini PARALEL test et; (alive, dead_with_reason) döner.

    `dead_with_reason` her ölü URL için (url, kısa_sebep) tuple'ı içerir;
    kullanıcıya stderr'e "şu URL şu yüzden ölü" şeklinde raporlanır.

    Kullanım: session başında ölü gatekeeper'ları o oturumdan ele. Network
    cost: ~32 paralel HTTP GET, ~1-2 saniye total (asyncio.gather).
    Aynı session re-use edilir — connection pool'dan faydalanılır.
    """
    if not urls:
        return [], []

    async def _check(url: str) -> tuple[str, str | None]:
        # asyncio.wait_for ile hard cap — bazı endpoint'ler timeout
        # parametresine rağmen asılı kalabilir (DNS hangs vb.).
        try:
            kind, reason, _elapsed = await asyncio.wait_for(
                _probe_gatekeeper(session, url),
                timeout=timeout + 1.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return url, "timeout"
        except Exception as e:  # noqa: BLE001
            return url, f"{type(e).__name__}"
        return url, (None if kind == "ok" else reason)

    results = await asyncio.gather(*(_check(u) for u in urls))
    alive: list[str] = []
    dead: list[tuple[str, str]] = []
    for url, reason in results:
        if reason is None:
            alive.append(url)
        else:
            dead.append((url, reason))
    return alive, dead


def _status(msg: str, silent: bool) -> None:
    """Bootstrap fazında 'şu an X yapıyorum' satırı.

    Tarama başlamadan önce sırayla input okuma → reputation yükleme → public
    IP + judge tespiti aşamaları çalışır; tek başına 3-5 saniyeyi bulabilir
    (özellikle judge listesinin ilkleri ölü ise). Bu sessiz boşluk yerine her
    aşamanın başında ne yapıldığını yazdırırız; bir önceki adımın tamamlandığı
    bir sonraki satırın görünmesinden anlaşılır."""
    if not silent:
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {msg}\n")
        sys.stderr.flush()


def _print_judge_unavailable_hints() -> None:
    """`JudgeUnavailable` sonrası kullanıcıya 4 somut çözüm öner.

    Sebep: tüm aday judge'lar timeout / 4xx-5xx / DNS-fail oldu. Olası
    nedenler: internet down, geçici outage, agresif timeout, firewall.
    Hint'ler yapılabilirden zora doğru sıralı."""
    sys.stderr.write("\n" + t("judge.unavail_intro") + "\n")
    sys.stderr.write(t("judge.unavail_hint_net") + "\n")
    sys.stderr.write(t("judge.unavail_hint_timeout") + "\n")
    sys.stderr.write(t("judge.unavail_hint_custom") + "\n")
    sys.stderr.write(t("judge.unavail_hint_skip") + "\n\n")
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
    sys.stderr.write(t("judge.cf_warn_alt_intro") + "\n")
    sys.stderr.write(t("judge.cf_warn_alt_custom") + "\n")
    sys.stderr.write(t("judge.cf_warn_alt_skip") + "\n\n")
    sys.stderr.flush()


async def amain(args: argparse.Namespace) -> int:
    _status(t("bootstrap.reading"), args.silent)
    proxies = read_proxies(args.file)

    # Yerel ağ IP filtresi: default'ta RFC1918 / loopback / link-local /
    # multicast adresleri input'tan eler. `--keep-local-ips` ile bypass —
    # internal lab scan'lerinde yerel proxy'leri test etmek isteyen kullanıcı
    # için. Public proxy listelerinde tipik: yanlış kopyalanmış output,
    # private subnet sızıntısı, ya da bozuk parse.
    if not getattr(args, "keep_local_ips", False):
        proxies, dropped_local = filter_local_ips(proxies)
        if dropped_local > 0:
            _status(t("bootstrap.dropped_local_ips", n=dropped_local), args.silent)
        if not proxies:
            sys.exit(f"{_paint('proxyprof:', _C_DIM)} {t('input.no_valid_pairs')}")

    # Protokol-port uyumsuzluğu sezgisi: `-p socks5` + input'ta port 4145
    # (SOCKS4 konvansiyon portu) yoğunsa kullanıcıyı uyar. Bu proxy'ler
    # büyük olasılıkla SOCKS4 daemon — SOCKS5 olarak test edilirse handshake
    # geçer (compat shim) ama HTTPS payload transferinde connection drop
    # yaşanır → tabloda `err` patlaması olur.
    if args.protocol == "socks5" and not args.silent:
        port_4145 = sum(1 for p in proxies if p.endswith(":4145"))
        if port_4145 > 0 and port_4145 / max(len(proxies), 1) >= 0.05:
            pct = 100.0 * port_4145 / len(proxies)
            sys.stderr.write(
                f"{_paint('proxyprof:', _C_DIM)} {t('hint.socks5_port_4145', n=port_4145, pct=pct)}\n"
            )

    # Output dosyası seed: `--retest-output` (default açık) iken `-o FILE`
    # mevcut ve doluysa, dosyadaki proxy'leri input'a ekle. Mantık: -o tipik
    # olarak "geçen taramanın çalışan proxy'leri" listesidir; bu run'da onları
    # da tekrar test ederek "hala çalışıyor mu?" sorusuna cevap almak istiyoruz.
    # Sonuçta -o üzerine yeni tarama çıktısı yazılır (eski içerik tekrar bu
    # taramadan geçtikten sonra kalanları ile değişir).
    if args.retest_output and args.output:
        try:
            out_path = Path(args.output)
            if out_path.exists() and out_path.stat().st_size > 0:
                with out_path.open(encoding="utf-8") as fh:
                    seed_text = fh.read()
                seed_proxies = parse_proxies(seed_text)
                if seed_proxies and not getattr(args, "keep_local_ips", False):
                    seed_proxies, _ = filter_local_ips(seed_proxies)
                if seed_proxies:
                    # Dedup ile birleştir: input order + sonra seed'in input'ta
                    # olmayanları. dict.fromkeys insertion order'ı korur.
                    before = len(proxies)
                    proxies = list(dict.fromkeys([*proxies, *seed_proxies]))
                    added = len(proxies) - before
                    if added > 0:
                        _status(
                            t("bootstrap.reseeded",
                              n=added, total=len(seed_proxies),
                              path=args.output),
                            args.silent,
                        )
        except OSError:
            # Output dosyası okunamadıysa sessizce geç — yazma vakti gelince
            # zaten error_log basılır; burada scan'i blokeleme.
            pass

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

    # `--top-n N`: input'tan SADECE en muhtemel-iyi N proxy'yi tara.
    # Sıralama: HOT (en son başarı önce) → WARM (en son başarı önce) → NEW.
    # COLD bucket tamamen DIŞARIDA — top-N kısa-zaman "şu an çalışanları test"
    # senaryosu için; ölü kuyruğu beklemiyoruz. Probation skipped'lar nasılsa
    # zaten dışarıda. Reputation kapalıysa anlamsız → hata.
    if args.top_n is not None:
        if reputation is None:
            sys.stderr.write(
                f"{_paint('proxyprof:', _C_DIM)} {t('misc.top_n_needs_reputation')}\n"
            )
            return 1
        skip_set = set(probation_skipped)
        now_for_sort = now_epoch()
        hot_cutoff = now_for_sort - 24 * 3600

        def _priority_key(p: str) -> tuple[int, int]:
            if p in skip_set:
                return (9, 0)   # probation: en arkaya at
            bucket = bucket_map.get(p, BUCKET_NEW)
            rec = bucket_records.get(p)
            ls = getattr(rec, "last_success", None) if rec else None
            if bucket == BUCKET_HOT:
                return (0, -(ls or 0))
            if bucket == BUCKET_WARM:
                return (1, -(ls or 0))
            if bucket == BUCKET_NEW:
                return (2, 0)
            return (8, 0)  # COLD

        sorted_proxies = sorted(proxies, key=_priority_key)
        # COLD ve probation'ı tamamen at
        sorted_proxies = [
            p for p in sorted_proxies
            if p not in skip_set
            and bucket_map.get(p) != BUCKET_COLD
        ]
        proxies_before = len(proxies)
        proxies = sorted_proxies[:args.top_n]
        _status(
            t("bootstrap.top_n_applied",
              n=len(proxies), max=args.top_n, total=proxies_before),
            args.silent,
        )

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
    # AsyncResolver (aiodns/c-ares) kullan — default ThreadedResolver
    # `loop.getaddrinfo`'yu blocking thread executor'a koyar; bir DNS yavaş
    # olduğunda thread tıkanır ve sonraki tüm DNS'ler kuyrukta bekleyip
    # timeout'a kadar gidebilir. AsyncResolver gerçek async DNS yapar.
    bootstrap_conn = aiohttp.TCPConnector(resolver=aiohttp.AsyncResolver())
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        connector=bootstrap_conn,
    ) as bootstrap:
        public_ip = await get_public_ip(bootstrap, timeout=bootstrap_timeout)
        if public_ip:
            _status(t("bootstrap.public_ip_found", ip=public_ip), args.silent)
        else:
            _status(t("bootstrap.public_ip_unknown"), args.silent)

        if args.no_judge:
            # --no-judge: judge'a hiç gidilmez. Anonimlik tespiti devre dışı.
            # Sadece tunnel/access/mitm testleri çalışır.
            judge_url = None  # type: ignore[assignment]
        elif args.judge:
            # Kullanıcı explicit judge verdi. CF arkasında mı kontrol et —
            # arkasındaysa tarama biasını uyar ve E/h onayı al.
            judge_url = args.judge
            is_cf, evidence = await is_judge_behind_cf(
                judge_url, bootstrap, timeout=bootstrap_timeout,
            )
            if is_cf and not args.silent:
                _print_cf_judge_warning(judge_url, evidence)
                if not _prompt(t("judge.cf_continue_prompt"), default_yes=True):
                    sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('judge.cf_aborted')}\n")
                    if reputation is not None:
                        reputation.close()
                    return 1
        else:
            # Default: CF-dışı judge'lardan rastgele bir sıralama ile dene.
            # `is_judge_behind_cf` ile CF'e geçenleri pre-filter et; geriye
            # kalanları shuffle ile her oturumda farklı sıra dene → tek bir
            # public judge'ın yükünü bizim taramamız üstüne yıkmaz.
            #
            # PARALEL filtering: eskiden sıralı çağrı yapıyordu, dead domain'in
            # DNS lookup'ı 5-15s asılabiliyordu → 9 candidate × yavaş = bootstrap
            # 30s+. Şimdi asyncio.gather ile hepsini aynı anda check ediyoruz;
            # her bir lookup için sıkı timeout (3s) — timeout olursa "muhtemelen
            # ölü, pick_judge'a yolla, orada kesin kararı verecek" varsayarak
            # non-CF olarak işaretle.
            candidates = list(judges_for(args.protocol))

            async def _cf_check(url: str) -> tuple[str, bool]:
                # Trusted self-host judge'lar (proxyjudge.php) BİLEREK CF
                # arkasında — CF-Connecting-IP normalize edip ülke kodunu
                # çıkartmak için. CF filtresi public azenv'lar bir gün CF'e
                # geçerse onları elemek içindi; kendi judge'umuzu hayır.
                if _judge_accepts_proxyprof_header(url):
                    return url, False
                try:
                    is_cf, _ = await asyncio.wait_for(
                        is_judge_behind_cf(url, session=None, timeout=3.0),
                        timeout=3.0,
                    )
                    return url, is_cf
                except (TimeoutError, asyncio.TimeoutError, OSError):
                    return url, False  # DNS timeout → non-CF varsay, pick_judge eler

            cf_results = await asyncio.gather(
                *(_cf_check(u) for u in candidates),
                return_exceptions=False,
            )
            non_cf = [u for u, is_cf in cf_results if not is_cf]
            if not non_cf:
                sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('judge.all_defaults_cf')}\n")
                non_cf = candidates
            random.shuffle(non_cf)
            pick_timeout = min(bootstrap_timeout, 6.0)
            _status(
                t("bootstrap.judge_candidates",
                  n=len(non_cf), timeout=pick_timeout),
                args.silent,
            )

            def _on_judge_attempt(
                url: str, ok: bool, reason: str, elapsed: float,
            ) -> None:
                # pick_judge bir aday bittiğinde (success/fail) çağırır;
                # cancel edilenler çağrılmaz → satır sayısı = test edilen
                # gerçek aday adedi.
                if args.silent:
                    return
                key = ("bootstrap.judge_attempt_ok" if ok
                       else "bootstrap.judge_attempt_fail")
                # URL cyan, elapsed dim, reason kırmızı (fail) ya da yok (ok);
                # ✓/× i18n string'inin içinde, dolayısıyla her dilde aynı
                # mark karakterini ayrı boyamak için raw'da bırakıp burada
                # boya: ✓ → yeşil, × → kırmızı.
                raw = t(key, url=_paint(url, _C_CYAN), reason=_paint(reason, _C_RED),
                        elapsed=elapsed)
                raw = raw.replace("✓", _paint("✓", _C_GREEN))
                raw = raw.replace("×", _paint("×", _C_RED))
                sys.stderr.write(raw + "\n")

            try:
                # pick_judge artık paralel (judges.py'de race-based);
                # candidate'ları aynı anda fırlatır, ilk parseable 200 wins.
                # Daha kısa per-judge timeout: 6s yeterli (canlı judge tipik
                # 100-500ms; 6s = "kesin ölü" sınırı).
                judge_url, _ = await pick_judge(
                    bootstrap, non_cf,
                    timeout=pick_timeout,
                    on_attempt=_on_judge_attempt,
                )
            except JudgeUnavailable as e:
                print(f"{_paint('proxyprof:', _C_DIM)} {e}", file=sys.stderr)
                _print_judge_unavailable_hints()
                if reputation is not None:
                    reputation.close()
                return 1
            _status(t("bootstrap.judge_selected", url=judge_url), args.silent)

        # ----- Auto-verify access gatekeepers (session-start prune) -----
        # access-test aktifse, kullanılacak URL'leri proxy'siz olarak şimdi
        # test et. Ölü URL'ler (CF zone disable etmiş, auth gerektiriyor,
        # DNS hijack vs.) o oturumdan elenir → tarama sırasında binlerce
        # proxy üzerinden gereksiz fail yaşanmaz.
        #
        # Tasarım kararı: --verify-gatekeepers'ın overlay yazma özelliğini
        # tetiklemez (overlay user'ın explicit komut çağırması ile yazılır).
        # Bu, sadece bu oturum için in-memory filtreleme. Kullanıcı her run'da
        # yeni ölü URL'leri görebilir; pattern oluştuysa overlay komutunu
        # çağırır.
        if access_urls:
            _status(
                t("bootstrap.verifying_access", n=len(access_urls)),
                args.silent,
            )
            alive_urls, dead_urls = await _filter_alive_gatekeepers(
                access_urls, bootstrap, timeout=4.0,
            )
            if dead_urls and not args.silent:
                # İlk birkaç ölü URL'i listele; çok sayıda varsa "..N daha".
                summary = "; ".join(
                    f"{u} ({r})" for u, r in dead_urls[:3]
                )
                if len(dead_urls) > 3:
                    summary += f"; +{len(dead_urls) - 3} daha"
                sys.stderr.write(
                    f"{_paint('proxyprof:', _C_DIM)} {t('bootstrap.access_pruned', n=len(dead_urls), total=len(access_urls), dead=summary)}\n"
                )
            if not alive_urls:
                # Tüm gatekeeper'lar ölü — access-test bu oturum için devre dışı.
                sys.stderr.write(
                    f"{_paint('proxyprof:', _C_DIM)} {t('bootstrap.no_alive_gatekeepers')}\n"
                )
                access_urls = []
                access_mode = ACCESS_MODE_OFF
            else:
                # Preset modlarda alive havuzdan N random örnek al; custom
                # modda kullanıcının verdiği URL'leri olduğu gibi kullan (ama
                # ölüler atılmış).
                if access_mode in (ACCESS_MODE_CF, ACCESS_MODE_GOOGLE):
                    k = min(ACCESS_AUTO_COUNT, len(alive_urls))
                    access_urls = random.sample(alive_urls, k=k)
                else:
                    access_urls = alive_urls

    send_identity = (
        False if args.no_judge
        else _judge_accepts_proxyprof_header(judge_url)
    )

    # HTTP proxy + HTTPS judge uyumsuzluğu uyarısı.
    # HTTPS judge'a giden trafik CONNECT tunnel + TLS içinden geçer, proxy
    # header inject EDEMEZ. Bu yüzden anonimlik tespiti (L1/L2/L2d) bu
    # senaryoda HTTP forwarding gözlemine bağlıdır ve yanıltıcı olabilir:
    # CONNECT-yetkin proxy hep L1 görünür, CONNECT-yetkinsiz proxy plain
    # forwarding'e düşerse L2/L2d görünebilir. HTTP judge auto-seçimi (-j
    # vermezsen) bu sorunu otomatik elimine eder. no_judge mode'da judge
    # yok zaten → uyarı yok.
    if (
        not args.no_judge
        and args.protocol == "http"
        and judge_url is not None
        and judge_url.lower().startswith("https://")
        and not args.silent
    ):
        print(f"{_paint('proxyprof:', _C_DIM)} {t('warn.http_proxy_https_judge')}",
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

    # 'q' tuşu için event'ler — LiveTable._on_key set ediyor, scan() ve
    # finalization akışı bunlara bakıyor. asyncio.Event running loop içinde
    # yaratılır; bu noktada amain zaten async context'inde.
    quit_event = asyncio.Event()
    force_event = asyncio.Event()
    # pause_event SEMANTIK INVERTED: SET = çalışıyor (default), CLEAR = duraklatıldı.
    # Worker'lar `await pause_event.wait()` ile devam sinyalini bekler. 'p' tuşu
    # toggle eder. Başlangıç set'li (=çalışıyor).
    pause_event = asyncio.Event()
    pause_event.set()

    # LiveTable total = aslında test edilecek proxy sayısı (probation skipped'lar
    # hariç). Probation skipped'lar tabloda görünmez ama özet kutuda raporlanır.
    # Interactive mod'da level filtresi yokmuş gibi davran (level_max=3) →
    # "seviye" status'ü hiç tetiklenmez; L2d'ler "iyi" görünür.
    table = LiveTable(
        enabled=not args.silent, total=len(tasks),
        level_max=3 if interactive_mode else args.level,
        access_mode=access_mode, access_count=len(access_urls),
        quit_event=quit_event, force_event=force_event,
        pause_event=pause_event,
        protocol=args.protocol,
        tunnel_test=args.tunnel_test, mitm_test=args.mitm_test,
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
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.cannot_open_output', path=args.output, err=e)}",
            file=sys.stderr,
        )
        return 1

    # Klavye listener: 'd' (toggle legend) + 'q' (graceful quit) / 'q' tekrar
    # (force quit). cbreak mode'a stdin'i alıyor; ne olursa olsun (Ctrl+C,
    # exception, normal exit) detach EDİLMEK ZORUNDA, aksi halde terminal
    # cbreak'te kalıp shell bozulur → try/finally.
    table.attach_keyboard_listener(asyncio.get_running_loop())
    started = time.monotonic()
    try:
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
            no_judge=args.no_judge,
            quit_event=quit_event,
            force_event=force_event,
            pause_event=pause_event,
            access_timeout=args.access_timeout,
            access_strict=args.access_strict,
        )
    finally:
        table.detach_keyboard_listener()
    table.finish()
    elapsed = time.monotonic() - started

    # Force quit ('q' iki kez basıldı): SONUÇ kutusu ve reputation update'i
    # ATLA, ama EXIT ETMEDEN ÖNCE writer.finalize() çağır ki o ana kadar
    # bulunan good proxy'ler `-o` dosyasına yazılsın. Aksi halde `-o`
    # ESKİ HALİYLE kalırdı ve bu run'da bulunanlar kaybolurdu.
    #
    # Reputation update'in atlanma sebebi farklı: yarıda kesilen tarama'nın
    # yanıltıcı veriyle DB'yi kirletmesi (yarısı skipped olan run
    # consecutive_failures sayacını shoot etmemeli).
    if force_event.is_set():
        writer.finalize()  # `-o` dosyasına o ana kadarki bulguları yaz
        if reputation is not None:
            reputation.close()
        sys.stderr.write(f"{_paint('proxyprof:', _C_DIM)} {t('misc.force_quit_finished')}\n")
        return 130  # SIGINT konvansiyonu — yarıda kesildi

    # Reputation'ı güncelle — IP-poison ile pre-skipped olanlar (r.skipped=True
    # AND r.bucket reputation'dan geliyor) DB'ye değişiklik yapmamalı; onlar
    # için consecutive_failures artırılmamalı çünkü gerçek probe çalışmadı.
    # Quit-skipped'ler de (quit_event ile skip edilenler) atlanır — aynı sebep.
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
        # --no-judge mode'da r.level None — counts[None] KeyError verir;
        # level sayımı conditional.
        if r.level is not None:
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
        # outbound_ip None: judge response eksik. --no-judge mode'da bu
        # NORMAL → sayıma katmıyoruz.
        if r.outbound_ip is None and not r.judge_skipped:
            counts["judge_incomplete"] += 1
            continue
        if r.level is not None and r.level > args.level:
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
_C_RED     = "\033[31m"
_C_GREEN   = "\033[32m"
_C_YELLOW  = "\033[33m"
_C_BLUE    = "\033[34m"
_C_MAGENTA = "\033[35m"
_C_CYAN    = "\033[36m"

# Visible-length hesabı için tüm `\x1b[...m` SGR sekanslarını eşler. Renkli
# hücreleri box render'da hizalarken ham `len()` yerine bunu kullanıyoruz;
# aksi halde ANSI escape karakterleri "görünmez" oldukları halde sütuna
# saydırıp tüm tabloyu kayık gösteriyor.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _pad_left(s: str, width: int) -> str:
    """`f"{s:<{width}}"` eşdeğeri — ama ANSI escape'leri visible width'e
    saymaz. Renkli string'ler sola yaslandığında sağa doğru tam doldurur."""
    pad = width - _visible_len(s)
    return s + " " * pad if pad > 0 else s


def _pad_right(s: str, width: int) -> str:
    pad = width - _visible_len(s)
    return " " * pad + s if pad > 0 else s


def _paint(s: str, *codes: str) -> str:
    """Stderr TTY ise `s`'yi ANSI kod(lar) ile sarar; değilse aynen döner.
    Birden fazla kod verirsen sırayla uygulanır (örn. bold+cyan)."""
    if not _color_enabled():
        return s
    return "".join(codes) + s + _C_RESET


# Legend renklendirme: bölüm başlıkları (SEVİYE/LEVEL, SÜTUNLAR/COLUMNS),
# anonimlik kod sütunu (L1/L2/L2d/L3), erişim reason kodları (mitm/to/err/Nxx)
# pre-formatted metnin içine post-process ile boyanır. Format string'de
# alignment bozulmaz — boyama satırın görünür içeriğine değil, ANSI
# escape'lere genişleme katar.
_LEGEND_SECTION_RE = re.compile(
    r"^(  )(SEVİYE|LEVEL|SÜTUNLAR(?: \(✓ = iyi sonuç\))?|COLUMNS(?: \(✓ = desirable outcome\))?)$",
    re.MULTILINE,
)
_LEGEND_LEVEL_RE = re.compile(
    r"(^    )(L1|L2d|L2|L3)(\b)", re.MULTILINE,
)
_LEGEND_COL_RE = re.compile(
    r"(^    )(TÜNEL|MITM YOK|ERİŞİM|TUNNEL|NO MITM|ACCESS)(\b)", re.MULTILINE,
)


def _colorize_legend(text: str) -> str:
    if not _color_enabled():
        return text
    text = _LEGEND_SECTION_RE.sub(
        lambda m: m.group(1) + _paint(m.group(2), _C_BOLD, _C_CYAN), text,
    )
    # Seviye kodları aynı renk skalasıyla tabloyla uyumlu.
    _LVL_COLOR = {
        "L1":  _C_GREEN, "L2": _C_YELLOW, "L2d": _C_MAGENTA, "L3": _C_RED,
    }
    text = _LEGEND_LEVEL_RE.sub(
        lambda m: m.group(1) + _paint(m.group(2), _LVL_COLOR[m.group(2)]),
        text,
    )
    text = _LEGEND_COL_RE.sub(
        lambda m: m.group(1) + _paint(m.group(2), _C_BOLD), text,
    )
    # Reason kodu satırları "Nxx", "to", "err", "mitm", "?" → kırmızı
    # (hepsi access fail neden kodu). Satır formatı her ikisinde de
    # "                  CODE  = açıklama" — boşluk + kod + " " + "=".
    text = re.sub(
        r"(^\s+)(Nxx|to|err|mitm|\?)(\s+=)",
        lambda m: m.group(1) + _paint(m.group(2), _C_RED) + m.group(3),
        text, flags=re.MULTILINE,
    )
    # `✓` her yerde yeşil (legend'de "✓ = iyi sonuç" gibi tek geçişler).
    text = text.replace("✓", _paint("✓", _C_GREEN))
    return text

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
        "--keep-local-ips", action="store_true", dest="keep_local_ips",
        help=t("cli.help.keep_local_ips"),
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
        "--no-judge", action="store_true", dest="no_judge",
        help=t("cli.help.no_judge"),
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
        "--access-strict", action="store_true", dest="access_strict",
        help=t("cli.help.access_strict"),
    )
    g_scan.add_argument(
        "--access-timeout", type=float, default=None, metavar="SECONDS",
        dest="access_timeout",
        help=t("cli.help.access_timeout"),
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
    g_scan.add_argument(
        "--top-n", type=int, default=None, metavar="N", dest="top_n",
        help=t("cli.help.top_n"),
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
        "--exclude-distorting",
        action=argparse.BooleanOptionalAction,
        default=None,
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
        "--retest-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=t("cli.help.retest_output"),
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
        "--export-good", action="store_true", dest="export_good",
        help=t("cli.help.export_good"),
    )
    g_misc.add_argument(
        "--verify-gatekeepers", action="store_true", dest="verify_gatekeepers",
        help=t("cli.help.verify_gatekeepers"),
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
                f"{_paint('proxyprof:', _C_DIM)} {t('misc.cannot_open_debug', path=args.debug, err=e)}",
                file=sys.stderr,
            )
            return 1
        print(
            f"{_paint('proxyprof:', _C_DIM)} {t('misc.debug_enabled', path=args.debug)}",
            file=sys.stderr,
        )

    # --db-stats inspeksiyon modu: tarama yok, sadece reputation DB'yi
    # özetle ve çık. -p/--protocol bu modda gerekmez (argparse'de required
    # olmadığı için kontrolü manuel yapıyoruz).
    if args.db_stats:
        return _show_db_stats(args)
    if args.export_good:
        return _export_good(args)
    if args.verify_gatekeepers:
        return _verify_gatekeepers(args)

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
                f"{_paint('proxyprof:', _C_DIM)} {t('misc.legacy_db_hint', legacy=legacy, target=target)}",
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
