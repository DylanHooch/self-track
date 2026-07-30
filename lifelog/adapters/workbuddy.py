"""workbuddy（Electron 应用）：~/.workbuddy/projects/<proj>/<conversationId>.jsonl

每行 JSON：
- type=message：role user(content[].input_text) / assistant(content[].output_text)，ms epoch timestamp
- type=function_call / function_call_result：工具调用
- type=ai-title：会话标题
~/.workbuddy/app/sessions.json 提供 conversationId → workDir/startedAt 映射（每次运行新鲜读取，不作水位）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .base import Adapter, Msg, RawSession, extract_user_query, iter_jsonl, looks_like_noise, note_tool_call


class WorkbuddyAdapter(Adapter):
    source = "workbuddy"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".workbuddy"

    def discover(self) -> Iterator[Path]:
        proj = self.root / "projects"
        if not proj.is_dir():
            return
        yield from sorted(proj.glob("*/*.jsonl"))

    def _load_session_index(self) -> dict:
        idx = self.root / "app" / "sessions.json"
        try:
            data = json.loads(idx.read_text(encoding="utf-8", errors="replace"))
            return {s["conversationId"]: s for s in data.get("sessions", [])}
        except (OSError, json.JSONDecodeError, KeyError):
            return {}

    def parse(self, path: Path) -> RawSession:
        rs = RawSession(
            source=self.source,
            session_id=path.stem,
            raw_path=str(path),
            raw_mtime=path.stat().st_mtime,
            project=path.parent.name,
        )
        meta = self._load_session_index().get(rs.session_id)
        if meta:
            rs.cwd = meta.get("workDir")
            started = meta.get("startedAt")
            if started:
                from datetime import datetime
                try:
                    rs.started_at = datetime.fromisoformat(
                        started.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
        for obj in iter_jsonl(path):
            otype = obj.get("type")
            # jsonl 行内自带 sessionId/cwd，作为 sessions.json 映射缺失时的 fallback
            if obj.get("sessionId"):
                rs.session_id = obj["sessionId"]
            if obj.get("cwd") and not rs.cwd:
                rs.cwd = obj["cwd"]
        for obj in iter_jsonl(path):
            otype = obj.get("type")
            ts = obj.get("timestamp")
            ts = ts / 1000.0 if isinstance(ts, (int, float)) else None
            if otype == "ai-title":
                rs.title = obj.get("aiTitle") or obj.get("title") or rs.title
            elif otype == "message":
                role = obj.get("role")
                texts = [
                    c.get("text", "") for c in (obj.get("content") or [])
                    if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
                ]
                text = "\n".join(t for t in texts if t)
                if role == "user":
                    # 真实输入包在 <user_query> 里，外层是 system-reminder 壳（review P0 修正）
                    real = extract_user_query(text)
                    if real is not None:
                        text = real
                    if text and not looks_like_noise(text):
                        rs.messages.append(Msg("user", text, ts))
                elif role == "assistant" and text:
                    rs.messages.append(Msg("assistant", text, ts))
            elif otype == "function_call":
                rs.tool_call_ts.append(ts)
                note_tool_call(rs, obj.get("name"),
                               obj.get("arguments") or obj.get("args"))
        return self._finish(rs)
