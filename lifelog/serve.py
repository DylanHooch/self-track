"""本地 web 应用：python -m lifelog serve

- 静态服务 web/（看板 + 深度页）
- POST /api/deep-dive {source, session_id} → 直接在本进程跑深度分析（首次/增量）
只绑 127.0.0.1，纯标准库。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import DB
from .deepdive import deep_dive

PORT = int(os.environ.get("LIFELOG_PORT", "8791"))
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"


def _refresh_kimi_token():
    """kimi token 短时有效：分析前用 CLI 刷新一次（失败不阻塞，backend 会自己报错）。"""
    kimi = Path.home() / ".kimi-code" / "bin" / "kimi"
    if kimi.exists():
        try:
            subprocess.run([str(kimi), "-p", "ok"], input=b"ok\n",
                           capture_output=True, timeout=60)
        except Exception:
            pass


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 安静

    def do_POST(self):
        if self.path != "/api/deep-dive":
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            source, sid = body["source"], body["session_id"]
        except Exception:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        try:
            os.environ.setdefault("LIFELOG_LLM_BACKEND", "kimi-code")
            _refresh_kimi_token()
            db = DB(DATA / "lifelog.sqlite")
            try:
                out = deep_dive(db, source, sid, WEB)
            finally:
                db.close()
            self._json(200, {"ok": True, "page": out.name})
        except SystemExit as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:300]})

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(open_browser: bool = True) -> int:
    os.environ.setdefault("LIFELOG_LLM_BACKEND", "kimi-code")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"self-track 本地应用：{url}（Ctrl+C 停止）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
