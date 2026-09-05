"""The SQLite response store and the fetcher that decorates a provider.

Where this sits, and why
------------------------

Between the provider and ``core/``, wrapping the provider's HTTP transport —
not the command layer (KTD3, ARCHITECTURE §14.4). ESPN's ``box_scores()`` and
``free_agents()`` each fan out into two or three sequential requests with no
internal dedup, and they share sub-requests with each other. A cache above them
still pays every round trip on a miss; keyed on **URL plus params**, a command
pays only for the sub-requests it did not already share with another command.

That is also why :class:`CachingFetcher` takes the transport as a plain
callable. The cache never opens a socket, never imports ``requests``, and has
no opinion about how the bytes arrive.

What is on disk
---------------

Bodies, redacted, and the tags needed to purge them. The file is created
``0600`` and treated as sensitive regardless: redaction removes credential
*shapes*, but the store still holds other people's league data.

The redaction runs **after** decoding. ESPN answers gzipped, and a regex over a
gzip stream matches nothing, returns the bytes unchanged, and reports success —
the credential lands in SQLite with every test still green if the fixture it
was written against happened to be uncompressed
(``docs/memory/cassette-scrubbing-blind-spots.md``). A body that cannot be
decoded is not stored at all: bytes that cannot be read cannot be proven clean.

Failure posture
---------------

**A cache problem is never the user's problem.** A corrupt file, an unwritable
directory, a disk full — every one of them degrades to a live fetch. The store
is a latency optimisation; a read that would have succeeded without it must
still succeed. Nothing in this module raises a
:class:`~fantasy_sports.core.errors.FantasySportsError`, and the only
``ValueError`` is for a caller mistake that would destroy data
(:meth:`CacheStore.purge_by_league_tag` handed a season tag).

This module imports only the standard library and ``core``/``cache`` siblings.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fantasy_sports.cache.tags import RequestContext, TagScope, scope_of, tags_for, ttl_for
from fantasy_sports.core.redaction import (
    CREDENTIAL_QUERY_PARAMS,
    UnscrubbableResponseError,
    scrub_body,
    scrub_credential_patterns,
)

__all__ = [
    "DEFAULT_CACHE_FILENAME",
    "CacheEntry",
    "CacheMode",
    "CacheStore",
    "CachingFetcher",
    "FetchResult",
    "cache_key",
    "canonical_url",
]

DEFAULT_CACHE_FILENAME: Final[str] = "http-cache.sqlite3"

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS entries (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    body       TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS entry_tags (
    key TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (key, tag)
);
CREATE INDEX IF NOT EXISTS entry_tags_tag ON entry_tags (tag);
"""

Params = Mapping[str, Any] | None


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def _merged_query(url: str, params: Params) -> tuple[str, list[tuple[str, str]]]:
    """Split ``url``, folding its own query string together with ``params``.

    Credential parameters are dropped rather than hashed. ``espn_s2`` rotates
    whenever the human re-extracts a cookie; keying on it would throw the whole
    cache away for a change that alters no response.
    """
    parts = urlsplit(url)
    pairs = list(parse_qsl(parts.query, keep_blank_values=True))
    if params:
        pairs.extend((str(key), str(value)) for key, value in params.items())
    kept = [(key, value) for key, value in pairs if key.lower() not in CREDENTIAL_QUERY_PARAMS]
    return urlunsplit(parts._replace(query="")), sorted(kept)


def canonical_url(url: str, params: Params = None) -> str:
    """``url`` with its parameters merged in, sorted, and credentials removed.

    Sorted because ``?view=mTeam&scoringPeriodId=3`` and the reverse are one
    request, and two spellings of one request that miss each other are a cache
    that never hits.
    """
    base, pairs = _merged_query(url, params)
    query = urlencode(pairs)
    return f"{base}?{query}" if query else base


def cache_key(
    url: str,
    params: Params = None,
    *,
    extra: Params = None,
    method: str = "GET",
) -> str:
    """A stable key for one request.

    ``extra`` is for a dimension that changes the response but is not in the
    URL — ESPN's free-agent query travels in the ``x-fantasy-filter`` header,
    and two filters against one URL are two different payloads. Omitting it
    would serve the wrong body; putting the whole header set in would key on
    the ``Cookie``, which is both a credential and irrelevant.
    """
    material = [method.upper(), canonical_url(url, params)]
    if extra:
        material.extend(f"{key}={value}" for key, value in sorted(extra.items()))
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CacheEntry:
    """One stored response. ``body`` is the redacted text, never raw bytes."""

    key: str
    url: str
    body: str
    stored_at: float
    expires_at: float | None
    tags: tuple[str, ...] = ()

    def is_fresh(self, now: float) -> bool:
        """``expires_at is None`` means forever — a completed week, a past season."""
        return self.expires_at is None or now < self.expires_at


class CacheMode(StrEnum):
    """How one call is allowed to use the store."""

    DEFAULT = "default"
    """Read, and write on a miss."""

    FRESH = "fresh"
    """``--fresh``: skip the read, do the fetch, **and update the entry**.

    A refresh, not a bypass. The user asking for current data is the best
    evidence available that the entry is stale, so the next call should hit
    rather than pay for the same fetch again.
    """

    BYPASS = "no-cache"
    """``--no-cache``: neither read nor write. The store is left exactly as it was."""


@dataclass(frozen=True)
class FetchResult:
    """One fetch, and what the cache did about it."""

    body: str
    cached: bool
    """The body came from the store."""

    stored: bool = False
    """This call wrote the body to the store."""


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class CacheStore:
    """SQLite response storage under the XDG cache directory.

    Every method fails soft. If the file cannot be opened, cannot be parsed, or
    turns out to be corrupt mid-flight, the store marks itself unavailable and
    behaves like an empty cache that refuses writes — which is exactly what a
    caller needs to fall through to a live fetch.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        if path is None:
            from fantasy_sports.config.paths import cache_home

            path = cache_home() / DEFAULT_CACHE_FILENAME
        self.path = Path(path)
        if now is None:
            import time

            now = time.time
        self._now = now
        self._connection: sqlite3.Connection | None = None
        self._unavailable = False

    # --- connection -------------------------------------------------------- #

    @property
    def available(self) -> bool:
        """Whether the store can be used at all. Opens it if it is not open yet."""
        return self._connect() is not None

    def _connect(self, *, _retry: bool = True) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        if self._unavailable:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create the file ourselves so it is never briefly world-readable:
            # sqlite3 would create it 0644 and there is no umask-free way to
            # ask it for anything narrower.
            if not self.path.exists():
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            else:
                os.chmod(self.path, 0o600)
            connection = sqlite3.connect(self.path)
            connection.executescript(_SCHEMA)
            connection.commit()
        except (OSError, sqlite3.Error):
            # The most likely cause of a DatabaseError here is a file that is
            # not a database. It is a cache: rebuilding it costs one refetch,
            # and leaving it corrupt costs one refetch on every call forever.
            if _retry and self._discard_file():
                return self._connect(_retry=False)
            self._unavailable = True
            return None
        self._connection = connection
        return connection

    def _discard_file(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def _fail(self) -> None:
        """Give up on the store after a mid-flight error, without raising."""
        self.close()
        self._unavailable = True

    def close(self) -> None:
        """Release the connection. Reopening is automatic on the next call."""
        if self._connection is not None:
            with contextlib.suppress(sqlite3.Error):
                self._connection.close()
            self._connection = None

    def __enter__(self) -> CacheStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- reads ------------------------------------------------------------- #

    def get(self, key: str) -> CacheEntry | None:
        """The entry for ``key`` if it exists and is still fresh, else ``None``.

        An expired entry is deleted on the way past rather than left to
        accumulate; a cache nobody ever prunes is a disk-usage bug.
        """
        connection = self._connect()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT key, url, body, stored_at, expires_at FROM entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            entry = CacheEntry(
                key=row[0],
                url=row[1],
                body=row[2],
                stored_at=row[3],
                expires_at=row[4],
                tags=self._tags_of(connection, key),
            )
            if not entry.is_fresh(self._now()):
                self._delete(connection, [key])
                return None
        except sqlite3.Error:
            self._fail()
            return None
        return entry

    @staticmethod
    def _tags_of(connection: sqlite3.Connection, key: str) -> tuple[str, ...]:
        rows = connection.execute("SELECT tag FROM entry_tags WHERE key = ?", (key,)).fetchall()
        return tuple(row[0] for row in rows)

    # --- writes ------------------------------------------------------------ #

    def put(
        self,
        key: str,
        body: bytes | str,
        *,
        tags: Iterable[str] = (),
        ttl: float | None = None,
        url: str = "",
    ) -> bool:
        """Store ``body`` under ``key``. Returns whether it was written.

        ``body`` is decoded — gunzipped if it needs it — and scrubbed before it
        reaches SQLite, in that order. A body that cannot be decoded is not
        stored: it cannot be proven free of credentials, and an unreadable body
        is worth far less than the guarantee that nothing unaudited is on disk.

        ``ttl=None`` means the entry never expires.
        """
        try:
            text = scrub_body(body)
        except UnscrubbableResponseError:
            return False
        connection = self._connect()
        if connection is None:
            return False
        now = self._now()
        expires_at = None if ttl is None else now + ttl
        try:
            with connection:
                connection.execute(
                    "INSERT INTO entries (key, url, body, stored_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "url = excluded.url, body = excluded.body, "
                    "stored_at = excluded.stored_at, expires_at = excluded.expires_at",
                    (key, url, text, now, expires_at),
                )
                connection.execute("DELETE FROM entry_tags WHERE key = ?", (key,))
                connection.executemany(
                    "INSERT INTO entry_tags (key, tag) VALUES (?, ?)",
                    [(key, tag) for tag in tags],
                )
        except sqlite3.Error:
            self._fail()
            return False
        return True

    # --- purging ----------------------------------------------------------- #

    def purge_by_tag(self, tag: str) -> int:
        """Delete every entry carrying ``tag``. Returns how many were removed.

        Nothing calls this in v0.1 — writes arrive in v0.3 (ARCHITECTURE §9).
        It exists now because the tags it depends on are being written now, and
        an entry cached forever outlives the release that learns to purge it.
        """
        connection = self._connect()
        if connection is None:
            return 0
        try:
            keys = [
                row[0]
                for row in connection.execute(
                    "SELECT key FROM entry_tags WHERE tag = ?", (tag,)
                ).fetchall()
            ]
            if not keys:
                return 0
            with connection:
                self._delete(connection, keys)
        except sqlite3.Error:
            self._fail()
            return 0
        return len(keys)

    def purge_by_league_tag(self, tag: str) -> int:
        """:meth:`purge_by_tag`, refusing anything that is not league-scoped.

        This is the guard the two tag classes exist for. Handed a season tag,
        an unguarded purge would delete the whole-season player map that every
        league shares — the single most expensive object ESPN serves — because
        one league had a roster move. A caller that reaches this with the wrong
        tag has a bug, and a bug that destroys shared data should stop rather
        than proceed.
        """
        if scope_of(tag) is not TagScope.LEAGUE:
            raise ValueError(
                f"{tag!r} is not league-scoped; purging it would evict entries "
                "shared by every league in the season"
            )
        return self.purge_by_tag(tag)

    def clear(self) -> int:
        """Empty the store. Backs ``fantasy-sports cache clear``."""
        connection = self._connect()
        if connection is None:
            return 0
        try:
            with connection:
                removed = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                connection.execute("DELETE FROM entry_tags")
                connection.execute("DELETE FROM entries")
        except sqlite3.Error:
            self._fail()
            return 0
        return int(removed)

    @staticmethod
    def _delete(connection: sqlite3.Connection, keys: list[str]) -> None:
        placeholders = ",".join("?" * len(keys))
        connection.execute(f"DELETE FROM entry_tags WHERE key IN ({placeholders})", keys)
        connection.execute(f"DELETE FROM entries WHERE key IN ({placeholders})", keys)


# --------------------------------------------------------------------------- #
# The decorator
# --------------------------------------------------------------------------- #


class CachingFetcher:
    """A provider's HTTP transport with the store wrapped around it.

    ``fetch`` is called as ``fetch(url, params)`` and may return ``bytes`` or
    ``str``; anything gzipped is decompressed on the way in.

    **A hit and a miss return the same bytes.** The stored body is redacted, so
    the miss returns the redacted body too. Returning the live body on a miss
    and the redacted one on a hit would make the adapter's normalized output
    depend on cache state, which is a far worse failure than either — and one
    that only shows up on the second run.
    """

    def __init__(
        self,
        fetch: Callable[..., bytes | str],
        store: CacheStore,
        *,
        mode: CacheMode = CacheMode.DEFAULT,
    ) -> None:
        self._fetch = fetch
        self._store = store
        self.mode = mode

    def fetch(
        self,
        url: str,
        params: Params = None,
        *,
        context: RequestContext,
        extra: Params = None,
    ) -> FetchResult:
        """Fetch ``url``, using and updating the store according to :attr:`mode`."""
        key = cache_key(url, params, extra=extra)

        if self.mode is CacheMode.DEFAULT:
            entry = self._store.get(key)
            if entry is not None:
                return FetchResult(body=entry.body, cached=True)

        text, storable = _readable(self._fetch(url, params))

        # Scrubbed even on a bypass, so `--no-cache` and a cache hit hand the
        # adapter the same shape. A flag that changes what gets parsed is a
        # worse bug than a slow read.
        if self.mode is CacheMode.BYPASS or not storable:
            return FetchResult(body=text, cached=False, stored=False)

        stored = self._store.put(
            key,
            text,
            tags=tags_for(context),
            ttl=ttl_for(context),
            url=canonical_url(url, params),
        )
        return FetchResult(body=text, cached=False, stored=stored)

    __call__ = fetch


def _readable(body: bytes | str) -> tuple[str, bool]:
    """``body`` as scrubbed text, and whether it was clean enough to store.

    A body that will not decode is still handed back — the fetch succeeded, and
    a cache must never be the reason a read fails — but with ``False``, so it
    never reaches disk. Bytes that cannot be read cannot be proven free of
    credentials, and an unreadable body is worth much less than that guarantee.
    """
    try:
        return scrub_body(body), True
    except UnscrubbableResponseError:
        # Only ``bytes`` can fail to decode; a ``str`` body always succeeds.
        salvaged = bytes(body).decode("utf-8", errors="replace")  # type: ignore[arg-type]
        return scrub_credential_patterns(salvaged), False
