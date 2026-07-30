"""tcodex（Codex CLI 系）：~/.tcodex/sessions/YYYY/MM/DD/rollout-*.jsonl

每行 JSON：timestamp(ISO) + type + payload。
- session_meta：cwd / git / cli_version
- response_item payload.type=message：role user(input_text) / assistant(output_text)
- response_item payload.type=function_call / local_shell_call 等：工具调用
- event_msg payload.type=token_count：token 用量（取最后一个）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .base import Adapter, Msg, RawSession, iter_jsonl, looks_like_noise, note_tool_call

_TOOL_ITEM_TYPES = {"function_call", "local_shell_call", "custom_tool_call", "web_search_call"}


def _parse_iso(ts: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _text_of(content) -> str:
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                texts.append(block.get("text", ""))
    return "\n".join(t for t in texts if t)


class TcodexAdapter(Adapter):
    source = "tcodex"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".tcodex"

    def discover(self) -> Iterator[Path]:
        sess = self.root / "sessions"
        if not sess.is_dir():
            return
        yield from sorted(sess.glob("*/*/*/rollout-*.jsonl"))

    def id_of(self, path: Path) -> str:
        # rollout-<ts>-<uuid>：uuid 是最后 5 个 dash 段，与 session_meta.session_id 一致
        return "-".join(path.stem.split("-")[-5:])

    def parse(self, path: Path) -> RawSession:
        rs = RawSession(
            source=self.source,
            session_id=path.stem,
            raw_path=str(path),
            raw_mtime=path.stat().st_mtime,
        )
        for obj in iter_jsonl(path):
            otype = obj.get("type")
            payload = obj.get("payload") or {}
            ts = _parse_iso(obj.get("timestamp", ""))
            if otype == "session_meta":
                rs.session_id = payload.get("session_id") or payload.get("id") or rs.session_id
                rs.cwd = payload.get("cwd")
                if rs.cwd:
                    rs.project = Path(rs.cwd).name
                if ts:
                    rs.started_at = ts
            elif otype == "response_item":
                ptype = payload.get("type")
                if ptype == "message":
                    role = payload.get("role")
                    text = _text_of(payload.get("content"))
                    if role == "user":
                        if text and not looks_like_noise(text):
                            rs.messages.append(Msg("user", text, ts))
                    elif role == "assistant":
                        if text:
                            rs.messages.append(Msg("assistant", text, ts))
                elif ptype in _TOOL_ITEM_TYPES:
                    rs.tool_call_ts.append(ts)
                    note_tool_call(rs, payload.get("name"),
                                   payload.get("arguments") or payload.get("action"))
            elif otype == "event_msg" and payload.get("type") == "token_count":
                info = (payload.get("info") or {}).get("total_token_usage") or {}
                if info:
                    rs.n_input_tokens = info.get("input_tokens")
                    rs.n_output_tokens = info.get("output_tokens")
        return self._finish(rs)
