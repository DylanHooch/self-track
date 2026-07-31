"""扫描编排：发现 → 增量判定 → 解析 → 落库 → 记录受影响日期。

中断恢复策略（review 修订）：
- 每个 session 解析+落库后立即 commit，崩溃最多损失当前 session。
- 受影响日期 = 本次重处理 session 覆盖的日期 ∪ 日统计 JSON 缺失的日期；
  日统计 JSON 只是 DB 的 projection，每次运行对受影响日期整体重算（覆盖写），
  因此崩溃后重跑自动补全所有缺失日——跨周补数据不需要专门逻辑。
- 半行竞态：解析前后各取一次水位，若解析期间文件仍在变化，本次不推进水位（下轮重处理）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .adapters import all_adapters
from .adapters.base import RawSession
from .db import DB, to_day


def day_stats_of(rs: RawSession) -> list[dict]:
    """把 session 的消息/工具调用按本地自然日拆成事实行（review：跨日归属的修正）。"""
    buckets: dict[str, dict] = {}

    def bucket(ts):
        day = to_day(ts)
        b = buckets.setdefault(day, {
            "source": rs.source, "session_id": rs.session_id, "day": day,
            "n_user": 0, "n_assistant": 0, "n_tool_calls": 0, "hours": set(),
        })
        b["hours"].add(datetime.fromtimestamp(ts).astimezone().hour)
        return b

    for m in rs.messages:
        if m.ts is None:
            continue
        b = bucket(m.ts)
        if m.role == "user":
            b["n_user"] += 1
        elif m.role == "assistant":
            b["n_assistant"] += 1
        # role='tool' 的消息条不计入对话统计（工具调用数另有 tool_call_ts）
    for ts in rs.tool_call_ts:
        if ts is not None:
            bucket(ts)["n_tool_calls"] += 1
    return list(buckets.values())


def scan(db: DB) -> tuple[set[str], list[str]]:
    """返回 (受影响日期集合, warnings)。"""
    known = db.known_watermarks()
    seen_keys: set[tuple[str, str]] = set()
    affected: set[str] = set()
    warnings: list[str] = []
    n_scanned = n_new = n_skipped = n_failed = 0
    run_id = db.begin_run()
    try:
        for adapter in all_adapters():
            for path in adapter.discover():
                n_scanned += 1
                try:
                    wm_before = (adapter.mtime_of(path), adapter.size_of(path))
                except OSError as e:
                    warnings.append(f"{adapter.source}: stat 失败 {path}: {e}")
                    n_failed += 1
                    continue
                # 增量预判的 key 必须与 parse 产出的 session_id 一致（adapter.id_of 保证）
                key = (adapter.source, adapter.id_of(path))
                seen_keys.add(key)
                old_wm = known.get(key)
                if old_wm and old_wm[0] >= wm_before[0] and old_wm[1] == wm_before[1]:
                    n_skipped += 1
                    continue
                try:
                    rs = adapter.parse(path)
                    if rs.session_id != adapter.id_of(path):
                        # id_of/parse 契约自检（review I1）：不一致会导致每轮全量重解析
                        warnings.append(
                            f"{adapter.source}: id_of 与 parse 产出 session_id 不一致 "
                            f"({adapter.id_of(path)} vs {rs.session_id})，{path.name}")
                    wm_after = (adapter.mtime_of(path), adapter.size_of(path))
                    if wm_after != wm_before:
                        # 解析期间仍在写入：本次结果可信（半行被忽略），但不推进水位，
                        # 强制下一轮重处理以采全
                        rs.raw_mtime, rs.raw_size = old_wm if old_wm else (0.0, 0)
                        warnings.append(f"{rs.source}: {path.name} 解析期间仍在写入，水位不推进")
                    else:
                        rs.raw_mtime, rs.raw_size = wm_before
                    stats = day_stats_of(rs)
                    db.upsert_session(rs, stats)
                    affected.update(s["day"] for s in stats)
                    n_new += 1
                except Exception as e:  # 宽容解析：单 session 失败不中断整体
                    warnings.append(f"{adapter.source}: 解析失败 {path}: {e}")
                    n_failed += 1
    finally:
        db.finish_run(run_id, n_scanned, n_new, n_skipped, n_failed, warnings)
    return affected, warnings
