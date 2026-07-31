"""日统计聚合：DB → stats/daily/*.json（projection，原子覆盖写）。

数字一律确定性计算；narrative 部分从 daily_reports 表拼接（无 LLM 产出时给空结构）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .db import DB, now_iso


def atomic_write_text(path: Path, text: str):
    """tmp + rename，避免崩溃留下截断文件；tmp 名带进程与随机后缀，并发写不互踩。"""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_name, path)


def day_rhythm(c: sqlite3.Connection, day: str) -> dict:
    """当天作息信号（确定性计算，零 token；digest 层的日叙事也复用）。

    - first/last：当天最早/最晚活动时刻（"HH:MM"）。会话区间 [started, ended]
      裁剪到当天、再用该会话当天的真实活跃小时（session_day_stats.hours）收紧，
      防止「几天前开启、ended_at 缺失」的会话把 first 顶到 00:00。
    - late_until：次日凌晨 [00:00,04:00) 有活动 → 记在当天头上（「昨晚熬夜」），
      值=该凌晨活动最晚延续到的时刻（按 <6 点的最大活跃小时 +1h 封顶，防空洞误判）；≥04:30 视为「通宵」。
    - tags：熬夜 / 通宵 / 早起。早起 = first ∈ [04:00,07:00)；first <04:00 属于
      前一夜的延续，已由前一天的「熬夜」认领，当天不重复标。
    假定本地时区无 DST（review P2-5；有夏令时的时区跨天边界会偏 1h）。
    """
    from datetime import datetime, timedelta

    day0 = datetime.fromisoformat(day + "T00:00:00").astimezone()

    def _parse(ts):
        try:
            return datetime.fromisoformat(ts).astimezone()
        except (ValueError, TypeError):
            return None

    def _intervals(d0: datetime) -> list:
        """返回 [(lo, hi, hours)]：会话区间裁剪到当天，并用当天真实活跃小时收紧两端。"""
        rows = c.execute(
            "SELECT s.started_at, s.ended_at, t.hours FROM sessions s "
            "JOIN session_day_stats t ON s.source=t.source AND s.session_id=t.session_id "
            "WHERE t.day=?", (d0.strftime("%Y-%m-%d"),)).fetchall()
        out = []
        for r in rows:
            t0 = _parse(r["started_at"])
            if not t0:
                continue
            try:
                hours = [h for h in json.loads(r["hours"]) if isinstance(h, int) and 0 <= h <= 23]
            except (ValueError, TypeError):
                hours = []
            if not hours:
                continue  # hours 为空/损坏：跳过该行，不合成虚假活跃时刻（review P2-4）
            h_lo = d0 + timedelta(hours=min(hours))
            h_hi = d0 + timedelta(hours=max(hours) + 1)
            # ended_at 缺失（进行中/崩溃）：回退到当天最大活跃小时+1h，否则 hi 塌缩
            # 到 t0，跨天进行中会话永远判不出熬夜/通宵（review P2-3）
            t1 = _parse(r["ended_at"]) or h_hi
            lo = min(max(t0, h_lo), h_hi)
            hi = max(min(t1, h_hi), lo)
            out.append((lo, hi, hours))
        return out

    iv = _intervals(day0)
    if not iv:
        return {"first": None, "last": None, "late_until": None, "tags": []}
    day1 = day0 + timedelta(days=1)
    hm_last = lambda t: "24:00" if t >= day1 else t.strftime("%H:%M")  # last 不会越出当天
    first, last = min(lo for lo, _, _ in iv), max(hi for _, hi, _ in iv)
    tags: list[str] = []
    late_until = None
    # 熬夜：次日凌晨核心窗 [00:00,04:00) 有真实活跃小时才成立；尾部时刻按
    # 「<6 点的最大活跃小时 +1h」封顶——长会话中间有 idle 空洞（如 hours=[0,8,9]）
    # 时不能用会话 ended_at，否则把次日早上的活动误算成熬夜延续
    tails = []
    for lo, hi, hours in _intervals(day1):
        if not any(h < 4 for h in hours):
            continue
        cap = day1 + timedelta(hours=max(h for h in hours if h < 6) + 1)
        tails.append(min(hi, cap))
    if tails:
        late_until = max(tails)
        tags.append("通宵" if late_until >= day1 + timedelta(hours=4, minutes=30) else "熬夜")
    if day0 + timedelta(hours=4) <= first < day0 + timedelta(hours=7):
        tags.append("早起")
    return {"first": first.strftime("%H:%M"), "last": hm_last(last),
            "late_until": late_until.strftime("%H:%M") if late_until else None, "tags": tags}


def compute_daily(db: DB, day: str) -> dict:
    c = db.conn
    rows = c.execute(
        "SELECT * FROM session_day_stats WHERE day=?", (day,)
    ).fetchall()
    sessions = c.execute(
        """SELECT s.*, d.n_user AS day_user, d.n_tool_calls AS day_tools FROM sessions s
           JOIN session_day_stats d ON s.source=d.source AND s.session_id=d.session_id
           WHERE d.day=? ORDER BY s.started_at""",
        (day,),
    ).fetchall()

    by_source = Counter()
    hours: set[int] = set()
    n_user = n_tool = 0
    projects = Counter()
    for r in rows:
        by_source[r["source"]] += 1
        n_user += r["n_user"]
        n_tool += r["n_tool_calls"]
        hours.update(json.loads(r["hours"]))
    for s in sessions:
        # 项目名优先取 cwd 末级目录（如 ohos），目录名型 project（wd_xxx/-Users-xxx）兜底
        proj = s["project"] or "?"
        if s["cwd"]:
            from pathlib import Path as _P
            proj = _P(s["cwd"].rstrip("/")).name or proj
        projects[proj] += 1

    report = c.execute("SELECT report_json, llm_used FROM daily_reports WHERE date=?", (day,)).fetchone()
    if report:
        narrative = json.loads(report["report_json"])
        narrative["llm_used"] = bool(report["llm_used"])
    else:
        narrative = {
            "summary": None, "focus": [], "progress": [], "hotspots": [],
            "commitments": [], "llm_used": False,
        }

    # 决策（review B3 修正）：token 无法跨源按日归属（kimi 是按 turn 累加、tcodex 是累计快照、
    # 其余源没有），日统计不放 token，避免跨日重复计数误导；会话级总量仍在 sessions 表。
    return {
        "date": day,
        "generated_at": now_iso(),
        "rhythm": day_rhythm(c, day),
        "stats": {
            "n_sessions": len(rows),
            "by_source": dict(by_source),
            "n_user_msgs": n_user,
            "n_tool_calls": n_tool,
            "active_hours": sorted(hours),
            "projects": [{"name": p, "n_sessions": n} for p, n in projects.most_common()],
        },
        "narrative": narrative,
        "sessions": [
            {
                "ref": f'{s["source"]}:{s["session_id"]}',
                "source": s["source"],
                "session_id": s["session_id"],
                "title": s["title"],
                "project": s["project"],
                "cwd": s["cwd"],
                "started_at": s["started_at"],
                "n_user_msgs": s["day_user"],      # 当日量，非会话全生命周期
                "n_tool_calls": s["day_tools"],
                "digest_status": s["digest_status"],
                "digest": json.loads(s["digest_json"]) if s["digest_json"] else None,
            }
            for s in sessions
        ],
    }


def rebuild_dirty_days(db: DB, stats_dir: Path, extra_days: set[str] | None = None) -> list[str]:
    """重算 dirty_days ∪ extra_days：有数据→覆盖写，无数据→删除投影，最后清 dirty 标记。

    dirty_days 在 upsert 同一事务里持久化，崩溃后重跑自动补算（review P0 修正）。
    """
    days = {r["day"] for r in db.conn.execute("SELECT day FROM dirty_days")}
    days |= extra_days or set()
    written = []
    for day in sorted(days):
        data = compute_daily(db, day)
        path = stats_dir / "daily" / f"{day}.json"
        if data["stats"]["n_sessions"] == 0:
            path.unlink(missing_ok=True)  # 该日已无数据，删除过期投影
        else:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1))
            written.append(day)
    db.conn.execute("DELETE FROM dirty_days")
    db.conn.commit()
    return written


def days_with_data(db: DB) -> list[str]:
    rows = db.conn.execute("SELECT DISTINCT day FROM session_day_stats ORDER BY day").fetchall()
    return [r["day"] for r in rows]


def missing_daily_days(db: DB, stats_dir: Path) -> set[str]:
    """DB 里有数据但 JSON 缺失的日期（崩溃恢复：projection 与 canonical 对齐）。"""
    daily_dir = stats_dir / "daily"
    return {d for d in days_with_data(db) if not (daily_dir / f"{d}.json").exists()}
