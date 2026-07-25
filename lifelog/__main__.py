"""CLI：python -m lifelog <command>

commands:
  scan       增量扫描所有源并落库，重算受影响日期的日统计 JSON
  report     只重算日统计 JSON（全部日期）
  build-web  生成 web/index.html
  run        scan + digest（LLM 整理）+ report + build-web（每日完整流程）
  deep-dive  单会话深度分析页：python -m lifelog deep-dive <source> <session_id>
"""
from __future__ import annotations

import fcntl
import sys
from datetime import datetime
from pathlib import Path

from .aggregate import missing_daily_days, rebuild_dirty_days
from .db import DB, to_day

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"


def _open_db() -> DB:
    return DB(DATA / "lifelog.sqlite")


class RunLock:
    """单实例锁：launchd 与手工执行可能重叠（review 修订）。"""

    def __init__(self):
        self.path = DATA / "lifelog.lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个 lifelog 进程正在运行，退出。", file=sys.stderr)
            sys.exit(2)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def cmd_scan() -> int:
    from .scan import scan
    db = _open_db()
    try:
        affected, warnings = scan(db)
        written = rebuild_dirty_days(db, DATA / "stats", missing_daily_days(db, DATA / "stats"))
        print(f"扫描完成：受影响日期 {sorted(affected)}，重算 {len(written)} 天")
        for w in warnings:
            print(f"  warning: {w}", file=sys.stderr)
    finally:
        db.close()
    return 0


def cmd_report() -> int:
    from .aggregate import days_with_data
    db = _open_db()
    try:
        written = rebuild_dirty_days(db, DATA / "stats", set(days_with_data(db)))
        print(f"全量重算 {len(written)} 天")
    finally:
        db.close()
    return 0


def cmd_build_web() -> int:
    from .web import build_web
    db = _open_db()
    try:
        out = build_web(db, DATA / "stats", WEB)
        print(f"前端已生成：{out}")
    finally:
        db.close()
    return 0


def cmd_run() -> int:
    from .scan import scan
    from .digest import run_digest
    db = _open_db()
    try:
        affected, warnings = scan(db)
        run_digest(db)  # digest 层自己把触动的日期标 dirty
        extra = missing_daily_days(db, DATA / "stats")
        extra.add(to_day(datetime.now().timestamp()))
        written = rebuild_dirty_days(db, DATA / "stats", extra)
        from .web import build_web
        out = build_web(db, DATA / "stats", WEB)
        print(f"完成：重算 {len(written)} 天，前端 {out}")
        for w in warnings:
            print(f"  warning: {w}", file=sys.stderr)
    finally:
        db.close()
    return 0


def cmd_deep_dive() -> int:
    from .deepdive import deep_dive
    if len(sys.argv) < 4:
        print("用法: python -m lifelog deep-dive <source> <session_id>", file=sys.stderr)
        return 1
    db = _open_db()
    try:
        out = deep_dive(db, sys.argv[2], sys.argv[3], WEB)
        print(f"深度分析页：{out}")
    finally:
        db.close()
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    with RunLock():
        if cmd == "scan":
            return cmd_scan()
        if cmd == "report":
            return cmd_report()
        if cmd == "build-web":
            return cmd_build_web()
        if cmd == "run":
            return cmd_run()
        if cmd == "deep-dive":
            return cmd_deep_dive()
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
