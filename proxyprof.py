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
        sys.stderr.write(f"proxyprof: creating venv at {venv_dir}\n")
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
                    "proxyprof: failed to create venv.\n"
                    "  Install the venv module first: sudo apt install python3-venv\n"
                )
                sys.exit(1)

    if not (venv_dir / "bin" / "pip").exists():
        sys.stderr.write(f"proxyprof: downloading {_GET_PIP_URL}\n")
        try:
            with urllib.request.urlopen(_GET_PIP_URL, timeout=30) as resp:
                get_pip_src = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            sys.stderr.write(
                f"proxyprof: failed to download get-pip.py: {e}\n"
                "  Check your network connection and try again.\n"
            )
            sys.exit(1)
        sys.stderr.write("proxyprof: bootstrapping pip into venv\n")
        if subprocess.run([str(venv_py)], input=get_pip_src).returncode != 0:
            sys.stderr.write("proxyprof: pip bootstrap failed.\n")
            sys.exit(1)

    sys.stderr.write(f"proxyprof: installing {' '.join(packages)}\n")
    if subprocess.run(
        [str(venv_py), "-m", "pip", "install", "--quiet", *packages]
    ).returncode != 0:
        sys.stderr.write("proxyprof: pip install failed.\n")
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
            f"proxyprof: missing dependency: {pkg_str}\n"
            f"Install with: {sys.executable} -m pip install {pkg_str}\n"
        )
        sys.exit(1)

    in_venv = sys.prefix != sys.base_prefix

    if in_venv:
        # Aktif venv'deyiz — sistem Python'unu kirletme riski yok, direkt pip.
        sys.stderr.write(
            f"proxyprof: missing dependency: {pkg_str}\n"
            f"  (active venv: {sys.prefix})\n"
        )
        if not _prompt(
            f"Install with `pip install {pkg_str}`?", default_yes=True,
        ):
            sys.exit(1)
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing]
        sys.stderr.write(f"proxyprof: running `{' '.join(cmd)}`\n")
        if subprocess.run(cmd).returncode != 0:
            sys.stderr.write("proxyprof: install failed.\n")
            sys.exit(1)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    # Sistem Python'dayız: tek doğru yol yerel venv. PEP 668'i baştan bypass
    # eder, sudo gerektirmez, sistem paketlerini kirletmez. pip yoksa
    # get-pip.py ile bootstrap edilir; varsa direkt kullanılır.
    venv_dir = Path(__file__).resolve().parent / ".venv"
    sys.stderr.write(
        f"proxyprof: missing dependency: {pkg_str}\n"
        f"  (system Python: {sys.executable})\n"
        f"Auto-setup will create {venv_dir} and install {pkg_str} there, "
        f"then restart proxyprof. No sudo, no system-package changes.\n"
    )
    if not _prompt("Proceed?", default_yes=True):
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


DEFAULT_LEVEL = 1
DEFAULT_CONCURRENCY = 500
DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 1

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


def _judge_accepts_proxyprof_header(url: str) -> bool:
    """Judge URL'i bizim CF-aware judge'umuz mu? Sadece o zaman
    `X-Proxyprof-Proxy` gönderilir.

    Detection: URL path'inin SON SEGMENTI 'proxyjudge.php' veya 'proxyjudge'
    (case-insensitive). Domain adında 'proxyjudge' geçen public servisler
    (örn. proxyjudge.biz, proxyjudge.us) bu testten geçmez — onlar
    azenv.php benzeri public judge'lardır, header sızıntısı istenmez.

    Kullanıcı kendi judge'unu farklı bir isimle deploy ederse bu kontrol
    fail eder; script'i 'proxyjudge.php' olarak adlandırmak yeterli."""
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except (ValueError, AttributeError):
        return False
    basename = path.rsplit("/", 1)[-1]
    return basename in ("proxyjudge.php", "proxyjudge")

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
            sys.exit(
                "proxyprof: no proxy input. Pipe a list via stdin "
                "(e.g. `proxine http -s | proxyprof http`) or use `-f FILE`."
            )
        text = sys.stdin.read()
    else:
        try:
            with open(file_arg, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            sys.exit(f"proxyprof: cannot read '{file_arg}': {e}")
    proxies = parse_proxies(text)
    if not proxies:
        sys.exit("proxyprof: no valid IP:PORT pairs in input.")
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
                # geliyorum" der. Sadece bizim CF-aware judge'umuz (URL'i
                # 'proxyjudge' içerir) bunu log'a yazar; public azenv'lar
                # header'ı yoksaymalı ama bazıları log'layabilir, gizliliği
                # bozmamak için onlara hiç göndermiyoruz.
                extra_headers: dict[str, str] = {}
                if _judge_accepts_proxyprof_header(judge_url):
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

            # Tunnel test: SOCKS proxy'leri zaten tünel'er, sadece http/https
            # proxy'lerde anlamlı.
            tunnel_ok: bool | None = None
            if tunnel_test:
                if protocol in ("socks4", "socks5"):
                    tunnel_ok = True
                else:
                    tunnel_ok = await _tunnel_check(
                        proxy, proxy_type, timeout,
                    )

            return ScanResult(
                proxy=proxy, ok=True, level=level, distorting=distorting,
                outbound_ip=outbound, country=country,
                elapsed=time.monotonic() - started,
                access_ok=access_ok, tunnel_ok=tunnel_ok,
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


async def _tunnel_check(
    proxy: str, proxy_type: ProxyType, timeout: float,
) -> bool:
    """HTTPS CONNECT tüneli kurulabiliyor mu? HTTP proxy'nin CONNECT desteği
    sınanır; başarı = TLS handshake + 204 yanıtı."""
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
                return resp.status == 204
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class LiveTable:
    """Tarama sırasında her sonuç satır olarak akan canlı tablo.

    Sütunlar: #, STATUS, PROXY, LVL, OUT, CC, TIME, TUN, ACC, NOTE.
    - LVL: L1 / L2 / L2d (distorting) / L3 / —
    - TUN/ACC: ✓ (geçti) / × (kaldı) / — (test yok)
    - NOTE: fail için hata mesajı; satır sığacak kadar truncate edilir.

    Append-only render (ANSI imleç manipülasyonu YOK). asyncio.gather sonuçları
    tamamlanma sırasıyla geldiğinden satırlar test sırasında doğal olarak
    "en hızlı önce" sıralanır. `#` sütunu tamamlanma sırasıdır, input
    indeksi değildir.
    """

    # Sabit genişlikli sütunlar. NOTE sütunu terminal genişliğine göre kalanı alır.
    _FIXED: dict[str, int] = {
        "#":      5,   # "999/9999" gibi; total>9999 ise auto-genişlemez
        "STATUS": 7,
        "PROXY":  21,  # 255.255.255.255:65535 = 21
        "LVL":    3,
        "OUT":    15,
        "CC":     2,
        "TIME":   6,
        "TUN":    3,
        "ACC":    3,
    }
    _MIN_NOTE = 12

    def __init__(self, enabled: bool, total: int, file=sys.stderr) -> None:
        self.file = file
        self.enabled = enabled
        self.total = total
        self.count = 0
        self._headered = False
        self._cols = dict(self._FIXED)
        # # sütunu total'a göre genişlesin: "1000/1000" = 9 chars
        self._cols["#"] = max(self._FIXED["#"], len(f"{total}/{total}"))
        self._term_width = self._detect_width()

    @staticmethod
    def _detect_width() -> int:
        try:
            return shutil.get_terminal_size((120, 24)).columns
        except OSError:
            return 120

    def _note_width(self) -> int:
        fixed_sum = sum(self._cols.values())
        # Her sütun arasında "│" + 2 boşluk = 3 char; başta ve sonda da bir │.
        seps = (len(self._cols) + 1) * 3 + 1
        return max(self._MIN_NOTE, self._term_width - fixed_sum - seps - 1)

    def _all_widths(self) -> list[int]:
        return list(self._cols.values()) + [self._note_width()]

    def _border(self, left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in self._all_widths()) + right

    def _row(self, cells: list[str]) -> str:
        widths = self._all_widths()
        parts = []
        for c, w in zip(cells, widths):
            s = c if len(c) <= w else c[: w - 1] + "…"
            # Sayısal sütunlar sağa, geri kalan sola yaslı
            if w == self._cols["TIME"] or (w == self._cols["#"] and c and c[0].isdigit()):
                parts.append(f" {s:>{w}} ")
            else:
                parts.append(f" {s:<{w}} ")
        return "│" + "│".join(parts) + "│"

    def _emit_header(self) -> None:
        labels = list(self._cols.keys()) + ["NOTE"]
        print(self._border("┌", "┬", "┐"), file=self.file)
        print(self._row(labels), file=self.file)
        print(self._border("├", "┼", "┤"), file=self.file)
        self.file.flush()

    def update(self, r: ScanResult) -> None:
        self.count += 1
        if not self.enabled:
            return
        if not self._headered:
            self._emit_header()
            self._headered = True

        # STATUS belirleme: judge'ı geçti ama ek testler düştü → "filter"
        if not r.ok:
            status = "fail"
        elif (r.access_ok is False) or (r.tunnel_ok is False):
            status = "filter"
        else:
            status = "ok"

        if not r.ok:
            lvl = "—"
        elif r.level == 1:
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

        cells = [
            f"{self.count}/{self.total}",
            status,
            r.proxy,
            lvl,
            r.outbound_ip or "—",
            r.country or "—",
            f"{r.elapsed:.1f}s",
            _mark(r.tunnel_ok),
            _mark(r.access_ok),
            (r.error or "") if not r.ok else "",
        ]
        print(self._row(cells), file=self.file)
        self.file.flush()

    def finish(self) -> None:
        if self.enabled and self._headered:
            print(self._border("└", "┴", "┘"), file=self.file)
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


def print_summary_box(
    protocol: str,
    judge: str,
    public_ip: str,
    scanned: int,
    counts: dict,
    timings: list[float],
    countries: Counter,
    output_path: str | None,
    elapsed: float,
    tunnel_test: bool,
    file=sys.stderr,
) -> None:
    """proxine'in summary kutusuyla aynı görsel dili kullan."""
    elite = counts.get(1, 0)
    anon = counts.get(2, 0)
    trans = counts.get(3, 0)
    distorting = counts.get("distorting", 0)
    bad = counts.get("bad", 0)
    blocked = counts.get("blocked")
    tunneled = counts.get("tunneled")
    dest = f"  →  {output_path}" if output_path else ""

    anon_text = f"{anon} anon"
    if distorting:
        anon_text = f"{anon} anon ({distorting} distorting)"

    rows = [
        ("protocol", protocol),
        ("judge",    judge),
        ("publicIP", public_ip or "unknown"),
        ("scanned",  f"{scanned:,} proxies"),
        ("good",     f"{elite} elite, {anon_text}, {trans} transparent{dest}"),
        ("bad",      f"{bad} (timeout/error)"),
    ]
    if blocked is not None:
        rows.append(("blocked", f"{blocked} access denied"))
    if tunnel_test and tunneled is not None:
        rows.append(("tunnel",  f"{tunneled} CONNECT-capable (of {elite + anon + trans} good)"))
    if timings:
        p50 = _percentile(timings, 50)
        p95 = _percentile(timings, 95)
        rows.append(("timing", f"p50 {p50:.1f}s · p95 {p95:.1f}s"))
    if countries:
        top = countries.most_common(5)
        country_str = " ".join(f"{c}={n}" for c, n in top)
        others = sum(countries.values()) - sum(n for _, n in top)
        if others:
            country_str += f"  +{others} more"
        rows.append(("country", country_str))
    rows.append(("elapsed", f"{elapsed:.1f}s"))

    w_key = max(len(k) for k, _ in rows)
    w_val = max(len(v) for _, v in rows)

    def line(left: str, mid: str, right: str) -> str:
        return left + "─" * (w_key + 2) + mid + "─" * (w_val + 2) + right

    def row(k: str, v: str) -> str:
        return f"│ {k:<{w_key}} │ {v:<{w_val}} │"

    print(line("┌", "┬", "┐"), file=file)
    for k, v in rows:
        print(row(k, v), file=file)
    print(line("└", "┴", "┘"), file=file)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def scan(
    proxies: list[str],
    protocol: str,
    judge_url: str,
    public_ip: str,
    concurrency: int,
    timeout: float,
    retries: int,
    access_urls: list[str],
    tunnel_test: bool,
    table: LiveTable | None,
) -> list[ScanResult]:
    sem = asyncio.Semaphore(concurrency)

    async def worker(p: str) -> ScanResult:
        async with sem:
            r = await probe(
                proxy=p, protocol=protocol, judge_url=judge_url,
                timeout=timeout, retries=retries,
                public_ip=public_ip, access_urls=access_urls,
                tunnel_test=tunnel_test,
            )
            if table is not None:
                table.update(r)
            return r

    return await asyncio.gather(*(worker(p) for p in proxies))


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
            sys.exit(
                f"proxyprof: --access-test URL '{u}' "
                "must start with http:// or https://"
            )
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
    access_urls = _resolve_access_test(args.access_test)

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
                return 1

    if not args.silent:
        extras = []
        if access_urls:
            extras.append(f"access={len(access_urls)}")
        extras.append(
            "tunnel-test=on" if args.tunnel_test else "tunnel-test=off"
        )
        extra = "  " + "  ".join(extras)
        print(
            f"proxyprof  protocol={args.protocol}  "
            f"proxies={len(proxies):,}  judge={judge_url}  "
            f"public_ip={public_ip or 'unknown'}  "
            f"concurrency={args.concurrency}  level≤{args.level}{extra}",
            file=sys.stderr,
        )

    table = LiveTable(enabled=not args.silent, total=len(proxies))

    started = time.monotonic()
    results = await scan(
        proxies=proxies,
        protocol=args.protocol,
        judge_url=judge_url,
        public_ip=public_ip,
        concurrency=args.concurrency,
        timeout=args.timeout,
        retries=args.retries,
        access_urls=access_urls,
        tunnel_test=args.tunnel_test,
        table=table,
    )
    table.finish()
    elapsed = time.monotonic() - started

    # Sayım: counts[1..3] iyi proxy seviye dağılımı (filtre öncesi gerçek).
    # distorting, blocked, tunneled iyi proxy alt türleri. timings sadece
    # ok proxy'leri içerir (percentile için).
    counts = {1: 0, 2: 0, 3: 0,
              "bad": 0, "blocked": 0, "distorting": 0, "tunneled": 0}
    timings: list[float] = []
    countries: Counter = Counter()
    kept: list[str] = []
    for r in results:
        if not r.ok:
            counts["bad"] += 1
            continue
        counts[r.level] += 1
        if r.distorting:
            counts["distorting"] += 1
        if r.tunnel_ok is True:
            counts["tunneled"] += 1
        timings.append(r.elapsed)
        if r.country:
            countries[r.country] += 1

        # Filtre: -l seviye, -a tüm URL'lere erişim, --tunnel-test ise tunnel.
        if r.level > args.level:
            continue
        if access_urls and not r.access_ok:
            counts["blocked"] += 1
            continue
        if args.tunnel_test and r.tunnel_ok is False:
            continue
        kept.append(r.proxy)

    kept_sorted = sorted(set(kept), key=_ip_port_sort_key)

    if args.output:
        try:
            out_fh = open(args.output, "w", encoding="utf-8")
        except OSError as e:
            print(f"proxyprof: cannot open '{args.output}': {e}",
                  file=sys.stderr)
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
        print_summary_box(
            protocol=args.protocol,
            judge=judge_url,
            public_ip=public_ip,
            scanned=len(results),
            counts=summary_counts,
            timings=timings,
            countries=countries,
            output_path=args.output,
            elapsed=elapsed,
            tunnel_test=args.tunnel_test,
        )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    epilog = (
        "Examples:\n"
        "  proxine http -s | proxyprof http                            "
        "Pipe proxine output, keep only elite (with CONNECT tunnel test)\n"
        "  proxyprof http -f list.lst -l 2 -o ok.lst                   "
        "File in, elite+anon, save to file\n"
        "  proxyprof socks5 -f - -c 1000 -T 8                          "
        "Stdin, 1000 concurrent, 8s timeout\n"
        "  proxyprof http -f l.lst --access-test                       "
        "Add 3 random CF gatekeepers to the filter\n"
        "  proxyprof http -f l.lst --access-test https://a.com,https://b.com  "
        "Use specific gatekeepers\n"
        "  proxyprof http -f l.lst --no-tunnel-test                    "
        "Skip CONNECT test (faster, accepts non-tunnel proxies)\n"
        "  proxyprof http -j https://yours.tld/proxyjudge.php          "
        "Use your CF-protected judge (adds country info + visit logs)\n"
    )
    p = argparse.ArgumentParser(
        prog="proxyprof",
        description=(
            "Profile a list of proxies: connectivity, anonymity level "
            "(elite / anonymous / transparent), and optional access test. "
            "Designed to chain after `proxine`."
        ),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "protocol",
        choices=("http", "https", "socks4", "socks5"),
        help="Proxy protocol of every entry in the input list.",
    )
    p.add_argument(
        "-f", "--file", metavar="FILE",
        help="Proxy list file (default: stdin if piped). Use '-' for stdin.",
    )
    p.add_argument(
        "-o", "--output", metavar="FILE",
        help="Write good proxies to FILE; stdout stays empty.",
    )
    p.add_argument(
        "-l", "--level", type=int, choices=(1, 2, 3),
        default=DEFAULT_LEVEL,
        help=(
            "Maximum anonymity level kept (1=elite only, 2=elite+anon, "
            f"3=all). Default: {DEFAULT_LEVEL}."
        ),
    )
    p.add_argument(
        "-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=f"Concurrent probes (default: {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "-T", "--timeout", type=float, default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Per-proxy timeout (default: {DEFAULT_TIMEOUT}).",
    )
    p.add_argument(
        "-r", "--retries", type=int, default=DEFAULT_RETRIES,
        metavar="N",
        help=f"Retries per proxy on failure (default: {DEFAULT_RETRIES}).",
    )
    p.add_argument(
        "-j", "--judge", metavar="URL",
        help="Custom judge URL (azenv.php-compatible).",
    )
    p.add_argument(
        "--access-test", nargs="?", const=ACCESS_AUTO_SENTINEL,
        default=None, metavar="URLS",
        help=(
            "Filter proxies by access through gatekeeper URLs. "
            "Without value: pick "
            f"{ACCESS_AUTO_COUNT} random sites from the built-in Cloudflare "
            "list (every CF-protected site's /cdn-cgi/trace endpoint). "
            "With value: comma-separated URLs; proxy must reach ALL of them. "
            "Disabled by default."
        ),
    )
    p.add_argument(
        "--tunnel-test", action=argparse.BooleanOptionalAction, default=True,
        dest="tunnel_test",
        help=(
            "For http/https proxies, verify HTTPS CONNECT tunneling works "
            f"({TUNNEL_TEST_URL} must return 204). SOCKS proxies are "
            "skipped (always tunnel). Enabled by default; "
            "use --no-tunnel-test to skip and roughly halve request count."
        ),
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Deprecated, no-op (the live table is now the default).",
    )
    p.add_argument(
        "-s", "--silent", action="store_true",
        help="Only print the proxy list to stdout; suppress all stderr.",
    )
    args = p.parse_args(argv)

    if args.judge and not (
        args.judge.startswith("http://") or args.judge.startswith("https://")
    ):
        p.error("--judge must start with http:// or https://")
    # --access-test validation _parse_access_urls içinde yapılıyor.

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nproxyprof: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
