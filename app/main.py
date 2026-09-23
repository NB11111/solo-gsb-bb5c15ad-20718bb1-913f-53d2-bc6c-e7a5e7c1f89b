"""FastAPI application: web UI + REST API for standard-address matching."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, ingest
from .config import Settings, get_settings
from .matcher import candidate_to_dict, match_address, result_to_dict
from .schemas import AddressBatchIn, BatchMatchRequest, MatchRequest, ReviewAction

BASE_DIR = Path(__file__).resolve().parent

# A single connection is fine for this local, low-concurrency tool; SQLite
# in WAL mode serialises writes internally.
_conn: db.sqlite3.Connection | None = None

# --- CSV preview sessions ---------------------------------------------------
#
# A preview never writes addresses; it only hands out an opaque token that
# pins the confirmed upload to (a) the exact file bytes (SHA-256) and
# (b) a content snapshot of the address table taken at preview time. The
# client re-submits the same file together with the token; inside one
# ``BEGIN IMMEDIATE`` transaction both bindings are re-checked before the
# previewed valid rows are inserted.

PREVIEW_TTL_SECONDS = 30 * 60
_previews: dict[str, dict[str, Any]] = {}
_previews_lock = threading.Lock()


class PreviewExpired(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=400,
            detail="预览凭证无效或已过期，请重新预览后再确认写入",
        )


def _purge_expired_previews(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [token for token, item in _previews.items()
               if item["expires_at"] <= now]
    for token in expired:
        _previews.pop(token, None)


def get_conn() -> db.sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = db.connect()
        db.init_db(_conn)
    return _conn


def settings_dep() -> Settings:
    return get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Auto-load the bundled sample data on first run so the UI works
    # immediately with the documented one-line Docker command.
    conn = get_conn()
    if db.count_addresses(conn) == 0:
        sample = Path(get_settings().sample_csv)
        if sample.exists():
            ingest.add_many(conn, ingest.parse_csv(sample.read_bytes()))
    yield
    if _conn is not None:
        _conn.close()


app = FastAPI(
    title="Address Matcher",
    description="把外部地址文本匹配到标准地址表（FastAPI + SQLite）",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# -------------------------------------------------------------------- pages --


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# --------------------------------------------------------------------- api --


@app.get("/api/health")
def health(conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {
        "status": "ok",
        "address_count": db.count_addresses(conn),
        "needs_review": db.count_match_logs(conn, "needs_review"),
    }


# --- standard address table -------------------------------------------------


@app.get("/api/addresses")
def list_addresses(limit: int = 100, offset: int = 0,
                   conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    rows = db.list_addresses(conn, limit=limit, offset=offset)
    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "full_text": row["full_text"],
            "house": row["house"],
            "line1": row["line1"],
            "city": row["city"],
            "state": row["state"],
            "postal_code": row["postal_code"],
            "country": row["country"],
            "external_id": row["external_id"],
        })
    return {"total": db.count_addresses(conn), "items": items}


@app.post("/api/addresses")
def create_address(payload: AddressBatchIn,
                   conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = []
    for item in payload.addresses:
        raw = item.lookup_fields()
        raw["full_text"] = item.full_text or item.address
        rows.append(raw)
    summary = ingest.add_many(conn, rows, skip_duplicates=payload.skip_duplicates)
    return summary


@app.post("/api/addresses/upload")
async def upload_csv(file: UploadFile = File(...),
                     conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")
    content = await file.read()
    try:
        rows = ingest.parse_csv(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"CSV 解析失败: {exc}") from exc
    summary = ingest.add_many(conn, rows)
    summary["filename"] = file.filename
    return summary


async def _read_csv_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")
    content = await file.read()
    try:
        ingest.parse_csv_numbered(content)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"CSV 解析失败: {exc}") from exc
    return content


@app.post("/api/addresses/upload/preview")
async def preview_csv_upload(
    file: UploadFile = File(...),
    skip_duplicates: bool = True,
    conn: db.sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Validate/classify a CSV without writing anything.

    Returns counts of valid/duplicate/invalid rows (with invalid line
    numbers) plus an opaque ``token`` that must be presented together with
    the *same* file to :func:`confirm_csv_upload`.
    """
    content = await _read_csv_upload(file)
    numbered = ingest.parse_csv_numbered(content)
    classified = ingest.classify_rows(conn, numbered,
                                      skip_duplicates=skip_duplicates)
    snapshot = db.address_snapshot(conn)
    token = secrets.token_urlsafe(32)
    with _previews_lock:
        _purge_expired_previews()
        _previews[token] = {
            "filename": file.filename,
            "digest": hashlib.sha256(content).hexdigest(),
            "snapshot": snapshot,
            "skip_duplicates": skip_duplicates,
            "valid_count": len(classified["valid"]),
            "expires_at": time.monotonic() + PREVIEW_TTL_SECONDS,
        }
    return {
        "token": token,
        "filename": file.filename,
        "digest": _previews[token]["digest"],
        "skip_duplicates": skip_duplicates,
        "total": len(numbered),
        "valid_count": len(classified["valid"]),
        "duplicate_count": len(classified["duplicates"]),
        "invalid_count": len(classified["invalid"]),
        "duplicate_lines": [item["line"] for item in classified["duplicates"]],
        "invalid_rows": classified["invalid"],
        "valid_lines": [item["line"] for item in classified["valid"]],
        "expires_in": PREVIEW_TTL_SECONDS,
    }


@app.post("/api/addresses/upload/confirm")
async def confirm_csv_upload(
    file: UploadFile = File(...),
    token: str = Form(...),
    skip_duplicates: bool | None = None,
    conn: db.sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Persist exactly the rows that were valid in a previous preview.

    The client must re-submit the same file together with the preview
    ``token``. Within one SQLite write transaction we verify that

    1. the file digest matches the one bound to the token, and
    2. the address-table snapshot matches the one captured at preview time.

    On either mismatch (or a reused/expired token) nothing is written and the
    client has to preview the file again.
    """
    content = await _read_csv_upload(file)
    digest = hashlib.sha256(content).hexdigest()

    with _previews_lock:
        _purge_expired_previews()
        session = _previews.pop(token, None)

    if session is None:
        raise PreviewExpired()

    skip = session["skip_duplicates"] if skip_duplicates is None else skip_duplicates
    if skip != session["skip_duplicates"]:
        raise HTTPException(
            status_code=400,
            detail="跳过重复选项与预览时不一致，请重新预览后再确认写入",
        )

    # Serialise confirmations against each other while the shared connection
    # runs a BEGIN IMMEDIATE transaction.
    with _previews_lock:
        try:
            with db.immediate_transaction(conn):
                current_snapshot = db.address_snapshot(conn)
                if digest != session["digest"]:
                    raise HTTPException(
                        status_code=409,
                        detail="文件内容与预览时不一致，已拒绝写入，请重新预览",
                    )
                if current_snapshot != session["snapshot"]:
                    raise HTTPException(
                        status_code=409,
                        detail="标准地址表在预览后已发生变化，已拒绝写入，请重新预览",
                    )

                numbered = ingest.parse_csv_numbered(content)
                classified = ingest.classify_rows(
                    conn, numbered, skip_duplicates=session["skip_duplicates"]
                )
                # Defence in depth: only rows that were valid in the preview
                # may ever be inserted, and reclassification must agree.
                if len(classified["valid"]) != session["valid_count"]:
                    raise HTTPException(
                        status_code=409,
                        detail="预览结果与当前校验结果不一致，已拒绝写入，请重新预览",
                    )

                inserted = 0
                for item in classified["valid"]:
                    result = ingest.add_address(
                        conn, item["row"],
                        skip_duplicates=session["skip_duplicates"],
                    )
                    if result is None:
                        # The snapshot matched and classification just ran in
                        # this same transaction, so this cannot happen; fail
                        # closed instead of silently dropping data.
                        raise HTTPException(
                            status_code=409,
                            detail="写入时发现记录状态与预览不一致，已拒绝写入，请重新预览",
                        )
                    inserted += 1
                if inserted != session["valid_count"]:
                    raise HTTPException(
                        status_code=409,
                        detail="写入条数与预览不一致，已拒绝写入，请重新预览",
                    )
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail=f"写入失败: {exc}") from exc

    return {
        "ok": True,
        "filename": session["filename"],
        "inserted": inserted,
        "duplicates": len(classified["duplicates"]),
        "invalid": len(classified["invalid"]),
        "invalid_rows": classified["invalid"],
    }


@app.post("/api/addresses/load-sample")
def load_sample(conn: db.sqlite3.Connection = Depends(get_conn),
                settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    sample = Path(settings.sample_csv)
    if not sample.exists():
        raise HTTPException(status_code=404, detail="未找到示例数据文件")
    rows = ingest.parse_csv(sample.read_bytes())
    summary = ingest.add_many(conn, rows)
    return summary


# --- matching ---------------------------------------------------------------


def _persist_match(conn: db.sqlite3.Connection, result: Any) -> int:
    best = result.best
    return db.insert_match_log(
        conn,
        query=result.query,
        status=result.status,
        score=best.score if best else None,
        candidate_id=best.address["id"] if best else None,
        token_score=best.token_score.f1 if best else None,
        house_score=best.house_score if best else None,
        matched_tokens=(
            best.token_score.street.matched + best.token_score.locality.matched
            if best else []
        ),
        conflicts=best.conflicts if best else [],
        candidates=[candidate_to_dict(c) for c in result.candidates],
    )


@app.post("/api/match")
def api_match(payload: MatchRequest,
              conn: db.sqlite3.Connection = Depends(get_conn),
              settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    if db.count_addresses(conn) == 0:
        raise HTTPException(status_code=400, detail="标准地址表为空，请先上传或加载示例数据")
    result = match_address(payload.query, db.all_addresses(conn), settings)
    data = result_to_dict(result)
    if payload.save:
        with db.transaction(conn):
            data["match_id"] = _persist_match(conn, result)
    return data


@app.post("/api/match/batch")
def api_match_batch(payload: BatchMatchRequest,
                    conn: db.sqlite3.Connection = Depends(get_conn),
                    settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    if db.count_addresses(conn) == 0:
        raise HTTPException(status_code=400, detail="标准地址表为空，请先上传或加载示例数据")
    rows = db.all_addresses(conn)
    items = []
    with db.transaction(conn):
        for query in payload.queries:
            result = match_address(query, rows, settings)
            data = result_to_dict(result)
            if payload.save:
                data["match_id"] = _persist_match(conn, result)
            items.append(data)
    summary = {
        "total": len(items),
        "auto_matched": sum(1 for i in items if i["status"] == "auto_matched"),
        "needs_review": sum(1 for i in items if i["status"] == "needs_review"),
        "no_match": sum(1 for i in items if i["status"] == "no_match"),
    }
    return {"summary": summary, "results": items}


# --- review queue -----------------------------------------------------------


def _serialize_log(row: db.sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "query": row["query"],
        "status": row["status"],
        "review_action": row["review_action"],
        "score": row["score"],
        "candidate_id": row["candidate_id"],
        "selected_id": row["selected_id"],
        "token_score": row["token_score"],
        "house_score": row["house_score"],
        "matched_tokens": json.loads(row["matched_tokens"] or "[]"),
        "conflicts": json.loads(row["conflicts"] or "[]"),
        "candidates": json.loads(row["candidates_json"]),
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
    }


@app.get("/api/matches")
def list_matches(status: str | None = None, limit: int = 100, offset: int = 0,
                 conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if status and status not in {"auto_matched", "needs_review", "no_match", "reviewed"}:
        raise HTTPException(status_code=400, detail="非法 status 过滤值")
    limit = max(1, min(limit, 500))
    rows = db.list_match_logs(conn, status=status, limit=limit, offset=offset)
    return {
        "total": db.count_match_logs(conn, status),
        "needs_review": db.count_match_logs(conn, "needs_review"),
        "items": [_serialize_log(r) for r in rows],
    }


@app.get("/api/matches/{match_id}")
def get_match(match_id: int,
              conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = db.get_match_log(conn, match_id)
    if row is None:
        raise HTTPException(status_code=404, detail="匹配记录不存在")
    return _serialize_log(row)


@app.post("/api/matches/{match_id}/review")
def review_match(match_id: int, payload: ReviewAction,
                 conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = db.get_match_log(conn, match_id)
    if row is None:
        raise HTTPException(status_code=404, detail="匹配记录不存在")

    score: float | None = None
    if payload.action == "overridden":
        if payload.selected_id is None:
            raise HTTPException(status_code=400, detail="改派时必须提供 selected_id")
        target = db.get_address(conn, payload.selected_id)
        if target is None:
            raise HTTPException(status_code=404, detail="所选标准地址不存在")
        candidates = json.loads(row["candidates_json"])
        for cand in candidates:
            if cand["address"]["id"] == payload.selected_id:
                score = cand["score"]
                break
    elif payload.action == "confirmed":
        payload = ReviewAction(action="confirmed", selected_id=row["candidate_id"])
        score = row["score"]

    with db.transaction(conn):
        db.review_match_log(
            conn,
            match_id,
            action=payload.action,
            selected_id=payload.selected_id,
            score=score,
        )
    return {"ok": True, "match_id": match_id, "action": payload.action,
            "selected_id": payload.selected_id}
