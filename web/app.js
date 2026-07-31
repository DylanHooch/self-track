/* 在路上 · 看板主逻辑（数据来自 index.html 内联 JSON，构建期注入） */
'use strict';
const PAYLOAD = JSON.parse(document.getElementById('data').textContent);
const DATA = PAYLOAD.days;
const PROJECTS = PAYLOAD.projects || [];
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

// localStorage 在 file:// 或隐私策略下可能抛 SecurityError：批注是增强功能，不许拖垮主看板
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};

let current = days.length - 1;

// ——— 3D 山径 hero（失败则 2D 兜底）———
const heroEl = $('#hero'), tipEl = $('#sceneTip');
let journey3d = false;
try {
  journey3d = window.Journey3D && Journey3D.init($('#scene3d'),
    DATA.map(d => ({ date: d.date, n_sessions: d.stats.n_sessions })), {
      selected: current,
      onPick(i) {           // 点路碑 → 切到那一天
        selectDay(i);
        switchTab('daily');
        document.getElementById('daySection').scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
      onHover(i, x, y) {    // 悬停：路碑变化时才重建内容
        if (i == null) { tipEl.style.display = 'none'; return; }
        const d = DATA[i];
        tipEl.innerHTML = `<b>${esc(d.date)}</b> · ${d.stats.n_sessions} 个会话`;
        tipEl.style.left = x + 'px'; tipEl.style.top = y + 'px';
        tipEl.style.display = 'block';
      },
      onHoverMove(x, y) {   // 同一块路碑内移动：只挪位置（每次移动都跟手，不卡顿）
        tipEl.style.left = x + 'px'; tipEl.style.top = y + 'px';
      },
      onContextLost() {     // GPU 上下文丢失：降级海报 + 2D 路碑
        journey3d = false;
        heroEl.classList.add('no-webgl');
        render2DJourney();
      },
    });
} catch (e) { journey3d = false; }
if (!journey3d) {
  heroEl.classList.add('no-webgl');
  render2DJourney();   // WebGL 不可用：海报 + 2D 路碑兜底（仍可点击切日期）
}
if (!days.length) {    // 空数据：没有路碑可点，别给误导性提示（review 修正）
  const hint = heroEl.querySelector('.hero-hint');
  if (hint) hint.style.display = 'none';
}

function render2DJourney() {
  const journey = $('#journey2d-inner');
  if (!journey || !days.length) return;
  const t0 = new Date(days[0]), t1 = new Date(days[days.length-1]);
  const span = Math.max(1, (t1 - t0) / 86400000);
  const maxSess = Math.max(...DATA.map(d => d.stats.n_sessions), 1);
  const labelEvery = Math.max(1, Math.ceil(DATA.length / 8));
  // 单日数据固定摆中点，与 3D 侧一致（review 修正）；多日按真实日期间隔分布
  const xs = DATA.length === 1 ? [50]
    : DATA.map(d => ((new Date(d.date) - t0) / 86400000 / span) * 96 + 2);
  DATA.forEach((d, i) => {
    const m = document.createElement('div');
    m.className = 'milestone' + (d.stats.n_sessions >= maxSess * 0.7 ? ' hot' : '')
      + (i === current ? ' on' : '');
    m.style.left = xs[i] + '%'; m.style.height = (8 + (d.stats.n_sessions / maxSess) * 26) + 'px';
    m.title = `${d.date} · ${d.stats.n_sessions} 个会话`;
    m.onclick = () => { selectDay(i); switchTab('daily'); };
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
  const q = ($('#ideaFilter').value || '').trim().toLowerCase();
  const visible = IDEAS.filter(it => !trash.has(it.key)
    && (!q || it.title.toLowerCase().includes(q)
        || it.text.toLowerCase().includes(q)
        || STATUS_CN[it.status].includes(q)));
  $('#ideaBoard').innerHTML = visible.length ? visible.map((it) => `
    <div class="idea-card" data-key="${esc(it.key)}">
      <button class="trash-btn" data-trash="${esc(it.key)}" title="归档到回收站">🗑</button>
      ${it.haunting ? '<span class="badge haunting">萦绕</span>' : `<span class="badge ${it.status}">${STATUS_CN[it.status]}</span>`}
      <div class="title">${esc(it.title)}</div>
      <div class="t">${esc(it.text)}</div>
      <div class="m">首次 ${it.firstDate.slice(5)} · 提及 ${it.occurrences.length} 次 · 相关会话 ${it.related.length}</div>
    </div>`).join('')
    : '<div style="color:var(--ink-dim);font-size:13px">还没有捕捉到想法。</div>';
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
  const link = first ? ` · <a href="deep/${pageKey(first.session.source, first.session.session_id)}.html" style="color:var(--accent)">出处详情 →</a>` : '';
  $('#ideaDetail').innerHTML = `
    <span class="badge ${it.status}">${STATUS_CN[it.status]}</span>
    <button class="trash-btn" data-trash="${esc(it.key)}" title="归档到回收站" style="position:static;float:right">🗑</button>
    <div class="t">💡 ${esc(it.title)}</div>
    <div class="desc">${esc(it.text)}</div>
    <div class="m">首次出现 ${it.firstDate} · 最近 ${it.lastDate} · 提及 ${it.occurrences.length} 次${link}</div>`;
  $('#ideaDetail').querySelector('[data-trash]').onclick = () => archiveIdea(it.key);
  // 落地证据：✅ 只给名字强匹配且提交日期 ≥ 首次记录日的项目；标签命中的只列相关
  const projs = ideaProjects(it);
  $('#ideaEvidence').innerHTML = projs.length ? projs.map(p =>
    `<div class="evidence${p.evidence ? '' : ' none'}">${p.evidence ? '✅' : '📁'} <b>${esc(p.name)}</b>` +
    (p.git_last ? ` · git 最后提交 ${p.git_last.slice(0, 10)}` : ' · 无 git 记录') +
    (p.evidence ? '（提交日期 ≥ 首次记录日，日级近似，仅作线索）' : (p.labelHit && !p.nameHit ? '（标签相关，弱关联）' : '')) + '</div>'
  ).join('') : '<div class="evidence none">当前启发式未匹配到相关项目</div>';
  renderCards($('#ideaSessions'),
    it.related.map(r => ({ ...r.s, _date: r.date })));
}
$('#ideaBack').onclick = e => { e.preventDefault(); renderBoard(); };
$('#ideaFilter').oninput = () => renderBoard();

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
    : '<div style="color:var(--ink-dim);font-size:13px;padding:10px 2px">还没有捕捉到承诺。</div>')
    + '<div style="color:var(--ink-dim);font-size:11px;padding-top:14px">* 有下文率 = 承诺日之后出现同主题（热点标签重叠）会话的比例，是确定性近似，不等于承诺被兑现。统计范围为最近 90 个记录日。</div>';
}

// 项目看板：真实目录 + git 证据
function renderProjects() {
  $('#projectBoard').innerHTML = PROJECTS.length ? PROJECTS.map(p => `
    <div class="idea-card proj-card">
      <div class="title">${esc(p.name)}</div>
      <div class="cwd">${esc(p.cwd)}</div>
      <div class="m">
        ${p.n_sessions} 个会话 · ${esc(p.sources.join(' / '))}<br>
        ${p.first ? `活跃 ${p.first} → ${p.last}` : '活跃时间未知'}
        ${p.git_last ? `<br>git 最后提交 ${p.git_last.slice(0, 10)}` : (p.exists ? '<br>（无 git 提交记录）' : '<br>（目录已不存在）')}
      </div>
      <div class="chips">${(p.labels || []).map(l => `<span class="badge">${esc(l)}</span>`).join('')}</div>
    </div>`).join('')
    : '<div style="color:var(--ink-dim);font-size:13px">没有项目数据。</div>';
}

// 想法 ↔ 项目匹配：项目名出现在想法文本（强信号），或项目标签与出处会话标签重叠（弱信号）
// review 修正：名字匹配守卫落在归一化后的长度上（normIdea('..')/('@')→'' 时 includes('') 恒真）
function ideaProjects(it) {
  const text = normIdea(it.text + it.title);
  const originLabels = new Set();
  for (const o of it.occurrences)
    for (const l of ((o.session.digest && o.session.digest.hotspot_labels) || []))
      originLabels.add(l);
  return PROJECTS
    .map(p => {
      const nn = normIdea(p.name);
      const nameHit = nn.length >= 2 && text.includes(nn);
      const labelHit = (p.labels_all || p.labels || []).some(l => originLabels.has(l));
      // ✅ 落地证据只给强信号（名字命中）且 git 提交日期不早于首次记录日（日级近似）
      return { ...p, nameHit, labelHit,
               evidence: !!(nameHit && p.git_last && p.git_last.slice(0, 10) >= it.firstDate) };
    })
    .filter(p => p.nameHit || p.labelHit);
}

// tab 切换
function switchTab(name) {
  document.querySelectorAll('.tabbar button').forEach(x =>
    x.classList.toggle('on', x.dataset.tab === name));
  $('#view-daily').style.display = name === 'daily' ? '' : 'none';
  $('#view-ideas').style.display = name === 'ideas' ? '' : 'none';
  $('#view-promises').style.display = name === 'promises' ? '' : 'none';
  $('#view-projects').style.display = name === 'projects' ? '' : 'none';
  $('#view-artifacts').style.display = name === 'artifacts' ? '' : 'none';
  if (name === 'ideas') renderBoard();
  if (name === 'promises') renderPromises();
  if (name === 'projects') renderProjects();
  if (name === 'artifacts') renderArtifacts();
}
document.querySelectorAll('.tabbar button').forEach(b => {
  b.onclick = () => { switchTab(b.dataset.tab); history.replaceState(null, '', '#' + b.dataset.tab); };
});

// ——— 产物账本 ———
// 打包/补路径走 serve 的本地接口，file:// 直开时隐藏按钮并提示
const ARTIFACTS = PAYLOAD.artifacts || [];
const ON_SERVE = location.protocol === 'http:' || location.protocol === 'https:';
const NEED_SERVE_MSG = '这个操作需要通过本地应用打开页面：终端跑 selftrack，或访问 http://127.0.0.1:8791/（file:// 直开只能浏览）';
function needServe() { if (!ON_SERVE) alert(NEED_SERVE_MSG); return !ON_SERVE; }

function artifactBadge(a) {
  if (a.kind === 'commit') return '<span class="badge">commit</span>';
  if (!a.exists) return (a.packed_in && a.packed_in.length)
    ? '<span class="badge packed">已打包</span>'   // 原件被删但打包副本还在（用户决策）
    : '<span class="badge pending">已消失</span>';
  if (a.moved) return '<span class="badge followed">已移动</span>';
  return '<span class="badge followed">在</span>';
}

// 产物类型分类（勾选条用，默认不勾 commit）
const ART_IMG_EXT = new Set('png jpg jpeg gif webp svg heic bmp tiff tif'.split(' '));
const ART_VIDEO_EXT = new Set('mp4 mov webm avi mkv m4v'.split(' '));
function artType(a) {
  if (a.kind === 'commit') return 'commit';
  const ext = (a.name.match(/\.([^.]+)$/) || ['', ''])[1].toLowerCase();
  if (ART_IMG_EXT.has(ext)) return 'img';
  if (ART_VIDEO_EXT.has(ext)) return 'video';
  return 'doc';  // md/html/pdf/office/txt 等其余白名单扩展都归文档
}

function renderArtifacts() {
  $('#artHint').style.display = ON_SERVE ? 'none' : '';
  const q = ($('#artFilter').value || '').trim().toLowerCase();
  const types = new Set([...document.querySelectorAll('#artTypeBar input:checked')]
    .map(x => x.dataset.artType));
  const visible = ARTIFACTS.filter(a => types.has(artType(a)) && (!q
    || a.name.toLowerCase().includes(q)
    || (a.display_path || '').toLowerCase().includes(q)
    || (a.repo || '').toLowerCase().includes(q)
    || (a.note || '').toLowerCase().includes(q)));
  $('#artifactBoard').innerHTML = visible.length ? visible.map(a => `
    <div class="idea-card" data-id="${a.id}">
      ${artifactBadge(a)}
      <div class="title">${esc(a.name)}</div>
      ${a.note || a.head ? `<div class="t">${esc(a.note || a.head)}</div>` : ''}
      ${a.display_path ? `<div class="cwd">${esc(a.display_path)}</div>`
        : (a.packed_in && a.packed_in.length ? `<div class="cwd">副本在 ${esc(a.packed_in.join('、'))}</div>` : '')}
      <div class="m">${a.first_day.slice(5)}${a.last_day !== a.first_day ? ' → ' + a.last_day.slice(5) : ''} · ${a.sessions.length} 个会话${a.kind === 'commit' && a.repo ? ' · ' + esc(a.repo) : ''}</div>
      <div class="art-actions">
        ${a.kind === 'file' && a.exists ? `<button data-preview="${a.id}">预览</button>` : ''}
        ${a.kind === 'file' ? `<button data-pack="${a.id}">打包</button>` : ''}
        ${a.kind === 'file' ? `<button data-patch="${a.id}">补路径</button>` : ''}
        ${a.sessions.map(s =>
          `<a class="art-src" href="deep/${pageKey(s.source, s.session_id)}.html">${esc(s.source)} · ${esc((s.title || '').slice(0, 24))}</a>`).join('')}
      </div>
    </div>`).join('')
    : '<div style="color:var(--ink-dim);font-size:13px">还没有产物记录（会话写过的文件、执行过的 commit 会出现在这里）。</div>';
  $('#artifactBoard').querySelectorAll('[data-pack]').forEach(b => {
    b.onclick = e => { e.stopPropagation(); packArtifacts([+b.dataset.pack]); };
  });
  $('#artifactBoard').querySelectorAll('[data-patch]').forEach(b => {
    b.onclick = e => { e.stopPropagation(); patchArtifactPath(+b.dataset.patch); };
  });
  $('#artifactBoard').querySelectorAll('[data-preview]').forEach(b => {
    b.onclick = e => { e.stopPropagation(); previewArtifact(+b.dataset.preview); };
  });
  $('#artifactBoard').querySelectorAll('.art-src').forEach(l => {
    l.onclick = e => e.stopPropagation();
  });
  $('#artifactBoard').querySelectorAll('.idea-card[data-id]').forEach(card => {
    const a = ARTIFACTS.find(x => x.id === +card.dataset.id);
    if (a && a.kind === 'file' && a.exists) {  // file:// 下点击会提示走 serve（needServe 守卫）
      card.style.cursor = 'pointer';
      card.onclick = () => previewArtifact(a.id);
    }
  });
}

async function packArtifacts(ids) {
  if (needServe()) return;
  try {
    const r = await fetch('/api/pack', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
    const j = await r.json();
    alert(j.ok
      ? `已复制 ${j.copied} 个文件到 ${j.dir}${j.skipped ? `（${j.skipped} 个跳过：文件已不存在）` : ''}`
        + `\n「已打包」徽章在下次 selftrack-run 构建后更新。`
      : `打包失败：${j.error}`);
  } catch (e) { alert('打包失败：' + e); }
}

async function patchArtifactPath(id) {
  if (needServe()) return;
  const a = ARTIFACTS.find(x => x.id === id);
  const p = prompt(`「${a ? a.name : ''}」被移动后的新绝对路径：`);
  if (!p || !p.trim()) return;
  try {
    const r = await fetch('/api/artifact/path', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, path: p.trim()})});
    const j = await r.json();
    if (j.ok) {
      a.exists = true; a.moved = true; a.display_path = j.display_path;
      renderArtifacts();
    } else alert(j.error || '补路径失败');
  } catch (e) { alert('补路径失败：' + e); }
}
$('#artFilter').oninput = () => renderArtifacts();
document.querySelectorAll('#artTypeBar input').forEach(c => {
  c.onchange = () => renderArtifacts();
});

// ——— 产物预览（仅 serve 模式）：markdown 本地渲染，html/图片/pdf/视频走 iframe ———
function miniMarkdown(src) {
  // 极简渲染器：标题/代码块/行内代码/粗斜体/链接/列表/引用，预览够用，不引依赖
  const escH = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = s => escH(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\*([^*]+)\*/g, '<i>$1</i>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$1" target="_blank" rel="noopener">$2</a>');
  const blocks = [];
  let inCode = false, list = null;
  for (const line of src.split('\n')) {
    if (/^```/.test(line)) {
      blocks.push(inCode ? '</code></pre>' : '<pre><code>');
      inCode = !inCode; list = null; continue;
    }
    if (inCode) { blocks.push(escH(line) + '\n'); continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      list = null;
      blocks.push(`<h${m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
      if (list !== 'ul') { blocks.push('<ul>'); list = 'ul'; }
      blocks.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.、]\s*(.*)/))) {
      if (list !== 'ol') { blocks.push('<ol>'); list = 'ol'; }
      blocks.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^>\s?(.*)/))) {
      list = null;
      blocks.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else if (line.trim() === '') {
      if (list) { blocks.push(list === 'ul' ? '</ul>' : '</ol>'); list = null; }
    } else {
      if (list) { blocks.push(list === 'ul' ? '</ul>' : '</ol>'); list = null; }
      blocks.push(`<p>${inline(line)}</p>`);
    }
  }
  if (list) blocks.push(list === 'ul' ? '</ul>' : '</ol>');
  if (inCode) blocks.push('</code></pre>');
  return blocks.join('');
}

function ensurePreviewModal() {
  let m = $('#previewModal');
  if (m) return m;
  m = document.createElement('div');
  m.id = 'previewModal';
  m.className = 'preview-modal';
  m.innerHTML = `
    <div class="preview-box">
      <div class="preview-head">
        <span class="preview-title" id="previewTitle"></span>
        <button class="preview-close" id="previewClose">✕ 关闭</button>
      </div>
      <div class="preview-body" id="previewBody"></div>
    </div>`;
  document.body.appendChild(m);
  const close = () => { m.style.display = 'none'; $('#previewBody').innerHTML = ''; };
  $('#previewClose').onclick = close;
  m.onclick = e => { if (e.target === m) close(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  return m;
}

// ——— 深度页 modal（用户决策：不再跳转单独页面）———
// 全局捕获阶段拦截 a[href^="deep/"]：capture 先于卡片上的 stopPropagation，
// 会话卡「深度 →」、想法出处链接、产物挂名链接全部生效；cmd/ctrl 点击保留原行为
function openDeepModal(url) {
  let m = $('#deepModal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'deepModal';
    m.className = 'preview-modal';
    m.innerHTML = `
      <div class="preview-box">
        <div class="preview-head">
          <span class="preview-title">会话深度分析</span>
          <span style="display:flex;gap:10px;align-items:center">
            <a class="art-src" id="deepOpenRaw" href="#" target="_blank" rel="noopener">新窗口打开 ↗</a>
            <button class="preview-close" id="deepClose">✕ 关闭</button>
          </span>
        </div>
        <div class="preview-body" style="padding:0">
          <iframe class="preview-frame" id="deepFrame" style="border:none"></iframe>
        </div>
      </div>`;
    document.body.appendChild(m);
    const close = () => { m.style.display = 'none'; $('#deepFrame').src = 'about:blank'; };
    m.querySelector('#deepClose').onclick = close;
    m.onclick = e => { if (e.target === m) close(); };
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && m.style.display !== 'none') close();
    });
  }
  $('#deepFrame').src = url;
  $('#deepOpenRaw').href = url;
  m.style.display = 'block';
}
document.addEventListener('click', e => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  const a = e.target.closest && e.target.closest('a[href^="deep/"]');
  if (!a) return;
  e.preventDefault();
  openDeepModal(a.getAttribute('href'));
}, true);

async function previewArtifact(id) {
  if (needServe()) return;
  const a = ARTIFACTS.find(x => x.id === id);
  if (!a || !a.exists) return;
  const m = ensurePreviewModal();
  $('#previewTitle').textContent = a.name;
  const body = $('#previewBody');
  body.innerHTML = '<div style="color:var(--ink-dim);font-size:13px">加载中…</div>';
  m.style.display = 'block';  // 不能设 ''：那只会落回 .preview-modal 的 display:none（踩过）
  const ext = (a.name.match(/\.([^.]+)$/) || ['', ''])[1].toLowerCase();
  const url = `/api/artifact/raw?id=${id}`;
  try {
    if (['md', 'markdown', 'txt'].includes(ext)) {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      body.innerHTML = `<div class="md">${miniMarkdown(await r.text())}</div>`;
    } else {
      // html/svg/图片/pdf/视频：浏览器原生渲染；serve 侧已加 CSP sandbox 隔离
      body.innerHTML = `<iframe class="preview-frame" sandbox="allow-scripts" src="${url}"></iframe>`;
    }
  } catch (e) {
    body.innerHTML = `<div style="color:var(--ink-dim);font-size:13px">预览失败：${esc(String(e))}</div>`;
  }
}

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

// resume 指令映射：~/.claude 的会话日常用 tcode（tclaude 别名）恢复；带 cwd 前缀
// session_id 不可信（来自会话源文件），一律 shq 防 shell 注入（review 修正）
const RESUME_CMD = {
  'kimi-code': s => `kimi -r ${shq('session_' + s.session_id)}`,  // kimi 恢复要 session_ 前缀（库里存的是去掉前缀的 uuid）
  'tclaude': s => `tclaude -r ${shq(s.session_id)}`,
  'claude': s => `tcode -r ${shq(s.session_id)}`,
  'tcodex': s => `tcodex resume ${shq(s.session_id)}`,
};
function shq(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }  // shell 单引号转义
function resumeText(s) {
  if (s.source === 'workbuddy') return s.title || s.session_id;  // 无 resume 指令，只复制标题文本（不拼命令）
  const fn = RESUME_CMD[s.source];
  const cmd = fn ? fn(s) : shq(s.title || s.session_id);
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
function selectDay(i) {
  if (!Number.isInteger(i) || i < 0 || i >= DATA.length) return;  // 防污染核心状态（review 修正）
  current = i;
  document.querySelectorAll('.day-picker button').forEach(b =>
    b.classList.toggle('on', +b.dataset.i === current));
  if (journey3d) Journey3D.setSelected(i);
  else document.querySelectorAll('.journey .milestone').forEach((m, mi) =>
    m.classList.toggle('on', mi === current));
  renderDay();
}
$('#dayPicker').innerHTML = days.map((d, i) =>
  `<button data-i="${i}" class="${i === current ? 'on' : ''}">${d.slice(5)}</button>`).join('');
$('#dayPicker').onclick = e => {
  if (e.target.dataset.i === undefined) return;
  selectDay(+e.target.dataset.i);
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
  // 作息（程序确定性统计，与 LLM 叙事独立，老日期也有）
  const rh = d.rhythm || {};
  if (rh.first) {
    const tags = (rh.tags || []).map(t => {
      if (t === '熬夜') return `<span class="rh-tag night">🌙 熬夜到次日 ${esc(rh.late_until || '')}</span>`;
      if (t === '通宵') return `<span class="rh-tag night">🌙 通宵到次日 ${esc(rh.late_until || '')}</span>`;
      if (t === '早起') return `<span class="rh-tag early">🌅 早起 ${esc(rh.first)}</span>`;
      return '';
    }).join('');
    html += `<div class="rhythm">${tags}<span class="rh-span">${esc(rh.first)} – ${esc(rh.last)}</span></div>`;
  }
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

  const q = $('#sessFilter').value || '';
  let filtered = ordered.filter(s => sessionMatches(s, q));
  // 视图切换：当日所有（当天活跃过，含更早创建）/ 当日创建（started_at 落在当天）
  if ($('#sessView').value === 'created')
    filtered = filtered.filter(s => (s.started_at || '').slice(0, 10) === d.date);
  // 全局搜索：跨所有日期；非空时盖过当日列表（用户决策：日报 tab 的总搜索入口）
  const gq = ($('#sessGlobalFilter').value || '').trim();
  if (gq) {
    const hits = [];
    for (const dd of DATA)
      for (const s of dd.sessions)
        if (sessionMatches(s, gq)) hits.push({ ...s, _date: dd.date, _showDate: true });
    hits.sort((a, b) => b._date.localeCompare(a._date) || importance(b) - importance(a));
    $('#sessGlobalHint').textContent = `全局结果 ${hits.length} 条 · 按日期倒序（清空全局搜索返回当日列表）`;
    renderCards($('#sessions'), hits);
  } else {
    $('#sessGlobalHint').textContent = '';
    renderCards($('#sessions'), filtered);
  }
  if (!$('#sessFilter').dataset.bound) {  // 无条件绑定：否则空筛选时首次渲染后输入无反应
    $('#sessFilter').dataset.bound = '1';
    $('#sessFilter').oninput = () => renderDay();  // 输入即重筛当前天
    $('#sessView').onchange = () => renderDay();
    $('#sessGlobalFilter').oninput = () => renderDay();
  }
}

// 会话筛选：标题/cwd/来源 子串匹配；~ 开头展开为 home（从项目 cwd 推断）
const HOME_DIR = (() => {
  for (const p of PROJECTS) { const m = (p.cwd || '').match(/^\/Users\/[^/]+/); if (m) return m[0]; }
  return '/Users';
})();
function sessionMatches(s, q) {
  if (!q) return true;
  q = q.trim().toLowerCase();
  if (q.startsWith('~')) q = (HOME_DIR + q.slice(1)).toLowerCase();
  return ((s.title || '').toLowerCase().includes(q)
    || (s.cwd || '').toLowerCase().includes(q)
    || (s.project || '').toLowerCase().includes(q)
    || (s.source || '').toLowerCase().includes(q));
}

// 通用会话卡片渲染（日报会话列表 + 想法视图的相关会话共用）
function renderCards(container, list) {
  list.forEach(s => { if (s._imp === undefined) s._imp = importance(s); });
  container.innerHTML = list.map((x, i) => {
    const page = pageKey(x.source, x.session_id);
    return `
    <div class="card${x._imp < 0 ? ' chore' : ''}" data-i="${i}">
      <span class="copied">已复制 ✓</span>
      <div class="src">${esc(x.source)}</div>
      <div class="t">${esc(x.title || '(无标题)')}</div>
      ${x.digest && x.digest.what ? `<div class="what">${esc(x.digest.what)}</div>` : ''}
      <div class="meta"><span>${x._showDate ? (x.started_at || '').slice(5, 16).replace('T', ' ') : timeLabel(x)}</span><span>${x.n_user_msgs} 条消息</span></div>
      <a class="deep" href="deep/${page}.html">深度 →</a>
    </div>`;
  }).join('');
  container.querySelectorAll('.card').forEach(card => {
    card.onclick = () => copyText(resumeText(list[+card.dataset.i]), card);
  });
  container.querySelectorAll('a.deep').forEach(el => {
    el.onclick = e => e.stopPropagation();
  });
}

// 卡片时间：创建日不同于所在视图日时带日期前缀（跨日会话一眼可辨，不再误以为按创建日归类）
function timeLabel(x) {
  const st = x.started_at || '';
  const day = x._date || (DATA[current] && DATA[current].date);
  return (st && day && st.slice(0, 10) !== day) ? st.slice(5, 16).replace('T', ' ') : st.slice(11, 16);
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

// hash 恢复放在最后：switchTab 依赖的 const（ARTIFACTS 等）必须都已初始化，
// 否则带 #hash 刷新时 TDZ 报错会中断整个脚本（产物列表空白的根因）
const _hashTab = (location.hash || '').slice(1);
if (['ideas', 'promises', 'projects', 'artifacts'].includes(_hashTab)) switchTab(_hashTab);
