"""SQLite 存储层。schema 见 docs/02-schema.md（含 review 修订）。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# L1/L2 schema 版本：prompt 或字段结构变化时 +1，旧版 done 卡自动转 pending 重算
DIGEST_SCHEMA_VERSION = 4

# ——— 非交互会话识别（用户决策 2026-08-01：机器发起的与人类区分，看板收拢默认不展示）———
# 判定只依赖已落库字段（first_user_msg / n_user_msgs），不碰原始文件——规则升级后
# 旧会话靠 upsert 重算或手工回填，无需重解析。
# - dispatch：agent-dispatch 委派（skill 标准任务模板 "# Task"）
# - skill-auto：skill 后端非交互启动（image/video 生成的 "You were launched by the ..."）
# - probe：探测类（token 刷新 "ok"、"Reply with exactly ..."、cron-fire 包装）
_AUTO_HEAD_RULES = [
    ("dispatch", re.compile(r"^#\s*(Task\b|任务\b)")),
    ("skill-auto", re.compile(r"^You were launched by the ")),
    ("probe", re.compile(r"^(Reply with exactly\b|<cron-fire\b)")),
]


def classify_auto(first_user_msg: str | None, n_user_msgs: int) -> str | None:
    """返回 auto_kind（None = 人类发起）。模板命中即判（resume 续跑也是机器文本，
    不看消息数）；短消息探测只认白名单 "ok"（review P1：按长度猜会误伤人类
    短开场如「你好」「在吗」——藏人比藏机器代价高，宁漏勿错）。"""
    t = (first_user_msg or "").strip()
    if not t:
        return None
    for kind, pat in _AUTO_HEAD_RULES:
        if pat.search(t):
            return kind
    if n_user_msgs <= 1 and t.lower() == "ok":  # token 刷新：echo ok | kimi -p "ok"
        return "probe"
    return None

# 产物文件白名单（用户决策）：只要文档/图片/视频，代码文件不入账；
# html 明确要（worktree 分析报告是核心场景）
_ARTIFACT_DOC_EXT = {
    ".md", ".markdown", ".txt", ".rtf", ".pdf",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".pages", ".numbers", ".key",
    ".html", ".htm",
}
_ARTIFACT_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                     ".heic", ".bmp", ".tiff", ".tif"}
_ARTIFACT_VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
ARTIFACT_FILE_EXT = _ARTIFACT_DOC_EXT | _ARTIFACT_IMG_EXT | _ARTIFACT_VIDEO_EXT

# 可读出头两行的文本类扩展（pdf/office 二进制读不了，跳过）
_HEAD_READABLE_EXT = {".md", ".markdown", ".txt", ".csv", ".html", ".htm"}


def head_lines(path: str) -> str | None:
    """产物头两行外显（用户决策：不然不知道是什么东西）。

    md/txt/csv 取前两行非空文本（剥 markdown 标记）；html 优先 <title>，
    否则首个去标签文本行。只读前 8KB，失败返回 None。
    """
    import re
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(8192)
    except OSError:
        return None
    if Path(path).suffix.lower() in (".html", ".htm"):
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        if m and m.group(1).strip():
            return re.sub(r"\s+", " ", m.group(1).strip())[:140]
        text = re.sub(r"<[^>]+>", "\n", raw)
        lines = [t.strip() for t in text.split("\n") if t.strip()]
        return " / ".join(lines[:2])[:140] or None
    lines = []
    for line in raw.split("\n"):
        t = re.sub(r"^[#>\s*\-]+", "", line).strip()
        if t:
            lines.append(t)
        if len(lines) >= 2:
            break
    return " / ".join(lines[:2])[:140] or None

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
  auto_kind        TEXT,                -- 非交互会话分类（classify_auto）；NULL=人类发起
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

-- 产物账本（用户决策：产物导向，追踪会话写过的文件与 commit，防 worktree 中间产物散落）
CREATE TABLE IF NOT EXISTS artifacts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,          -- 'file' | 'commit'
  path          TEXT,                   -- file: 绝对路径（去重键）；commit: NULL
  name          TEXT NOT NULL,          -- 文件名 / commit 摘要
  repo          TEXT,                   -- commit 的仓库目录
  first_day     TEXT NOT NULL,
  last_day      TEXT NOT NULL,
  note          TEXT,                   -- 简介（LLM 后补，先空）
  head          TEXT,                   -- 头两行外显（确定性提取，插入时一次性）
  path_override TEXT                    -- 用户在前端补的移动后路径
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_file
  ON artifacts(path) WHERE kind='file';
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_commit
  ON artifacts(repo, name, first_day) WHERE kind='commit';
-- 挂名制：一个产物可被多个会话写过（用户决策：所有写过的都挂名）
CREATE TABLE IF NOT EXISTS artifact_sessions (
  artifact_id INTEGER NOT NULL,
  source      TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  PRIMARY KEY (artifact_id, source, session_id)
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
        # timeout：写者撞锁等待而非立刻 OperationalError；WAL：serve 长驻后
        # scan-light 写与看板 GET 读真正并发（review P1），WAL 允许单写多读
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
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
                          ("digest_ver", "INTEGER"),
                          ("auto_kind", "TEXT")]:
            if col not in cols:
                self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
                if col == "auto_kind":
                    self._backfill_auto_kind()
        acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(artifacts)")}
        if acols and "head" not in acols:
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN head TEXT")

    def _backfill_auto_kind(self):
        """老库一次性回填 auto_kind（只读已落库字段，几百行规模，秒级）。"""
        rows = self.conn.execute(
            "SELECT source, session_id, first_user_msg, n_user_msgs FROM sessions").fetchall()
        n = 0
        for r in rows:
            kind = classify_auto(r["first_user_msg"], r["n_user_msgs"])
            if kind:
                self.conn.execute(
                    "UPDATE sessions SET auto_kind=? WHERE source=? AND session_id=?",
                    (kind, r["source"], r["session_id"]))
                n += 1
        if n:
            print(f"[db] 回填非交互会话标记：{n} 个")

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
        if old:
            unchanged = (old["digest_mtime"] is not None
                         and rs.raw_mtime <= old["digest_mtime"]
                         and rs.raw_size == old["digest_size"])
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
        # 叙事稳定性（用户决策，替代 review P0 的失效删除）：已生成的日叙事不再随
        # 会话内容变化而作废。沉淀标准 = 叙事生成日期晚于叙事所指日期（即最后总结
        # 发生在当天 23:59 之后）；当天生成的叙事不算沉淀，digest 阶段允许重算，
        # 无叙事的日期由 missing_days 补齐逻辑生成。
        c.execute(
            """INSERT OR REPLACE INTO sessions
               (source, session_id, project, cwd, title, started_at, ended_at, active_date,
                n_user_msgs, n_assistant_msgs, n_tool_calls, n_input_tokens, n_output_tokens,
                first_user_msg, digest_status, digest_json, digest_mtime, digest_size, digest_ver,
                auto_kind, raw_path, raw_mtime, raw_size, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rs.source, rs.session_id, rs.project, rs.cwd, rs.title,
             started, to_local_iso(rs.ended_at), to_day(rs.started_at) if rs.started_at else started[:10],
             rs.n_user_msgs, rs.n_assistant_msgs, rs.n_tool_calls,
             rs.n_input_tokens, rs.n_output_tokens, rs.first_user_msg,
             digest_status, digest_json, digest_mtime, digest_size, digest_ver,
             classify_auto(rs.first_user_msg, rs.n_user_msgs),
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
        self._upsert_artifacts(c, rs, sorted(new_days) or [started[:10]])
        # commit 由外层 upsert_session 统一负责

    def _upsert_artifacts(self, c, rs, days_sorted: list[str]):
        """产物账本：文件写入 + commit。幂等：先摘本会话旧挂名再重建；
        note/path_override 是人工/LLM 增补，重建时保留。"""
        from pathlib import Path as _P
        c.execute("DELETE FROM artifact_sessions WHERE source=? AND session_id=?",
                  (rs.source, rs.session_id))
        first_day, last_day = days_sorted[0], days_sorted[-1]
        for p in sorted(set(rs.file_writes)):
            if not p.startswith("/"):
                continue  # adapter 层已按 cwd 归一，兜一层防御
            if _P(p).suffix.lower() not in ARTIFACT_FILE_EXT:
                continue  # 白名单：只收文档/图片/视频（用户决策，代码文件不入账）
            if _P(p).name.lower() == "skill.md" or "/memory/" in p:
                continue  # 用户决策：skill 定义与 agent 记忆不算交付产物
            head = head_lines(p) if _P(p).suffix.lower() in _HEAD_READABLE_EXT else None
            c.execute(
                """INSERT INTO artifacts (kind, path, name, first_day, last_day, head)
                   VALUES ('file', ?, ?, ?, ?, ?)
                   ON CONFLICT(path) WHERE kind='file'
                   DO UPDATE SET last_day=MAX(last_day, excluded.last_day),
                                 first_day=MIN(first_day, excluded.first_day),
                                 head=COALESCE(artifacts.head, excluded.head)""",
                (p, _P(p).name, first_day, last_day, head))
            aid = c.execute("SELECT id FROM artifacts WHERE kind='file' AND path=?",
                            (p,)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO artifact_sessions VALUES (?,?,?)",
                      (aid, rs.source, rs.session_id))
        for cm in rs.commits:
            subject = (cm.get("subject") or "").strip()
            if not subject:
                continue  # 拿不到摘要的 commit 没有辨识度，不记
            repo = cm.get("repo") or rs.cwd or ""
            c.execute(
                """INSERT INTO artifacts (kind, name, repo, first_day, last_day)
                   VALUES ('commit', ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (subject, repo, first_day, last_day))
            aid = c.execute(
                "SELECT id FROM artifacts WHERE kind='commit' AND repo=? AND name=? AND first_day=?",
                (repo, subject, first_day)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO artifact_sessions VALUES (?,?,?)",
                      (aid, rs.source, rs.session_id))
        # 孤儿清理：所有挂名都被摘掉的产物（重解析后不再被任何会话写过）
        c.execute("DELETE FROM artifacts WHERE id NOT IN (SELECT artifact_id FROM artifact_sessions)")

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
