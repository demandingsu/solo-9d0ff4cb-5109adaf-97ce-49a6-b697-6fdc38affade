"""冷档 ZIP 的解析、登记与校验。

每个冷档是一个 ZIP，包内有且仅有一份 CSV，表头固定为:
    date,revision,feed_kg
- date:     ISO 日期 YYYY-MM-DD
- revision: 非负整数修订号
- feed_kg:  当天进料公斤数（非负有限数）
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import zipfile
from datetime import date as date_type
from typing import NamedTuple

EXPECTED_HEADER = ("date", "revision", "feed_kg")


class PackageError(ValueError):
    """冷档内容不合法（登记时拒绝，HTTP 400）。"""


class DayRow(NamedTuple):
    date: str
    revision: int
    feed_kg: float
    row_sha256: str


def row_checksum(date_s: str, revision: int, feed_kg: float) -> str:
    return hashlib.sha256(f"{date_s}|{revision}|{feed_kg!r}".encode("utf-8")).hexdigest()


def parse_package(buf: bytes) -> list[DayRow]:
    """解析 ZIP 字节，返回去重后的逐日修订行；不合法则抛 PackageError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(buf))
    except zipfile.BadZipFile as exc:
        raise PackageError("不是合法的 ZIP 文件") from exc

    csv_names = [
        n for n in zf.namelist()
        if not n.endswith("/") and n.lower().endswith(".csv")
    ]
    if len(csv_names) != 1:
        raise PackageError(f"包内必须有且仅有一份 CSV，实际找到 {len(csv_names)} 份")

    raw = zf.read(csv_names[0])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageError("CSV 必须是 UTF-8 编码") from exc

    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise PackageError("CSV 为空")
    header = tuple(c.strip().lower() for c in rows[0])
    if header != EXPECTED_HEADER:
        raise PackageError(f"CSV 表头必须是 {','.join(EXPECTED_HEADER)}")

    seen: dict[tuple[str, int], tuple[float, str]] = {}
    for lineno, r in enumerate(rows[1:], start=2):
        if len(r) != 3:
            raise PackageError(f"第 {lineno} 行：应为 3 列，实际 {len(r)} 列")
        d_s, rev_s, kg_s = (c.strip() for c in r)

        try:
            d = date_type.fromisoformat(d_s).isoformat()
        except ValueError as exc:
            raise PackageError(f"第 {lineno} 行：日期 {d_s!r} 不是合法的 YYYY-MM-DD") from exc

        try:
            rev = int(rev_s)
        except ValueError as exc:
            raise PackageError(f"第 {lineno} 行：修订号 {rev_s!r} 不是整数") from exc
        if rev < 0:
            raise PackageError(f"第 {lineno} 行：修订号不能为负")

        try:
            kg = float(kg_s)
        except ValueError as exc:
            raise PackageError(f"第 {lineno} 行：进料公斤数 {kg_s!r} 不是数字") from exc
        if math.isnan(kg) or math.isinf(kg) or kg < 0:
            raise PackageError(f"第 {lineno} 行：进料公斤数必须是非负有限数")

        key = (d, rev)
        if key in seen:
            if seen[key][0] != kg:
                raise PackageError(
                    f"第 {lineno} 行：同一 (日期, 修订号)={key} 在包内出现异值"
                )
            continue  # 完全相同的重复行，去重
        seen[key] = (kg, row_checksum(d, rev, kg))

    if not seen:
        raise PackageError("CSV 没有数据行")

    return [
        DayRow(date=d, revision=rev, feed_kg=kg, row_sha256=h)
        for (d, rev), (kg, h) in sorted(seen.items())
    ]


def verify_package_file(path: str, expected_sha256: str) -> tuple[bool, str | None]:
    """调取前校验整包：文件存在且 SHA-256 与登记值一致。

    返回 (是否通过, 失败原因)。reason ∈ {"missing", "checksum_mismatch"}。
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except FileNotFoundError:
        return False, "missing"
    except OSError:
        return False, "missing"
    if h.hexdigest() != expected_sha256:
        return False, "checksum_mismatch"
    return True, None
