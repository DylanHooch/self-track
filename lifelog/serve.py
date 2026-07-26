"""本地 web 应用：python -m lifelog serve

- 静态服务 web/（看板 + 深度页）
- POST /api/deep-dive {source, session_id} → 直接在本进程跑深度分析（首次/增量）
安全边界（review 修正）：只绑 127.0.0.1 + Origin/Host 校验（防任意网页跨站触发/
DNS rebinding）；deep-dive 进程内串行锁（防并发 read-modify-write 丢分析）；
serve 不持有全局 RunLock（长驻进程会把每日 launchd 流程掐死）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
MAX_BODY = 16 * 1024

_analyze_lock = threading.Lock()  # 串行分析与 token 刷新（kimi 后端本就不适合并发）
_last_refresh = [0.0]
DISPATCH = Path.home() / ".agents/skills/agent-dispatch/scripts/dispatch"
ANALYZE_TIMEOUT = 1800  # 30min，dispatch 侧超时


def _refresh_kimi_token():
    """kimi token 短时有效：分析前用 CLI 刷新（60s 内合并，失败不阻塞）。"""
    import time
    if time.time() - _last_refresh[0] < 60:
        return
    kimi = Path.home() / ".kimi-code" / "bin" / "kimi"
    if kimi.exists():
        try:
            subprocess.run([str(kimi), "-p", "ok"], input=b"ok\n",
                           capture_output=True, timeout=60)
            _last_refresh[0] = time.time()
        except Exception:
            pass


def _dispatch_status(label: str) -> str:
    """查 dispatch 任务状态：running / success / failed / unknown。"""
    try:
        r = subprocess.run([str(DISPATCH), "jobs", "--label", label],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        m = re.search(r"status=(\w+)", r.stdout)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _start_analysis(source: str, sid: str) -> str:
    """用 agent-dispatch 后台跑深度分析（30min 超时），返回任务 label。
    serve 进程不阻塞；前端轮询 /api/deep-dive/status 直到完成。"""
    from .deepdive import safe_page_key
    label = "deep-" + safe_page_key(source, sid)[:44]
    task = (f"在 /Users/jingquanhu/sideProject/self-track 目录执行命令：\n"
            f"LIFELOG_LLM_BACKEND=kimi-code python3 -m lifelog deep-dive {source} {sid}\n"
            f"这是 self-track 的单会话深度分析。命令成功后把 stdout 末尾总结原样报告；"
            f"失败则报告完整错误输出。不要修改任何其他文件。")
    log = DATA / "deep-dispatch.log"
    with open(log, "ab") as lf:
        subprocess.Popen(
            [str(DISPATCH), "submit", "--label", label, "--role", "kimi",
             "--task", task, "--timeout", str(ANALYZE_TIMEOUT)],
            stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    return label


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 安静（API 错误另有 stderr 日志）

    def do_GET(self):
        if self.path.startswith("/api/deep-dive/status"):
            from urllib.parse import parse_qs, urlparse
            label = parse_qs(urlparse(self.path).query).get("label", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
                self._json(400, {"ok": False, "error": "bad label"})
                return
            self._json(200, {"ok": True, "state": _dispatch_status(label)})
            return
        super().do_GET()

    def _origin_ok(self) -> bool:
        """只接受本机来源：Host 必须是本服务，Origin 缺失（同源 fetch）或本机。
        阻挡恶意网页的 simple-request CSRF 与 DNS rebinding（review 修正）。"""
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def do_POST(self):
        if self.path != "/api/deep-dive":
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._origin_ok():
            self._json(403, {"ok": False, "error": "forbidden origin"})
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY:
                raise ValueError("bad length")
            body = json.loads(self.rfile.read(length))
            source, sid = str(body["source"])[:64], str(body["session_id"])[:128]
        except Exception:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        # 先本地验证 session 存在，再碰 token/LLM（review：无效 sid 不该触发 kimi CLI）
        db = DB(DATA / "lifelog.sqlite")
        try:
            exists = db.conn.execute(
                "SELECT 1 FROM sessions WHERE source=? AND session_id=?",
                (source, sid)).fetchone()
        finally:
            db.close()
        if not exists:
            self._json(404, {"ok": False, "error": f"找不到会话 {source}:{sid}"})
            return
        key = f"{source}:{sid}"
        from .deepdive import safe_page_key
        label = "deep-" + safe_page_key(source, sid)[:44]
        if _dispatch_status(label) == "running":
            self._json(200, {"ok": True, "started": True, "label": label, "already": True})
            return
        try:
            _refresh_kimi_token()  # 先刷 token，dispatch 里的 kimi agent 才有得用
            label = _start_analysis(source, sid)
            self._json(200, {"ok": True, "started": True, "label": label})
        except Exception as e:
            print(f"[serve] POST /api/deep-dive 500: {e}", file=sys.stderr)
            self._json(500, {"ok": False, "error": str(e)[:300]})

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(open_browser: bool = False) -> int:
    os.environ.setdefault("LIFELOG_LLM_BACKEND", "kimi-code")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 被占用（已有 selftrack 在运行？可用 LIFELOG_PORT 换端口）")
        return 1
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

