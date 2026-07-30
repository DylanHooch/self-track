"""适配器基类与统一数据结构。

每个 agent 源一个 adapter，把各异的会话格式归一成 RawSession + 精简消息列表。
解析原则：宽容解析，单行失败跳过，整体失败抛异常由 scan 层记 warning。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class Msg:
    role: str          # 'user' | 'assistant'
    text: str
    ts: Optional[float] = None   # epoch seconds


# ——— 产物账本抽取（用户决策：产物导向，会话写过什么文件/commit 是确定性事实）———
# 写文件类工具名（小写）：覆盖 claude/kimi 的 Write/Edit、codex 的 apply_patch 等
_WRITE_TOOL_NAMES = {
    "write", "edit", "multiedit", "notebookedit",
    "apply_patch", "create_file", "str_replace_editor",
}
_PATH_KEYS = ("file_path", "path", "filename", "target_file")
_GIT_COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+(?P<repo>\S+)\s+)?commit\b")
_GIT_MSG_RE = re.compile(r"-m\s*(?P<q>['\"])(?P<msg>.*?)(?P=q)", re.S)
# heredoc 形式：git commit -m "$(cat <<'EOF'\n消息\nEOF\n)"
_GIT_HEREDOC_MSG_RE = re.compile(
    r"-m\s+[\"']?\$\(cat\s+<<\s*'?(?P<tag>[A-Za-z_]+)'?\s*\n(?P<msg>.*?)\n(?P=tag)", re.S)
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Add|Update) File: (.+)")


def _absolutize(p: str, cwd: Optional[str]) -> Optional[str]:
    """相对路径按会话 cwd 归一为绝对路径；归一不了（无 cwd）则丢弃。"""
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        if not cwd:
            return None
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def note_tool_call(rs: "RawSession", name, args) -> None:
    """从一次工具调用抽产物线索：写过的文件路径 + git commit。

    args 为 dict 或 JSON 字符串；宽容处理，任何异常静默跳过（产物是增量信息，
    不允许拖垮会话解析）。
    """
    try:
        if isinstance(args, str):
            raw = args
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(args, dict):
            raw = json.dumps(args, ensure_ascii=False)
        else:
            return
        lname = (name or "").lower()
        if lname in _WRITE_TOOL_NAMES:
            if lname == "apply_patch":
                for m in _PATCH_FILE_RE.finditer(raw):
                    p = _absolutize(m.group(1).strip(), rs.cwd)
                    if p:
                        rs.file_writes.append(p)
            else:
                for k in _PATH_KEYS:
                    v = args.get(k)
                    if isinstance(v, str) and v.strip():
                        p = _absolutize(v.strip(), rs.cwd)
                        if p:
                            rs.file_writes.append(p)
                        break
        # shell 类工具：只认 git commit（用户决策：不做时间窗归因，只记会话里出现的）
        cmd = args.get("command") or args.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if isinstance(cmd, str):
            m = _GIT_COMMIT_RE.search(cmd)
            if m:
                hm = _GIT_HEREDOC_MSG_RE.search(cmd)
                msg_m = hm or _GIT_MSG_RE.search(cmd)
                subject = (msg_m.group("msg").strip().split("\n", 1)[0] if msg_m else "")[:120]
                rs.commits.append({"repo": m.group("repo") or rs.cwd, "subject": subject})
    except Exception:
        pass


@dataclass
class RawSession:
    source: str
    session_id: str
    raw_path: str
    raw_mtime: float
    project: Optional[str] = None
    cwd: Optional[str] = None
    title: Optional[str] = None
    started_at: Optional[float] = None   # epoch seconds
    ended_at: Optional[float] = None
    n_user_msgs: int = 0
    n_assistant_msgs: int = 0
    n_tool_calls: int = 0
    tool_call_ts: list = field(default_factory=list)  # list[float]，按日归属用
    n_input_tokens: Optional[int] = None
    n_output_tokens: Optional[int] = None
    first_user_msg: Optional[str] = None
    messages: list = field(default_factory=list)  # list[Msg]，digest 用，不落库
    file_writes: list = field(default_factory=list)  # list[str] 写过的绝对路径，产物账本用
    commits: list = field(default_factory=list)      # list[dict{repo,subject}]，产物账本用


def iter_jsonl(path: Path) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def looks_like_noise(text: str) -> bool:
    """系统注入类内容不算真实用户消息。"""
    t = text.lstrip()
    prefixes = (
        "<system-reminder", "<permissions instructions>", "<skills_instructions>",
        "<environment_context>", "<codex_internal_context", "<turn_aborted",
        "# AGENTS.md instructions", "<user_info>",
    )
    return t.startswith(prefixes)


def extract_user_query(text: str) -> str | None:
    """workbuddy 风格：<system-reminder ...>...</system-reminder><user_query>真实输入</user_query>
    返回真实用户输入；没有 user_query 包装时返回 None（调用方再走噪声判断）。"""
    import re
    m = re.search(r"<user_query>(.*?)</user_query>", text, re.DOTALL)
    return m.group(1).strip() if m else None


class Adapter:
    source: str = ""

    def discover(self) -> Iterator[Path]:
        """产出候选 session 文件/目录路径。"""
        raise NotImplementedError

    def id_of(self, path: Path) -> str:
        """不解析文件即可推导的 session_id，必须与 parse 产出的 session_id 一致。"""
        return path.stem

    def mtime_of(self, path: Path) -> float:
        """增量水位：不解析文件即可拿到的 mtime。"""
        return path.stat().st_mtime

    def size_of(self, path: Path) -> int:
        """水位辅助：文件大小（目录类源为组成文件大小之和）。"""
        return path.stat().st_size

    def parse(self, path: Path) -> RawSession:
        raise NotImplementedError

    @staticmethod
    def _finish(rs: RawSession) -> RawSession:
        """从 messages 归约统计字段。"""
        rs.n_user_msgs = sum(1 for m in rs.messages if m.role == "user")
        rs.n_assistant_msgs = sum(1 for m in rs.messages if m.role == "assistant")
        if rs.tool_call_ts:
            rs.n_tool_calls = len(rs.tool_call_ts)
        for m in rs.messages:
            if m.role == "user" and rs.first_user_msg is None and not looks_like_noise(m.text):
                rs.first_user_msg = m.text[:500]
        tss = [m.ts for m in rs.messages if m.ts]
        if tss:
            rs.started_at = rs.started_at or min(tss)
            rs.ended_at = max(tss)
        if rs.title is None and rs.first_user_msg:
            rs.title = rs.first_user_msg.split("\n", 1)[0][:80]
        return rs
