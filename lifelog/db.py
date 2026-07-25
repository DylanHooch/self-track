"""SQLite 存储层。schema 见 docs/02-schema.md（含 review 修订）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

# L1/L2 schema 版本：prompt 或字段结构变化时 +1，旧版 done 卡自动转 pending 重算
DIGEST_SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  source           TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  project          TEXT,
  cwd              TEXT,
  title            TEXT,
  started_at       TEXT NOT NULL,
  ended_at         TEXT,
  active_date      TEXT NOT NULL,
  n_user_msgs      INTEGER NOT NULL DEFAULT 0,
  n_assistant_msgs INTEGER NOT NULL DEFAULT 0,
  n_tool_calls     INTEGER NOT NULL DEFAULT 0,
  n_input_tokens   INTEGER,
  n_output_tokens  INTEGER,
  first_user_msg   TEXT,
  digest_status    TEXT NOT NULL DEFAULT 'pending',
  digest_json      TEXT,
  digest_mtime     REAL,
  digest_size      INTEGER,
  digest_ver       INTEGER,
  raw_path         TEXT NOT NULL,
  raw_mtime        REAL NOT NULL,
  raw_size         INTEGER NOT NULL DEFAULT 0,
  processed_at     TEXT NOT NULL,
  PRIMARY KEY (source, session_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(active_date);
CREATE INDEX IF NOT EXISTS idx_sessions_digest ON sessions(digest_status);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS session_day_stats (
  source       TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  day          TEXT NOT NULL,
  n_user       INTEGER NOT NULL DEFAULT 0,
  n_assistant  INTEGER NOT NULL DEFAULT 0,
  n_tool_calls INTEGER NOT NULL DEFAULT 0,
  hours        TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (source, session_id, day)
);
CREATE INDEX IF NOT EXISTS idx_daystats_day ON session_day_stats(day);

CREATE TABLE IF NOT EXISTS daily_reports (
  date         TEXT PRIMARY KEY,
  report_json  TEXT NOT NULL,
  llm_used     INTEGER NOT NULL DEFAULT 0,
  generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dirty_days (
  day TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  n_scanned   INTEGER DEFAULT 0,
  n_new       INTEGER DEFAULT 0,
  n_skipped   INTEGER DEFAULT 0,
  n_failed    INTEGER DEFAULT 0,
  warnings    TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def to_local_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def to_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """轻量迁移：CREATE IF NOT EXISTS 不会给老表加列，缺列则 ALTER（review 修正）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        for col, decl in [("raw_size", "INTEGER NOT NULL DEFAULT 0"),
                          ("digest_mtime", "REAL"),
                          ("digest_size", "INTEGER"),
                          ("digest_ver", "INTEGER")]:
            if col not in cols:
                self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")

    def close(self):
        self.conn.close()

    def known_watermarks(self) -> dict[tuple[str, str], tuple[float, int]]:
        rows = self.conn.execute("SELECT source, session_id, raw_mtime, raw_size FROM sessions")
        return {(r["source"], r["session_id"]): (r["raw_mtime"], r["raw_size"]) for r in rows}

    def upsert_session(self, rs, day_stats: list[dict]):
        c = self.conn
        started = to_local_iso(rs.started_at) or now_iso()
        try:
            self._upsert_inner(c, rs, day_stats, started)
            c.commit()
        except Exception:
            c.rollback()  # review P1：半成品事务不得被后续无关 commit 固化
            raise

    def _upsert_inner(self, c, rs, day_stats: list[dict], started: str):
        old = c.execute(
            "SELECT digest_status, digest_json, digest_mtime, digest_size, digest_ver FROM sessions "
            "WHERE source=? AND session_id=?",
            (rs.source, rs.session_id),
        ).fetchone()
        # digest 有效性：水位 (mtime, size) 一致 且 schema 版本匹配 才复用（review 修正：
        # skipped 也只在内容未变时保留；prompt/schema 升级后旧卡自动转 pending 重算）
        digest_status, digest_json, digest_mtime, digest_size = "pending", None, None, None
        digest_ver = None
        content_changed = old is None
        if old:
            unchanged = (old["digest_mtime"] is not None
                         and rs.raw_mtime <= old["digest_mtime"]
                         and rs.raw_size == old["digest_size"])
            content_changed = not unchanged
            if unchanged and old["digest_status"] in ("done", "skipped") \
                    and old["digest_ver"] == DIGEST_SCHEMA_VERSION:
                digest_status = old["digest_status"]
            if old["digest_status"] in ("done", "skipped"):
                # 旧卡/预筛结论保留展示，直到新结论覆盖
                digest_json = old["digest_json"]
                digest_mtime, digest_size = old["digest_mtime"], old["digest_size"]
                digest_ver = old["digest_ver"]
        # 受影响日期 = 旧日期 ∪ 新日期，持久化到 dirty_days（review 修正：
        # 崩溃后靠它恢复重算，不依赖进程内集合；日期消失也要删除投影）
        old_days = {r["day"] for r in c.execute(
            "SELECT day FROM session_day_stats WHERE source=? AND session_id=?",
            (rs.source, rs.session_id))}
        new_days = {d["day"] for d in day_stats}
        c.executemany("INSERT OR IGNORE INTO dirty_days (day) VALUES (?)",
                      [(d,) for d in old_days | new_days])
        if content_changed:
            # L2 叙事随输入集合变化失效（review P0）：任何新增/变更 session 都使
            # 覆盖日期的日报失效，同事务删除，digest 阶段重新生成；
            # 宁可暂时没有叙事，也不展示与统计矛盾的陈旧叙事
            c.executemany("DELETE FROM daily_reports WHERE date=?",
                          [(d,) for d in old_days | new_days])
        c.execute(
            """INSERT OR REPLACE INTO sessions
               (source, session_id, project, cwd, title, started_at, ended_at, active_date,
                n_user_msgs, n_assistant_msgs, n_tool_calls, n_input_tokens, n_output_tokens,
                first_user_msg, digest_status, digest_json, digest_mtime, digest_size, digest_ver,
                raw_path, raw_mtime, raw_size, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rs.source, rs.session_id, rs.project, rs.cwd, rs.title,
             started, to_local_iso(rs.ended_at), to_day(rs.started_at) if rs.started_at else started[:10],
             rs.n_user_msgs, rs.n_assistant_msgs, rs.n_tool_calls,
             rs.n_input_tokens, rs.n_output_tokens, rs.first_user_msg,
             digest_status, digest_json, digest_mtime, digest_size, digest_ver,
             rs.raw_path, rs.raw_mtime, rs.raw_size, now_iso()),
        )
        c.execute("DELETE FROM session_day_stats WHERE source=? AND session_id=?",
                  (rs.source, rs.session_id))
        c.executemany(
            """INSERT OR REPLACE INTO session_day_stats
               (source, session_id, day, n_user, n_assistant, n_tool_calls, hours)
               VALUES (?,?,?,?,?,?,?)""",
            [(d["source"], d["session_id"], d["day"], d["n_user"], d["n_assistant"],
              d["n_tool_calls"], json.dumps(sorted(d["hours"]))) for d in day_stats],
        )
        # commit 由外层 upsert_session 统一负责

    def begin_run(self) -> int:
        cur = self.conn.execute("INSERT INTO scan_runs (started_at) VALUES (?)", (now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, n_scanned: int, n_new: int, n_skipped: int,
                   n_failed: int, warnings: list[str]):
        self.conn.execute(
            """UPDATE scan_runs SET finished_at=?, n_scanned=?, n_new=?, n_skipped=?, n_failed=?, warnings=?
               WHERE id=?""",
            (now_iso(), n_scanned, n_new, n_skipped, n_failed, json.dumps(warnings, ensure_ascii=False), run_id),
        )
        self.conn.commit()
