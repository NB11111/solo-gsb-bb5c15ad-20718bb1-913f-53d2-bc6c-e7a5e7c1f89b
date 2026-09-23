"""Tests for normalisation, scoring and the FastAPI end-to-end flow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402
from app.matcher import (  # noqa: E402
    levenshtein_ratio,
    match_address,
    score_house,
)
from app.normalization import normalize  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------- normalisation


def test_unicode_and_case_unification():
    n = normalize("１２３ ＭＡＩＮ ＳＴＲＥＥＴ")
    # house number is split out of the comparable text
    assert n.house == "123"
    assert "123" not in n.text
    assert "main" in n.tokens
    # street -> st
    assert "st" in n.tokens


def test_abbreviations_and_diacritics():
    n = normalize("Müller Straße 45")
    assert "muller" in n.tokens
    assert n.house == "45"


def test_house_number_formats():
    assert normalize("12 Main St").house == "12"
    assert normalize("#12-B Main St").house == "12-b"
    assert normalize("No. 12b Main Road").house == "12b"
    assert normalize("北京东路88号").house == "88"
    # 5-digit trailing value is a postcode, not a house number
    assert normalize("Main St, Austin TX 73301").house is None
    # building refs don't grab the house number
    assert normalize("99 SW 26th Ave Unit 7B, Portland").house == "99"


def test_cjk_traditional_to_simplified_and_fullwidth():
    n = normalize("北京東路８８號 3楼")
    assert n.house == "88"
    assert "东" in n.tokens          # 東 -> 东
    assert "北京" in n.tokens        # bigram


# ------------------------------------------------------------------ scoring


def test_levenshtein_ratio():
    assert levenshtein_ratio("main", "main") == 1.0
    assert levenshtein_ratio("", "x") == 0.0
    assert levenshtein_ratio("main", "mn") < 0.7


def test_house_scoring():
    assert score_house("12", "12") == 1.0
    assert score_house("12b", "12") == 0.7
    assert score_house(None, None) == 0.5
    assert score_house("12", None) == 0.15
    assert score_house("100", "1000") < 0.5
    assert score_house("12", "99") == 0.0


# ------------------------------------------------------------- end-to-end API


def test_health_and_sample_loaded(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["address_count"] >= 15


def test_web_page_and_static(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "地址匹配" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_match_abbreviation_auto(client):
    r = client.post("/api/match", json={"query": "123 main st., New York, NY 10001"})
    data = r.json()
    assert data["status"] == "auto_matched"
    assert data["best_id"] == 1
    assert data["best_score"] >= 0.8


def test_match_house_conflict_needs_review(client):
    r = client.post("/api/match", json={"query": "1010 Lake Dr, Austin TX"})
    data = r.json()
    # street tokens match fully but the closest house number differs -> review
    best = data["candidates"][0]
    assert best["token_f1"] >= 0.99
    assert data["status"] == "needs_review"
    conflict_fields = {c["field"] for c in best["conflicts"]}
    assert "house_number" in conflict_fields


def test_match_with_unit_suffix(client):
    r = client.post("/api/match",
                    json={"query": "99 SW 26th Ave Unit 7B, Portland OR"})
    data = r.json()
    assert data["normalized"]["house"] == "99"
    assert data["best_id"] == 14
    assert data["status"] == "auto_matched"


def test_match_terrace_abbreviation(client):
    r = client.post("/api/match", json={"query": "742 Evergreen Tce"})
    data = r.json()
    assert data["best_id"] == 15
    assert data["status"] == "auto_matched"


def test_match_chinese(client):
    r = client.post("/api/match", json={"query": "北京東路８８號 3楼, 上海"})
    data = r.json()
    assert data["normalized"]["house"] == "88"
    assert data["best_id"] == 10
    assert data["status"] == "auto_matched"


def test_match_no_match(client):
    r = client.post("/api/match", json={"query": "1 Milky Way, Mars Base"})
    assert r.json()["status"] == "no_match"


def test_batch_and_review_flow(client):
    r = client.post("/api/match/batch", json={
        "queries": ["Hillcrest Road, Dallas", "123 main st ny"],
    })
    data = r.json()
    assert data["summary"]["total"] == 2
    # ambiguous query (50 vs 52 Hillcrest, no house number) needs review
    ambiguous = data["results"][0]
    assert ambiguous["status"] == "needs_review"
    log_id = ambiguous["match_id"]

    # review queue lists it
    r = client.get("/api/matches", params={"status": "needs_review"})
    ids = [item["id"] for item in r.json()["items"]]
    assert log_id in ids

    # confirm the system's preferred candidate
    r = client.post(f"/api/matches/{log_id}/review", json={"action": "confirmed"})
    assert r.status_code == 200
    r = client.get(f"/api/matches/{log_id}")
    assert r.json()["status"] == "reviewed"
    assert r.json()["review_action"] == "confirmed"


def test_override_review(client):
    r = client.post("/api/match", json={"query": "Hillcrest Rd, Dallas TX"})
    log_id = r.json()["match_id"]
    candidates = r.json()["candidates"]
    second = candidates[1]["address"]["id"]
    r = client.post(f"/api/matches/{log_id}/review",
                    json={"action": "overridden", "selected_id": second})
    assert r.status_code == 200
    detail = client.get(f"/api/matches/{log_id}").json()
    assert detail["review_action"] == "overridden"
    assert detail["selected_id"] == second


def test_csv_upload_and_dedup(client):
    csv_bytes = b"id,full_address,town,zip\nX1,9 Test Lane,Testville,99999\n"
    r = client.post("/api/addresses/upload",
                    files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    r2 = client.post("/api/addresses/upload",
                     files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 1

    r = client.post("/api/match", json={"query": "9 Test Ln, Testville 99999"})
    assert r.json()["best_id"] is not None


# ------------------------------------------------- CSV preview / confirm flow


def _unique_csv(marker: str, *, with_invalid: bool = False,
                with_duplicate: bool = False) -> bytes:
    rows = [
        f"id,full_address,town,zip",
        f"PV{marker}A,{marker} Preview Alpha Street,PV City {marker},91001",
        f"PV{marker}B,{marker} Preview Beta Avenue,PV Town {marker},91002",
    ]
    if with_duplicate:
        rows.append(rows[1])  # exact repeat of the first data row
    if with_invalid:
        rows.append(",,,")
    return ("\n".join(rows) + "\n").encode("utf-8")


def test_preview_does_not_write_and_reports_counts(client):
    before = client.get("/api/health").json()["address_count"]
    csv_bytes = _unique_csv("X1", with_invalid=True)
    r = client.post("/api/addresses/upload/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid_count"] == 2
    assert data["invalid_count"] == 1
    # header is line 1, so data rows start at 2; invalid row is line 4
    assert data["invalid_rows"][0]["line"] == 4
    assert data["token"]
    # preview alone must not change the table
    assert client.get("/api/health").json()["address_count"] == before


def test_preview_then_confirm_writes_only_valid_rows(client):
    csv_bytes = _unique_csv("X2", with_invalid=True, with_duplicate=True)
    before = client.get("/api/health").json()["address_count"]

    preview = client.post("/api/addresses/upload/preview",
                          files={"file": ("p.csv", csv_bytes, "text/csv")}).json()
    assert preview["valid_count"] == 2
    assert preview["duplicate_count"] == 1
    assert preview["invalid_count"] == 1
    assert preview["duplicate_lines"] == [4]

    r = client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"token": preview["token"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 2
    assert body["duplicates"] == 1
    assert body["invalid"] == 1

    after = client.get("/api/health").json()["address_count"]
    assert after == before + 2

    # token is single-use
    r2 = client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"token": preview["token"]},
    )
    assert r2.status_code == 400
    assert client.get("/api/health").json()["address_count"] == after


def test_confirm_rejects_changed_file(client):
    csv_bytes = _unique_csv("X3")
    preview = client.post("/api/addresses/upload/preview",
                          files={"file": ("p.csv", csv_bytes, "text/csv")}).json()
    before = client.get("/api/health").json()["address_count"]

    tampered = csv_bytes + b"PVX3Z,99 Tampered Lane,Rigged,91099\n"
    r = client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", tampered, "text/csv")},
        data={"token": preview["token"]},
    )
    assert r.status_code == 409
    assert "不一致" in r.json()["detail"]
    assert client.get("/api/health").json()["address_count"] == before


def test_confirm_rejects_stale_table_snapshot(client):
    csv_bytes = _unique_csv("X4")
    preview = client.post("/api/addresses/upload/preview",
                          files={"file": ("p.csv", csv_bytes, "text/csv")}).json()
    before = client.get("/api/health").json()["address_count"]

    # Another write lands between preview and confirm -> snapshot diverges.
    other = b"id,full_address\nPVOTHER4,42 Racer Lane, Speedtown 92003\n"
    assert client.post("/api/addresses/upload",
                       files={"file": ("o.csv", other, "text/csv")}).status_code == 200

    r = client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"token": preview["token"]},
    )
    assert r.status_code == 409
    assert "标准地址表" in r.json()["detail"]
    after = client.get("/api/health").json()["address_count"]
    assert after == before + 1  # only the interleaved upload landed


def test_confirm_requires_known_token(client):
    csv_bytes = _unique_csv("X5")
    r = client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"token": "not-a-real-token"},
    )
    assert r.status_code == 400


def test_preview_uses_column_aliases_and_skip_rules(client):
    csv_bytes = (
        b"addr,town,zipcode\n"
        b"7 Preview Sunset Boulevard,Sunset City,93001\n"
    )
    r = client.post("/api/addresses/upload/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid_count"] == 1

    # Re-previewing the same content after it has been written marks it duplicate.
    token = data["token"]
    assert client.post(
        "/api/addresses/upload/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"token": token},
    ).status_code == 200

    again = client.post("/api/addresses/upload/preview",
                        files={"file": ("p.csv", csv_bytes, "text/csv")}).json()
    assert again["valid_count"] == 0
    assert again["duplicate_count"] == 1
    assert again["duplicate_lines"] == [2]


def test_direct_upload_endpoint_is_kept(client):
    csv_bytes = b"id,full_address\nPVDIRECT,8 Direct Upload Way, Nowhere 94001\n"
    r = client.post("/api/addresses/upload",
                    files={"file": ("d.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    # idempotent second upload -> all duplicates
    r2 = client.post("/api/addresses/upload",
                     files={"file": ("d.csv", csv_bytes, "text/csv")})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 1
