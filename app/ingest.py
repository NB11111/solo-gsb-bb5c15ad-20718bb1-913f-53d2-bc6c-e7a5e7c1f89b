"""Helpers for building/inserting standard addresses from CSV or JSON."""
from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Iterable

from . import db
from .normalization import normalize_parts

# Accepted CSV header aliases -> canonical field name.
_FIELD_ALIASES: dict[str, str] = {
    "address": "full_text",
    "addr": "full_text",
    "formatted_address": "full_text",
    "full_address": "full_text",
    "standard_address": "full_text",
    "line1": "line1",
    "street": "line1",
    "address_line1": "line1",
    "city": "city",
    "town": "city",
    "state": "state",
    "province": "state",
    "region": "state",
    "postal_code": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "zipcode": "postal_code",
    "country": "country",
    "external_id": "external_id",
    "id": "external_id",
    "code": "external_id",
}

_ADDRESS_KEYS = ("line1", "city", "state", "postal_code", "country")


def build_full_text(raw: dict[str, Any]) -> str:
    """Compose the comparable address string from structured parts."""
    explicit = raw.get("full_text")
    if explicit:
        return str(explicit).strip()
    parts = [
        raw.get("line1"),
        raw.get("city"),
        raw.get("state"),
        raw.get("postal_code"),
        raw.get("country"),
    ]
    return ", ".join(str(p).strip() for p in parts if p).strip()


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map arbitrary header aliases to canonical fields; strip blanks."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        canonical = _FIELD_ALIASES.get(str(key).strip().lower())
        if canonical and value is not None and str(value).strip() != "":
            out.setdefault(canonical, str(value).strip())
    out["full_text"] = build_full_text(out)
    return out


def add_address(conn: sqlite3.Connection, raw: dict[str, Any],
                skip_duplicates: bool = True) -> int | None:
    """Normalise and persist one standard address. Returns its id (or None)."""
    norm_row = normalize_row(raw)
    full_text = norm_row["full_text"]
    if not full_text:
        return None
    external_id = norm_row.get("external_id")
    if skip_duplicates and db.address_exists(conn, full_text, external_id):
        return None

    normalized = normalize_parts(
        line1=norm_row.get("line1") if not norm_row.get("full_text") else None,
        city=norm_row.get("city"),
        state=norm_row.get("state"),
        postal_code=norm_row.get("postal_code"),
        country=norm_row.get("country"),
        full_text=norm_row.get("full_text"),
    )

    fields = {key: norm_row.get(key) for key in _ADDRESS_KEYS}
    fields["external_id"] = external_id
    return db.insert_address(
        conn,
        full_text=full_text,
        tokens=normalized.tokens,
        street_tokens=normalized.street_tokens,
        locality_tokens=normalized.locality_tokens,
        house=normalized.house,
        fields=fields,
        raw=norm_row,
    )


def add_many(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]],
             skip_duplicates: bool = True) -> dict[str, int]:
    inserted = duplicates = invalid = 0
    with db.transaction(conn):
        for row in rows:
            norm_row = normalize_row(row)
            if not norm_row.get("full_text"):
                invalid += 1
                continue
            result = add_address(conn, row, skip_duplicates=skip_duplicates)
            if result is None:
                duplicates += 1
            else:
                inserted += 1
    return {"inserted": inserted, "duplicates": duplicates, "invalid": invalid}


def parse_csv(content: bytes) -> list[dict[str, str]]:
    # utf-8-sig transparently strips a possible BOM from Excel exports.
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def parse_csv_numbered(content: bytes) -> list[tuple[int, dict[str, str]]]:
    """Parse CSV into ``(line_no, row)`` pairs.

    ``line_no`` is the 1-based line number in the uploaded file (the header is
    line 1, so the first data row is line 2); blank lines are skipped the same
    way :func:`parse_csv` does.
    """
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    numbered: list[tuple[int, dict[str, str]]] = []
    for offset, values in enumerate(rows[1:], start=2):
        # A truly blank line parses as [] or a single whitespace field and is
        # skipped; a multi-column row that is merely all-empty (e.g. ",,,")
        # must be kept so it can be reported back as an invalid data row.
        if not values or (len(values) == 1 and not values[0].strip()):
            continue
        row = {header[i] if i < len(header) else None:
               (values[i] if i < len(values) else None)
               for i in range(max(len(header), len(values)))}
        numbered.append((offset, dict(row)))
    return numbered


def classify_rows(
    conn: sqlite3.Connection,
    numbered_rows: list[tuple[int, dict[str, Any]]],
    skip_duplicates: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Dry-run classification without writing anything.

    Uses the same column aliases (:data:`_FIELD_ALIASES`) and the same
    duplicate rule as :func:`add_address`: an existing row with the same
    ``full_text`` or ``external_id`` is a duplicate, and so are repeated
    keys inside the same uploaded file.
    """
    valid: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []

    seen_texts: set[str] = set()
    seen_external: set[str] = set()
    for line_no, row in numbered_rows:
        norm_row = normalize_row(row)
        full_text = norm_row.get("full_text")
        if not full_text:
            invalid.append({"line": line_no, "reason": "缺少地址内容（所有列均为空或无法识别）"})
            continue
        external_id = norm_row.get("external_id")
        if skip_duplicates and (
            db.address_exists(conn, full_text, external_id)
            or full_text in seen_texts
            or (external_id is not None and external_id in seen_external)
        ):
            duplicates.append({"line": line_no, "full_text": full_text})
        else:
            valid.append({"line": line_no, "full_text": full_text, "row": row})
        seen_texts.add(full_text)
        if external_id is not None:
            seen_external.add(external_id)

    return {"valid": valid, "duplicates": duplicates, "invalid": invalid}
