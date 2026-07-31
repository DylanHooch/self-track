"""Claude Code 系：claude (~/.claude) 与 tclaude (~/.tclaude)，格式同构。

每行 JSON：type=user/assistant，message.role/content，ISO timestamp，sessionId，cwd。
user 消息 content 为 str（真实输入）或 list（含 tool_result / 系统注入）。
assistant content list 中 tool_use 块计为工具调用。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .base import Adapter, Msg, RawSession, iter_jsonl, looks_like_noise, note_tool_call, summarize_tool


def _parse_iso(ts: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _text_from_content(content) -> tuple[str, int, list]:
    """返回 (text, n_tool_use, tool_calls)。tool_calls = [(name, input)]，产物账本用。"""
    if isinstance(content, str):
        return content, 0, []
    texts, n_tools, tool_calls = [], 0, []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use":
                n_tools += 1
                tool_calls.append((block.get("name"), block.get("input")))
    return "\n".join(t for t in texts if t), n_tools, tool_calls


class ClaudeLikeAdapter(Adapter):
    def __init__(self, source: str, root: Path):
        self.source = source
        self.root = root

    def discover(self) -> Iterator[Path]:
        proj_root = self.root / "projects"
        if not proj_root.is_dir():
            return
        yield from sorted(proj_root.glob("*/*.jsonl"))

    def parse(self, path: Path) -> RawSession:
        rs = RawSession(
            source=self.source,
            session_id=path.stem,
            raw_path=str(path),
            raw_mtime=path.stat().st_mtime,
            project=path.parent.name,
        )
        # 同一个 message.id 会被拆成多行（thinking/text/tool_use 各一行），
        # 必须按 id 归并，否则 n_assistant_msgs 系统性高估。
        assistant_parts: dict[str, list[str]] = {}
        assistant_ts: dict[str, Optional[float]] = {}
        seen_user_uuids: set[str] = set()
        first_ts: Optional[float] = None  # 无消息 session 的 started_at 兜底（review 修正）
        for obj in iter_jsonl(path):
            otype = obj.get("type")
            if obj.get("sessionId"):
                rs.session_id = obj["sessionId"]
            if obj.get("cwd") and not rs.cwd:
                rs.cwd = obj["cwd"]
            ts = _parse_iso(obj.get("timestamp", ""))
            if ts is not None and (first_ts is None or ts < first_ts):
                first_ts = ts
            if otype == "ai-title" and obj.get("aiTitle"):
                rs.title = obj["aiTitle"]
            if otype not in ("user", "assistant"):
                continue
            if obj.get("isSidechain"):
                continue  # 子 agent 侧链不单算，避免重复计数（MVP 决策）
            msg = obj.get("message") or {}
            text, n_tools, tool_calls = _text_from_content(msg.get("content"))
            rs.tool_call_ts.extend([ts] * n_tools)
            for tname, tinput in tool_calls:
                note_tool_call(rs, tname, tinput)
                kind, summary = summarize_tool(tname, tinput)
                rs.messages.append(Msg("tool", "", ts, kind, tname or "", summary))
            if otype == "user":
                uuid = obj.get("uuid")
                if not text or looks_like_noise(text):
                    continue
                if uuid and uuid in seen_user_uuids:
                    continue
                if uuid:
                    seen_user_uuids.add(uuid)
                rs.messages.append(Msg("user", text, ts))
            else:
                mid = msg.get("id") or obj.get("uuid") or str(len(assistant_parts))
                if text:
                    assistant_parts.setdefault(mid, []).append(text)
                    assistant_ts.setdefault(mid, ts)
        for mid, parts in assistant_parts.items():
            rs.messages.append(Msg("assistant", "\n".join(parts), assistant_ts.get(mid)))
        rs.messages.sort(key=lambda m: (m.ts is None, m.ts or 0))
        rs = self._finish(rs)
        if rs.started_at is None:
            rs.started_at = first_ts
        return rs


def claude() -> ClaudeLikeAdapter:
    return ClaudeLikeAdapter("claude", Path.home() / ".claude")


def tclaude() -> ClaudeLikeAdapter:
    return ClaudeLikeAdapter("tclaude", Path.home() / ".tclaude")
