"""前端构建：stats/daily/*.json → web/index.html（零依赖单文件，file:// 直开）。

设计（docs/01 D5 + review 修订）：
- 数据内联为 JSON，转义 < > U+2028 U+2029 防 `</script>` 注入。
- 只内联最近 90 天，体积可控。
- 「小人在路上」：一条横贯页面的路，SVG 小人 CSS 动画行走，路碑=有数据的日期。
"""
from __future__ import annotations

import json
from pathlib import Path

from .aggregate import atomic_write_text
from .db import DB

MAX_DAYS = 90


def _safe_inline_json(obj) -> str:
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def build_web(db: DB, stats_dir: Path, web_dir: Path, max_days: int = MAX_DAYS) -> Path:
    daily_dir = stats_dir / "daily"
    days = sorted(p.stem for p in daily_dir.glob("*.json"))[-max_days:]
    payload_days = []
    for day in days:
        try:
            payload_days.append(json.loads((daily_dir / f"{day}.json").read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    # 已生成的深度分析页：对照 manifest 水位判断新鲜/陈旧（review 修正）
    from .deepdive import safe_page_key
    deep_dir = web_dir / "deep"
    deep_fresh, deep_stale = [], []
    if deep_dir.is_dir():
        try:
            manifest = json.loads((deep_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        wm = {safe_page_key(r["source"], r["session_id"]): (r["raw_mtime"], r["raw_size"])
              for r in db.conn.execute("SELECT source, session_id, raw_mtime, raw_size FROM sessions")}
        for p in sorted(deep_dir.glob("*.html")):
            key = p.stem
            cur = wm.get(key)
            gen = manifest.get(key)
            if cur and gen and (gen.get("raw_mtime"), gen.get("raw_size")) == cur:
                deep_fresh.append(key)
            elif cur:
                deep_stale.append(key)  # 会话已更新，页面陈旧
            else:
                deep_fresh.append(key)  # 孤儿页（session 已不在库），保留链接
    payload = {"days": payload_days, "deep_pages": deep_fresh, "deep_stale": deep_stale,
               "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")}
    html = TEMPLATE.replace("/*__DATA__*/", _safe_inline_json(payload))
    out = web_dir / "index.html"
    atomic_write_text(out, html)
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>在路上 · 自我跟踪</title>
<style>
  :root { --ink:#2b2b2b; --dim:#8a8a8a; --line:#e5e1d8; --bg:#faf8f4; --accent:#c96f4a; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--ink); font-family:-apple-system,"PingFang SC",sans-serif;
         max-width:880px; margin:0 auto; padding:32px 20px 120px; }
  h1 { font-size:22px; font-weight:600; letter-spacing:2px; }
  .sub { color:var(--dim); font-size:13px; margin-top:4px; }

  /* ——— 小人在路上 ——— */
  .journey { position:relative; height:150px; margin:28px 0 8px; overflow:hidden; }
  .road { position:absolute; left:0; right:0; bottom:38px; border-top:2px solid var(--ink); }
  .road::after { content:""; position:absolute; top:6px; left:0; right:0;
    background:repeating-linear-gradient(90deg,var(--line) 0 18px,transparent 18px 36px); height:2px; }
  .walker { position:absolute; bottom:40px; left:0; width:34px; height:48px;
    animation:walk 24s linear infinite; }
  @keyframes walk { from { transform:translateX(-40px);} to { transform:translateX(920px);} }
  .walker svg { display:block; }
  .leg { transform-origin:17px 30px; animation:step .5s ease-in-out infinite alternate; }
  .leg.back { animation-delay:-.25s; }
  @keyframes step { from { transform:rotate(18deg);} to { transform:rotate(-18deg);} }
  .milestone { position:absolute; bottom:30px; width:2px; background:var(--dim); opacity:.5; }
  .milestone .d { position:absolute; top:100%; left:50%; transform:translateX(-50%);
    font-size:10px; color:var(--dim); white-space:nowrap; margin-top:6px; }
  .milestone.hot { background:var(--accent); opacity:.9; }

  /* ——— KPI ——— */
  .kpis { display:flex; gap:28px; flex-wrap:wrap; margin:26px 0 8px; }
  .kpi .n { font-size:34px; font-weight:700; font-variant-numeric:tabular-nums; }
  .kpi .l { font-size:12px; color:var(--dim); margin-top:2px; }

  section { margin-top:36px; }
  h2 { font-size:15px; font-weight:600; margin-bottom:12px; letter-spacing:1px; }
  .day-picker { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:16px; }
  .day-picker button { border:1px solid var(--line); background:#fff; color:var(--dim);
    font-size:12px; padding:3px 9px; border-radius:20px; cursor:pointer; }
  .day-picker button.on { border-color:var(--ink); color:var(--ink); }
  .narrative { background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px 18px; }
  .narrative .summary { font-size:14px; line-height:1.8; }
  .narrative .none { color:var(--dim); font-size:13px; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
  .chip { font-size:12px; background:var(--bg); border:1px solid var(--line);
    border-radius:20px; padding:3px 10px; }
  .chip.hot { border-color:var(--accent); color:var(--accent); }
  .focus-list { margin-top:12px; font-size:13px; line-height:2; }
  .focus-list li { list-style:none; padding-left:16px; position:relative; }
  .focus-list li::before { content:"—"; position:absolute; left:0; color:var(--dim); }
  .state { font-size:11px; color:var(--dim); border:1px solid var(--line);
    border-radius:4px; padding:0 5px; margin-left:6px; }
  .sess { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width:640px) { .sess { grid-template-columns:1fr; } }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px;
    padding:12px 14px; cursor:pointer; transition:border-color .15s, transform .15s; position:relative; }
  .card:hover { border-color:var(--ink); transform:translateY(-1px); }
  .card .src { color:var(--dim); font-size:11px; }
  .card .t { font-size:13px; font-weight:500; margin-top:4px; line-height:1.5; }
  .card .what { color:var(--dim); font-size:12px; margin-top:6px; line-height:1.6;
    display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
  .card .meta { display:flex; justify-content:space-between; margin-top:8px;
    font-size:11px; color:var(--dim); padding-right:76px; }  /* 给「深度」按钮留位 */
  .card .copied { position:absolute; top:10px; right:12px; font-size:11px;
    color:var(--accent); opacity:0; transition:opacity .2s; }
  .card.copied .copied { opacity:1; }
  .card.chore { opacity:.45; }
  /* ——— tab / 想法看板 ——— */
  .tabbar { display:flex; gap:8px; margin:20px 0 4px; }
  .tabbar button { border:1px solid var(--line); background:#fff; color:var(--dim);
    font-size:13px; padding:4px 16px; border-radius:20px; cursor:pointer; }
  .tabbar button.on { border-color:var(--ink); color:var(--ink); font-weight:600; }
  .idea-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:20px; }
  @media (max-width:640px) { .idea-grid { grid-template-columns:1fr; } }
  .idea-card { background:#fff; border:1px solid var(--line); border-radius:10px;
    padding:12px 14px; cursor:pointer; transition:border-color .15s, transform .15s; }
  .idea-card:hover { border-color:var(--ink); transform:translateY(-1px); }
  .idea-card .title { font-size:14px; font-weight:600; margin-top:6px; }
  .idea-card .t { font-size:12px; color:var(--dim); line-height:1.7; margin-top:4px; }
  .idea-card .m { font-size:11px; color:var(--dim); margin-top:8px; }
  .idea-card { position:relative; }
  .trash-btn { position:absolute; top:10px; right:10px; border:none; background:none;
    font-size:13px; cursor:pointer; opacity:.35; padding:2px; }
  .trash-btn:hover { opacity:1; }
  .trash-item { display:flex; align-items:baseline; gap:10px; padding:8px 2px;
    border-top:1px solid var(--line); font-size:13px; }
  .trash-item .t { flex:1; color:var(--dim); }
  .trash-item button { border:1px solid var(--line); background:#fff; color:var(--dim);
    font-size:11px; padding:2px 10px; border-radius:12px; cursor:pointer; }
  .trash-item button:hover { color:var(--ink); border-color:var(--ink); }
  .badge { display:inline-block; font-size:10px; border-radius:10px; padding:1px 8px;
    border:1px solid var(--dim); color:var(--dim); }
  .badge.open, .badge.haunting { border-color:var(--accent); color:var(--accent); }
  .badge.haunting { background:var(--accent); color:#fff; }
  .badge.landed { border-color:#5a8f5a; color:#5a8f5a; }
  .badge.followed { border-color:#5a8f5a; color:#5a8f5a; }
  .badge.pending { border-color:var(--accent); color:var(--accent); }
  .promise-item { display:flex; align-items:baseline; gap:10px; padding:10px 2px;
    border-top:1px solid var(--line); font-size:13px; line-height:1.7; }
  .promise-item .t { flex:1; }
  .promise-item .m { font-size:11px; color:var(--dim); white-space:nowrap; }
  @media (max-width:640px) {
    .promise-item { flex-wrap:wrap; }
    .promise-item .m { white-space:normal; width:100%; }
  }
  .idea-detail { background:#fff; border:1px solid var(--line); border-radius:10px;
    padding:16px 18px; margin-top:14px; }
  .idea-detail .t { font-size:16px; font-weight:600; line-height:1.8; }
  .idea-detail .desc { font-size:13px; color:var(--dim); line-height:1.8; margin-top:6px; }
  .idea-detail .m { font-size:12px; color:var(--dim); margin-top:8px; }
  .back { font-size:12px; color:var(--dim); text-decoration:none; }
  /* ——— 批注 / callout / 页脚 ——— */
  .annot { width:100%; min-height:56px; margin-top:10px; border:1px dashed var(--line);
    border-radius:10px; background:transparent; padding:10px 12px; font-size:13px;
    font-family:inherit; color:var(--ink); resize:vertical; }
  .annot:focus { outline:none; border-color:var(--accent); }
  .callout { border-left:3px solid var(--accent); background:#fff;
    border-radius:0 8px 8px 0; padding:10px 14px; margin-bottom:10px; font-size:13px; line-height:1.7; }
  .callout .t { font-weight:600; font-size:12px; color:var(--accent); }
  .footer { margin-top:48px; font-size:11px; color:var(--dim); line-height:1.8;
    border-top:1px solid var(--line); padding-top:12px; }
  .card .deep { position:absolute; bottom:10px; right:12px; font-size:11px;
    color:var(--accent); text-decoration:none; border:1px solid var(--accent);
    border-radius:12px; padding:1px 8px; background:#fff; cursor:pointer; }
  .hours { display:flex; gap:3px; align-items:flex-end; height:36px; margin-top:6px; }
  .hours .h { flex:1; background:var(--line); border-radius:2px; min-height:2px; }
  .hours .h.on { background:var(--accent); }
  .hours-label { display:flex; justify-content:space-between; font-size:10px; color:var(--dim); margin-top:2px; }
</style>
</head>
<body>
<h1>在路上</h1>
<div class="sub" id="subtitle"></div>

<div class="tabbar">
  <button data-tab="daily" class="on">日报</button>
  <button data-tab="ideas">想法看板</button>
  <button data-tab="promises">承诺</button>
</div>

<div id="view-daily">
<div class="journey" id="journey">
  <div class="road"></div>
  <div class="walker" id="walker">
    <svg viewBox="0 0 34 48" width="34" height="48">
      <circle cx="17" cy="8" r="5" fill="none" stroke="#2b2b2b" stroke-width="2"/>
      <line x1="17" y1="13" x2="17" y2="30" stroke="#2b2b2b" stroke-width="2"/>
      <line x1="17" y1="17" x2="9" y2="24" stroke="#2b2b2b" stroke-width="2"/>
      <line x1="17" y1="17" x2="25" y2="22" stroke="#2b2b2b" stroke-width="2"/>
      <line class="leg" x1="17" y1="30" x2="12" y2="44" stroke="#2b2b2b" stroke-width="2"/>
      <line class="leg back" x1="17" y1="30" x2="22" y2="44" stroke="#2b2b2b" stroke-width="2"/>
    </svg>
  </div>
</div>

<div class="kpis" id="kpis"></div>

<section>
  <h2>这一天</h2>
  <div class="day-picker" id="dayPicker"></div>
  <div class="narrative" id="narrative"></div>
  <textarea class="annot" id="annot" placeholder="批注：给自己的话（只存在本机浏览器）…"></textarea>
</section>

<section>
  <h2>会话 <span style="font-weight:400;color:var(--dim);font-size:12px">点击卡片复制 resume 指令 · 点「深度」看单会话分析</span></h2>
  <div class="sess" id="sessions"></div>
</section>
</div><!-- /view-daily -->

<div id="view-ideas" style="display:none">
  <div class="idea-grid" id="ideaBoard"></div>
  <div id="ideaView" style="display:none">
    <p><a class="back" href="#" id="ideaBack">← 返回想法看板</a></p>
    <div class="idea-detail" id="ideaDetail"></div>
    <h2 style="margin-top:28px">相关会话</h2>
    <div class="sess" id="ideaSessions"></div>
  </div>
  <div id="trashWrap" style="display:none">
    <h2 style="margin-top:32px">回收站 <span style="font-weight:400;color:var(--dim);font-size:12px">归档的想法在这里，可以恢复</span></h2>
    <div id="trashList"></div>
  </div>
</div>

<div id="view-promises" style="display:none">
  <div class="kpis" id="promiseKpis" style="margin-top:20px"></div>
  <div id="promiseList"></div>
</div>

<div class="footer" id="footer"></div>

<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const PAYLOAD = JSON.parse(document.getElementById('data').textContent);
const DATA = PAYLOAD.days;
const DEEP = new Set(PAYLOAD.deep_pages || []);
const DEEP_STALE = new Set(PAYLOAD.deep_stale || []);
const BUILT_AT = PAYLOAD.built_at || '';
// 与 deepdive.py safe_page_key 同规则：session_id 不可信，过滤危险字符
function pageKey(source, sid) { return `${source}-${sid}`.replace(/[^A-Za-z0-9._-]/g, '_'); }
const $ = s => document.querySelector(s);
const days = DATA.map(d => d.date);
const STATE_CN = {done:'完成', in_progress:'进行中', blocked:'卡住', exploring:'探索中'};

// subtitle
$('#subtitle').textContent = days.length
  ? `${days[0]} → ${days[days.length-1]} · 共 ${days.length} 天有记录`
  : '还没有数据';

// journey milestones
const journey = $('#journey');
if (days.length) {
  const t0 = new Date(days[0]), t1 = new Date(days[days.length-1]);
  const span = Math.max(1, (t1 - t0) / 86400000);
  const maxSess = Math.max(...DATA.map(d => d.stats.n_sessions), 1);
  const labelEvery = Math.max(1, Math.ceil(DATA.length / 8));  // 标签稀疏化防重叠
  const xs = DATA.map(d => ((new Date(d.date) - t0) / 86400000 / span) * 96 + 2);
  DATA.forEach((d, i) => {
    const m = document.createElement('div');
    m.className = 'milestone' + (d.stats.n_sessions >= maxSess * 0.7 ? ' hot' : '');
    m.style.left = xs[i] + '%'; m.style.height = (8 + (d.stats.n_sessions / maxSess) * 26) + 'px';
    const isLast = i === DATA.length - 1;
    const tooCloseToLast = !isLast && (xs[xs.length - 1] - xs[i]) < 6;
    if ((i % labelEvery === 0 && !tooCloseToLast) || isLast)
      m.innerHTML = `<div class="d">${d.date.slice(5)}</div>`;
    journey.appendChild(m);
  });
}

// KPI (近7天 vs 全部)
const sum = (arr, f) => arr.reduce((a, d) => a + f(d), 0);
const last7 = DATA.slice(-7);
const kpis = [
  [sum(last7, d => d.stats.n_sessions), '近7个记录日会话'],
  [sum(last7, d => d.stats.n_user_msgs), '近7个记录日消息'],
  [sum(DATA, d => d.stats.n_sessions), '累计会话'],
  [days.length, '有记录的天数'],
];
$('#kpis').innerHTML = kpis.map(([n, l]) =>
  `<div class="kpi"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');

// 想法看板：跨会话聚合 L1 卡的 ideas（含状态），点击进入单想法视图
function normIdea(t) { return t.toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, ''); }
const STATUS_CN = { open: '未落地', unclear: '状态不明', landed: '已落地', abandoned: '已放弃' };
const STATUS_ORDER = { open: 0, unclear: 1, landed: 2, abandoned: 3 };
function buildIdeas() {
  const byKey = {};
  for (const d of DATA) {
    for (const s of d.sessions) {
      for (const raw of ((s.digest && s.digest.ideas) || [])) {
        const idea = typeof raw === 'string' ? { title: raw.slice(0, 10), text: raw, status: 'unclear' } : raw;
        if (!idea || !idea.text) continue;
        const key = normIdea(idea.text);
        if (!byKey[key]) byKey[key] = { key, text: idea.text, title: idea.title || idea.text.slice(0, 10), occurrences: [] };
        const occ = byKey[key].occurrences;
        // 同一 session 跨日投影只算一次提及（取最早一天），否则"萦绕"被投影放大
        if (!occ.some(o => o.session.ref === s.ref))
          occ.push({ date: d.date, status: idea.status, title: idea.title, session: s });
      }
    }
  }
  const items = Object.values(byKey);
  for (const it of items) {
    it.occurrences.sort((a, b) => a.date.localeCompare(b.date));
    // 状态以最新一次提及为准（open 不再永久压过 landed）；同日按优先级取
    const latestDate = it.occurrences.slice(-1)[0].date;
    const latestStatuses = it.occurrences.filter(o => o.date === latestDate).map(o => o.status);
    it.status = ['open', 'unclear', 'abandoned', 'landed']
      .find(st => latestStatuses.includes(st)) || 'unclear';
    it.firstDate = it.occurrences[0].date;
    it.lastDate = latestDate;
    // 萦绕：反复提起（≥3 次）+ 拖了很久（≥2 周）还没落地——潜意识真正在意的事
    const spanDays = (new Date(it.lastDate) - new Date(it.firstDate)) / 86400000;
    it.haunting = it.status === 'open' && it.occurrences.length >= 3 && spanDays >= 14;
    // 标题取最近一次提及的（最新措辞），兜底前 10 字
    const latest = it.occurrences.slice().sort((a, b) => b.date.localeCompare(a.date))[0];
    it.title = (latest && latest.title) || it.text.slice(0, 10);
    it.related = relatedSessions(it);
  }
  items.sort((a, b) => (b.haunting - a.haunting)
    || STATUS_ORDER[a.status] - STATUS_ORDER[b.status]
    || b.lastDate.localeCompare(a.lastDate));
  return items;
}
function relatedSessions(item) {
  // 相关 = 提过同一想法（归一化同键）或与出处会话共享热点标签，确定性打分
  const originLabels = new Set();
  for (const o of item.occurrences)
    for (const l of ((o.session.digest && o.session.digest.hotspot_labels) || []))
      originLabels.add(l);
  const seen = new Set(), scored = [];
  for (const d of DATA) {
    for (const s of d.sessions) {
      if (seen.has(s.ref)) continue;
      let score = 0;
      const keys = ((s.digest && s.digest.ideas) || [])
        .map(x => normIdea(typeof x === 'string' ? x : (x && x.text) || ''));
      if (keys.includes(item.key)) score += 10;
      for (const l of ((s.digest && s.digest.hotspot_labels) || []))
        if (originLabels.has(l)) score += 2;
      if (score > 0) { seen.add(s.ref); scored.push({ s, date: d.date, score }); }
    }
  }
  scored.sort((a, b) => b.score - a.score || b.date.localeCompare(a.date));
  return scored;
}
const IDEAS = buildIdeas();

// 手动归档（回收站）：key 存 localStorage，跨构建保留
function getTrash() {
  try { return new Set(JSON.parse(store.get('lifelog-idea-trash') || '[]')); }
  catch (e) { return new Set(); }
}
function setTrash(s) { store.set('lifelog-idea-trash', JSON.stringify([...s])); }
function archiveIdea(key) { const t = getTrash(); t.add(key); setTrash(t); renderBoard(); }
function restoreIdea(key) { const t = getTrash(); t.delete(key); setTrash(t); renderBoard(); }

function renderBoard() {
  $('#ideaView').style.display = 'none';
  $('#ideaBoard').style.display = '';
  const trash = getTrash();
  const visible = IDEAS.filter(it => !trash.has(it.key));
  $('#ideaBoard').innerHTML = visible.length ? visible.map((it) => `
    <div class="idea-card" data-key="${esc(it.key)}">
      <button class="trash-btn" data-trash="${esc(it.key)}" title="归档到回收站">🗑</button>
      ${it.haunting ? '<span class="badge haunting">萦绕</span>' : `<span class="badge ${it.status}">${STATUS_CN[it.status]}</span>`}
      <div class="title">${esc(it.title)}</div>
      <div class="t">${esc(it.text)}</div>
      <div class="m">首次 ${it.firstDate.slice(5)} · 提及 ${it.occurrences.length} 次 · 相关会话 ${it.related.length}</div>
    </div>`).join('')
    : '<div style="color:var(--dim);font-size:13px">还没有捕捉到想法。</div>';
  $('#ideaBoard').querySelectorAll('.idea-card').forEach(c => {
    c.onclick = () => showIdea(c.dataset.key);
  });
  $('#ideaBoard').querySelectorAll('[data-trash]').forEach(b => {
    b.onclick = e => { e.stopPropagation(); archiveIdea(b.dataset.trash); };
  });
  renderTrash(trash);
}

function renderTrash(trash) {
  trash = trash || getTrash();
  const items = IDEAS.filter(it => trash.has(it.key));
  $('#trashWrap').style.display = items.length ? '' : 'none';
  $('#trashList').innerHTML = items.map(it => `
    <div class="trash-item">
      <span class="t">💡 ${esc(it.title)}<span style="font-size:11px"> · ${it.firstDate.slice(5)}</span></span>
      <button data-restore="${esc(it.key)}">恢复</button>
    </div>`).join('');
  $('#trashList').querySelectorAll('[data-restore]').forEach(b => {
    b.onclick = () => restoreIdea(b.dataset.restore);
  });
}

function showIdea(key) {
  const it = IDEAS.find(x => x.key === key);
  if (!it) return;
  $('#ideaBoard').style.display = 'none';
  $('#ideaView').style.display = '';
  const first = it.occurrences.slice().sort((a, b) => a.date.localeCompare(b.date))[0];
  const page = first ? pageKey(first.session.source, first.session.session_id) : '';
  const link = first && DEEP.has(page) ? ` · <a href="deep/${page}.html" style="color:var(--accent)">出处详情 →</a>` : '';
  $('#ideaDetail').innerHTML = `
    <span class="badge ${it.status}">${STATUS_CN[it.status]}</span>
    <button class="trash-btn" data-trash="${esc(it.key)}" title="归档到回收站" style="position:static;float:right">🗑</button>
    <div class="t">💡 ${esc(it.title)}</div>
    <div class="desc">${esc(it.text)}</div>
    <div class="m">首次出现 ${it.firstDate} · 最近 ${it.lastDate} · 提及 ${it.occurrences.length} 次${link}</div>`;
  $('#ideaDetail').querySelector('[data-trash]').onclick = () => archiveIdea(it.key);
  renderCards($('#ideaSessions'),
    it.related.map(r => ({ ...r.s, _date: r.date })));
}
$('#ideaBack').onclick = e => { e.preventDefault(); renderBoard(); };

// 承诺对账：L1 卡的 commitments + 后续是否有同主题会话（热点标签重叠，确定性近似）
// review 修正：排除承诺来源 session 自身（跨日投影会自己当自己的"后续"）；
// 同日但时间更晚的其他会话也算后续；KPI 措辞诚实化为"有下文率"
function buildPromises() {
  const byKey = {};
  for (const d of DATA) {
    for (const s of d.sessions) {
      for (const text of ((s.digest && s.digest.commitments) || [])) {
        if (typeof text !== 'string' || !text) continue;
        const key = normIdea(text);
        if (!byKey[key]) byKey[key] = { key, text, date: d.date, labels: new Set(),
                                        refs: new Set(), latestStart: '' };
        const p = byKey[key];
        for (const l of ((s.digest && s.digest.hotspot_labels) || [])) p.labels.add(l);
        p.refs.add(s.ref);
        if (d.date < p.date) p.date = d.date;
        if (s.started_at && s.started_at > p.latestStart) p.latestStart = s.started_at;
      }
    }
  }
  const items = Object.values(byKey);
  for (const p of items) {
    const later = new Set();
    for (const d of DATA) {
      for (const s of d.sessions) {
        if (p.refs.has(s.ref)) continue;                    // 排除承诺来源自身
        if (d.date < p.date) continue;
        if (d.date === p.date && p.latestStart && s.started_at && s.started_at <= p.latestStart)
          continue;  // 同日只认时间上更晚的其他会话
        if (((s.digest && s.digest.hotspot_labels) || []).some(l => p.labels.has(l)))
          later.add(d.date);
      }
    }
    p.followDays = later.size;
    p.followed = later.size > 0;
  }
  items.sort((a, b) => b.date.localeCompare(a.date));
  return items;
}
const PROMISES = buildPromises();

function renderPromises() {
  const total = PROMISES.length;
  const followed = PROMISES.filter(p => p.followed).length;
  const rate = total ? Math.round(followed / total * 100) : 0;
  $('#promiseKpis').innerHTML = [
    [total, '许下的承诺'],
    [followed, '有下文'],
    [total - followed, '悬着'],
    [rate + '%', '有下文率*'],
  ].map(([n, l]) => `<div class="kpi"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
  $('#promiseList').innerHTML = (PROMISES.length ? PROMISES.map(p => `
    <div class="promise-item">
      <span class="badge ${p.followed ? 'followed' : 'pending'}">${p.followed ? '有下文' : '悬着'}</span>
      <span class="t">${esc(p.text)}</span>
      <span class="m">${p.date.slice(5)}${p.followed ? ` · 后续 ${p.followDays} 天有同主题活动` : ''}</span>
    </div>`).join('')
    : '<div style="color:var(--dim);font-size:13px;padding:10px 2px">还没有捕捉到承诺。</div>')
    + '<div style="color:var(--dim);font-size:11px;padding-top:14px">* 有下文率 = 承诺日之后出现同主题（热点标签重叠）会话的比例，是确定性近似，不等于承诺被兑现。统计范围为最近 90 个记录日。</div>';
}

// tab 切换
function switchTab(name) {
  document.querySelectorAll('.tabbar button').forEach(x =>
    x.classList.toggle('on', x.dataset.tab === name));
  $('#view-daily').style.display = name === 'daily' ? '' : 'none';
  $('#view-ideas').style.display = name === 'ideas' ? '' : 'none';
  $('#view-promises').style.display = name === 'promises' ? '' : 'none';
  if (name === 'ideas') renderBoard();
  if (name === 'promises') renderPromises();
}
document.querySelectorAll('.tabbar button').forEach(b => {
  b.onclick = () => { switchTab(b.dataset.tab); history.replaceState(null, '', '#' + b.dataset.tab); };
});
if (location.hash === '#ideas') switchTab('ideas');
if (location.hash === '#promises') switchTab('promises');

// 异常检测（确定性规则，不依赖 LLM）
function detectAnomalies() {
  const marks = {};  // date -> [callouts]
  const counts = DATA.map(d => d.stats.n_sessions).filter(n => n > 0);
  const median = counts.sort((a, b) => a - b)[Math.floor(counts.length / 2)] || 1;
  // 每个会话在数据内的最后一天（blocked 是终态，只标在最后一天，不回溯投影）
  const lastDay = {};
  for (const d of DATA)
    for (const s of d.sessions) lastDay[s.ref] = d.date;
  let prev = null;
  for (const d of DATA) {
    const m = [];
    if (d.stats.n_sessions >= Math.max(8, median * 2.5))
      m.push(`爆发日：${d.stats.n_sessions} 个会话，是中位数的 ${(d.stats.n_sessions / median).toFixed(1)} 倍`);
    const late = d.stats.active_hours.filter(h => h >= 0 && h <= 4);
    if (late.length >= 1)
      m.push(`深夜活跃：凌晨 ${late[0]} 点仍有活动`);
    if (prev) {
      const gap = (new Date(d.date) - new Date(prev)) / 86400000;
      if (gap >= 4) m.push(`回归日：中间空了 ${gap - 1} 天没有记录`);
    }
    const blocked = d.sessions.filter(s =>
      s.digest && s.digest.progress_state === 'blocked' && lastDay[s.ref] === d.date);
    if (blocked.length) m.push(`${blocked.length} 个会话卡在 blocked 状态`);
    if (m.length) marks[d.date] = m;
    prev = d.date;
  }
  return marks;
}
const ANOMALIES = detectAnomalies();

// localStorage 在 file:// 或隐私策略下可能抛 SecurityError：批注是增强功能，不许拖垮主看板
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};

// resume 指令映射：~/.claude 的会话日常用 tcode（tclaude 别名）恢复；带 cwd 前缀
const RESUME_CMD = {
  'kimi-code': s => `kimi -r session_${s.session_id}`,  // kimi 恢复要 session_ 前缀（库里存的是去掉前缀的 uuid）
  'tclaude': s => `tclaude -r ${s.session_id}`,
  'claude': s => `tcode -r ${s.session_id}`,
  'tcodex': s => `tcodex resume ${s.session_id}`,
  'workbuddy': s => s.title || s.session_id,  // 无 resume 指令，复制标题
};
function shq(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }  // shell 单引号转义
function resumeText(s) {
  const fn = RESUME_CMD[s.source];
  const cmd = fn ? fn(s) : (s.title || s.session_id);
  return s.cwd ? `cd ${shq(s.cwd)} && ${cmd}` : cmd;
}
function copyText(text, card) {
  const done = () => {
    card.classList.add('copied');
    setTimeout(() => card.classList.remove('copied'), 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => legacyCopy(text, done));
  } else legacyCopy(text, done);
}
function legacyCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) {}
  document.body.removeChild(ta);
}

// day picker
let current = days.length - 1;
$('#dayPicker').innerHTML = days.map((d, i) =>
  `<button data-i="${i}" class="${i === current ? 'on' : ''}">${d.slice(5)}</button>`).join('');
$('#dayPicker').onclick = e => {
  if (e.target.dataset.i === undefined) return;
  current = +e.target.dataset.i;
  document.querySelectorAll('.day-picker button').forEach(b =>
    b.classList.toggle('on', +b.dataset.i === current));
  renderDay();
};

function renderDay() {
  const d = DATA[current];
  if (!d) return;
  d.sessions.forEach(s => s._imp = importance(s));
  const ordered = [...d.sessions].sort((a, b) => b._imp - a._imp);  // 重要程度排序，chore 沉底（不改原数组）
  const n = d.narrative || {};
  let html = '';
  for (const a of (ANOMALIES[d.date] || []))
    html += `<div class="callout"><span class="t">⚠ 异常</span> ${esc(a)}</div>`;
  if (n.summary) html += `<div class="summary">${esc(n.summary)}</div>`;
  else html += `<div class="none">这一天还没有叙事（未配置 LLM 或会话过少）。下面是硬统计。</div>`;
  const s = d.stats;
  html += `<div class="chips">
    <span class="chip">${s.n_sessions} 个会话</span>
    <span class="chip">${s.n_user_msgs} 条消息</span>
    <span class="chip">${s.n_tool_calls} 次工具调用</span>
    ${Object.entries(s.by_source).map(([k, v]) => `<span class="chip">${esc(k)} ×${v}</span>`).join('')}
  </div>`;
  html += `<div class="hours">${Array.from({length: 24}, (_, h) =>
    `<div class="h ${s.active_hours.includes(h) ? 'on' : ''}"></div>`).join('')}</div>
    <div class="hours-label"><span>0时</span><span>12时</span><span>23时</span></div>`;
  if (n.focus && n.focus.length)
    html += `<ul class="focus-list">${n.focus.map(f => `<li>${esc(f)}</li>`).join('')}</ul>`;
  if (n.progress && n.progress.length)
    html += `<ul class="focus-list">${n.progress.map(p =>
      `<li>${esc(p.topic)}<span class="state">${esc(STATE_CN[p.state] || String(p.state || ''))}</span></li>`).join('')}</ul>`;
  if (n.commitments && n.commitments.length)
    html += `<ul class="focus-list">${n.commitments.map(c =>
      `<li>📌 ${esc(c.text || c)}</li>`).join('')}</ul>`;
  if (n.hotspots && n.hotspots.length)
    html += `<div class="chips">${n.hotspots.map(h =>
      `<span class="chip hot">${esc(h.label)}</span>`).join('')}</div>`;
  $('#narrative').innerHTML = html;

  // 批注框：按天持久化到 localStorage（借鉴 work-canvas 的 commentbox）
  const annot = $('#annot');
  annot.value = store.get('lifelog-note-' + d.date) || '';
  annot.oninput = () => {
    if (annot.value.trim()) store.set('lifelog-note-' + d.date, annot.value);
    else store.del('lifelog-note-' + d.date);
  };

  renderCards($('#sessions'), ordered);
}

// 通用会话卡片渲染（日报会话列表 + 想法视图的相关会话共用）
function renderCards(container, list) {
  list.forEach(s => { if (s._imp === undefined) s._imp = importance(s); });
  container.innerHTML = list.map((x, i) => {
    const page = pageKey(x.source, x.session_id);
    const deep = DEEP.has(page)
      ? `<a class="deep" href="deep/${page}.html">深度 →</a>`
      : DEEP_STALE.has(page)
        ? `<span class="deep" data-deep="${esc(x.source)}|${esc(x.session_id)}">↻ 重新生成</span>`
        : `<span class="deep" data-deep="${esc(x.source)}|${esc(x.session_id)}">深度</span>`;
    return `
    <div class="card${x._imp < 0 ? ' chore' : ''}" data-i="${i}">
      <span class="copied">已复制 ✓</span>
      <div class="src">${esc(x.source)}</div>
      <div class="t">${esc(x.title || '(无标题)')}</div>
      ${x.digest && x.digest.what ? `<div class="what">${esc(x.digest.what)}</div>` : ''}
      <div class="meta"><span>${(x.started_at || '').slice(11, 16)}</span><span>${x.n_user_msgs} 条消息</span></div>
      ${deep}
    </div>`;
  }).join('');
  container.querySelectorAll('.card').forEach(card => {
    card.onclick = () => copyText(resumeText(list[+card.dataset.i]), card);
  });
  // 深度分析入口：新鲜页面走链接（上面 a 标签），陈旧/没有则复制生成命令
  container.querySelectorAll('[data-deep]').forEach(el => {
    el.onclick = e => {
      e.stopPropagation();
      const [src, sid] = el.dataset.deep.split('|');
      copyText(`LIFELOG_LLM_BACKEND=kimi-code python3 -m lifelog deep-dive ${shq(src)} ${shq(sid)}`, el.closest('.card'));
    };
  });
  container.querySelectorAll('a.deep').forEach(el => {
    el.onclick = e => e.stopPropagation();
  });
}

// 重要程度打分：有实质交互和 LLM 卡的在前；skipped 和一眼 chore 的沉底
function importance(s) {
  if (s.digest_status === 'skipped') return -100 + s.n_user_msgs;
  let sc = s.n_user_msgs * 2;
  if (s.digest && s.digest.what) sc += 5;
  if (s.digest && s.digest.progress_state === 'blocked') sc += 3;  // 卡住的值得关注
  const t = s.title || '';
  if (/^#\s*Task|^Reply with exactly|^\/?(测试|test)\b/i.test(t)) sc -= 4;
  return sc;
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
// 出处页脚：谁生成、何时、哪部分是硬算哪部分是 LLM
$('#footer').innerHTML =
  `lifelog 生成于 ${esc(BUILT_AT.slice(0, 16).replace('T', ' '))} · 统计数字为代码确定性计算 · ` +
  `会话卡与日叙事由 LLM（k3）生成、按水位缓存 · 数据全部留在本机`;
renderDay();
</script>
</body>
</html>
"""
