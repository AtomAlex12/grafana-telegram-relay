'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  settings: {},
  routes: [],
  logFilter: '',
  editing: null,       // route being edited (null = new)
  previewSource: 'sample',
  defaultTemplate: '',
  timers: [],
};

// ------------------------------------------------------------------ helpers

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    showLogin();
    throw new Error('Требуется вход');
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ');
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'html') node.innerHTML = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function toast(text, kind = '') {
  const t = el('div', { class: `toast ${kind}` }, text);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString('ru-RU')
    : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function ago(ts) {
  if (!ts) return 'никогда';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.round(s / 60)} мин назад`;
  if (s < 86400) return `${Math.round(s / 3600)} ч назад`;
  return `${Math.round(s / 86400)} д назад`;
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}

// Render a Telegram-HTML string safely (whitelist of tags, http(s) links only).
const TG_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'INS', 'S', 'STRIKE', 'DEL', 'CODE', 'PRE', 'A', 'BLOCKQUOTE', 'TG-SPOILER', 'SPAN']);
function tgHtml(text) {
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const frag = document.createDocumentFragment();
  (function walk(src, dst) {
    for (const n of src.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) { dst.append(n.textContent); continue; }
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      if (!TG_TAGS.has(n.tagName)) { walk(n, dst); continue; }
      const spoiler = n.tagName === 'TG-SPOILER' || (n.tagName === 'SPAN' && n.classList.contains('tg-spoiler'));
      const out = document.createElement(spoiler || n.tagName === 'SPAN' ? 'span' : n.tagName.toLowerCase());
      if (spoiler) out.className = 'spoiler';
      if (n.tagName === 'A') {
        const href = n.getAttribute('href') || '';
        if (/^https?:\/\//i.test(href)) { out.href = href; out.target = '_blank'; out.rel = 'noopener noreferrer'; }
      }
      walk(n, out);
      dst.append(out);
    }
  })(doc.body, frag);
  return frag;
}

function bubble(text, meta) {
  return el('div', { class: 'bubble' }, tgHtml(text), el('span', { class: 'meta' }, meta));
}

function hookUrl(route) {
  const base = (state.settings.public_url || location.origin).replace(/\/$/, '');
  return `${base}/hook/${route.token}`;
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el('textarea', {}, text);
    document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  toast('Скопировано', 'ok');
}

// ------------------------------------------------------------------ auth

async function boot() {
  fetch('/healthz').then(r => r.json()).then(h => {
    $$('.app-version').forEach(x => { x.textContent = h.version ? `v${h.version}` : ''; });
  }).catch(() => {});
  const s = await api('/api/auth/state');
  if (!s.authed) return showLogin(s.configured);
  showApp();
}

function showLogin(configured = true) {
  state.timers.forEach(clearInterval);
  state.timers = [];
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#loginForm').dataset.mode = configured ? 'login' : 'setup';
  $('#loginHint').textContent = configured ? 'Введите пароль администратора' : 'Первый запуск — придумайте пароль администратора';
  $('#loginPassword2').classList.toggle('hidden', configured);
  $('#loginPassword2').required = !configured;
  $('#loginPassword').autocomplete = configured ? 'current-password' : 'new-password';
  $('#loginBtn').textContent = configured ? 'Войти' : 'Сохранить и войти';
  $('#loginPassword').focus();
}

$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const setup = e.target.dataset.mode === 'setup';
  const password = $('#loginPassword').value;
  $('#loginError').textContent = '';
  if (setup && password !== $('#loginPassword2').value) {
    $('#loginError').textContent = 'Пароли не совпадают';
    return;
  }
  try {
    await busy($('#loginBtn'), () => api(setup ? '/api/auth/setup' : '/api/auth/login', { method: 'POST', body: { password } }));
    $('#loginPassword').value = $('#loginPassword2').value = '';
    showApp();
  } catch (err) {
    $('#loginError').textContent = err.message;
  }
});

$('#logoutBtn').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' });
  showLogin(true);
});

async function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  const [settings, tpl] = await Promise.all([api('/api/settings'), api('/api/default-template')]);
  state.settings = settings;
  state.defaultTemplate = tpl.template;
  fillSettings();
  await loadRoutes();
  checkConnection();
  state.timers.push(setInterval(loadStats, 10000));
  state.timers.push(setInterval(() => { if (!$('#tab-log').classList.contains('hidden')) loadLog(); }, 5000));
}

// ------------------------------------------------------------------ tabs

$('#tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-tab]');
  if (!btn) return;
  $$('#tabs button').forEach(b => b.classList.toggle('active', b === btn));
  $$('.tab').forEach(t => t.classList.toggle('hidden', t.id !== `tab-${btn.dataset.tab}`));
  if (btn.dataset.tab === 'log') loadLog();
  if (btn.dataset.tab === 'routes') loadRoutes();
});

function switchTab(name) { $(`#tabs button[data-tab="${name}"]`).click(); }

// ------------------------------------------------------------------ status & stats

async function checkConnection() {
  const pill = $('#statusPill');
  pill.className = 'status-pill busy';
  pill.querySelector('.label').textContent = 'Проверка…';
  try {
    const r = await api('/api/check', { method: 'POST' });
    const mode = r.transport === 'mtproto' ? 'MTProto' : 'Bot API';
    if (r.ok) {
      pill.className = 'status-pill ok';
      pill.querySelector('.label').textContent = `@${r.bot.username} · ${mode} · ${r.ms} мс`;
      pill.title = 'Подключение работает. Нажмите, чтобы проверить ещё раз';
    } else {
      pill.className = 'status-pill err';
      pill.querySelector('.label').textContent = `${mode}: ошибка`;
      pill.title = r.error;
    }
    return r;
  } catch (err) {
    pill.className = 'status-pill err';
    pill.querySelector('.label').textContent = 'Ошибка';
    pill.title = err.message;
    return { ok: false, error: err.message };
  }
}
$('#statusPill').addEventListener('click', async () => {
  const r = await checkConnection();
  if (!r.ok) toast(r.error, 'err');
});

async function loadStats() {
  let s;
  try { s = await api('/api/stats'); } catch { return; }
  const badge = $('#pendingBadge');
  badge.textContent = s.pending;
  badge.classList.toggle('hidden', !s.pending);
  const mode = state.settings.transport === 'mtproto' ? 'MTProto' : 'Bot API';
  $('#stats').replaceChildren(
    el('div', { class: 'stat' }, el('div', { class: 'v' }, s.sent_24h), el('div', { class: 'k' }, 'отправлено за 24 ч'),
      el('div', { class: 'sub' }, `последнее: ${ago(s.last_sent)}`)),
    el('div', { class: `stat ${s.pending ? 'warn' : ''}` }, el('div', { class: 'v' }, s.pending), el('div', { class: 'k' }, 'в очереди')),
    el('div', { class: `stat ${s.failed_24h ? 'err' : ''}` }, el('div', { class: 'v' }, s.failed_24h), el('div', { class: 'k' }, 'ошибок за 24 ч'),
      s.last_error ? el('div', { class: 'sub', title: s.last_error.error }, s.last_error.error) : null),
    el('div', { class: 'stat' }, el('div', { class: 'v' }, state.routes.length), el('div', { class: 'k' }, 'маршрутов'),
      el('div', { class: 'sub' }, `режим: ${mode}`)),
  );
}

// ------------------------------------------------------------------ routes

async function loadRoutes() {
  state.routes = await api('/api/routes');
  renderRoutes();
  loadStats();
}

function renderRoutes() {
  const box = $('#routes');
  if (!state.routes.length) {
    box.replaceChildren(el('div', { class: 'empty' },
      el('b', {}, 'Маршрутов пока нет'),
      'Создайте первый — получите адрес вебхука для Grafana.',
      el('div', { style: 'margin-top:14px' }, el('button', { class: 'btn primary', onclick: () => openRoute(null) }, icon('plus'), 'Новый маршрут'))));
    return;
  }
  box.replaceChildren(...state.routes.map(r => {
    const url = hookUrl(r);
    return el('div', { class: `route ${r.enabled ? '' : 'off'}` },
      el('div', { class: 'route-top' },
        el('span', { class: 'route-name' }, r.name),
        el('span', { class: `chip ${r.enabled ? 'ok' : 'off'}` }, r.enabled ? 'включён' : 'выключен'),
        r.split ? el('span', { class: 'chip' }, 'по одному алерту') : null,
        r.silent_resolved ? el('span', { class: 'chip' }, 'тихие «решено»') : null,
        el('div', { class: 'route-actions' },
          el('button', { class: 'btn sm', onclick: (e) => testRoute(r, e.currentTarget) }, 'Тест'),
          el('button', { class: 'btn sm', onclick: () => openRoute(r) }, 'Изменить'),
          el('button', { class: 'btn sm danger', onclick: () => deleteRoute(r) }, 'Удалить'))),
      el('div', { class: 'hookurl' },
        el('span', { class: 'method' }, 'POST'),
        el('span', { title: url }, url),
        el('button', { class: 'icon-btn', title: 'Копировать', onclick: () => copy(url) }, icon('copy'))),
      el('div', { class: 'route-meta' },
        el('span', {}, 'Чат: ', el('code', {}, r.chat_id), r.thread_id ? [' · тема ', el('code', {}, r.thread_id)] : null),
        el('span', {}, `Последний вебхук: ${ago(r.last_hit)}`),
        el('button', { class: 'link', onclick: () => regenToken(r) }, 'сменить адрес')));
  }));
}

async function testRoute(route, btn) {
  const r = await busy(btn, () => api(`/api/routes/${route.id}/test`, { method: 'POST' }));
  if (r.ok) toast(`Тест отправлен в «${route.name}»`, 'ok');
  else toast(`Не отправлено: ${r.error}`, 'err');
  loadStats();
}

async function deleteRoute(route) {
  if (!confirm(`Удалить маршрут «${route.name}»? Grafana перестанет доставлять по его адресу.`)) return;
  await api(`/api/routes/${route.id}`, { method: 'DELETE' });
  toast('Маршрут удалён');
  loadRoutes();
}

async function regenToken(route) {
  if (!confirm('Сгенерировать новый адрес вебхука? Старый перестанет работать — его нужно будет заменить в Grafana.')) return;
  await api(`/api/routes/${route.id}/regen-token`, { method: 'POST' });
  toast('Адрес обновлён', 'ok');
  loadRoutes();
}

// ------------------------------------------------------------------ route editor

const routeDialog = $('#routeDialog');
const routeForm = $('#routeForm');

$('#addRouteBtn').addEventListener('click', () => openRoute(null));

function openRoute(route) {
  state.editing = route;
  $('#routeDialogTitle').textContent = route ? `Маршрут «${route.name}»` : 'Новый маршрут';
  $('#routeError').textContent = '';
  const f = routeForm.elements;
  f.name.value = route ? route.name : '';
  f.chat_id.value = route ? route.chat_id : '';
  f.thread_id.value = route && route.thread_id ? route.thread_id : '';
  f.enabled.checked = route ? route.enabled : true;
  f.split.checked = route ? route.split : false;
  f.silent_resolved.checked = route ? route.silent_resolved : false;
  f.template.value = route ? route.template : state.defaultTemplate;
  const hasLast = !!(route && route.has_payload);
  $('#lastPayloadBtn').disabled = !hasLast;
  $('#lastPayloadBtn').title = hasLast ? '' : 'Grafana ещё ничего не присылала на этот маршрут';
  setPreviewSource(hasLast ? 'last' : 'sample');
  routeDialog.showModal();
  f.name.focus();
}

function setPreviewSource(src) {
  state.previewSource = src;
  $$('#previewSource button').forEach(b => b.classList.toggle('active', b.dataset.source === src));
  refreshPreview();
}

$('#previewSource').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-source]');
  if (b && !b.disabled) setPreviewSource(b.dataset.source);
});

let previewTimer = null;
let previewSeq = 0;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 350);
}

async function refreshPreview() {
  const seq = ++previewSeq;
  const f = routeForm.elements;
  const box = $('#preview');
  let r;
  try {
    r = await api('/api/preview', {
      method: 'POST',
      body: {
        template: f.template.value,
        split: f.split.checked,
        source: state.previewSource,
        route_id: state.editing ? state.editing.id : null,
      },
    });
  } catch (err) {
    r = { ok: false, error: err.message };
  }
  if (seq !== previewSeq) return;
  $('#payloadView').textContent = r.payload ? JSON.stringify(r.payload, null, 2) : '';
  if (!r.ok) {
    box.replaceChildren(el('div', { class: 'preview-error' }, r.error));
    return;
  }
  if (!r.messages.length) {
    box.replaceChildren(el('div', { class: 'muted small' }, 'Шаблон дал пустой текст — сообщение не будет отправлено.'));
    return;
  }
  const now = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  box.replaceChildren(...r.messages.map(m =>
    bubble(m.text, `${m.text.length} симв. · ${f.silent_resolved.checked && m.silent ? '🔕 ' : ''}${now}`)));
}

routeForm.addEventListener('input', (e) => {
  if (['template', 'split', 'silent_resolved'].includes(e.target.name)) schedulePreview();
});

routeForm.elements.template.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab') return;
  e.preventDefault();
  const ta = e.target;
  ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end');
  schedulePreview();
});

$('#resetTplBtn').addEventListener('click', () => {
  if (!confirm('Заменить шаблон на стандартный?')) return;
  routeForm.elements.template.value = state.defaultTemplate;
  refreshPreview();
});

$$('[data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));

routeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = routeForm.elements;
  const thread = f.thread_id.value.trim();
  if (thread && !/^\d+$/.test(thread)) {
    $('#routeError').textContent = 'ID темы должен быть числом';
    return;
  }
  const body = {
    name: f.name.value.trim(),
    chat_id: f.chat_id.value.trim(),
    thread_id: thread ? Number(thread) : null,
    template: f.template.value,
    split: f.split.checked,
    silent_resolved: f.silent_resolved.checked,
    enabled: f.enabled.checked,
  };
  const btn = routeForm.querySelector('button[type=submit]');
  try {
    const saved = await busy(btn, () => state.editing
      ? api(`/api/routes/${state.editing.id}`, { method: 'PUT', body })
      : api('/api/routes', { method: 'POST', body }));
    routeDialog.close();
    toast(state.editing ? 'Маршрут сохранён' : 'Маршрут создан — скопируйте адрес вебхука в Grafana', 'ok');
    await loadRoutes();
    loadStats();
    if (!state.editing) copy(hookUrl(saved)).catch(() => {});
  } catch (err) {
    $('#routeError').textContent = err.message;
  }
});

// ------------------------------------------------------------------ chat finder

const chatsDialog = $('#chatsDialog');
$('#findChatBtn').addEventListener('click', () => { chatsDialog.showModal(); loadChats(); });
$('#reloadChatsBtn').addEventListener('click', loadChats);

async function loadChats() {
  const box = $('#chatsList');
  box.replaceChildren(el('div', { class: 'muted' }, 'Загрузка…'));
  let r;
  try { r = await api('/api/chats'); } catch (err) {
    box.replaceChildren(el('div', { class: 'preview-error' }, err.message));
    return;
  }
  if (!r.chats.length) {
    box.replaceChildren(el('div', { class: 'muted' }, 'Бот пока не видел ни одного сообщения. Напишите что-нибудь в нужный чат и нажмите «Обновить».'));
    return;
  }
  const types = { private: 'личка', group: 'группа', supergroup: 'супергруппа', channel: 'канал' };
  box.replaceChildren(...r.chats.map(c => el('button', {
    type: 'button', class: 'chat-item',
    onclick: () => {
      routeForm.elements.chat_id.value = c.chat_id;
      routeForm.elements.thread_id.value = c.thread_id || '';
      chatsDialog.close();
    },
  }, el('div', {},
    el('div', { class: 't' }, c.title, c.thread_id ? ` → ${c.topic || 'тема #' + c.thread_id}` : ''),
    el('div', { class: 's' }, `${types[c.type] || c.type} · ${c.chat_id}${c.thread_id ? ' · тема ' + c.thread_id : ''}`)))));
}

// ------------------------------------------------------------------ log

$('#logFilter').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-status]');
  if (!b) return;
  state.logFilter = b.dataset.status;
  $$('#logFilter button').forEach(x => x.classList.toggle('active', x === b));
  loadLog();
});

$('#clearLogBtn').addEventListener('click', async () => {
  if (!confirm('Удалить из журнала отправленные и ошибочные сообщения? Очередь не трогается.')) return;
  await api('/api/messages', { method: 'DELETE' });
  loadLog(); loadStats();
});

const openMessages = new Set();

async function loadLog() {
  const q = state.logFilter ? `?status=${state.logFilter}` : '';
  const msgs = await api(`/api/messages${q}`);
  const box = $('#log');
  if (!msgs.length) {
    box.replaceChildren(el('div', { class: 'empty' }, el('b', {}, 'Пусто'), 'Здесь появятся сообщения, пришедшие от Grafana.'));
    return;
  }
  const labels = { sent: 'отправлено', pending: 'в очереди', failed: 'ошибка' };
  box.replaceChildren(...msgs.map(m => {
    const plain = m.text.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ');
    const node = el('div', { class: `msg ${openMessages.has(m.id) ? 'open' : ''}` },
      el('div', {
        class: 'msg-head',
        onclick: () => { node.classList.toggle('open'); openMessages[node.classList.contains('open') ? 'add' : 'delete'](m.id); },
      },
        el('span', { class: 'time' }, fmtTime(m.created)),
        el('span', { class: `status ${m.status}` }, labels[m.status] || m.status),
        el('span', { class: 'route-n' }, m.route_name || '—'),
        el('span', { class: 'snippet' }, plain)),
      el('div', { class: 'msg-body' },
        el('div', { class: 'tg-chat' }, bubble(m.text, fmtTime(m.sent_at || m.created))),
        el('div', { class: 'msg-info' },
          el('div', {}, 'Чат: ', el('code', {}, m.chat_id), m.thread_id ? [' · тема ', el('code', {}, m.thread_id)] : null),
          el('div', {}, `Попыток: ${m.attempts}`),
          m.sent_at ? el('div', {}, `Доставлено: ${fmtTime(m.sent_at)}`) : null,
          m.status === 'pending' && m.attempts ? el('div', {}, `Следующая попытка: ${fmtTime(m.next_try)}`) : null,
          m.silent ? el('div', {}, '🔕 без звука') : null,
          m.error ? el('div', { class: 'err' }, m.error) : null,
          m.status !== 'sent' ? el('div', {}, el('button', {
            class: 'btn sm', onclick: async (e) => {
              await busy(e.currentTarget, () => api(`/api/messages/${m.id}/retry`, { method: 'POST' }));
              toast('Поставлено в очередь'); setTimeout(loadLog, 800);
            },
          }, 'Отправить сейчас')) : null)));
    return node;
  }));
}

// ------------------------------------------------------------------ settings

const settingsForm = $('#settingsForm');
const SECRET_FIELDS = ['bot_token', 'mtproto_api_hash', 'mtproto_secret'];

function fillSettings() {
  const s = state.settings;
  const f = settingsForm.elements;
  for (const k of ['botapi_proxy', 'botapi_base', 'mtproto_api_id', 'mtproto_host', 'mtproto_port', 'max_attempts', 'public_url']) {
    f[k].value = s[k] || '';
  }
  for (const k of SECRET_FIELDS) {
    f[k].value = '';
    f[k].placeholder = s[`${k}_hint`] ? `сохранён (${s[`${k}_hint`]}) — оставьте пустым, чтобы не менять` : f[k].dataset.ph || f[k].placeholder;
    f[k].dataset.ph = f[k].dataset.ph || f[k].placeholder;
  }
  for (const r of $$('input[name=transport]')) r.checked = r.value === (s.transport || 'botapi');
  syncTransport();
}

function syncTransport() {
  const t = settingsForm.elements.transport.value || 'botapi';
  $$('.transport-fields').forEach(x => x.classList.toggle('hidden', x.dataset.for !== t));
}
$('#transport').addEventListener('change', syncTransport);

// Paste a tg://proxy or t.me/proxy link into the host field — split it into host/port/secret.
settingsForm.elements.mtproto_host.addEventListener('input', (e) => {
  const v = e.target.value.trim();
  const m = v.match(/^(?:tg:\/\/|https?:\/\/t\.me\/)proxy\?(.+)$/i);
  if (!m) return;
  const p = new URLSearchParams(m[1]);
  const f = settingsForm.elements;
  if (p.get('server')) f.mtproto_host.value = p.get('server');
  if (p.get('port')) f.mtproto_port.value = p.get('port');
  if (p.get('secret')) f.mtproto_secret.value = p.get('secret');
  toast('Ссылка на прокси разобрана', 'ok');
});

settingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  await saveSettings(e.submitter);
});

async function saveSettings(btn) {
  const f = settingsForm.elements;
  const body = { transport: f.transport.value || 'botapi' };
  for (const k of ['botapi_proxy', 'botapi_base', 'mtproto_api_id', 'mtproto_host', 'mtproto_port', 'max_attempts', 'public_url']) {
    body[k] = f[k].value.trim();
  }
  for (const k of SECRET_FIELDS) if (f[k].value.trim()) body[k] = f[k].value.trim();
  try {
    state.settings = await busy(btn, () => api('/api/settings', { method: 'PUT', body }));
    fillSettings();
    renderRoutes();
    toast('Настройки сохранены', 'ok');
    const r = await checkConnection();
    if (!r.ok) toast(`Подключение не работает: ${r.error}`, 'err');
    else toast(`Бот @${r.bot.username} на связи (${r.ms} мс)`, 'ok');
    loadStats();
    return true;
  } catch (err) {
    toast(err.message, 'err');
    return false;
  }
}

$('#checkBtn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const dirty = [...settingsForm.elements].some(x => x.name && SECRET_FIELDS.includes(x.name) && x.value.trim());
  if (dirty) { await saveSettings(btn); return; }
  const r = await busy(btn, checkConnection);
  if (r.ok) toast(`Бот @${r.bot.username} на связи (${r.ms} мс)`, 'ok');
  else toast(r.error, 'err');
});

$('#passwordForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    await busy(e.submitter, () => api('/api/password', { method: 'POST', body: { old: f.old.value, new: f.new.value } }));
    e.target.reset();
    toast('Пароль изменён, остальные сессии завершены', 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
});

boot().catch(err => toast(err.message, 'err'));
