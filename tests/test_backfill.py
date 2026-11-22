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
from app import packages as packages_mod  # noqa: E402


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


class FlakyVolume:
    """模拟冷档卷：指定包本次读盘抛 EIO，其余正常。"""

    def __init__(self, paths: dict[str, int]) -> None:
        self.paths = paths

    def __call__(self, path: str):
        if path in self.paths:
            raise OSError(5, "Input/output error")
        return open(path, "rb")


@pytest.fixture()
def flaky_volume(monkeypatch):
    volume = FlakyVolume({})
    monkeypatch.setattr(packages_mod, "_open_binary", volume)
    return volume


def statuses(client) -> dict[int, str]:
    return {p["id"]: p["status"] for p in client.get("/packages").json()}


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


# ---------- 读盘 I/O 错误：只影响本次，不记入目录 ----------

def test_io_error_not_catalogued_and_retry_keeps_locked_revision(client, flaky_volume):
    rev2 = register(client, "only_rev2.zip",
                    build_zip([("2026-11-01", 2, 110.0),
                               ("2026-11-02", 2, 120.0)]))
    register(client, "old_rev1.zip",
             build_zip([("2026-11-01", 1, 111.0),
                        ("2026-11-02", 1, 121.0)]))
    flaky_volume.paths[stored_path(client, rev2["id"])] = rev2["id"]

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    # 暂不可读的包按可用计才能补齐 -> 503 请调用方重试
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "retry_required"
    assert detail["reason"] == "recovery_required_for_coverage"
    assert detail["retry_packages"] == [
        {"package_id": rev2["id"], "name": "only_rev2.zip", "reason": "io_error"}
    ]
    # 读盘错误没有写入目录，后续请求仍可核验该包
    assert statuses(client)[rev2["id"]] == "registered"

    flaky_volume.paths.clear()  # 卷恢复
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["revision"] for d in body["days"]] == [2, 2]
    assert {p["name"] for p in body["packages"]} == {"only_rev2.zip"}
    assert body["retry_packages"] == []


def test_io_error_with_equal_cost_alternate_still_delivers(client, flaky_volume):
    # 两个同覆盖、同字节的包（内容相同）；其中一个暂不可读，
    # 恢复后也不会更省 -> 照常交付，不改修订
    rows = [("2026-11-01", 1, 100.0), ("2026-11-02", 1, 101.0)]
    a = register(client, "a.zip", build_zip(rows))
    b = register(client, "b.zip", build_zip(rows))
    assert a["size_bytes"] == b["size_bytes"]
    flaky_volume.paths[stored_path(client, a["id"])] = a["id"]

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["name"] for p in body["packages"]} == {"b.zip"}
    assert body["reselections"] == []
    assert body["retry_packages"] == [
        {"package_id": a["id"], "name": "a.zip", "reason": "io_error"}
    ]
    assert statuses(client)[a["id"]] == "registered"


def test_io_error_plus_missing_503_then_recovery(client, overlapping, flaky_volume):
    cheap_id = overlapping["cheap"]["id"]
    fix_id = overlapping["fix"]["id"]
    os.unlink(stored_path(client, cheap_id))            # 确实缺失 -> 记入目录
    flaky_volume.paths[stored_path(client, fix_id)] = fix_id  # 读盘错误 -> 只影响本次

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "recovery_required_for_coverage"
    assert [p["package_id"] for p in detail["retry_packages"]] == [fix_id]
    assert detail["excluded_packages"] == [
        {"package_id": cheap_id, "name": "cheap_rev1.zip", "reason": "missing"}
    ]
    st = statuses(client)
    assert st[cheap_id] == "missing"
    assert st[fix_id] == "registered"

    flaky_volume.paths.clear()  # 卷恢复
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["name"] for p in body["packages"]} == {
        "week1_rev1.zip", "week2_rev1.zip", "fix_rev2.zip"}
    by_date = {d["date"]: d for d in body["days"]}
    assert by_date["2026-11-06"]["revision"] == 2  # 锁定修订未回落到 rev1
    assert by_date["2026-11-06"]["feed_kg"] == 215.5


def test_io_error_on_cheaper_package_retries_for_bytes(client, overlapping, flaky_volume):
    cheap_id = overlapping["cheap"]["id"]
    flaky_volume.paths[stored_path(client, cheap_id)] = cheap_id

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "recovery_reduces_bytes"
    assert detail["recovered_total_bytes"] < detail["delivered_total_bytes"]
    assert [p["package_id"] for p in detail["retry_packages"]] == [cheap_id]
    assert statuses(client)[cheap_id] == "registered"

    flaky_volume.paths.clear()
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["name"] for p in body["packages"]} == {
        "cheap_rev1.zip", "week2_rev1.zip", "fix_rev2.zip"}
    assert body["total_bytes"] == sum(p["size_bytes"] for p in body["packages"])


def test_checksum_still_catalogued_alongside_io_error(client):
    # 只有 rev1 的包（可读）+ 唯一持 rev2 的包（SHA-256 不符）；11-06/07 的
    # 最高修订只存在于损坏包中 -> 409；随后卷恢复也补不齐
    rev1 = register(client, "rev1.zip",
                    build_zip([("2026-11-01", 1, 100.0), ("2026-11-02", 1, 101.0)]))
    rev2 = register(client, "rev2.zip",
                    build_zip([("2026-11-01", 2, 110.0), ("2026-11-02", 2, 120.0)]))
    with open(stored_path(client, rev2["id"]), "wb") as f:
        f.write(b"tampered-bytes")                      # SHA-256 不符 -> 记入目录

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "no_full_coverage"
    assert detail["uncoverable_days"] == ["2026-11-01", "2026-11-02"]
    assert detail["excluded_packages"] == [
        {"package_id": rev2["id"], "name": "rev2.zip", "reason": "checksum_mismatch"}
    ]
    assert detail["unreadable_packages"] == []
    st = statuses(client)
    assert st[rev2["id"]] == "corrupt"
    assert st[rev1["id"]] == "registered"

    # 后续请求按目录重新确定修订（损坏包已按原规则出目录）：只剩 rev1，
    # 因此正常交付 rev1，而不会把 rev2 记成"缺失"后又在同一请求内回落。
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 200, r.text
    assert [d["revision"] for d in r.json()["days"]] == [1, 1]


def test_unmounted_volume_is_io_error_not_missing(client):
    """整个卷掉挂载：包目录消失、open 抛 FileNotFoundError -> 读盘错误，不落库。"""
    rev2 = register(client, "only_rev2.zip",
                    build_zip([("2026-11-01", 2, 110.0),
                               ("2026-11-02", 2, 120.0)]))
    register(client, "old_rev1.zip",
             build_zip([("2026-11-01", 1, 111.0),
                        ("2026-11-02", 1, 121.0)]))
    pkg_dir = os.path.join(client.data_dir, "packages")
    backup = os.path.join(client.data_dir, "packages_offline")
    os.rename(pkg_dir, backup)  # 模拟挂载点整体消失

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["excluded_packages"] == []
    assert statuses(client)[rev2["id"]] == "registered"

    os.rename(backup, pkg_dir)  # 卷恢复
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-02"})
    assert r.status_code == 200, r.text
    assert [d["revision"] for d in r.json()["days"]] == [2, 2]


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


def test_volume_recovery_with_missing_and_io_error(client, overlapping, flaky_volume):
    """卷恢复 + 坏包（确实缺失）+ 读盘错误同时出现。"""
    cheap_id = overlapping["cheap"]["id"]
    fix_id = overlapping["fix"]["id"]
    os.unlink(stored_path(client, cheap_id))                   # 确实缺失 -> 落库
    flaky_volume.paths[stored_path(client, fix_id)] = fix_id   # 读盘错误 -> 不落库

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    # cheap 缺失、fix 暂不可读；缺 fix 则 11-06/07 的 rev2 无法补齐 -> 503
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "recovery_required_for_coverage"
    assert [p["package_id"] for p in detail["retry_packages"]] == [fix_id]
    assert detail["excluded_packages"] == [
        {"package_id": cheap_id, "name": "cheap_rev1.zip", "reason": "missing"}
    ]
    st = statuses(client)
    assert st[cheap_id] == "missing"
    assert st[fix_id] == "registered"

    flaky_volume.paths.clear()  # 卷恢复
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["name"] for p in body["packages"]} == {
        "week1_rev1.zip", "week2_rev1.zip", "fix_rev2.zip"}
    by_date = {d["date"]: d for d in body["days"]}
    assert by_date["2026-11-06"]["revision"] == 2      # 锁定修订未回落
    assert by_date["2026-11-06"]["feed_kg"] == 215.5
    assert body["reselections"] == []
    assert body["retry_packages"] == []


def test_corrupt_plus_io_error_409_then_recovery(client, overlapping, flaky_volume):
    """坏包（SHA-256 不符）+ 读盘错误；恢复后坏包仍不可用。"""
    fix_id = overlapping["fix"]["id"]
    cheap_id = overlapping["cheap"]["id"]
    with open(stored_path(client, fix_id), "wb") as f:
        f.write(b"tampered-bytes")                             # 落库 corrupt
    flaky_volume.paths[stored_path(client, cheap_id)] = cheap_id  # 不落库

    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["uncoverable_days"] == ["2026-11-06", "2026-11-07"]
    assert detail["excluded_packages"] == [
        {"package_id": fix_id, "name": "fix_rev2.zip", "reason": "checksum_mismatch"}
    ]
    assert detail["unreadable_packages"] == [
        {"package_id": cheap_id, "name": "cheap_rev1.zip", "reason": "io_error"}
    ]
    st = statuses(client)
    assert st[fix_id] == "corrupt"
    assert st[cheap_id] == "registered"

    flaky_volume.paths.clear()  # 卷恢复：cheap 可读，但 fix 确实损坏不复活
    r = client.post("/backfill", json={"start_date": "2026-11-01",
                                       "end_date": "2026-11-11"})
    # 损坏包已按原规则出目录，目录里只剩 rev1 -> 后续请求正常交付 rev1
    assert r.status_code == 200, r.text
    assert {d["revision"] for d in r.json()["days"]} == {1}
    assert statuses(client)[fix_id] == "corrupt"


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
