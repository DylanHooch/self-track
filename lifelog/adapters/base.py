"""适配器基类与统一数据结构。

每个 agent 源一个 adapter，把各异的会话格式归一成 RawSession + 精简消息列表。
解析原则：宽容解析，单行失败跳过，整体失败抛异常由 scan 层记 warning。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class Msg:
    role: str          # 'user' | 'assistant'
    text: str
    ts: Optional[float] = None   # epoch seconds


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
