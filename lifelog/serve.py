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


def _copy_artifacts(db: DB, ids: list, out_dir: Path):
    """把产物的当前内容复制到 out_dir 并构建 manifest 条目（复制语义，原文件不动；
    commit 类无文件可拷，只进 manifest）。返回 (items, copied, skipped)。"""
    import shutil
    items, copied, skipped = [], 0, 0
    for aid in ids:
        r = db.conn.execute(
            "SELECT kind, name, path, path_override, repo, first_day, last_day, note "
            "FROM artifacts WHERE id=?", (aid,)).fetchone()
        if not r:
            continue
        entry = {"id": aid, "kind": r["kind"], "name": r["name"],
                 "first_day": r["first_day"], "last_day": r["last_day"],
                 "note": r["note"]}
        if r["kind"] == "commit":
            entry["repo"] = r["repo"]
            items.append(entry)
            continue
        src = r["path_override"] or r["path"]
        entry["original_path"] = r["path"]
        if not src or not Path(src).is_file():
            entry["skipped"] = "文件已不存在"
            items.append(entry)
            skipped += 1
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / Path(src).name
        if dest.exists():
            dest = out_dir / f"{aid}-{Path(src).name}"
        shutil.copy2(src, dest)
        entry["copied_as"] = dest.name
        items.append(entry)
        copied += 1
    return items, copied, skipped


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 安静（API 错误另有 stderr 日志）

    def end_headers(self):
        # 本地应用迭代频繁：不发 Cache-Control 时 Chrome 启发式缓存 js/css，
        # 用户刷新看不到新版（踩过）。no-cache = 每次都回源校验（本机无成本）。
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/deep-dive/status"):
            from urllib.parse import parse_qs, urlparse
            label = parse_qs(urlparse(self.path).query).get("label", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
                self._json(400, {"ok": False, "error": "bad label"})
                return
            self._json(200, {"ok": True, "state": _dispatch_status(label)})
            return
        if self.path.startswith("/api/artifact/raw"):
            self._get_artifact_raw()
            return
        super().do_GET()

    # 预览用内容类型（file:// 打开不了本地文件，预览只能走 serve）
    _RAW_CONTENT_TYPES = {
        ".md": "text/plain; charset=utf-8", ".markdown": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8",
        ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    }

    def _get_artifact_raw(self):
        """按产物 id 流式返回文件内容。只放行 artifacts 表登记的路径（含用户补的
        path_override），不存在任意路径读取面。"""
        from urllib.parse import parse_qs, urlparse
        try:
            aid = int(parse_qs(urlparse(self.path).query).get("id", [""])[0])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad id"})
            return
        db = DB(DATA / "lifelog.sqlite")
        try:
            r = db.conn.execute(
                "SELECT path, path_override FROM artifacts WHERE id=? AND kind='file'",
                (aid,)).fetchone()
        finally:
            db.close()
        src = (r["path_override"] or r["path"]) if r else None
        p = Path(src) if src else None
        if not p or not p.is_file():
            self._json(404, {"ok": False, "error": "文件已不存在"})
            return
        ctype = self._RAW_CONTENT_TYPES.get(p.suffix.lower(),
                                            "application/octet-stream")
        try:
            data = p.read_bytes()
        except OSError as e:
            self._json(500, {"ok": False, "error": str(e)[:200]})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 预览 html 产物：允许脚本（报告常有内联图表），但 iframe sandbox 不含
        # allow-same-origin，父页与本服务均不可达
        self.send_header("Content-Security-Policy", "sandbox allow-scripts")
        self.end_headers()
        self.wfile.write(data)

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

    def _read_json(self) -> dict | None:
        """校验 Content-Type/长度并解析 JSON body；不合法时响应 None 已回 4xx。"""
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY:
                raise ValueError("bad length")
            return json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"ok": False, "error": "bad request"})
            return None

    def do_POST(self):
        if not self._origin_ok():
            self._json(403, {"ok": False, "error": "forbidden origin"})
            return
        if self.path == "/api/deep-dive":
            self._post_deep_dive()
        elif self.path == "/api/artifact/path":
            self._post_artifact_path()
        elif self.path == "/api/pack":
            self._post_pack()
        elif self.path == "/api/pack-session":
            self._post_pack_session()
        else:
            self._json(404, {"ok": False, "error": "unknown endpoint"})

    def _post_artifact_path(self):
        """用户补录产物被移动后的新路径（产物一般是用户自己移的，他知道在哪）。"""
        body = self._read_json()
        if body is None:
            return
        try:
            aid = int(body["id"])
            path = str(body["path"]).strip()
            assert path.startswith("/") and len(path) <= 500
        except (KeyError, ValueError, TypeError, AssertionError):
            self._json(400, {"ok": False, "error": "bad request"})
            return
        if not Path(path).is_file():
            self._json(400, {"ok": False, "error": f"路径不存在或不是文件：{path}"})
            return
        db = DB(DATA / "lifelog.sqlite")
        try:
            cur = db.conn.execute(
                "UPDATE artifacts SET path_override=? WHERE id=? AND kind='file'", (path, aid))
            db.conn.commit()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "找不到该文件产物"})
                return
        finally:
            db.close()
        self._json(200, {"ok": True, "display_path": path})

    def _post_pack(self):
        """一键打包：把产物的当前内容复制到 ~/Deliverables/<时间戳>/ + manifest.json。"""
        body = self._read_json()
        if body is None:
            return
        try:
            ids = [int(x) for x in body["ids"]][:500]
            assert ids
        except (KeyError, ValueError, TypeError, AssertionError):
            self._json(400, {"ok": False, "error": "bad request"})
            return
        from datetime import datetime
        out_dir = Path.home() / "Deliverables" / datetime.now().strftime("%Y%m%d-%H%M%S")
        db = DB(DATA / "lifelog.sqlite")
        try:
            manifest, copied, skipped = _copy_artifacts(db, ids, out_dir)
            # 挂名会话进 manifest（产物的背景）
            links = db.conn.execute(
                f"""SELECT l.artifact_id, s.source, s.session_id, s.title
                    FROM artifact_sessions l
                    JOIN sessions s ON s.source=l.source AND s.session_id=l.session_id
                    WHERE l.artifact_id IN ({','.join('?' * len(ids))})""", ids).fetchall()
            by_aid = {}
            for l in links:
                by_aid.setdefault(l["artifact_id"], []).append(
                    {"source": l["source"], "session_id": l["session_id"], "title": l["title"]})
            for entry in manifest:
                entry["sessions"] = by_aid.get(entry["id"], [])
        finally:
            db.close()
        if copied or manifest:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_text(
                json.dumps({"packed_at": datetime.now().isoformat(timespec="seconds"),
                            "items": manifest}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        self._json(200, {"ok": True, "dir": str(out_dir), "copied": copied, "skipped": skipped})

    def _post_pack_session(self):
        """打包单个会话：会话详情页 + 该会话全部产物 → ~/Deliverables/<目录名>/。
        目录名取用户输入（可空），默认会话标题；消毒路径分隔/控制符，撞名加 -2/-3。"""
        body = self._read_json()
        if body is None:
            return
        try:
            source, sid = str(body["source"])[:64], str(body["session_id"])[:128]
            name = str(body.get("name") or "").strip()[:120]
        except Exception:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        import shutil
        from datetime import datetime
        from .deepdive import safe_page_key
        from .adapters import all_adapters
        db = DB(DATA / "lifelog.sqlite")
        try:
            row = db.conn.execute(
                "SELECT * FROM sessions WHERE source=? AND session_id=?",
                (source, sid)).fetchone()
            if not row:
                self._json(404, {"ok": False, "error": f"找不到会话 {source}:{sid}"})
                return
            ids = [r["artifact_id"] for r in db.conn.execute(
                "SELECT artifact_id FROM artifact_sessions WHERE source=? AND session_id=?",
                (source, sid)).fetchall()]
            base = re.sub(r"[/\\\x00-\x1f]+", "_", name or row["title"] or "").strip(" .")
            base = base[:80] or safe_page_key(source, sid)
            root = Path.home() / "Deliverables"
            out_dir, n = root / base, 2
            while out_dir.exists():
                out_dir = root / f"{base}-{n}"
                n += 1
            out_dir.mkdir(parents=True)
            page_file = None
            page = WEB / "deep" / f"{safe_page_key(source, sid)}.html"
            # 打包前重渲深度页：磁盘页可能是旧代码/旧数据的投影
            # （踩过：旧版打出来的 session.html 没有完整对话区）
            from .deepdive import load_manifest, render_page, session_artifacts
            entry = load_manifest(WEB).get(safe_page_key(source, sid))
            if entry and not entry.get("analysis"):
                entry = None
            messages, stale = None, 0
            try:
                adapter = {a.source: a for a in all_adapters()}[source]
                rs = adapter.parse(Path(row["raw_path"]))
                messages = rs.messages
                if entry:
                    stale = max(0, len(rs.messages) - entry.get("n_messages", 0))
            except Exception:
                if entry:
                    stale = -1
            render_page(row, WEB, entry, stale, messages,
                        session_artifacts(db, source, sid))
            if page.is_file():
                shutil.copy2(page, out_dir / "session.html")
                page_file = "session.html"
            # 原会话记录（复制语义）：jsonl 源拷单文件；kimi-code 是目录，拷
            # state.json + agents/main/wire.jsonl（与 adapter 解析范围一致）
            raw_files = []
            try:
                # Path() 挪进 try：raw_path 混入 NUL 等坏值时 ValueError 也有兜底（review P2）
                raw = Path(row["raw_path"]) if row["raw_path"] else None
                if raw and raw.is_file():
                    dst = out_dir / "raw" / raw.name
                    dst.parent.mkdir(exist_ok=True)
                    shutil.copy2(raw, dst)
                    raw_files.append(raw.name)
                elif raw and raw.is_dir():
                    for rel in ("state.json", "agents/main/wire.jsonl"):
                        src = raw / rel
                        if src.is_file():
                            dst = out_dir / "raw" / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                            raw_files.append(rel)
                elif raw:
                    print(f"  warning: 原会话已不在磁盘: {raw}", file=sys.stderr)
            except (OSError, ValueError) as e:
                print(f"  warning: 打包原会话失败: {e}", file=sys.stderr)
            items, copied, skipped = _copy_artifacts(db, ids, out_dir)
            (out_dir / "manifest.json").write_text(
                json.dumps({"packed_at": datetime.now().isoformat(timespec="seconds"),
                            "session": {"source": source, "session_id": sid,
                                        "title": row["title"], "cwd": row["cwd"],
                                        "started_at": row["started_at"],
                                        "page_file": page_file,
                                        "raw_files": raw_files},
                            "items": items}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        finally:
            db.close()
        self._json(200, {"ok": True, "dir": str(out_dir), "copied": copied,
                         "skipped": skipped, "raw_files": raw_files})

    def _post_deep_dive(self):
        body = self._read_json()
        if body is None:
            return
        try:
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

