#!/usr/bin/env python3
"""生成演示用冷档 ZIP（交叠覆盖 + 可用于坏包重选演练）。

用法: python3 scripts/make_samples.py [输出目录]
默认输出到 ./samples/。每个 ZIP 内一份 CSV（date,revision,feed_kg），
用 ZIP 注释字段填充到不同字节数，便于观察“总字节最少”的选包结果。
"""
from __future__ import annotations

import csv
import io
import os
import sys
import zipfile
from datetime import date, timedelta


def build_zip(rows: list[tuple[str, int, float]], pad_bytes: int = 0) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "revision", "feed_kg"])
    for d, rev, kg in rows:
        w.writerow([d, rev, kg])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("intake.csv", buf.getvalue())
        if pad_bytes > 0:
            zf.comment = b" " * pad_bytes
    return out.getvalue()


def day_range(start: str, n: int, revision: int, base_kg: float):
    d0 = date.fromisoformat(start)
    return [
        ((d0 + timedelta(days=i)).isoformat(), revision, round(base_kg + i * 1.5, 2))
        for i in range(n)
    ]


SAMPLES = {
    # 第 1 周：11-01..11-07 修订 1（大包）
    "week1_rev1.zip": (day_range("2026-11-01", 7, 1, 100.0), 1500),
    # 第 2 周：11-05..11-11 修订 1，与 week1 交叠 3 天且交叠日取值一致（大包）
    "week2_rev1.zip": ([("2026-11-05", 1, 106.0), ("2026-11-06", 1, 107.5),
                        ("2026-11-07", 1, 109.0), ("2026-11-08", 1, 209.0),
                        ("2026-11-09", 1, 210.5), ("2026-11-10", 1, 212.0),
                        ("2026-11-11", 1, 213.5)], 1500),
    # 修订包：11-06、11-07 修订 2（小包，覆盖交叠区中两天的更高修订）
    "fix_1106_1107_rev2.zip": (
        [("2026-11-06", 2, 215.5), ("2026-11-07", 2, 217.0)], 0),
    # 便宜小包：11-01..11-04 修订 1（字节最少，会被优先选中，适合演练坏包重选）
    "cheap_1101_1104_rev1.zip": (day_range("2026-11-01", 4, 1, 100.0), 0),
}


def main() -> None:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "samples"
    os.makedirs(out_dir, exist_ok=True)
    for name, (rows, pad) in SAMPLES.items():
        data = build_zip(rows, pad)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        print(f"{path}  {len(data)} bytes  {len(rows)} rows")


if __name__ == "__main__":
    main()
