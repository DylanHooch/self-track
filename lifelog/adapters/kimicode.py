"""kimi-code：~/.kimi-code/sessions/<ws>/session_<uuid>/

- state.json：title / workDir / createdAt（原地重写，不作水位依据）
- agents/main/wire.jsonl：只取主 agent（MVP 决策：子 agent agents/agent-N 不采集，避免重复计数）
  - turn.prompt：真实用户输入（text + time ms epoch）
  - context.append_loop_event：content.part type=text → 助手文本；tool.call → 工具调用
  - usage.record：token 用量（inputOther+inputCacheRead+inputCacheCreation / output）
水位：session 目录内 state.json + agents/*/wire.jsonl 的 mtime 最大值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .base import Adapter, Msg, RawSession, iter_jsonl, looks_like_noise, note_tool_call


class KimiCodeAdapter(Adapter):
    source = "kimi-code"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".kimi-code"

    def discover(self) -> Iterator[Path]:
        sess = self.root / "sessions"
        if not sess.is_dir():
            return
        # 产出 session 目录（含 state.json 者）
        for state in sorted(sess.glob("*/*/state.json")):
            yield state.parent

    @staticmethod
    def _watched_files(path: Path) -> list[Path]:
        # 水位只覆盖真正参与解析的文件（review 修正：子 agent wire 不纳入）
        return [path / "state.json", path / "agents" / "main" / "wire.jsonl"]

    @classmethod
    def _dir_mtime(cls, path: Path) -> float:
        mtimes = [path.stat().st_mtime]
        mtimes += [p.stat().st_mtime for p in cls._watched_files(path) if p.exists()]
        return max(mtimes)

    def id_of(self, path: Path) -> str:
        name = path.name
        return name[len("session_"):] if name.startswith("session_") else name

    def mtime_of(self, path: Path) -> float:
        return self._dir_mtime(path)

    def size_of(self, path: Path) -> int:
        return sum(p.stat().st_size for p in self._watched_files(path) if p.exists())

    def parse(self, path: Path) -> RawSession:
        session_id = path.name
        if session_id.startswith("session_"):
            session_id = session_id[len("session_"):]
        rs = RawSession(
            source=self.source,
            session_id=session_id,
            raw_path=str(path),
            raw_mtime=self._dir_mtime(path),
            project=path.parent.name,
        )
        state = path / "state.json"
        if state.exists():
            try:
                st = json.loads(state.read_text(encoding="utf-8", errors="replace"))
                rs.title = st.get("title") or None
                rs.cwd = st.get("workDir") or None
                created = st.get("createdAt")
                if created:
                    from datetime import datetime
                    try:
                        rs.started_at = datetime.fromisoformat(
                            created.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
            except json.JSONDecodeError:
                pass
        wire = path / "agents" / "main" / "wire.jsonl"
        n_in = n_out = 0
        if wire.exists():
            for obj in iter_jsonl(wire):
                otype = obj.get("type")
                ts = obj.get("time")
                ts = ts / 1000.0 if isinstance(ts, (int, float)) else None
                if otype == "turn.prompt":
                    text = "\n".join(
                        b.get("text", "") for b in (obj.get("input") or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    if text and not looks_like_noise(text):
                        rs.messages.append(Msg("user", text, ts))
                elif otype == "context.append_loop_event":
                    ev = obj.get("event") or {}
                    etype = ev.get("type")
                    if etype == "content.part":
                        part = ev.get("part") or {}
                        if part.get("type") == "text" and part.get("text"):
                            rs.messages.append(Msg("assistant", part["text"], ts))
                    elif etype == "tool.call":
                        rs.tool_call_ts.append(ts)
                        note_tool_call(rs, ev.get("name"), ev.get("args"))
                elif otype == "usage.record":
                    u = obj.get("usage") or {}
                    n_in += u.get("inputOther", 0) + u.get("inputCacheRead", 0) + u.get("inputCacheCreation", 0)
                    n_out += u.get("output", 0)
        if n_in or n_out:
            rs.n_input_tokens, rs.n_output_tokens = n_in, n_out
        return self._finish(rs)
