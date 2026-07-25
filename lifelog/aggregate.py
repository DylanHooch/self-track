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
    """tmp + rename，避免崩溃留下截断文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
