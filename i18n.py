"""
proxyprof — Çok dilli mesaj destek modülü.

Çeviriler tek bir merkezi yerde tutulur: `i18n/` dizini. Her dil bir
`xx.json` dosyasıdır (`xx` = ISO 639-1 kodu, örn. `en`, `tr`, `de`).
İngilizce (`en.json`) canonical referanstır; diğer dosyalar onun anahtar
setini takip eder. Bir anahtar yeni dilde yoksa runtime'da otomatik
olarak İngilizce'ye fallback olunur — yani contributor'lar dosyayı
kademeli olarak çevirip PR atabilir.

Yeni dil eklemek (contributor için):
    1. `cp i18n/en.json i18n/de.json`
    2. JSON `"key": "value"` çiftlerini hedef dile çevir
    3. `proxyprof --lang de` ile test et
    4. PR aç

Dil seçim önceliği (yüksekten alçağa):
    1. `proxyprof --lang <code>` veya `-L <code>` CLI bayrağı
    2. `PROXYPROF_LANG` ortam değişkeni
    3. Sistem locale'i (`locale.getlocale()` / `LANG`)
    4. İngilizce (her zaman mevcut fallback)
"""

from __future__ import annotations

import json
import locale as _locale
import os
from pathlib import Path


_LANG_DIR = Path(__file__).resolve().parent / "i18n"
_DEFAULT_LANG = "en"
_ENV_VAR = "PROXYPROF_LANG"

_translations: dict[str, str] = {}
_fallback: dict[str, str] = {}
_current_lang = _DEFAULT_LANG


def available_languages() -> list[str]:
    """`i18n/` dizinindeki `xx.json` dosyalarının dil kodlarını döndür.

    Sıralı: en (varsa) önce, sonra alfabetik diğerleri."""
    if not _LANG_DIR.is_dir():
        return []
    codes = sorted(p.stem for p in _LANG_DIR.glob("*.json") if p.stem)
    if _DEFAULT_LANG in codes:
        codes.remove(_DEFAULT_LANG)
        codes.insert(0, _DEFAULT_LANG)
    return codes


def _normalise(code: str) -> str:
    """`tr_TR.UTF-8` → `tr`, `de_DE@euro` → `de`, `en-US` → `en`."""
    if not code:
        return ""
    s = code.strip().lower()
    # POSIX & BCP-47 ayraçları
    for sep in ("_", "-", "@", "."):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s


def detect_system_language() -> str:
    """Env + locale tabanlı dil tespiti. Desteklenmiyorsa İngilizce.

    Sıra:
        1. `PROXYPROF_LANG`
        2. `LC_ALL` / `LC_MESSAGES` / `LANG` (locale fonksiyonları üzerinden)
        3. `locale.getdefaultlocale()`
        4. Doğrudan `LANG` env (POSIX fallback)
        5. `_DEFAULT_LANG`
    """
    candidates: list[str] = []
    env = os.environ.get(_ENV_VAR)
    if env:
        candidates.append(env)
    try:
        loc = _locale.getlocale()[0]
    except (TypeError, ValueError):
        loc = None
    if loc:
        candidates.append(loc)
    if not loc:
        try:
            loc = _locale.getdefaultlocale()[0]
        except (TypeError, ValueError):
            loc = None
        if loc:
            candidates.append(loc)
    raw_lang = os.environ.get("LANG")
    if raw_lang:
        candidates.append(raw_lang)

    supported = set(available_languages())
    for c in candidates:
        code = _normalise(c)
        if code in supported:
            return code
    return _DEFAULT_LANG


def _load_json(path: Path) -> dict[str, str]:
    """Bir JSON dosyasını flat string→string sözlük olarak yükle. Hata = {}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Yalnız string değerler; nested dict'ler atılır (flat-only şema).
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def set_language(lang: str | None = None) -> str:
    """Aktif dili ayarla. None = sistem tespiti.

    İngilizce her zaman fallback olarak yüklenir; eksik anahtarlar
    için runtime'da kullanılır.

    Returns: efektif olarak ayarlanan dil kodu.
    """
    global _translations, _fallback, _current_lang

    _fallback = _load_json(_LANG_DIR / f"{_DEFAULT_LANG}.json")

    target = _normalise(lang) if lang else detect_system_language()
    if not target:
        target = _DEFAULT_LANG
    target_path = _LANG_DIR / f"{target}.json"
    if not target_path.exists():
        target = _DEFAULT_LANG
        target_path = _LANG_DIR / f"{_DEFAULT_LANG}.json"

    if target == _DEFAULT_LANG:
        _translations = dict(_fallback)
    else:
        _translations = _load_json(target_path)
        # Hiç yüklenememişse (bozuk JSON vb.) → en fallback
        if not _translations:
            _translations = dict(_fallback)
            target = _DEFAULT_LANG

    _current_lang = target
    return target


def current_language() -> str:
    return _current_lang


def t(key: str, **kwargs: object) -> str:
    """Anahtarı çevir. Eksikse İngilizce'ye, o da yoksa anahtarın kendisine düş.

    Placeholders: `t("deps.missing", pkgs="aiohttp")` →
    "missing dependency: aiohttp"
    """
    msg = _translations.get(key) or _fallback.get(key) or key
    if kwargs:
        try:
            return msg.format(**kwargs)
        except (KeyError, IndexError):
            return msg
    return msg


def pre_parse_lang(argv: list[str] | None) -> str | None:
    """argparse'tan ÖNCE argv'de `--lang`/`-L` ara. Help text çevirisi için.

    Desteklenen formlar: `--lang tr`, `--lang=tr`, `-L tr`, `-Ltr`.
    Bulamazsa None döner; çağıran taraf detect_system_language()'a düşer.
    """
    av = list(argv) if argv is not None else None
    if av is None:
        import sys
        av = sys.argv[1:]
    i = 0
    while i < len(av):
        a = av[i]
        if a == "--lang" or a == "-L":
            if i + 1 < len(av):
                return av[i + 1]
        elif a.startswith("--lang="):
            return a.split("=", 1)[1]
        elif a.startswith("-L") and len(a) > 2 and not a.startswith("-L "):
            return a[2:]
        i += 1
    return None
