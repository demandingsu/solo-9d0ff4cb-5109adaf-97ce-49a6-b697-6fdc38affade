"""端到端测试：交叠覆盖、最高修订、最少字节选包、坏包重选、整次退回。"""
from __future__ import annotations

import csv
import io
import os
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import create_app  # noqa: E402


def build_zip(rows, pad_bytes: int = 0, name: str = "intake.csv") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "revision", "feed_kg"])
    for d, rev, kg in rows:
        w.writerow([d, rev, kg])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, buf.getvalue())
        if pad_bytes > 0:
            zf.comment = b" " * pad_bytes
    return out.getvalue()


def days(start, n, revision, base):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), revision, base + i)
            for i in range(n)]


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    with TestClient(app) as c:
        c.data_dir = str(tmp_path / "data")
        yield c


def register(client, filename: str, content: bytes) -> dict:
    r = client.post("/packages", files={"file": (filename, content, "application/zip")})
    assert r.status_code == 201, r.text
    return r.json()


def stored_path(client, pkg_id: int) -> str:
    return os.path.join(client.data_dir, "packages", f"pkg_{pkg_id:06d}.zip")


@pytest.fixture()
def overlapping(client):
    """登记交叠覆盖的四个包，返回 {名字: 登记响应}。

    week1: 11-01..11-07 rev1；week2: 11-05..11-11 rev1（交叠日取值一致）；
    fix: 11-06/11-07 rev2（更高修订）；cheap: 11-01..11-04 rev1（字节最少）。
    """
    week2_rows = [("2026-11-05", 1, 104.0), ("2026-11-06", 1, 105.0),
                  ("2026-11-07", 1, 106.0),  # 与 week1 同修订同值
                  ("2026-11-08", 1, 207.0), ("2026-11-09", 1, 208.0),
                  ("2026-11-10", 1, 209.0), ("2026-11-11", 1, 210.0)]
    pkgs = {}
    pkgs["week1"] = register(client, "week1_rev1.zip",
                             build_zip(days("2026-11-01", 7, 1, 100.0), pad_bytes=1500))
    pkgs["week2"] = register(client, "week2_rev1.zip",
                             build_zip(week2_rows, pad_bytes=1500))
    pkgs["fix"] = register(client, "fix_rev2.zip",
                           build_zip([("2026-11-06", 2, 215.5),
                                      ("2026-11-07", 2, 217.0)]))
    pkgs["cheap"] = register(client, "cheap_rev1.zip",
                             build_zip(days("2026-11-01", 4, 1, 100.0)))
    return pkgs


# ---------- 交叠覆盖 + 最高修订 + 最少字节 ----------

def test_overlapping_min_bytes(client, overlapping):
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()

    chosen = {p["name"] for p in body["packages"]}
    # week1(大) 与 cheap(小) 都覆盖 01-04，最少字节应选 cheap+week2+fix
    assert chosen == {"cheap_rev1.zip", "week2_rev1.zip", "fix_rev2.zip"}
    assert body["total_bytes"] == sum(p["size_bytes"] for p in body["packages"])

    by_date = {d["date"]: d for d in body["days"]}
    assert len(by_date) == 11
    # 交叠区按日取最高修订：06、07 取 fix 的修订 2
    assert by_date["2026-11-06"]["revision"] == 2
    assert by_date["2026-11-06"]["feed_kg"] == 215.5
    assert by_date["2026-11-06"]["source_package_name"] == "fix_rev2.zip"
    assert by_date["2026-11-07"]["revision"] == 2
    # 其余天取修订 1，并注明来源包
    assert by_date["2026-11-01"]["revision"] == 1
    assert by_date["2026-11-01"]["source_package_name"] == "cheap_rev1.zip"
    assert by_date["2026-11-05"]["source_package_name"] == "week2_rev1.zip"
    assert by_date["2026-11-05"]["feed_kg"] == 104.0  # 交叠日两包同值，不冲突
    assert by_date["2026-11-11"]["source_package_name"] == "week2_rev1.zip"
    assert body["reselections"] == []


# ---------- 坏包剔除重选 ----------

def test_checksum_failure_reselect(client, overlapping):
    cheap_id = overlapping["cheap"]["id"]
    with open(stored_path(client, cheap_id), "wb") as f:
        f.write(b"tampered-bytes")  # 内容被篡改 -> SHA-256 不符

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["reselections"] == [
        {"package_id": cheap_id, "name": "cheap_rev1.zip",
         "reason": "checksum_mismatch"}
    ]
    chosen = {p["name"] for p in body["packages"]}
    assert chosen == {"week1_rev1.zip", "week2_rev1.zip", "fix_rev2.zip"}
    by_date = {d["date"]: d for d in body["days"]}
    # 01-04 改由 week1 供数，修订不变（不重定修订）
    assert by_date["2026-11-01"]["source_package_name"] == "week1_rev1.zip"
    assert by_date["2026-11-01"]["revision"] == 1
    # 坏包状态已登记，后续请求不再考虑
    pkgs = {p["id"]: p for p in client.get("/packages").json()}
    assert pkgs[cheap_id]["status"] == "corrupt"


def test_missing_file_reselect(client, overlapping):
    cheap_id = overlapping["cheap"]["id"]
    os.unlink(stored_path(client, cheap_id))

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reselections"][0]["reason"] == "missing"
    assert "cheap_rev1.zip" not in {p["name"] for p in body["packages"]}
    pkgs = {p["id"]: p for p in client.get("/packages").json()}
    assert pkgs[cheap_id]["status"] == "missing"


# ---------- 无全覆盖 -> 整次退回，不用旧修订补齐 ----------

def test_no_coverage_rejects_without_downgrade(client):
    only = register(client, "only_rev2.zip",
                    build_zip([("2026-11-01", 2, 110.0),
                               ("2026-11-02", 2, 120.0)]))
    register(client, "old_rev1.zip",
             build_zip([("2026-11-01", 1, 111.0),
                        ("2026-11-02", 1, 121.0)]))
    os.unlink(stored_path(client, only["id"]))  # 唯一持修订 2 的包丢失

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "no_full_coverage"
    assert detail["uncoverable_days"] == ["2026-11-01", "2026-11-02"]
    assert detail["excluded_packages"][0]["reason"] == "missing"
    # 没有拿修订 1 的旧值补齐：整个请求被退回，无 days 结果


# ---------- 缺日 / 异值 / 区间限制 ----------

def test_missing_day_422(client, overlapping):
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-14"})
    assert r.status_code == 422
    assert r.json()["detail"]["missing_days"] == ["2026-11-12", "2026-11-13",
                                                  "2026-11-14"]


def test_conflict_on_max_revision(client):
    register(client, "a.zip", build_zip([("2026-11-03", 1, 100.0)]))
    register(client, "b.zip", build_zip([("2026-11-03", 1, 200.0)]))
    r = client.post("/backfill", json={"start_date": "2026-11-03",
                                       "end_date": "2026-11-03"})
    assert r.status_code == 422
    conflicts = r.json()["detail"]["conflicts"]
    assert conflicts == [{"date": "2026-11-03", "revision": 1,
                          "values": [100.0, 200.0]}]


def test_lower_revision_conflict_is_ignored(client):
    # 低修订异值不影响：只校验最高修订
    register(client, "a.zip", build_zip([("2026-11-03", 1, 100.0),
                                         ("2026-11-03", 2, 300.0)]))
    register(client, "b.zip", build_zip([("2026-11-03", 1, 999.0),
                                         ("2026-11-03", 2, 300.0)]))
    r = client.post("/backfill", json={"start_date": "2026-11-03",
                                       "end_date": "2026-11-03"})
    assert r.status_code == 200, r.text
    assert r.json()["days"][0]["revision"] == 2


def test_range_over_14_days(client):
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-15"})
    assert r.status_code == 422
    assert "最多" in r.json()["detail"]


def test_end_before_start(client):
    r = client.post("/backfill", json={"start_date": "2026-11-05",
                                       "end_date": "2026-11-01"})
    assert r.status_code == 422


# ---------- 登记校验 ----------

def test_register_rejects_bad_zip(client):
    r = client.post("/packages", files={"file": ("x.zip", b"not-a-zip",
                                                 "application/zip")})
    assert r.status_code == 400


def test_register_rejects_two_csvs(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("a.csv", "date,revision,feed_kg\n2026-11-01,1,1\n")
        zf.writestr("b.csv", "date,revision,feed_kg\n2026-11-01,1,1\n")
    r = client.post("/packages", files={"file": ("x.zip", out.getvalue(),
                                                 "application/zip")})
    assert r.status_code == 400


def test_register_rejects_bad_header(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("a.csv", "day,rev,kg\n2026-11-01,1,1\n")
    r = client.post("/packages", files={"file": ("x.zip", out.getvalue(),
                                                 "application/zip")})
    assert r.status_code == 400


def test_register_rejects_inner_conflict(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("a.csv", "date,revision,feed_kg\n"
                             "2026-11-01,1,100\n2026-11-01,1,200\n")
    r = client.post("/packages", files={"file": ("x.zip", out.getvalue(),
                                                 "application/zip")})
    assert r.status_code == 400


def test_register_dedupes_identical_rows(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("a.csv", "date,revision,feed_kg\n"
                             "2026-11-01,1,100\n2026-11-01,1,100\n")
    r = client.post("/packages", files={"file": ("x.zip", out.getvalue(),
                                                 "application/zip")})
    assert r.status_code == 201
    assert r.json()["rows_registered"] == 1
