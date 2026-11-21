"""SQLite connection, schema and transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id TEXT PRIMARY KEY,
    title     TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    volume_id  TEXT NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
    page_id    TEXT NOT NULL,
    section_id TEXT NOT NULL,
    label      TEXT NOT NULL,              -- display page number, always TEXT
    active     INTEGER NOT NULL CHECK (active IN (0, 1)),
    seq        INTEGER NOT NULL,           -- position in the physical order
    PRIMARY KEY (volume_id, page_id),
    UNIQUE (volume_id, seq)
);

CREATE INDEX IF NOT EXISTS pages_lookup
    ON pages (volume_id, section_id, label, active);
CREATE INDEX IF NOT EXISTS pages_seq
    ON pages (volume_id, seq);

CREATE TABLE IF NOT EXISTS citations (
    citation_id      TEXT PRIMARY KEY,
    text             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('PINNED', 'RANGE')),
    volume_id        TEXT NOT NULL REFERENCES volumes(volume_id),
    position         INTEGER NOT NULL UNIQUE, -- stable export order
    created_revision INTEGER NOT NULL,
    origin_json      TEXT
);

CREATE TABLE IF NOT EXISTS pinned_members (
    citation_id TEXT NOT NULL REFERENCES citations(citation_id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    volume_id   TEXT NOT NULL,
    page_id     TEXT NOT NULL,
    PRIMARY KEY (citation_id, position),
    FOREIGN KEY (volume_id, page_id)
        REFERENCES pages(volume_id, page_id)
);

-- Reverse lookup of pinned members is an indexed identity lookup.
CREATE INDEX IF NOT EXISTS pinned_members_page
    ON pinned_members (volume_id, page_id);
CREATE INDEX IF NOT EXISTS pinned_members_citation
    ON pinned_members (citation_id, position);

CREATE TABLE IF NOT EXISTS range_endpoints (
    citation_id    TEXT PRIMARY KEY REFERENCES citations(citation_id) ON DELETE CASCADE,
    volume_id      TEXT NOT NULL,
    start_page_id  TEXT NOT NULL,
    end_page_id    TEXT NOT NULL,
    FOREIGN KEY (volume_id, start_page_id)
        REFERENCES pages(volume_id, page_id),
    FOREIGN KEY (volume_id, end_page_id)
        REFERENCES pages(volume_id, page_id)
);

CREATE INDEX IF NOT EXISTS range_endpoints_start
    ON range_endpoints (volume_id, start_page_id);
CREATE INDEX IF NOT EXISTS range_endpoints_end
    ON range_endpoints (volume_id, end_page_id);

CREATE TABLE IF NOT EXISTS operation_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_index    INTEGER,
    batch_id         TEXT,
    request_json     TEXT NOT NULL,
    status           TEXT NOT NULL,       -- applied | noop | rejected
    catalog_revision INTEGER,
    error_code       TEXT,
    error_detail     TEXT,
    expectation      TEXT,
    expectation_met  INTEGER,
    recorded_at      TEXT NOT NULL
);
"""

# Proof / print pre-review schema.  Kept separate so it can be created on
# databases that were already built by earlier versions (``ensure_proof_schema``).
PROOF_SCHEMA = """
-- A volume has two independent printing names, owned by the print shop.
CREATE TABLE IF NOT EXISTS volume_names (
    volume_id  TEXT PRIMARY KEY,
    full_name  TEXT NOT NULL,
    short_name TEXT NOT NULL
);

-- A chapter opens empty and never inherits groups from another chapter.
CREATE TABLE IF NOT EXISTS proof_chapters (
    chapter_id    TEXT PRIMARY KEY,
    chapter_order INTEGER NOT NULL UNIQUE,
    title         TEXT,
    created_revision INTEGER
);

-- A stable footnote group belongs to exactly one chapter.  ``position`` is the
-- discovery order (first appearance) used for grouping and reporting only; it
-- is never a total order imposed by the editor.
CREATE TABLE IF NOT EXISTS proof_groups (
    group_id      TEXT PRIMARY KEY,
    chapter_id    TEXT NOT NULL REFERENCES proof_chapters(chapter_id),
    position      INTEGER NOT NULL,
    created_revision INTEGER,
    UNIQUE (chapter_id, position)
);

-- An excerpt is a fixed reference (PINNED / RANGE) already stored in
-- ``citations``.  A group contains a de-duplicated sequence of excerpts in
-- first-appearance order of their complete (volume, stable page) sequence.
CREATE TABLE IF NOT EXISTS proof_excerpts (
    group_id    TEXT NOT NULL REFERENCES proof_groups(group_id),
    seq         INTEGER NOT NULL,
    citation_id TEXT NOT NULL REFERENCES citations(citation_id),
    PRIMARY KEY (group_id, seq)
);

-- A signed ordering claim: "from_group must print before to_group" inside one
-- chapter.  The same pair may be claimed independently by several desks; each
-- desk's claim is its own row and can be retracted on its own.
CREATE TABLE IF NOT EXISTS proof_claims (
    claim_id     TEXT PRIMARY KEY,
    chapter_id   TEXT NOT NULL REFERENCES proof_chapters(chapter_id),
    from_group   TEXT NOT NULL REFERENCES proof_groups(group_id),
    to_group     TEXT NOT NULL REFERENCES proof_groups(group_id),
    desk_id      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active','retracted')),
    retracted_at TEXT,
    created_revision INTEGER
);

-- One desk may not hold the same relation twice *at the same time*; after it
-- retracts, it may sign the relation again (a later correction often does).
CREATE UNIQUE INDEX IF NOT EXISTS proof_claims_active_unique
    ON proof_claims (chapter_id, from_group, to_group, desk_id)
    WHERE status = 'active';

-- A correction replaces a prior claim's binding.  It must point at the claim
-- it corrects; the old claim is retracted and a new claim is created.
CREATE TABLE IF NOT EXISTS proof_corrections (
    correction_id TEXT PRIMARY KEY,
    claim_id      TEXT NOT NULL REFERENCES proof_claims(claim_id),
    new_claim_id  TEXT NOT NULL REFERENCES proof_claims(claim_id),
    desk_id       TEXT NOT NULL,
    reason        TEXT,
    created_revision INTEGER
);

-- Proof-layer correction of an excerpt's binding.  The original catalog
-- citation is never touched; only the group points at a different, already
-- existing fixed reference.
CREATE TABLE IF NOT EXISTS proof_excerpt_corrections (
    correction_id   TEXT PRIMARY KEY,
    group_id        TEXT NOT NULL REFERENCES proof_groups(group_id),
    seq             INTEGER NOT NULL,
    old_citation_id TEXT NOT NULL,
    new_citation_id TEXT NOT NULL,
    desk_id         TEXT NOT NULL,
    reason          TEXT,
    created_revision INTEGER
);

-- Issued handover batches are immutable.  Content is the serialised chapter
-- snapshot at issue time; re-opening never rewrites it.
CREATE TABLE IF NOT EXISTS proof_handover_batches (
    batch_id          TEXT PRIMARY KEY,
    catalog_revision  INTEGER NOT NULL,
    issued_at         TEXT NOT NULL,
    content_json      TEXT NOT NULL
);
"""


def ensure_proof_schema(conn: sqlite3.Connection) -> None:
    """Create the proof schema if absent (safe on existing databases)."""

    conn.executescript(PROOF_SCHEMA)



def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; transactions are explicit."""

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a read snapshot (export) coexist with a writer.  The export code
    # loads the complete state inside one read transaction and evaluates from
    # that immutable snapshot only.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def catalog_revision(conn: sqlite3.Connection) -> int:
    value = get_meta(conn, "catalog_revision", "0")
    return int(value or 0)


def set_catalog_revision(conn: sqlite3.Connection, revision: int) -> None:
    set_meta(conn, "catalog_revision", str(int(revision)))


def bump_catalog_revision(conn: sqlite3.Connection) -> int:
    revision = catalog_revision(conn) + 1
    set_catalog_revision(conn, revision)
    return revision


def next_citation_position(conn: sqlite3.Connection) -> int:
    value = int(get_meta(conn, "citation_position", "0") or 0) + 1
    set_meta(conn, "citation_position", str(value))
    return value


def proof_revision(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "proof_revision", "0") or 0)


def bump_proof_revision(conn: sqlite3.Connection) -> int:
    revision = proof_revision(conn) + 1
    set_meta(conn, "proof_revision", str(revision))
    return revision


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """An immediate write transaction, committed only when the block returns."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """A consistent read transaction (SQLite snapshot isolation in WAL mode)."""

    conn.execute("BEGIN")
    try:
        yield
    finally:
        # Ending a read transaction with ROLLBACK is harmless and never writes.
        conn.execute("ROLLBACK")


def in_transaction(conn: sqlite3.Connection) -> bool:
    return bool(conn.in_transaction)


def clear_all(conn: sqlite3.Connection) -> None:
    """Clear every application table (used only for an explicit reset)."""

    with write_transaction(conn):
        for table in (
            "proof_excerpt_corrections",
            "proof_corrections",
            "proof_claims",
            "proof_excerpts",
            "proof_groups",
            "proof_chapters",
            "proof_handover_batches",
            "volume_names",
            "operation_log",
            "pinned_members",
            "range_endpoints",
            "citations",
            "pages",
            "volumes",
            "meta",
        ):
            conn.execute(f"DELETE FROM {table}")
