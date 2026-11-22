"""FastAPI 入口：冷档登记 + 日进料补数。

接口：
- POST /packages  上传 ZIP 冷档并登记（包大小、SHA-256、逐日修订与行校验入 SQLite）
- GET  /packages  查看已登记冷档
- POST /backfill  对最多 14 天的区间出补数计划（总字节最少的整包组合）

读盘语义：文件确实缺失 / SHA-256 不符 -> 剔除并写入目录；读盘 I/O 错误只影响
本次请求，不写目录，后续请求仍会重新核验该包。
"""
from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .backfill import UnresolvableDays, coverage_masks, determine_targets, select_min_bytes
from .db import Database
from .packages import PackageError, parse_package, verify_package_file

MAX_DAYS = 14


# ---------- 响应模型 ----------

class PackageOut(BaseModel):
    id: int
    name: str
    size_bytes: int
    sha256: str
    status: str
    created_at: str


class RegisterOut(BaseModel):
    id: int
    name: str
    size_bytes: int
    sha256: str
    rows_registered: int
    date_span: tuple[str, str]


class BackfillRequest(BaseModel):
    start_date: date
    end_date: date


class DayEntry(BaseModel):
    date: str
    revision: int
    feed_kg: float
    source_package_id: int
    source_package_name: str


class Reselection(BaseModel):
    package_id: int
    name: str
    reason: Literal["missing", "checksum_mismatch"]


class SelectedPackage(BaseModel):
    id: int
    name: str
    size_bytes: int
    sha256: str


class RetryPackage(BaseModel):
    package_id: int
    name: str
    reason: Literal["io_error"]


class BackfillOut(BaseModel):
    start_date: str
    end_date: str
    days: list[DayEntry]
    packages: list[SelectedPackage]
    total_bytes: int = Field(description="入选整包字节合计，即本次调取计费量")
    reselections: list[Reselection] = Field(description="本次请求内被剔除并重选的包")
    retry_packages: list[RetryPackage] = Field(
        default_factory=list,
        description="本次读盘失败、未记入目录的包；若它们可读便能改善交付，则整次返回 503",
    )


# ---------- 应用工厂 ----------

def create_app(data_dir: str | None = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("DATA_DIR", "./data")
    pkg_dir = os.path.join(data_dir, "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    db = Database(os.path.join(data_dir, "app.db"))

    app = FastAPI(
        title="焚烧厂日进料补数服务",
        version="1.0.0",
        description=(
            "冷档（ZIP 内一份 CSV：date,revision,feed_kg）登记入 SQLite；"
            "补数按日取最高修订，缺日或最高修订异值报错；"
            "以总字节最少为原则选取整包组合，入选包缺失或校验失败则剔除重选，"
            "无法全覆盖则整次退回，不用旧修订补齐。"
            "读盘 I/O 错误只影响本次请求、不写入目录：若暂不可读的包恢复后能补齐"
            "全部日期或减少最少总字节，则返回 503 请调用方重试。"
        ),
    )
    app.state.db = db
    app.state.pkg_dir = pkg_dir

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/packages", response_model=RegisterOut, status_code=201)
    def register_package(file: UploadFile = File(...)) -> RegisterOut:
        buf = file.file.read()
        if not buf:
            raise HTTPException(400, "上传内容为空")
        try:
            rows = parse_package(buf)
        except PackageError as exc:
            raise HTTPException(400, f"冷档不合法：{exc}") from exc

        sha = hashlib.sha256(buf).hexdigest()
        name = file.filename or "unnamed.zip"
        stored = ""
        try:
            with db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(
                    "INSERT INTO packages(name, stored_path, size_bytes, sha256)"
                    " VALUES (?, '', ?, ?)",
                    (name, len(buf), sha),
                )
                pid = cur.lastrowid
                stored = os.path.join(pkg_dir, f"pkg_{pid:06d}.zip")
                con.execute("UPDATE packages SET stored_path=? WHERE id=?", (stored, pid))
                con.executemany(
                    "INSERT INTO package_days(package_id, date, revision, feed_kg, row_sha256)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [(pid, r.date, r.revision, r.feed_kg, r.row_sha256) for r in rows],
                )
                with open(stored, "wb") as f:  # 失败则整个登记事务回滚
                    f.write(buf)
        except Exception:
            if stored and os.path.exists(stored):
                os.unlink(stored)
            raise

        return RegisterOut(
            id=pid,
            name=name,
            size_bytes=len(buf),
            sha256=sha,
            rows_registered=len(rows),
            date_span=(rows[0].date, rows[-1].date),
        )

    @app.get("/packages", response_model=list[PackageOut])
    def list_packages() -> list[dict]:
        with db.connect() as con:
            rows = con.execute(
                "SELECT id, name, size_bytes, sha256, status, created_at"
                " FROM packages ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/backfill", response_model=BackfillOut)
    def backfill(req: BackfillRequest) -> BackfillOut:
        if req.end_date < req.start_date:
            raise HTTPException(422, "end_date 不能早于 start_date")
        n_days = (req.end_date - req.start_date).days + 1
        if n_days > MAX_DAYS:
            raise HTTPException(422, f"一次补数最多 {MAX_DAYS} 天，实际请求 {n_days} 天")
        days = [(req.start_date + timedelta(days=i)).isoformat() for i in range(n_days)]
        day_index = {d: i for i, d in enumerate(days)}

        # —— 请求开始时的快照：只用此刻已登记且状态正常的包 ——
        with db.connect() as con:
            max_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM packages").fetchone()[0]
            pkgs = con.execute(
                "SELECT id, name, stored_path, size_bytes, sha256 FROM packages"
                " WHERE id <= ? AND status = 'registered' ORDER BY id",
                (max_id,),
            ).fetchall()
            rows = con.execute(
                "SELECT pd.package_id, pd.date, pd.revision, pd.feed_kg"
                " FROM package_days pd JOIN packages p ON p.id = pd.package_id"
                " WHERE p.id <= ? AND p.status = 'registered'"
                "   AND pd.date BETWEEN ? AND ?",
                (max_id, days[0], days[-1]),
            ).fetchall()

        snap = [(r["package_id"], r["date"], r["revision"], r["feed_kg"]) for r in rows]

        # —— 按日取最高修订；缺日或最高修订异值 -> 422 ——
        try:
            targets = determine_targets(days, snap)
        except UnresolvableDays as exc:
            raise HTTPException(
                422,
                detail={
                    "error": "unresolvable_days",
                    "missing_days": exc.missing,
                    "conflicts": exc.conflicts,
                },
            ) from exc

        masks = coverage_masks([p["id"] for p in pkgs], snap, targets, day_index)
        by_id = {p["id"]: p for p in pkgs}

        # —— 选包 -> 核验 -> 剔除重选；目标修订保持不变 ——
        excluded: dict[int, str] = {}      # 确实缺失 / SHA-256 不符，本次及以后都不再考虑
        reselections: list[Reselection] = []
        verdict: dict[int, str] = {}       # 本请求内的核验结果缓存
        io_failed: set[int] = set()        # 读盘 I/O 错误：只影响本次，不落库

        def record_bad(pid: int, reason: str) -> None:
            """确实缺失或内容损坏：剔除并写入目录（后续请求不再考虑）。"""
            excluded[pid] = reason
            verdict[pid] = reason
            reselections.append(
                Reselection(package_id=pid, name=by_id[pid]["name"], reason=reason)
            )
            with db.connect() as con:
                con.execute(
                    "UPDATE packages SET status=? WHERE id=?",
                    ("missing" if reason == "missing" else "corrupt", pid),
                )

        def verify(pid: int) -> str:
            """返回 ok / missing / checksum_mismatch / io_error（本请求内缓存）。"""
            if pid in verdict:
                return verdict[pid]
            ok, reason = verify_package_file(
                by_id[pid]["stored_path"], by_id[pid]["sha256"]
            )
            status = "ok" if ok else (reason or "io_error")
            verdict[pid] = status
            return status

        def plan(assume_recovered: bool) -> tuple[int, tuple[int, ...]] | None:
            """求最省字节组合，并核验其中不由恢复假设覆盖的包。

            assume_recovered=False：交付方案。读盘失败的包本次不可用。
            assume_recovered=True ：恢复评估。本次读盘失败的包按可读计，其余入选包
                                    仍须核验通过；若核验又发现读盘错误，则该包也
                                    并入恢复假设后重算。
            确实缺失 / SHA-256 不符的包在两种方案中都会被剔除并落库。
            """
            while True:
                candidates = [
                    (p["id"], p["size_bytes"])
                    for p in pkgs
                    if p["id"] not in excluded
                    and (assume_recovered or p["id"] not in io_failed)
                ]
                best = select_min_bytes(candidates, masks, n_days)
                if best is None:
                    return None
                total, chosen = best

                changed = False
                for pid in chosen:
                    if assume_recovered and pid in io_failed:
                        continue  # 暂不可读的包按恢复后可读计，无需本次核验
                    status = verify(pid)
                    if status == "ok":
                        continue
                    if status == "io_error":
                        if pid not in io_failed:  # 新发现，需按排除它的方案重算
                            io_failed.add(pid)
                            changed = True
                    else:
                        record_bad(pid, status)
                        changed = True
                if not changed:
                    return total, chosen

        # 恢复评估可能新发现读盘失败的包，进而改变交付方案；迭代到目录稳定。
        while True:
            before = (frozenset(excluded), frozenset(io_failed))
            delivered = plan(assume_recovered=False)
            recovered = plan(assume_recovered=True) if io_failed else delivered
            if (frozenset(excluded), frozenset(io_failed)) == before:
                break

        if recovered is None:
            # 连"暂不可读的包全部恢复"都无法按锁定修订补齐 -> 409
            uncoverable = [
                d for d in days
                if not any(
                    masks.get(p["id"], 0) >> day_index[d] & 1
                    for p in pkgs if p["id"] not in excluded
                )
            ]
            raise HTTPException(
                409,
                detail={
                    "error": "no_full_coverage",
                    "message": "剔除失效包后无任何组合能按已确定的修订全覆盖，整次退回；"
                               "不使用旧修订补齐。",
                    "uncoverable_days": uncoverable,
                    "excluded_packages": [r.model_dump() for r in reselections],
                    "unreadable_packages": [
                        {"package_id": pid, "name": by_id[pid]["name"],
                         "reason": "io_error"}
                        for pid in sorted(io_failed)
                    ],
                },
            )

        if delivered is None or recovered[0] < delivered[0]:
            # 暂不可读的包按可用计：才能补齐，或能减少最少总字节 -> 请调用方重试
            needed = [pid for pid in recovered[1] if pid in io_failed]
            raise HTTPException(
                503,
                detail={
                    "error": "retry_required",
                    "message": "有冷档暂不可读；按恢复后可读评估，重试可补齐全部日期"
                               "或显著减少计费字节，请稍后重试。",
                    "reason": (
                        "recovery_required_for_coverage"
                        if delivered is None else "recovery_reduces_bytes"
                    ),
                    "retry_packages": [
                        {"package_id": pid, "name": by_id[pid]["name"],
                         "reason": "io_error"}
                        for pid in needed
                    ],
                    "unreadable_packages": [
                        {"package_id": pid, "name": by_id[pid]["name"],
                         "reason": "io_error"}
                        for pid in sorted(io_failed)
                    ],
                    "delivered_total_bytes": None if delivered is None else delivered[0],
                    "recovered_total_bytes": recovered[0],
                    "excluded_packages": [r.model_dump() for r in reselections],
                },
                headers={"Retry-After": "5"},
            )

        total_bytes, chosen_ids = delivered
        chosen = [by_id[pid] for pid in chosen_ids]
        day_entries = []
        for d in days:
            t = targets[d]
            bit = 1 << day_index[d]
            src = min((p for p in chosen if masks[p["id"]] & bit), key=lambda p: p["id"])
            day_entries.append(
                DayEntry(
                    date=d,
                    revision=t.revision,
                    feed_kg=t.feed_kg,
                    source_package_id=src["id"],
                    source_package_name=src["name"],
                )
            )

        return BackfillOut(
            start_date=days[0],
            end_date=days[-1],
            days=day_entries,
            packages=[
                SelectedPackage(
                    id=p["id"], name=p["name"],
                    size_bytes=p["size_bytes"], sha256=p["sha256"],
                )
                for p in chosen
            ],
            total_bytes=total_bytes,
            reselections=reselections,
            retry_packages=[
                RetryPackage(package_id=pid, name=by_id[pid]["name"], reason="io_error")
                for pid in sorted(io_failed)
            ],
        )

    return app


app = create_app()
