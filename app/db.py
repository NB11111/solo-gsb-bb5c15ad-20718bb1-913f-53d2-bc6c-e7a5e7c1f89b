"""SQLite persistence layer (standard-library ``sqlite3``, no ORM needed)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS addresses (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    full_text            TEXT NOT NULL,
    tokens_json          TEXT NOT NULL,
    street_tokens_json   TEXT NOT NULL DEFAULT '[]',
    locality_tokens_json TEXT NOT NULL DEFAULT '[]',
    house                TEXT,
    line1                TEXT,
    city                 TEXT,
    state                TEXT,
    postal_code          TEXT,
    country              TEXT,
    external_id          TEXT,
    raw_json             TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_addresses_external_id ON addresses(external_id);

CREATE TABLE IF NOT EXISTS match_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query            TEXT NOT NULL,
    status           TEXT NOT NULL,          -- auto_matched | needs_review | no_match
    score            REAL,
    selected_id      INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    candidate_id     INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    token_score      REAL,
    house_score      REAL,
    matched_tokens   TEXT,                   -- JSON
    conflicts        TEXT,                   -- JSON
    candidates_json  TEXT NOT NULL,          -- JSON
    created_at       TEXT NOT NULL,
    reviewed_at      TEXT,
    review_action    TEXT                   -- confirmed | overridden | rejected
);
CREATE INDEX IF NOT EXISTS idx_match_logs_status ON match_logs(status);
"""


def connect() -> sqlite3.Connection:
    settings = get_settings()
    db_file = Path(settings.db_path)
    if not db_file.is_absolute():
        db_file = Path.cwd() / db_file
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- addresses --

ADDRESS_FIELDS = ("line1", "city", "state", "postal_code", "country", "external_id")


def insert_address(conn: sqlite3.Connection, *, full_text: str, tokens: list[str],
                   house: str | None, fields: dict[str, Any],
                   raw: dict[str, Any],
                   street_tokens: list[str] | None = None,
                   locality_tokens: list[str] | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO addresses
            (full_text, tokens_json, street_tokens_json, locality_tokens_json,
             house, line1, city, state, postal_code, country, external_id,
             raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            full_text,
            json.dumps(tokens, ensure_ascii=False),
            json.dumps(street_tokens if street_tokens is not None else tokens,
                       ensure_ascii=False),
            json.dumps(locality_tokens or [], ensure_ascii=False),
            house,
            fields.get("line1"),
            fields.get("city"),
            fields.get("state"),
            fields.get("postal_code"),
            fields.get("country"),
            fields.get("external_id"),
            json.dumps(raw, ensure_ascii=False),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def count_addresses(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0])


def get_address(conn: sqlite3.Connection, address_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()


def list_addresses(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM addresses ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def all_addresses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM addresses ORDER BY id").fetchall()


def address_exists(conn: sqlite3.Connection, full_text: str, external_id: str | None) -> bool:
    if external_id:
        row = conn.execute(
            "SELECT 1 FROM addresses WHERE external_id = ? OR full_text = ? LIMIT 1",
            (external_id, full_text),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM addresses WHERE full_text = ? LIMIT 1", (full_text,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------- match logs --

def insert_match_log(conn: sqlite3.Connection, *, query: str, status: str,
                     score: float | None, candidate_id: int | None,
                     token_score: float | None, house_score: float | None,
                     matched_tokens: list[dict[str, Any]],
                     conflicts: list[dict[str, Any]],
                     candidates: list[dict[str, Any]]) -> int:
    cur = conn.execute(
        """
        INSERT INTO match_logs
            (query, status, score, candidate_id, token_score, house_score,
             matched_tokens, conflicts, candidates_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query,
            status,
            score,
            candidate_id,
            token_score,
            house_score,
            json.dumps(matched_tokens, ensure_ascii=False),
            json.dumps(conflicts, ensure_ascii=False),
            json.dumps(candidates, ensure_ascii=False),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def list_match_logs(conn: sqlite3.Connection, status: str | None = None,
                    limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM match_logs WHERE status = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM match_logs ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def get_match_log(conn: sqlite3.Connection, log_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM match_logs WHERE id = ?", (log_id,)).fetchone()


def review_match_log(conn: sqlite3.Connection, log_id: int, *, action: str,
                     selected_id: int | None, score: float | None) -> None:
    conn.execute(
        """
        UPDATE match_logs
           SET status = 'reviewed', review_action = ?, selected_id = ?,
               score = ?, reviewed_at = ?
         WHERE id = ?
        """,
        (action, selected_id, score, _now(), log_id),
    )


def count_match_logs(conn: sqlite3.Connection, status: str | None = None) -> int:
    if status:
        return int(
            conn.execute("SELECT COUNT(*) FROM match_logs WHERE status = ?", (status,))
            .fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM match_logs").fetchone()[0])
