"""
SQLite-backed response cache with per-entry TTLs.

Wraps billed / rate-limited collector calls (Google Places, Census, BLS QCEW)
so repeat runs are reproducible and cheap. Every entry stores the as-of
timestamp of the fetch; the report surfaces this in the source audit so a
reader can see how fresh the underlying data actually is.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class CacheEntry:
    value: Any
    as_of: str           # UTC ISO timestamp of the fetch
    ttl_seconds: int
    provider: str


_LATLON_KEYS = ("lat", "lon", "latitude", "longitude")


def _round_coord(value: Any) -> Any:
    """Round lat/lon to 4 decimals (~11 m). Non-numeric values pass through."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 4)
    return value


def _canonical_params(params: Dict[str, Any]) -> str:
    """Serialize params to a stable string for hashing.

    Coordinates are rounded to 4 decimal places before serialization so two
    queries within ~11 m collapse to the same key. Keys are sorted, separators
    are compact — no whitespace.
    """
    normalized: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        normalized[k] = _round_coord(v) if k in _LATLON_KEYS else v
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                      default=str)


def build_cache_key(provider: str, method: str, params: Dict[str, Any]) -> str:
    """Build a stable cache key of the form ``provider:method:params_hash``."""
    blob = _canonical_params(params)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{provider}:{method}:{digest}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
  key         TEXT PRIMARY KEY,
  provider    TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  as_of_iso   TEXT NOT NULL,
  ttl_seconds INTEGER NOT NULL,
  hits        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_provider ON cache_entries(provider);
"""


_VALID_MODES = ("auto", "no-cache", "force-refresh")


class Cache:
    """SQLite-backed key/value cache with per-entry TTLs and a mode."""

    def __init__(self, db_path: Path, *, mode: str = "auto") -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Cache mode must be one of {_VALID_MODES}; got {mode!r}"
            )
        self._mode = mode
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_or_recover()
        self._session_keys: Dict[str, list] = {}

    def _open_or_recover(self) -> sqlite3.Connection:
        # Bound before the try so the except branch can safely guard close().
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            # Sanity check — touch the table.
            conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
            return conn
        except sqlite3.DatabaseError as exc:
            logger.warning(
                f"cache: corrupted DB at {self._db_path} ({exc}); "
                f"recreating fresh."
            )
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                self._db_path.unlink(missing_ok=True)
            except OSError:
                pass
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

    # --- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        """Release the SQLite connection. Idempotent."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup if the caller forgot close() / context manager.
        try:
            self.close()
        except Exception:
            pass

    @property
    def mode(self) -> str:
        return self._mode

    def _row_to_entry(self, row: tuple) -> CacheEntry:
        provider, value_json, as_of_iso, ttl_seconds = row
        return CacheEntry(
            value=json.loads(value_json),
            as_of=as_of_iso,
            ttl_seconds=int(ttl_seconds),
            provider=provider,
        )

    def info(self, key: str) -> Optional[CacheEntry]:
        cur = self._conn.execute(
            "SELECT provider, value_json, as_of_iso, ttl_seconds "
            "FROM cache_entries WHERE key = ?", (key,),
        )
        row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def _is_stale(self, entry: CacheEntry) -> bool:
        parsed = _parse_iso(entry.as_of)
        if parsed is None:
            return True
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        return age > entry.ttl_seconds

    def _track(self, provider: str, as_of: str) -> None:
        self._session_keys.setdefault(provider, []).append(as_of)

    def get(self, key: str) -> Optional[CacheEntry]:
        if self._mode in ("no-cache", "force-refresh"):
            return None
        entry = self.info(key)
        if entry is None or self._is_stale(entry):
            return None
        self._conn.execute(
            "UPDATE cache_entries SET hits = hits + 1 WHERE key = ?", (key,),
        )
        self._conn.commit()
        self._track(entry.provider, entry.as_of)
        return entry

    def set(self, key: str, value: Any, ttl_seconds: int, *,
            provider: str) -> None:
        if self._mode == "no-cache":
            return  # never write in no-cache mode
        as_of = _utcnow_iso()
        self._conn.execute(
            "INSERT OR REPLACE INTO cache_entries "
            "(key, provider, value_json, as_of_iso, ttl_seconds, hits) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (key, provider, json.dumps(value, default=str), as_of,
             int(ttl_seconds)),
        )
        self._conn.commit()
        self._track(provider, as_of)

    def session_max_as_of(self, provider: str) -> Optional[str]:
        """Most recent as-of timestamp served for `provider` this session."""
        served = self._session_keys.get(provider) or []
        return max(served) if served else None

    def snapshot_session(self) -> Dict[str, str]:
        """Return {provider: max_as_of} for every provider served this session.

        This is a serializable snapshot suitable for embedding in the JSON
        report context, so report renderers can show data freshness without
        needing access to the live Cache object.
        """
        out: Dict[str, str] = {}
        for provider, served in self._session_keys.items():
            if served:
                out[provider] = max(served)
        return out

    def purge_stale(self) -> int:
        """Delete all expired entries. Returns the number removed."""
        now = datetime.now(timezone.utc)
        removed = 0
        cur = self._conn.execute(
            "SELECT key, as_of_iso, ttl_seconds FROM cache_entries"
        )
        stale_keys = []
        for key, as_of_iso, ttl_seconds in cur.fetchall():
            parsed = _parse_iso(as_of_iso)
            if parsed is None or (now - parsed).total_seconds() > int(ttl_seconds):
                stale_keys.append(key)
        for key in stale_keys:
            self._conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
            removed += 1
        self._conn.commit()
        return removed

    def clear(self, provider: Optional[str] = None) -> int:
        """Delete all entries (or just one provider's). Returns count removed."""
        if provider is None:
            cur = self._conn.execute("SELECT COUNT(*) FROM cache_entries")
            n = cur.fetchone()[0]
            self._conn.execute("DELETE FROM cache_entries")
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE provider = ?",
                (provider,),
            )
            n = cur.fetchone()[0]
            self._conn.execute(
                "DELETE FROM cache_entries WHERE provider = ?", (provider,),
            )
        self._conn.commit()
        return int(n)


from typing import Callable, Tuple


def cached_call(
    cache: Optional[Cache],
    provider: str,
    method: str,
    params: Dict[str, Any],
    ttl_seconds: int,
    live_call: Callable[[], Any],
) -> Tuple[Any, str]:
    """Wrap a live call with caching. Returns (value, as_of_iso).

    The as_of is the cached entry's timestamp on a hit, or the live fetch
    time on a miss. Either way it represents when the value was actually
    collected from the upstream source — what the report should display.
    ``cache=None`` is allowed and simply runs the live call uncached.
    """
    if cache is None:
        return live_call(), _utcnow_iso()
    key = build_cache_key(provider, method, params)
    hit = cache.get(key)
    if hit is not None:
        return hit.value, hit.as_of
    value = live_call()
    as_of = _utcnow_iso()
    cache.set(key, value, ttl_seconds=ttl_seconds, provider=provider)
    return value, as_of
