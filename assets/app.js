'use strict';

const DATA_URL = 'data/events.json';
const LOCAL_KEY = 'nct.localWatch';
const VIEW_KEY = 'nct.view';
const PAGE = 150;
const NEW_DAYS = 7;
const TZ = 'America/Chicago';
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const STATUS = {
  canceled: ['已取消', 'bad'],
  postponed: ['已延期', 'warn'],
  rescheduled: ['已改期', 'warn'],
  offsale: ['停售', 'muted'],
};

const state = {
  data: null, events: [], view: null, q: '', genre: '', venue: '', month: '',
  nearby: true, limit: PAGE, local: [],
};

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

/** Loose name key; mirrors norm() in scripts/fetch_events.py. */
function norm(s) {
  return String(s || '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
    .replace(/&/g, ' and ').replace(/[^\p{L}\p{N}]+/gu, ' ').trim().replace(/^the /, '');
}

function parseDay(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

const dtParts = new Intl.DateTimeFormat('en-US', {
  timeZone: TZ, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
/** "11月7日 10:00" in Nashville time. */
function fmtInstant(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = Object.fromEntries(dtParts.formatToParts(d).map((x) => [x.type, x.value]));
  return `${p.month}月${p.day}日 ${p.hour}:${p.minute}`;
}

function fmtDay(iso) {
  const d = parseDay(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEK[d.getDay()]}`;
}

function fmtPrice(p) {
  if (!p) return '';
  const f = (n) => '$' + (Number.isInteger(n) ? n : n.toFixed(2));
  return p.min === p.max ? f(p.min) : `${f(p.min)}–${f(p.max)}`;
}

function daysBetween(a, b) {
  return Math.round((parseDay(b) - parseDay(a)) / 864e5);
}

/* ------------------------------------------------------------------ data prep */

function watchNames() {
  const server = (state.data.watchlist || []).map((w) => ({ name: w.name, found: w.found, local: false }));
  const taken = new Set(server.map((w) => norm(w.name)));
  const local = state.local.filter((n) => !taken.has(norm(n))).map((n) => ({ name: n, found: null, local: true }));
  return [...server, ...local];
}

function localMatches(ev) {
  if (!state.local.length) return [];
  const artists = (ev.artists || []).map(norm);
  const title = ` ${norm(ev.name)} `;
  const tribute = /tribute|salute|experience|cover band/i.test(ev.name);
  return state.local.filter((n) => {
    const k = norm(n);
    return k && (artists.includes(k) || (!tribute && title.includes(` ${k} `)));
  });
}

function prepare() {
  const today = state.data.generated_at ? state.data.generated_at.slice(0, 10) : null;
  const since = state.data.tracking_since;
  state.events = (state.data.events || []).map((ev) => {
    const watch = [...(ev.watch || [])];
    for (const n of localMatches(ev)) if (!watch.some((w) => norm(w) === norm(n))) watch.push(n);
    return {
      ...ev,
      _watch: watch,
      _watchKeys: watch.map(norm),
      _new: Boolean(ev.first_seen && since && today && ev.first_seen > since && daysBetween(ev.first_seen, today) <= NEW_DAYS),
      _search: norm([ev.name, ...(ev.artists || []), ev.venue, ev.city, ev.genre, ev.subgenre].join(' ')),
    };
  });
}

/* ------------------------------------------------------------------ rendering */

function renderMeta() {
  const d = state.data;
  const meta = $('#meta');
  const notice = $('#notice');
  if (!d.generated_at) {
    meta.textContent = '还没有数据';
    notice.innerHTML = `<div class="notice">首次使用：在仓库 Settings → Secrets 中添加 <code>TICKETMASTER_API_KEY</code>，然后在 Actions 里手动运行一次 “Update concerts”。详见 README。</div>`;
    return;
  }
  const when = fmtInstant(d.generated_at);
  const total = state.events.filter((e) => e.area !== 'nearby').length;
  meta.innerHTML = `${esc(d.center || 'Nashville')} 周边 ${d.radius_miles ?? 30} 英里 · ${total} 场演出 · 更新于 ${esc(when)}（纳什维尔时间）`;
  notice.innerHTML = d.demo ? '<div class="notice warn">⚠️ 当前是示例数据，仅用于预览页面效果。</div>' : '';
  $('#cal-link').innerHTML = `<a href="${esc(new URL('data/watchlist.ics', location.href).href.replace(/^https?:/, 'webcal:'))}">订阅关注歌手日历</a>`;
}

function renderWatch() {
  const names = watchNames();
  const grid = $('#watch-grid');
  if (!names.length) {
    grid.innerHTML = '<p class="empty-inline">还没有关注的歌手。编辑 <code>config.json</code> 的 watchlist，或点右上角添加本地关注。</p>';
    return;
  }
  const cards = names.map((w, i) => {
    const key = norm(w.name);
    const evs = state.events.filter((e) => e._watchKeys.includes(key) && e.status !== 'canceled');
    const home = evs.filter((e) => e.area !== 'nearby');
    const near = evs.filter((e) => e.area === 'nearby');
    return { ...w, home, near, i };
  });
  const rank = (c) => (c.home.length ? 0 : c.near.length ? 1 : 2);
  cards.sort((a, b) => rank(a) - rank(b) || a.i - b.i);

  grid.innerHTML = cards.map((c) => {
    let main;
    let sub = '';
    if (c.home.length) {
      const e = c.home[0];
      main = `<span class="wc-next">${esc(fmtDay(e.date))} · ${esc(e.venue || '')}</span>`;
      if (c.home.length > 1) sub = `纳什维尔共 ${c.home.length} 场`;
    } else if (state.data.generated_at && c.found === false && !c.local) {
      main = '<span class="wc-none">Ticketmaster 上未找到该歌手</span>';
    } else {
      main = '<span class="wc-none">纳什维尔暂无场次</span>';
    }
    if (c.near.length) {
      const e = c.near[0];
      const nearText = `周边 ${c.near.length} 场 · 最近 ${e.city || ''} ${fmtDay(e.date).split(' ')[0]}`;
      sub = sub ? `${sub} · ${nearText}` : nearText;
    }
    const cls = c.home.length ? 'has-home' : c.near.length ? 'has-near' : '';
    return `<button class="watch-card ${cls}" type="button" data-artist="${esc(c.name)}">
      <span class="wc-name">${esc(c.name)}${c.local ? '<span class="tag">本地</span>' : ''}</span>
      ${main}
      ${sub ? `<span class="wc-sub">${esc(sub)}</span>` : ''}
    </button>`;
  }).join('');
}

function fillSelect(el, label, entries, current) {
  el.innerHTML = `<option value="">${label}</option>` + entries
    .map(([value, text]) => `<option value="${esc(value)}"${value === current ? ' selected' : ''}>${esc(text)}</option>`).join('');
}

function renderFilters() {
  const count = (key, pool) => {
    const m = new Map();
    for (const e of pool) if (e[key]) m.set(e[key], (m.get(e[key]) || 0) + 1);
    return m;
  };
  const home = state.events.filter((e) => e.area !== 'nearby');
  const genres = [...count('genre', state.events)].sort((a, b) => b[1] - a[1]);
  fillSelect($('#genre'), '全部曲风', genres.map(([g, n]) => [g, `${g} (${n})`]), state.genre);
  const venues = [...count('venue', home)].sort((a, b) => a[0].localeCompare(b[0]));
  fillSelect($('#venue'), '全部场馆', venues.map(([v, n]) => [v, `${v} (${n})`]), state.venue);
  const months = [...new Set(state.events.map((e) => e.date.slice(0, 7)))].sort();
  fillSelect($('#month'), '全部月份', months.map((m) => [m, `${m.slice(0, 4)}年${Number(m.slice(5))}月`]), state.month);
  $('#q').value = state.q;
  $('#nearby').checked = state.nearby;
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.view === state.view));
}

function visible() {
  const q = norm(state.q);
  return state.events.filter((e) => {
    if (state.view === 'watch' && !e._watch.length) return false;
    if (state.view === 'major' && !e.major) return false;
    if (e.area === 'nearby' && !state.nearby) return false;
    if (state.genre && e.genre !== state.genre) return false;
    if (state.venue && e.venue !== state.venue) return false;
    if (state.month && !e.date.startsWith(state.month)) return false;
    if (q && !e._search.includes(q)) return false;
    return true;
  });
}

function badge(text, kind) {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function eventRow(e) {
  const d = parseDay(e.date);
  const badges = [];
  if (e._new) badges.push(badge('NEW', 'new'));
  for (const n of e._watch) badges.push(badge(`★ ${n}`, 'watch'));
  if (e.area === 'nearby') {
    const where = [e.city, e.state].filter(Boolean).join(', ');
    badges.push(badge(`周边 · ${where}${e.distance_mi != null ? ` · ${Math.round(e.distance_mi)} 英里` : ''}`, 'nearby'));
  }
  const upcomingSale = e.onsale && new Date(e.onsale) > Date.now();
  if (STATUS[e.status] && !(e.status === 'offsale' && upcomingSale)) badges.push(badge(...STATUS[e.status]));
  if (upcomingSale) badges.push(badge(`${fmtInstant(e.onsale)} 开售`, 'sale'));
  const presale = (e.presales || []).find((p) => new Date(p.end || p.start) > Date.now());
  if (presale) badges.push(badge(`预售 ${fmtInstant(presale.start)}${presale.name ? ` · ${presale.name}` : ''}`, 'sale soft'));
  if (e.genre) badges.push(badge(e.genre, 'muted'));
  if (e.source === 'manual') badges.push(badge('手动添加', 'muted'));

  const artists = (e.artists || []).filter((a) => !norm(e.name).includes(norm(a)));
  const place = [e.venue, e.area === 'nearby' ? null : e.city !== 'Nashville' ? e.city : null].filter(Boolean).join(' · ');
  const time = e.time ? ` · ${e.time}` : '';
  const img = e.image
    ? `<img class="ev-img" src="${esc(e.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
    : '<div class="ev-img placeholder" aria-hidden="true">♪</div>';

  return `<article class="event${e._watch.length ? ' is-watch' : ''}${e.status === 'canceled' ? ' is-canceled' : ''}">
    <div class="ev-date" aria-label="${esc(fmtDay(e.date))}"><span class="m">${d.getMonth() + 1}月</span><span class="d">${d.getDate()}</span><span class="w">${WEEK[d.getDay()]}</span></div>
    ${img}
    <div class="ev-main">
      <h3 class="ev-title">${esc(e.name)}</h3>
      ${artists.length ? `<p class="ev-artists">${esc(artists.join(' · '))}</p>` : ''}
      <p class="ev-venue">${esc(place)}${esc(time)}</p>
      ${e.note ? `<p class="ev-note">${esc(e.note)}</p>` : ''}
      <div class="badges">${badges.join('')}</div>
    </div>
    <div class="ev-side">
      ${e.price ? `<span class="price">${esc(fmtPrice(e.price))}</span>` : ''}
      ${e.url ? `<a class="btn primary" href="${esc(e.url)}" target="_blank" rel="noopener">购票</a>` : ''}
      <button class="btn" type="button" data-ics="${esc(e.id)}" title="下载 .ics 加入日历">日历</button>
    </div>
  </article>`;
}

function emptyMessage() {
  if (!state.data.generated_at) return '数据还没生成。配置好 API key 并运行一次 GitHub Action 后，这里会显示演出。';
  if (state.view === 'watch' && !state.q && !state.genre && !state.venue && !state.month) {
    return '关注的歌手目前在纳什维尔及周边没有已公布的演出。有新场次时网站会自动更新，并在 GitHub 开 Issue 提醒你。';
  }
  return '没有符合条件的演出，试试切换到「全部」或清空筛选。';
}

function renderList() {
  const list = visible();
  $('#count').textContent = `${list.length} 场`;
  const box = $('#list');
  if (!list.length) {
    box.innerHTML = `<p class="empty">${esc(emptyMessage())}</p>`;
    return;
  }
  const shown = list.slice(0, state.limit);
  let html = '';
  let month = '';
  for (const e of shown) {
    const m = e.date.slice(0, 7);
    if (m !== month) {
      month = m;
      const n = list.filter((x) => x.date.startsWith(m)).length;
      html += `<h3 class="month">${m.slice(0, 4)}年${Number(m.slice(5))}月 <span>${n} 场</span></h3>`;
    }
    html += eventRow(e);
  }
  if (list.length > shown.length) {
    html += `<button class="btn more" type="button" data-more>再显示 ${Math.min(PAGE, list.length - shown.length)} 场（共 ${list.length}）</button>`;
  }
  box.innerHTML = html;
}

function renderAll() {
  renderMeta();
  renderWatch();
  renderFilters();
  renderList();
}

/* ------------------------------------------------------------------ calendar export */

function icsText(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/([,;])/g, '\\$1');
}
function icsStamp(d) {
  return d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
}
function icsFold(line) {
  const out = [];
  let cur = '';
  let size = 0;
  for (const ch of line) {
    const n = new TextEncoder().encode(ch).length;
    if (size + n > 75) { out.push(cur); cur = ' ' + ch; size = 1 + n; } else { cur += ch; size += n; }
  }
  out.push(cur);
  return out.join('\r\n');
}

function downloadIcs(e) {
  const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//nashville-concerts//event//ZH', 'BEGIN:VEVENT',
    `UID:${e.id}@nashville-concerts`, `DTSTAMP:${icsStamp(new Date())}`];
  if (e.start_utc) {
    const s = new Date(e.start_utc);
    lines.push(`DTSTART:${icsStamp(s)}`, `DTEND:${icsStamp(new Date(s.getTime() + 3 * 3600e3))}`);
  } else if (e.time) {
    lines.push(`DTSTART;TZID=${TZ}:${e.date.replace(/-/g, '')}T${e.time.replace(':', '')}00`, 'DURATION:PT3H');
  } else {
    lines.push(`DTSTART;VALUE=DATE:${e.date.replace(/-/g, '')}`);
  }
  lines.push(`SUMMARY:${icsText(e.name)}`);
  const where = [e.venue, e.city, e.state].filter(Boolean).join(', ');
  if (where) lines.push(`LOCATION:${icsText(where)}`);
  if (e.url) lines.push(`URL:${e.url}`, `DESCRIPTION:${icsText(`购票：${e.url}`)}`);
  lines.push('END:VEVENT', 'END:VCALENDAR');
  const blob = new Blob([lines.map(icsFold).join('\r\n') + '\r\n'], { type: 'text/calendar;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${e.date}-${norm(e.name).replace(/ /g, '-').slice(0, 40) || 'concert'}.ics`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ------------------------------------------------------------------ local watchlist */

function renderLocalList() {
  const ul = $('#local-list');
  ul.innerHTML = state.local.length
    ? state.local.map((n, i) => `<li><span>${esc(n)}</span><button type="button" class="link-btn" data-remove="${i}">移除</button></li>`).join('')
    : '<li class="hint">还没有本地关注的歌手。</li>';
}

function saveLocal(list) {
  state.local = list;
  store.set(LOCAL_KEY, list);
  prepare();
  renderLocalList();
  renderAll();
}

function addLocal() {
  const input = $('#local-input');
  const name = input.value.trim();
  if (!name) return;
  if (!state.local.some((n) => norm(n) === norm(name))) saveLocal([...state.local, name]);
  input.value = '';
  input.focus();
}

/* ------------------------------------------------------------------ wiring */

function update(patch) {
  Object.assign(state, patch, { limit: PAGE });
  if ('view' in patch) store.set(VIEW_KEY, state.view);
  renderFilters();
  renderList();
}

function bindUI() {
  for (const b of document.querySelectorAll('.tabs button')) b.addEventListener('click', () => update({ view: b.dataset.view }));
  let timer;
  $('#q').addEventListener('input', (ev) => {
    clearTimeout(timer);
    timer = setTimeout(() => update({ q: ev.target.value }), 120);
  });
  $('#genre').addEventListener('change', (ev) => update({ genre: ev.target.value }));
  $('#venue').addEventListener('change', (ev) => update({ venue: ev.target.value }));
  $('#month').addEventListener('change', (ev) => update({ month: ev.target.value }));
  $('#nearby').addEventListener('change', (ev) => update({ nearby: ev.target.checked }));

  $('#list').addEventListener('click', (ev) => {
    const ics = ev.target.closest('[data-ics]');
    if (ics) {
      const e = state.events.find((x) => x.id === ics.dataset.ics);
      if (e) downloadIcs(e);
      return;
    }
    if (ev.target.closest('[data-more]')) {
      state.limit += PAGE;
      renderList();
    }
  });

  // Image errors don't bubble, so listen in the capture phase.
  $('#list').addEventListener('error', (ev) => {
    if (ev.target.tagName !== 'IMG') return;
    const ph = document.createElement('div');
    ph.className = 'ev-img placeholder';
    ph.setAttribute('aria-hidden', 'true');
    ph.textContent = '♪';
    ev.target.replaceWith(ph);
  }, true);

  $('#watch-grid').addEventListener('click', (ev) => {
    const card = ev.target.closest('[data-artist]');
    if (!card) return;
    update({ view: 'watch', q: card.dataset.artist, genre: '', venue: '', month: '' });
    $('#events-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  const dialog = $('#local-dialog');
  $('#manage-local').addEventListener('click', () => {
    renderLocalList();
    dialog.showModal();
    $('#local-input').focus();
  });
  $('#local-add').addEventListener('click', addLocal);
  $('#local-input').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addLocal(); }
  });
  $('#local-list').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-remove]');
    if (btn) saveLocal(state.local.filter((_, i) => i !== Number(btn.dataset.remove)));
  });
}

async function init() {
  state.local = store.get(LOCAL_KEY, []);
  state.view = store.get(VIEW_KEY, null);
  bindUI();
  try {
    const res = await fetch(DATA_URL, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (err) {
    $('#meta').textContent = '数据加载失败';
    $('#list').innerHTML = `<p class="empty">无法读取 ${esc(DATA_URL)}（${esc(err.message)}）。本地预览请用 <code>python3 -m http.server</code> 启动，而不是直接双击打开 HTML。</p>`;
    return;
  }
  prepare();
  if (!['watch', 'major', 'all'].includes(state.view)) {
    state.view = state.events.some((e) => e._watch.length) ? 'watch' : 'major';
  }
  renderAll();
}

init();
