"""adapter 注册表。codex（原版）未安装，保留位置，发现即跳过。"""
from __future__ import annotations

import os
from pathlib import Path

from .base import Adapter
from .claude import claude, tclaude
from .kimicode import KimiCodeAdapter
from .tcodex import TcodexAdapter
from .workbuddy import WorkbuddyAdapter


def all_adapters() -> list[Adapter]:
    adapters: list[Adapter] = [
        claude(),
        tclaude(),
        TcodexAdapter(),
        KimiCodeAdapter(),
        WorkbuddyAdapter(),
    ]
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    if (codex_home / "sessions").is_dir():
        from .tcodex import TcodexAdapter as _CodexAdapter
        codex = _CodexAdapter(codex_home)
        codex.source = "codex"
        adapters.append(codex)
    return adapters
