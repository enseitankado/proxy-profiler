"""
reputation — persistent proxy reputation store.

Düzenli aralıklarla (cron vb.) çalıştırılan taramalarda işe yarar: ~100k
proxy'lik bir input listesinin büyük çoğunluğu önceki çalıştırmalardan
tanıdıktır ve %80–90'ı sürekli fail verir. Bu modül SQLite-tabanlı bir
"reputation" deposu tutar; tarayıcı bu depodan beslenip:

  - HOT  : son 24sa içinde başarılı olmuş → önce ve agresif test edilir
  - WARM : geçmişte başarılı olmuş ama HOT değil
  - NEW  : depoda hiç görülmemiş
  - COLD : üst üste `dead_threshold` (default 3) kez fail → üstel probation

biçiminde dört bucket'a ayırır. COLD bucket "her run'da arkaya itme" değil,
**üstel backoff** uygular: 3 fail sonrası 2 run'da bir, 4 fail sonrası 4 run'da
bir, …, tavanda her 64 run'da bir test edilir. Bu sayede %90'lık ölü kuyruk
pratikte iş yükünden çıkar; ama bir proxy hâlâ "tamamen unutulmaz" — geri
gelirse yakalanır.

Sadece `status=fail` (judge'a ulaşamayan) sayaç artırır. `filter` (judge geçti
ama tunnel/access düştü) proxy'nin canlı olduğunu gösterir → fail sayılmaz.

Şema basit: tek `proxy` tablosu + `meta` tablosu (run_index, schema_version).
WAL modu açık → aynı DB'ye paralel proxyprof süreçleri güvenli.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1

# `consecutive_failures >= dead_threshold` olan proxy COLD sayılır.
DEFAULT_DEAD_THRESHOLD = 3

# Probation tavanı: 2^6 = 64 run'da bir test. Bundan fazla seyrekleştirme
# "ölü proxy diriliyor mu?" probe'unu pratik olmaktan çıkarır.
DEFAULT_PROBATION_MAX_SKIP = 64

# "HOT" olmak için son başarıdan beri geçebilecek en uzun süre.
HOT_WINDOW_SECONDS = 24 * 3600

# Bucket isimleri — string sabit. UI ve scheduler her ikisi de kullanır.
BUCKET_HOT = "HOT"
BUCKET_WARM = "WARM"
BUCKET_NEW = "NEW"
BUCKET_COLD = "COLD"
BUCKETS = (BUCKET_HOT, BUCKET_WARM, BUCKET_NEW, BUCKET_COLD)

# Bucket'ın hangi oranda eşzamanlı slot alacağı. interleave order'ı (HOT 8 tane,
# sonra WARM 4 tane, …) belirler; tek shared semaphore ile birlikte HOT
# proxy'ler ilk dispatch dalgasının büyük kısmını alır ama COLD'lar da paralel
# olarak ilerler (sıradan değil ağırlıklı paralel).
DEFAULT_WEIGHTS: dict[str, int] = {
    BUCKET_HOT:  8,
    BUCKET_WARM: 4,
    BUCKET_NEW:  2,
    BUCKET_COLD: 1,
}


def _scrub(s: str | None) -> str | None:
    r"""SQLite/UTF-8'in saklayamayacağı lone surrogate'leri (\udcXX gibi)
    `?` ile değiştir.

    Bazı network kütüphaneleri (ssl, getaddrinfo) hata mesajlarında
    `surrogateescape` ile yanmış byte'lar geri verir. `executemany` o satıra
    geldiğinde tüm transaction patlar — 150k başarılı upsert kaybedilir.
    Tek bir kötü karakter yüzünden DB'yi feda etmemek için yazma sırasında
    temizlik yaparız."""
    if s is None:
        return None
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proxy (
    proxy                TEXT PRIMARY KEY,
    first_seen           INTEGER NOT NULL,
    last_seen            INTEGER NOT NULL,
    last_checked         INTEGER,
    last_checked_run     INTEGER,
    last_success         INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_attempts       INTEGER NOT NULL DEFAULT 0,
    total_successes      INTEGER NOT NULL DEFAULT 0,
    last_status          TEXT,
    last_error           TEXT,
    last_level           INTEGER,
    last_country         TEXT,
    last_outbound_ip     TEXT,
    last_elapsed         REAL
);

CREATE INDEX IF NOT EXISTS proxy_cfail_idx
    ON proxy(consecutive_failures);

CREATE INDEX IF NOT EXISTS proxy_last_seen_idx
    ON proxy(last_seen);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Record:
    proxy: str
    first_seen: int
    last_seen: int
    last_checked: int | None
    last_checked_run: int | None
    last_success: int | None
    consecutive_failures: int
    total_attempts: int
    total_successes: int
    last_status: str | None
    last_error: str | None
    last_level: int | None
    last_country: str | None
    last_outbound_ip: str | None
    last_elapsed: float | None


def default_db_dir() -> Path:
    """XDG-style config dizini: $XDG_CONFIG_HOME/proxyprof veya
    ~/.config/proxyprof."""
    import os
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "proxyprof"


def default_db_path(protocol: str | None = None) -> Path:
    """Protokole özel reputation DB yolu.

    Protokol verilirse `state-<protocol>.db` (örn. state-http.db,
    state-socks5.db). Verilmezse legacy `state.db` — geri uyumluluk için
    ve `--db-stats` global enumerasyonu için kullanılır.

    Protokol ayrımı: aynı IP:PORT farklı protokollerde farklı geçmiş
    yazar; http'de iyi olan socks5'te ölü olabilir, sayaçlar karışmaz.
    """
    name = f"state-{protocol}.db" if protocol else "state.db"
    return default_db_dir() / name


class Reputation:
    """SQLite-backed proxy reputation store.

    Tek dosyalık, daemon yok. Aynı DB'ye paralel süreçler güvenli (WAL).
    Tüm yazma yolları executemany ile batch. 100k satır < 1s upsert.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        # WAL → reader/writer izolasyonu, paralel proxyprof süreçleri güvenli.
        # NORMAL synchronous → WAL'de yine ACID, sadece fsync azalır (10-100x hız).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA_SQL)
        self._ensure_meta("schema_version", str(SCHEMA_VERSION))
        self._ensure_meta("run_index", "0")

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---- meta ---------------------------------------------------------

    def _ensure_meta(self, key: str, default: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, default),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def increment_run_index(self) -> int:
        cur = int(self.get_meta("run_index") or "0") + 1
        self.set_meta("run_index", str(cur))
        return cur

    # ---- bulk lookups -------------------------------------------------

    def get_records(self, proxies: list[str]) -> dict[str, Record]:
        """Verilen proxy listesi için var olan kayıtları çek.

        SQLite'ın default `SQLITE_MAX_VARIABLE_NUMBER` = 999. Daha büyük
        listelerde chunk'lara böl.
        """
        out: dict[str, Record] = {}
        if not proxies:
            return out
        chunk_size = 900
        for i in range(0, len(proxies), chunk_size):
            chunk = proxies[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT * FROM proxy WHERE proxy IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                out[row["proxy"]] = Record(**dict(row))
        return out

    def list_good(
        self,
        dead_threshold: int = DEFAULT_DEAD_THRESHOLD,
        now: int | None = None,
    ) -> list[str]:
        """DB'deki HOT + WARM proxy'leri 'iyilik' sırasında döndür.

        Tanım `classify()` ile uyumlu:
          - HOT  : last_success ∈ [now - 24h, now]
          - WARM : total_successes > 0 AND consecutive_failures < dead_threshold
                   AND HOT değil
          - COLD : consecutive_failures >= dead_threshold → dışarıda
          - NEW  : DB'de yok → bu fonksiyon zaten DB'den çekiyor, NEW olamaz

        Sıralama: HOT first (last_success desc), sonra WARM (last_success desc).
        En güvenli proxy'ler ilk satırlarda; uygulama ilk N'i alıp kullanabilir.

        Tarama YAPMAZ; sadece DB sorgusu. `--export-good` modunda kullanılır.
        """
        if now is None:
            now = int(time.time())
        hot_cutoff = now - HOT_WINDOW_SECONDS
        cur = self.conn.execute(
            """
            SELECT proxy FROM proxy
            WHERE total_successes > 0
              AND consecutive_failures < ?
            ORDER BY
                CASE WHEN last_success >= ? THEN 0 ELSE 1 END,
                COALESCE(last_success, 0) DESC,
                proxy
            """,
            (dead_threshold, hot_cutoff),
        )
        return [row[0] for row in cur.fetchall()]

    # ---- writes -------------------------------------------------------

    def mark_seen(self, proxies: list[str], now: int) -> None:
        """Input'taki tüm proxy'leri `last_seen=now` ile işaretle; yoksa
        en az `(first_seen, last_seen)` ile yarat.

        Bu, ileride pruning için kullanılabilir (en az 30 gündür görülmemişleri
        sil vb.). Mevcut implementasyon prune etmez."""
        if not proxies:
            return
        rows = [(p, now, now) for p in proxies]
        self.conn.executemany(
            """
            INSERT INTO proxy (proxy, first_seen, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(proxy) DO UPDATE SET last_seen = excluded.last_seen
            """,
            rows,
        )
        self.conn.commit()

    def summary(
        self,
        dead_threshold: int = DEFAULT_DEAD_THRESHOLD,
        now: int | None = None,
        country_limit: int = 10,
    ) -> dict:
        """DB snapshot — tarama YAPMADAN inspeksiyon için.

        Bucket'lar: NEW yok (NEW = input'ta var DB'de yok demek; sadece
        DB içeriğine bakarken anlamsız). HOT/WARM/COLD üzerinden dağılım,
        ek olarak last_status + last_seen age + country dağılımı döner.

        UX: bucket'ların zamanla nereye gittiğini (HOT bekleniyor ama yok,
        COLD birikmiş mi?) debug etmek için. Reputation iyi mi kötü mü
        çalışıyor, tarama yapmadan görebilelim.
        """
        if now is None:
            now = int(time.time())
        cur = self.conn.cursor()

        total = cur.execute("SELECT COUNT(*) FROM proxy").fetchone()[0]
        hot_cutoff = now - HOT_WINDOW_SECONDS
        hot = cur.execute(
            "SELECT COUNT(*) FROM proxy WHERE last_success >= ?",
            (hot_cutoff,),
        ).fetchone()[0]
        cold = cur.execute(
            "SELECT COUNT(*) FROM proxy "
            "WHERE consecutive_failures >= ? "
            "AND (last_success IS NULL OR last_success < ?)",
            (dead_threshold, hot_cutoff),
        ).fetchone()[0]
        warm = total - hot - cold

        statuses = dict(cur.execute(
            "SELECT last_status, COUNT(*) FROM proxy "
            "WHERE last_status IS NOT NULL "
            "GROUP BY last_status",
        ).fetchall())

        countries = cur.execute(
            "SELECT last_country, COUNT(*) FROM proxy "
            "WHERE last_country IS NOT NULL AND last_country != '' "
            "GROUP BY last_country "
            "ORDER BY COUNT(*) DESC LIMIT ?",
            (country_limit,),
        ).fetchall()

        # last_seen age histogram (kovalar arası overlap yok; her proxy tek
        # bucket'a düşer çünkü cutoff'lar artan sırada).
        age_buckets: list[tuple[str, int]] = [
            ("<1h", 3600),
            ("<1d", 86_400),
            ("<7d", 7 * 86_400),
            ("<30d", 30 * 86_400),
        ]
        ages: dict[str, int] = {}
        upper_cutoff = now + 1  # tüm gelecek timestamp'leri de dahil
        for label, delta in age_buckets:
            lower = now - delta
            n = cur.execute(
                "SELECT COUNT(*) FROM proxy "
                "WHERE last_seen >= ? AND last_seen < ?",
                (lower, upper_cutoff),
            ).fetchone()[0]
            ages[label] = n
            upper_cutoff = lower
        # Geri kalan: en eski (≥30d)
        ages["≥30d"] = cur.execute(
            "SELECT COUNT(*) FROM proxy WHERE last_seen < ?",
            (now - age_buckets[-1][1],),
        ).fetchone()[0]

        # Probation skip dağılımı (kaç COLD proxy hangi factor'da takılı)
        probation_factors = cur.execute(
            "SELECT consecutive_failures, COUNT(*) FROM proxy "
            "WHERE consecutive_failures >= ? "
            "GROUP BY consecutive_failures "
            "ORDER BY consecutive_failures",
            (dead_threshold,),
        ).fetchall()

        return {
            "db_path": str(self.db_path),
            "run_index": int(self.get_meta("run_index") or "0"),
            "total": total,
            "hot": hot,
            "warm": warm,
            "cold": cold,
            "statuses": statuses,
            "countries": list(countries),
            "ages": ages,
            "probation_factors": list(probation_factors),
        }

    def record_results(
        self,
        results: list,  # list[ScanResult] — duck-typed to avoid import cycle
        run_index: int,
        now: int,
    ) -> None:
        """Tarama sonuçlarını DB'ye işle.

        Politika (user-confirmed):
          - `r.ok = True` → consecutive_failures = 0, last_success = now.
            Eğer tunnel/access düştü ise last_status='filter', değilse 'ok'.
          - `r.ok = False` → consecutive_failures += 1, last_status='fail'.

        Sadece judge'a hiç ulaşamayan (`r.ok = False`) fail sayılır; "filter"
        proxy'ler probation'a girmez (canlılar, sadece bir kapıdan geçmediler).
        """
        if not results:
            return

        ok_rows: list[tuple] = []
        fail_rows: list[tuple] = []

        for r in results:
            if r.ok:
                # 'filter' = canlı ama tüm gatekeeperlardan geçmedi. Yine 'ok'
                # sayılır probation açısından; sadece last_status farkı.
                if (r.access_ok is False) or (r.tunnel_ok is False):
                    status = "filter"
                else:
                    status = "ok"
                ok_rows.append((
                    r.proxy, now, now, now, run_index, now,
                    status, r.level, _scrub(r.country),
                    _scrub(r.outbound_ip), r.elapsed,
                ))
            else:
                fail_rows.append((
                    r.proxy, now, now, now, run_index,
                    _scrub((r.error or "unknown")[:200]),
                ))

        if ok_rows:
            self.conn.executemany(
                """
                INSERT INTO proxy (
                    proxy, first_seen, last_seen, last_checked,
                    last_checked_run, last_success, consecutive_failures,
                    total_attempts, total_successes, last_status, last_error,
                    last_level, last_country, last_outbound_ip, last_elapsed
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, 1, 1, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(proxy) DO UPDATE SET
                    last_seen            = excluded.last_seen,
                    last_checked         = excluded.last_checked,
                    last_checked_run     = excluded.last_checked_run,
                    last_success         = excluded.last_success,
                    consecutive_failures = 0,
                    total_attempts       = proxy.total_attempts + 1,
                    total_successes      = proxy.total_successes + 1,
                    last_status          = excluded.last_status,
                    last_error           = NULL,
                    last_level           = excluded.last_level,
                    last_country         = excluded.last_country,
                    last_outbound_ip     = excluded.last_outbound_ip,
                    last_elapsed         = excluded.last_elapsed
                """,
                ok_rows,
            )

        if fail_rows:
            self.conn.executemany(
                """
                INSERT INTO proxy (
                    proxy, first_seen, last_seen, last_checked,
                    last_checked_run, consecutive_failures, total_attempts,
                    total_successes, last_status, last_error
                )
                VALUES (?, ?, ?, ?, ?, 1, 1, 0, 'fail', ?)
                ON CONFLICT(proxy) DO UPDATE SET
                    last_seen            = excluded.last_seen,
                    last_checked         = excluded.last_checked,
                    last_checked_run     = excluded.last_checked_run,
                    consecutive_failures = proxy.consecutive_failures + 1,
                    total_attempts       = proxy.total_attempts + 1,
                    last_status          = 'fail',
                    last_error           = excluded.last_error
                """,
                fail_rows,
            )

        self.conn.commit()


# ---------------------------------------------------------------------------
# Bucket classification + probation
# ---------------------------------------------------------------------------

def classify(record: Record | None, now: int, dead_threshold: int) -> str:
    """Bir proxy'nin bucket'ını döndür."""
    if record is None:
        return BUCKET_NEW
    if record.last_success is not None \
            and (now - record.last_success) <= HOT_WINDOW_SECONDS:
        return BUCKET_HOT
    if record.consecutive_failures >= dead_threshold:
        return BUCKET_COLD
    return BUCKET_WARM


def probation_skip_factor(
    consecutive_failures: int,
    dead_threshold: int,
    max_skip: int,
) -> int:
    """COLD proxy'nin kaç run'da bir test edilmesi gerektiği.

    consecutive_failures=dead_threshold     → her 2 run'da bir   (2^1)
    consecutive_failures=dead_threshold + 1 → her 4 run'da bir   (2^2)
    consecutive_failures=dead_threshold + 5 → her 64 run'da bir  (2^6, tavan)

    Tavandan büyük üs üretilmez; "ölü proxy tamamen unutulmaz" garantisi.
    """
    if consecutive_failures < dead_threshold:
        return 1
    exp = consecutive_failures - dead_threshold + 1
    factor = 1 << exp
    return min(factor, max_skip)


def should_test_now(
    record: Record | None,
    bucket: str,
    run_index: int,
    dead_threshold: int,
    max_skip: int,
) -> bool:
    """COLD bucket için probation kararı; diğerleri her zaman test edilir."""
    if bucket != BUCKET_COLD or record is None:
        return True
    if record.last_checked_run is None:
        return True
    factor = probation_skip_factor(
        record.consecutive_failures, dead_threshold, max_skip,
    )
    runs_since_check = run_index - record.last_checked_run
    return runs_since_check >= factor


# ---------------------------------------------------------------------------
# Weighted interleaved scheduling
# ---------------------------------------------------------------------------

def weighted_interleave(
    buckets: dict[str, list[str]],
    weights: dict[str, int],
) -> list[str]:
    """Her tur her bucket'tan `weights[bucket]` öğe çek; bittiğinde at.

    Sonuç sıra: HOT, HOT, ..., (8) WARM, WARM, ..., (4) NEW, NEW, (2) COLD, (1)
    HOT, HOT, ..., şeklinde devam eder. Tek bir asyncio.gather + Semaphore ile
    çalıştığında dispatch order = semafor edinme order'ı; HOT proxy'ler ilk
    dalganın büyük çoğunluğunu alır, ama COLD'lar da paralel başlar.
    """
    iters = {
        name: iter(buckets[name])
        for name in (BUCKET_HOT, BUCKET_WARM, BUCKET_NEW, BUCKET_COLD)
        if buckets.get(name)
    }
    out: list[str] = []
    while iters:
        for name in list(iters.keys()):
            for _ in range(weights.get(name, 1)):
                try:
                    out.append(next(iters[name]))
                except StopIteration:
                    del iters[name]
                    break
    return out


def now_epoch() -> int:
    return int(time.time())
