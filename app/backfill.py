"""补数核心逻辑：确定逐日目标修订 + 总字节最少的整包组合（集合覆盖 DP）。"""
from __future__ import annotations

from typing import Iterable, NamedTuple


class UnresolvableDays(Exception):
    """缺日或最高修订存在异值。携带结构化明细。"""

    def __init__(self, missing: list[str], conflicts: list[dict]) -> None:
        super().__init__("存在缺日或最高修订异值")
        self.missing = missing
        self.conflicts = conflicts


class DayTarget(NamedTuple):
    date: str
    revision: int
    feed_kg: float


def determine_targets(
    days: list[str],
    rows: Iterable[tuple[int, str, int, float]],
) -> dict[str, DayTarget]:
    """按日取最高修订。

    rows: (package_id, date, revision, feed_kg)，仅来自请求开始时已登记且状态正常的包。
    - 某日完全没有记录        -> 缺日，报错
    - 某日最高修订对应多个值  -> 异值，报错
    返回 {date: DayTarget}，本次请求内目标修订一经确定不再变更。
    """
    by_day: dict[str, list[tuple[int, float]]] = {}
    for _pid, d, rev, kg in rows:
        by_day.setdefault(d, []).append((rev, kg))

    missing: list[str] = []
    conflicts: list[dict] = []
    targets: dict[str, DayTarget] = {}

    for d in days:
        entries = by_day.get(d)
        if not entries:
            missing.append(d)
            continue
        max_rev = max(rev for rev, _ in entries)
        values = sorted({kg for rev, kg in entries if rev == max_rev})
        if len(values) > 1:
            conflicts.append({"date": d, "revision": max_rev, "values": values})
            continue
        targets[d] = DayTarget(date=d, revision=max_rev, feed_kg=values[0])

    if missing or conflicts:
        raise UnresolvableDays(missing, conflicts)
    return targets


def coverage_masks(
    package_ids: Iterable[int],
    rows: Iterable[tuple[int, str, int, float]],
    targets: dict[str, DayTarget],
    day_index: dict[str, int],
) -> dict[int, int]:
    """每个包对目标日的覆盖位图：仅当包内 (date, revision) 等于目标修订时置位。"""
    masks = {pid: 0 for pid in package_ids}
    for pid, d, rev, _kg in rows:
        t = targets.get(d)
        if t is not None and t.revision == rev and pid in masks:
            masks[pid] |= 1 << day_index[d]
    return masks


def select_min_bytes(
    candidates: Iterable[tuple[int, int]],
    masks: dict[int, int],
    n_days: int,
) -> tuple[int, tuple[int, ...]] | None:
    """选出能全覆盖 n_days 个目标日、且总字节最少的整包组合。

    candidates: (package_id, size_bytes)。天数 <= 14，用子集 DP 精确求解。
    平局时取包数更少、再取 id 字典序更小者，保证结果确定。
    返回 (总字节, package_id 元组)；无法全覆盖返回 None。
    """
    full = (1 << n_days) - 1
    # dp[mask] = (总字节, 包数, id 元组)
    dp: dict[int, tuple[int, int, tuple[int, ...]]] = {0: (0, 0, ())}
    for pid, size in sorted(candidates):
        m = masks.get(pid, 0)
        if m == 0:
            continue
        for mask, (cost, cnt, ids) in list(dp.items()):
            new_mask = mask | m
            cand = (cost + size, cnt + 1, tuple(sorted(ids + (pid,))))
            cur = dp.get(new_mask)
            if cur is None or cand < cur:
                dp[new_mask] = cand
    best = dp.get(full)
    if best is None:
        return None
    return best[0], best[2]
