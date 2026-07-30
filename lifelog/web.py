"""前端构建：stats/daily/*.json → web/index.html（本地零外发，file:// 直开）。

设计（docs/01 D5 + 2026-07-29 3D 重构）：
- 数据内联为 JSON，转义 < > U+2028 U+2029 防 `</script>` 注入；只内联最近 90 天。
- 本模板只是壳：样式在 web/style.css，逻辑在 web/app.js，3D hero 在 web/scene3d.js
  （three.js r147 vendor 在 web/vendor/，行者剪影内联在 web/sprite-walker.js）。
- 「小人在路上」：3D 暮色山径——线框山脉 + 发光小径 + 数据路碑（可点击切日期）
  + 行者剪影 + 旋转灯塔光束；WebGL 不可用时回退海报 + 2D 路碑。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .aggregate import atomic_write_text
from .db import DB

MAX_DAYS = 90


def _safe_inline_json(obj) -> str:
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def _git_last_commit(cwd: str) -> str | None:
    """项目目录的 git 最后提交时间（只读探测，落地证据用）。
    `-- .` 限定到该路径，避免子目录拿到外层仓库无关提交（review 修正）。"""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "log", "-1", "--format=%cI", "--", "."],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def compute_projects(db: DB) -> list[dict]:
    """按 cwd 聚合项目：会话数、活跃区间（session_day_stats 事实层）、热点标签、git 证据。

    review 修正：活跃区间取跨日真实活动；伪项目（WorkBuddy 临时目录/tmp）过滤；
    git 探测并发 + 路径限定；cwd realpath 归一。
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    home = os.path.expanduser("~")

    def normalize(cwd: str) -> str | None:
        if not cwd:
            return None
        if cwd.startswith("~"):
            cwd = home + cwd[1:]
        cwd = cwd.rstrip("/") or "/"   # '/' rstrip 后是空串，realpath('') 会污染成进程 cwd
        if cwd in ("/", "/tmp", "/private/tmp") or cwd.startswith("/private/tmp/"):
            return None
        cwd = os.path.realpath(cwd)
        # 只过滤 WorkBuddy 的时间戳临时目录（YYYY-MM-DD-HH-MM-SS），不误伤真实项目
        if cwd.startswith(home + "/WorkBuddy/") \
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", Path(cwd).name):
            return None
        return cwd

    rows = db.conn.execute(
        "SELECT cwd, source, digest_json, started_at FROM sessions WHERE cwd IS NOT NULL AND cwd != ''"
    ).fetchall()
    # 活跃区间用事实层 session_day_stats（跨日会话真实活动范围）
    day_rows = db.conn.execute(
        """SELECT s.cwd AS cwd, MIN(d.day) AS first_day, MAX(d.day) AS last_day
           FROM sessions s JOIN session_day_stats d
             ON s.source=d.source AND s.session_id=d.session_id
           WHERE s.cwd IS NOT NULL AND s.cwd != '' GROUP BY s.cwd"""
    ).fetchall()
    activity = {}
    for r in day_rows:
        c = normalize(r["cwd"])
        if not c:
            continue
        cur = activity.get(c)
        activity[c] = (min(cur[0], r["first_day"]), max(cur[1], r["last_day"])) if cur \
            else (r["first_day"], r["last_day"])

    projects: dict[str, dict] = {}
    for r in rows:
        cwd = normalize(r["cwd"])
        if not cwd:
            continue
        p = projects.setdefault(cwd, {
            "name": Path(cwd).name or cwd, "cwd": cwd, "n_sessions": 0,
            "sources": set(), "labels": {},
        })
        p["n_sessions"] += 1
        p["sources"].add(r["source"])
        if r["digest_json"]:
            try:
                card = json.loads(r["digest_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(card, dict):
                continue
            labels = card.get("hotspot_labels")
            if not isinstance(labels, list):  # int/str 等异常结构直接跳过（review 修正）
                continue
            for label in labels:
                if isinstance(label, str):
                    p["labels"][label] = p["labels"].get(label, 0) + 1

    out = []
    cwds = [c for c in projects if Path(c).is_dir()]
    with ThreadPoolExecutor(max_workers=8) as pool:  # 并发探测，避免串行 N×timeout
        git_results = dict(zip(cwds, pool.map(_git_last_commit, cwds)))
    for cwd, p in projects.items():
        exists = Path(cwd).is_dir()
        first, last = activity.get(cwd, ("", ""))
        sorted_labels = sorted(p["labels"].items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({
            "name": p["name"], "cwd": cwd,
            "n_sessions": p["n_sessions"],
            "sources": sorted(p["sources"]),
            "labels": [label for label, _ in sorted_labels[:6]],
            "labels_all": [label for label, _ in sorted_labels[:30]],
            "first": first, "last": last,
            "exists": exists,
            "git_last": git_results.get(cwd) if exists else None,
        })
    out.sort(key=lambda x: (x["last"] or "", x["n_sessions"]), reverse=True)
    return out


def compute_artifacts(db: DB) -> list[dict]:
    """产物账本投影：文件/commit + 挂名会话 + 构建时点的存在性探测。

    用户决策：文件被删 → 保留名字与简介，不再展示路径；被移动 → 用用户补的
    path_override 探测与展示（标 moved）。
    """
    rows = db.conn.execute(
        """SELECT a.id, a.kind, a.name, a.path, a.repo, a.first_day, a.last_day,
                  a.note, a.head, a.path_override,
                  s.source AS s_source, s.session_id AS s_sid, s.title AS s_title
           FROM artifacts a
           JOIN artifact_sessions l ON l.artifact_id = a.id
           JOIN sessions s ON s.source = l.source AND s.session_id = l.session_id
           ORDER BY a.last_day DESC, a.first_day DESC, a.id DESC"""
    ).fetchall()
    by_id: dict[int, dict] = {}
    for r in rows:
        a = by_id.setdefault(r["id"], {
            "id": r["id"], "kind": r["kind"], "name": r["name"],
            "repo": r["repo"], "first_day": r["first_day"], "last_day": r["last_day"],
            "note": r["note"], "head": r["head"], "sessions": [],
            "_raw_path": r["path"], "_override": r["path_override"],
        })
        a["sessions"].append({"source": r["s_source"], "session_id": r["s_sid"],
                              "title": r["s_title"]})
    out = []
    for a in by_id.values():
        raw, override = a.pop("_raw_path"), a.pop("_override")
        if a["kind"] == "file":
            eff = override or raw
            a["exists"] = bool(eff) and Path(eff).is_file()
            a["moved"] = bool(override) and a["exists"]
            a["display_path"] = eff if a["exists"] else None
        out.append(a)
    return out


def build_web(db: DB, stats_dir: Path, web_dir: Path, max_days: int = MAX_DAYS) -> Path:
    daily_dir = stats_dir / "daily"
    days = sorted(p.stem for p in daily_dir.glob("*.json"))[-max_days:]
    payload_days = []
    for day in days:
        try:
            payload_days.append(json.loads((daily_dir / f"{day}.json").read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    # 3D 重构后 index.html 依赖同目录静态文件；缺了就是空壳页，必须报出来（review 修正）
    missing = [rel for rel in ("style.css", "app.js", "scene3d.js", "sprite-walker.js",
                               "vendor/three.min.js", "assets/mark.png", "assets/poster.png")
               if not (web_dir / rel).exists()]
    if missing:
        import sys
        print(f"[build-web] 警告：{web_dir} 缺静态依赖：{', '.join(missing)}", file=sys.stderr)
    # 深度页：为全部 session 重建（未分析=stub 页，已分析=完整页带陈旧标记）。
    # 页面是 manifest/DB 的投影，可整体重建；分析本身只在用户点「首次/增量分析」时发生。
    from .deepdive import render_all_pages
    render_all_pages(db, web_dir)
    payload = {"days": payload_days, "projects": compute_projects(db),
               "artifacts": compute_artifacts(db),
               "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")}
    html = TEMPLATE.replace("/*__DATA__*/", _safe_inline_json(payload))
    # 静态资源 cache-busting：发版后浏览器可能还抱着旧 app.js 不放（踩过：
    # 旧 serve 无 no-cache 头时 Chrome 启发式缓存）。?v=mtime 让 URL 随内容变。
    # SimpleHTTPRequestHandler.translate_path 会丢弃 query，本地服务不受影响。
    for rel in ("style.css", "app.js", "scene3d.js", "sprite-walker.js"):
        f = web_dir / rel
        if f.exists():
            html = html.replace(f'src="{rel}"', f'src="{rel}?v={int(f.stat().st_mtime)}"')
            html = html.replace(f'href="{rel}"', f'href="{rel}?v={int(f.stat().st_mtime)}"')
    out = web_dir / "index.html"
    atomic_write_text(out, html)
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>在路上 · 自我跟踪</title>
<link rel="icon" href="assets/mark.png">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="hero" id="hero">
  <div id="scene3d"></div>
  <div class="hero-fallback">
    <div class="journey" id="journey2d">
      <div id="journey2d-inner" style="position:absolute;inset:0"></div>
      <div class="road"></div>
      <div class="walker">
        <svg viewBox="0 0 34 48" width="34" height="48">
          <circle cx="17" cy="8" r="5" fill="none" stroke="#ffd98f" stroke-width="2"/>
          <line x1="17" y1="13" x2="17" y2="30" stroke="#ffd98f" stroke-width="2"/>
          <line x1="17" y1="17" x2="9" y2="24" stroke="#ffd98f" stroke-width="2"/>
          <line x1="17" y1="17" x2="25" y2="22" stroke="#ffd98f" stroke-width="2"/>
          <line class="leg" x1="17" y1="30" x2="12" y2="44" stroke="#ffd98f" stroke-width="2"/>
          <line class="leg back" x1="17" y1="30" x2="22" y2="44" stroke="#ffd98f" stroke-width="2"/>
        </svg>
      </div>
    </div>
  </div>
  <div class="hero-overlay">
    <img class="mark" src="assets/mark.png" alt="">
    <h1>在路上</h1>
    <div class="sub" id="subtitle"></div>
  </div>
  <div class="hero-tip" id="sceneTip"></div>
  <div class="hero-hint">点击路碑切换到那一天</div>
</header>

<main>
<div class="tabbar">
  <button data-tab="daily" class="on">日报</button>
  <button data-tab="ideas">想法看板</button>
  <button data-tab="projects">项目</button>
  <button data-tab="promises">承诺</button>
  <button data-tab="artifacts">产物</button>
</div>

<div id="view-daily">
<div class="kpis" id="kpis"></div>

<section id="daySection">
  <h2>这一天</h2>
  <div class="day-picker" id="dayPicker"></div>
  <div class="narrative" id="narrative"></div>
  <textarea class="annot" id="annot" placeholder="批注：给自己的话（只存在本机浏览器）…"></textarea>
</section>

<section>
  <h2>会话 <span style="font-weight:400;color:var(--ink-dim);font-size:12px">点击卡片复制 resume 指令 · 点「深度」看单会话分析</span></h2>
  <input class="filter" id="sessGlobalFilter" placeholder="全局搜索：跨所有日期的会话（标题 / cwd / 来源）…">
  <div id="sessGlobalHint" style="font-size:12px;color:var(--ink-dim);margin:-6px 0 10px"></div>
  <div class="sess-bar">
    <input class="filter" id="sessFilter" placeholder="筛选当日：标题 / cwd（支持 ~）/ 来源…">
    <select class="viewsel" id="sessView">
      <option value="all">当日所有（活跃过）</option>
      <option value="created">当日创建</option>
    </select>
  </div>
  <div class="sess" id="sessions"></div>
</section>
</div><!-- /view-daily -->

<div id="view-ideas" style="display:none">
  <input class="filter" id="ideaFilter" placeholder="筛选想法：标题 / 描述 / 状态（未落地、已落地…）" style="margin-top:20px">
  <div class="idea-grid" id="ideaBoard"></div>
  <div id="ideaView" style="display:none">
    <p><a class="back" href="#" id="ideaBack">← 返回想法看板</a></p>
    <div class="idea-detail" id="ideaDetail"></div>
    <div id="ideaEvidence" style="margin-top:12px"></div>
    <h2 style="margin-top:28px">相关会话</h2>
    <div class="sess" id="ideaSessions"></div>
  </div>
  <div id="trashWrap" style="display:none">
    <h2 style="margin-top:32px">回收站 <span style="font-weight:400;color:var(--ink-dim);font-size:12px">归档的想法在这里，可以恢复</span></h2>
    <div id="trashList"></div>
  </div>
</div>

<div id="view-promises" style="display:none">
  <div class="kpis" id="promiseKpis" style="margin-top:20px"></div>
  <div id="promiseList"></div>
</div>

<div id="view-projects" style="display:none">
  <div class="idea-grid" id="projectBoard" style="margin-top:20px"></div>
</div>

<div id="view-artifacts" style="display:none">
  <div class="art-type-bar" id="artTypeBar">
    显示：
    <label><input type="checkbox" data-art-type="doc" checked> 文档</label>
    <label><input type="checkbox" data-art-type="img" checked> 图片</label>
    <label><input type="checkbox" data-art-type="video" checked> 视频</label>
    <label><input type="checkbox" data-art-type="commit"> commit</label>
  </div>
  <input class="filter" id="artFilter" placeholder="筛选产物：文件名 / 路径 / 简介…">
  <div id="artHint" style="font-size:12px;color:var(--ink-dim);margin-bottom:10px;display:none">
    打包与补路径需要通过本地应用访问（selftrack 命令起的 :8791），file:// 直开时只读。</div>
  <div class="idea-grid" id="artifactBoard"></div>
</div>

<div class="footer" id="footer"></div>
</main>

<script id="data" type="application/json">/*__DATA__*/</script>
<script src="vendor/three.min.js"></script>
<script src="sprite-walker.js"></script>
<script src="scene3d.js"></script>
<script src="app.js"></script>
</body>
</html>
"""
