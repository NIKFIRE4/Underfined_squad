'use strict';
const $ = id => document.getElementById(id);
const STORAGE_KEY = 'pool-local-job-v1';
const TOP_K = 10;
const PAGE_SIZE = 50;
const SVG_NS = 'http://www.w3.org/2000/svg';

const state = {
  health: null,
  selection: [],      // [{file, kind}] — выбранные файлы, живут в памяти вкладки
  autoStart: true,    // после ошибки обработки ждём изменения набора файлов или явного «Повторить»
  busy: false,
  jobId: null,
  job: null,
  group: 'verified',  // 'verified' — ранжированы моделью, 'unverified' — новые компании без оценки модели
  lots: [],
  hasMore: false,
  total: null,
  query: '',
  lotId: null,
  legacy: false,
  cardRequest: null,
  lastFocus: null,
};

/* ---------- Утилиты ---------- */
const fmt = n => Number(n || 0).toLocaleString('ru-RU');
const fmtScore = v => Number(v).toLocaleString('ru-RU', {maximumFractionDigits: 1});
const fmtSize = b => b < 1024 * 1024 ? Math.max(1, Math.round(b / 1024)) + ' КБ' : (b / 1024 / 1024).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' МБ';
function fmtMoney(v) {
  const n = Number(String(v).replace(/\s/g, '').replace(',', '.'));
  return Number.isFinite(n) && String(v).trim() !== '' ? n.toLocaleString('ru-RU', {maximumFractionDigits: 0}) + ' ₽' : '';
}
function fmtDate(v) {
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long', year: 'numeric'});
}
const yes = v => ['true', '1', 'да', 'yes'].includes(String(v).trim().toLowerCase());
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};
// Без обогащения модель отдаёт ИНН вместо названия — показываем это честно
const hasName = c => c.supplier_name && c.supplier_name !== c.supplier_inn;
const displayName = c => hasName(c) ? c.supplier_name : 'ИНН ' + c.supplier_inn;
// После обогащения название может так и не найтись: ЕГРЮЛ и «Прозрачный бизнес» не ответили
const noNameNote = c => /^обогащено/i.test(c.enrichment_status || '') ? 'Название не найдено в открытых источниках' : 'Название появится после обогащения';
// Модель кладёт вероятность победы первой причиной: «Вероятность победы по модели: 30%»
const WIN_RE = /^Вероятность победы по модели:\s*([\d.,]+)\s*%/;
const winChance = c => (c.reasons || []).map(r => WIN_RE.exec(r)).find(Boolean)?.[1] ?? null;
const pWin = c => c.explanation?.p_win ?? (winChance(c) != null ? Number(winChance(c).replace(',', '.')) / 100 : null);
const fmtChance = p => p < 0.01 ? 'меньше 1%' : Math.round(p * 100) + '%';
const mainReason = c => (c.reasons || []).find(r => !WIN_RE.test(r)) || '—';
const UNKNOWN_ROLE = /^не определена$/i;

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'style') el.style.cssText = value;  // CSSOM, а не атрибут: CSP запрещает inline-стили
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) el.append(child);
  return el;
}

const ICONS = {
  check: '<path d="m3.5 8.5 3 3 6-7"/>',
  plus: '<path d="M8 3.5v9M3.5 8h9"/>',
  sheet: '<rect x="2.5" y="2.5" width="11" height="11" rx="2"/><path d="M2.5 6.25h11M2.5 9.75h11M6.25 6.25v7.25"/>',
  x: '<path d="m4.5 4.5 7 7M11.5 4.5l-7 7"/>',
  copy: '<rect x="5.25" y="5.25" width="8.5" height="8.5" rx="1.5"/><path d="M10.75 5.25V3.75c0-.83-.67-1.5-1.5-1.5h-5.5c-.83 0-1.5.67-1.5 1.5v5.5c0 .83.67 1.5 1.5 1.5h1.5"/>',
  external: '<path d="M9.5 2.5h4v4M13.5 2.5 7.75 8.25M11.5 9.5v3c0 .55-.45 1-1 1h-7c-.55 0-1-.45-1-1v-7c0-.55.45-1 1-1h3"/>',
  chev: '<path d="m6 3.5 4.5 4.5L6 12.5"/>',
};
function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('class', 'icon' + (name === 'chev' ? ' chev' : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name];  // только статичные иконки, без пользовательских данных
  return svg;
}

async function api(url, options = {}) {
  const response = await fetch(url, {...options, signal: options.signal || AbortSignal.timeout(20000)});
  let payload = null;
  try { payload = await response.json(); } catch { /* пустой ответ */ }
  if (!response.ok) {
    const error = new Error(payload?.error || 'Сервер вернул ошибку ' + response.status);
    error.status = response.status;
    throw error;
  }
  return payload;
}
function remember(id) { try { id ? localStorage.setItem(STORAGE_KEY, id) : localStorage.removeItem(STORAGE_KEY); } catch { /* приватный режим */ } }
const maxBytes = () => state.health?.max_file_bytes || 512 * 1024 * 1024;

function show(view) {
  document.body.dataset.view = view;
  for (const name of ['upload', 'progress', 'results']) $('view-' + name).hidden = name !== view;
  window.scrollTo({top: 0});
}

/* ---------- Загрузка: чек-лист из двух слотов ---------- */
const SLOTS = [
  {kind: 'notices', title: 'Извещения'},
  {kind: 'items', title: 'Позиции ТРУ'},
];
const ADD_TEXT = {notices: 'Добавить извещения', items: 'Добавить файл ТРУ'};

function slot({filled, title, sub, onRemove, mark}) {
  return h('li', {class: 'slot' + (filled ? ' is-filled' : '')},
    h('span', {class: 'slot-mark'}, icon(mark || (filled ? 'check' : 'plus'))),
    h('span', {class: 'slot-text'}, h('span', {class: 'slot-title', text: title}), h('span', {class: 'slot-sub', text: sub, title: sub})),
    onRemove ? h('button', {class: 'icon-btn icon-btn-sm', type: 'button', 'aria-label': 'Убрать: ' + sub, disabled: state.busy, onclick: onRemove}, icon('x')) : h('span'));
}

function renderTray() {
  const workbooks = state.selection.filter(e => e.kind === 'workbook');
  const remove = entry => () => removeFile(state.selection.indexOf(entry));
  // Показываем только выбранные файлы; чего не хватает — подсказывает главная кнопка
  const rows = SLOTS.map(s => state.selection.find(e => e.kind === s.kind) && {s, entry: state.selection.find(e => e.kind === s.kind)})
    .filter(Boolean).map(({s, entry}) => slot({filled: true, title: s.title, sub: `${entry.file.name}, ${fmtSize(entry.file.size)}`, onRemove: remove(entry)}));
  for (const entry of workbooks) rows.push(slot({filled: true, mark: 'sheet', title: 'Книга Excel', sub: `${entry.file.name}, ${fmtSize(entry.file.size)}`, onRemove: remove(entry)}));
  $('tray').replaceChildren(...rows);
  $('tray').hidden = !rows.length;
  const missing = state.selection.length ? FileSelection.missing(state.selection) : [];
  $('pick-label').textContent = missing.length === 1 ? ADD_TEXT[missing[0]] : 'Загрузить файлы';
  $('pick-hint').textContent = state.selection.length && missing.length === 1 ? 'Подбор начнётся сразу после добавления' : 'или перетащите их в окно — можно оба сразу';
}

function uploadError(message, canRetry) {
  $('upload-error-text').textContent = message || '';
  $('upload-error').hidden = !message;
  $('retry').hidden = !canRetry;
}

async function addIncoming(files) {
  if (state.busy || !files.length) return;
  uploadError('');
  $('pick').setAttribute('aria-busy', 'true');
  try {
    state.selection = await FileSelection.addFiles(state.selection, files, maxBytes());
    state.autoStart = true;
  } catch (error) {
    uploadError(error.message, false);
  } finally {
    $('pick').removeAttribute('aria-busy');
    $('file-input').value = '';
  }
  renderTray();
  if (state.autoStart && FileSelection.isReady(state.selection)) startJob();
}

function removeFile(index) {
  if (state.busy || index < 0) return;
  state.selection.splice(index, 1);
  state.autoStart = true;
  uploadError('');
  renderTray();
  $('pick').focus();
}

function uploadFile(jobId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/jobs/${jobId}/files/auto`);
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.timeout = 1800000;
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* не JSON */ }
      xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(file.name + ': ' + (data.error || 'ошибка загрузки')));
    };
    xhr.onerror = () => reject(new Error('Не удалось передать файл. Проверьте, что сервер запущен.'));
    xhr.ontimeout = () => reject(new Error('Истекло время загрузки файла.'));
    xhr.send(file);
  });
}

async function startJob() {
  if (state.busy || !FileSelection.isReady(state.selection)) return;
  state.busy = true;
  uploadError('');
  renderTray();
  show('progress');
  $('progress-error').hidden = true;
  $('progress-files').textContent = state.selection.map(e => e.file.name).join(', ');
  for (const id of ['stage-upload-note', 'stage-check-note', 'stage-recommendation-note']) $(id).textContent = '';
  setProgress(0, 'Передаём файлы на сервер…', 'upload');
  try {
    const job = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({top_k: TOP_K})});
    state.jobId = job.id;
    const total = state.selection.reduce((sum, e) => sum + e.file.size, 0);
    let done = 0;
    for (const entry of state.selection) {
      await uploadFile(job.id, entry.file, loaded => {
        const share = (done + loaded) / total;
        setProgress(share * 20, 'Передаём файлы на сервер…', 'upload', fmtSize(done + loaded) + ' из ' + fmtSize(total));
      });
      done += entry.file.size;
    }
    await api(`/api/jobs/${job.id}/start`, {method: 'POST'});
    remember(job.id);
    setProgress(20, 'Ставим в очередь…', 'check', fmtSize(total));
    await poll();
  } catch (error) {
    failUpload(error.message);
  }
}

function failUpload(message) {
  state.busy = false;
  state.autoStart = false;
  remember(null);
  show('upload');
  renderTray();
  uploadError(message, FileSelection.isReady(state.selection));
}

/* ---------- Прогресс ---------- */
const STAGE_ORDER = ['upload', 'check', 'recommendation', 'export'];
const STAGE_OF = {upload: 'check', reading: 'check', validation: 'check', recommendation: 'recommendation', export: 'export', completed: 'done'};

function setProgress(percent, message, stage, uploadNote) {
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  $('progress-fill').style.width = value + '%';
  $('progress-bar').setAttribute('aria-valuenow', String(value));
  $('progress-value').textContent = value + '%';
  $('progress-message').textContent = message;
  const current = STAGE_ORDER.indexOf(stage);
  $('progress-hero').dataset.stage = stage;  // орбита: топ‑3 проявляется на этапе ранжирования
  for (const li of $('stages').children) {
    const index = STAGE_ORDER.indexOf(li.dataset.stage);
    li.dataset.state = stage === 'done' || index < current ? 'done' : index === current ? 'current' : 'pending';
  }
  if (uploadNote !== undefined) $('stage-upload-note').textContent = uploadNote;
}

function renderJobProgress(job) {
  const stage = STAGE_OF[job.stage] || 'check';
  const stats = job.stats || {};
  if (stats.notices || stats.items) $('stage-check-note').textContent = `${fmt(stats.notices)} изв., ${fmt(stats.items)} поз.`;
  if (stats.lots) $('stage-recommendation-note').textContent = `${fmt(stats.lots)} ${plural(stats.lots, 'лот', 'лота', 'лотов')}`;
  setProgress(20 + (job.progress || 0) * 0.8, job.message || 'Обрабатываем…', stage);
  if (job.files && !$('progress-files').textContent) $('progress-files').textContent = Object.values(job.files).map(f => f.name).join(', ');
}

async function poll() {
  state.busy = true;
  $('progress-error').hidden = true;
  try {
    for (;;) {
      const job = await api('/api/jobs/' + state.jobId);
      if (job.status === 'completed') { state.busy = false; await showResults(job); return; }
      if (job.status === 'failed') { failUpload(job.message || 'Обработка остановлена.'); return; }
      if (job.status === 'uploading') { failUpload(state.selection.length ? 'Загрузка была прервана. Запустите её ещё раз.' : ''); return; }
      renderJobProgress(job);
      await new Promise(resolve => setTimeout(resolve, 700));
    }
  } catch (error) {
    $('progress-error-text').textContent = 'Нет ответа от сервера: ' + error.message + '. Обработка могла продолжиться.';
    $('progress-error').hidden = false;
  }
}

/* ---------- Результат ---------- */
const GROUP_CAPTION = {
  verified: 'Компании с историей закупок, ранжированные моделью. Шанс победы — вероятность по модели; нажмите на компанию, чтобы увидеть, почему она на этом месте.',
  unverified: 'Новые компании из открытых реестров по кодам ОКПД2 лота. Моделью не оценивались — нужна ручная проверка.',
};

async function showResults(job) {
  state.job = job;
  state.jobId = job.id;
  state.legacy = false;
  show('results');
  const detected = job.detected ? [job.detected.notices, job.detected.items].filter(Boolean) : Object.values(job.files || {}).map(f => f.name);
  $('results-files').textContent = detected.join(', ');
  $('download').href = `/api/jobs/${job.id}/download`;
  const stats = job.stats || {};
  const groupCount = group => stats[group] ?? (job.preview || []).filter(r => (group === 'unverified') === Boolean(r.is_new)).length;
  $('summary').replaceChildren(...[
    ['Лотов', stats.lots], ['Позиций ТРУ', stats.items], ['Проверенных поставщиков', groupCount('verified')], ['Непроверенных', groupCount('unverified')],
  ].map(([label, value]) => h('div', {}, h('dt', {text: label}), h('dd', {text: fmt(value)}))));
  $('count-verified').textContent = fmt(groupCount('verified'));
  $('count-unverified').textContent = fmt(groupCount('unverified'));
  $('demo-note').hidden = job.mode !== 'demo';
  const warnings = job.warnings || [];
  $('warnings').replaceChildren(...(warnings.length ? [h('ul', {}, warnings.map(w => h('li', {text: w})))] : []));
  $('warnings').hidden = !warnings.length;
  selectGroup(state.group, false);
  $('search').value = state.query = '';
  await loadLots(true);
}

async function loadLots(reset) {
  const offset = reset ? 0 : state.lots.length;
  const params = new URLSearchParams({offset, limit: PAGE_SIZE});
  if (state.query) params.set('q', state.query);
  let page;
  try {
    page = state.legacy ? null : await api(`/api/jobs/${state.jobId}/lots?${params}`);
  } catch (error) {
    if (error.status !== 404) { $('lot-detail').replaceChildren(emptyState('Не удалось загрузить результат', error.message)); return; }
    state.legacy = true;
  }
  if (state.legacy) page = legacyPage();
  state.lots = reset ? page.lots : state.lots.concat(page.lots);
  state.hasMore = page.has_more;
  state.total = page.total;
  if (!state.lots.some(l => l.lot_id === state.lotId)) state.lotId = state.lots[0]?.lot_id ?? null;
  renderLots();
  renderLotDetail();
}

// Задачи, обработанные до появления lots.jsonl: собираем лоты из предпросмотра (до 100 строк)
function legacyPage() {
  const byLot = new Map();
  for (const row of state.job.preview || []) {
    if (!byLot.has(row.lot_id)) byLot.set(row.lot_id, {lot_id: row.lot_id, subject: row.subject, items: [], items_total: 0, verified: [], unverified: []});
    byLot.get(row.lot_id)[row.is_new ? 'unverified' : 'verified'].push(row);
  }
  const q = state.query.toLowerCase();
  const lots = [...byLot.values()].filter(l => !q || (l.lot_id + ' ' + l.subject).toLowerCase().includes(q));
  return {lots, has_more: false, total: q ? null : lots.length};
}

function renderLots() {
  const n = state.lots.length;
  $('lots-count').textContent = state.total != null && !state.query
    ? `${fmt(state.total)} ${plural(state.total, 'лот', 'лота', 'лотов')}`
    : `Найдено: ${fmt(n)}${state.hasMore ? '+' : ''}`;
  $('lots-list').replaceChildren(...(n ? state.lots.map(lot => h('li', {},
    h('button', {class: 'lot-btn', type: 'button', 'data-lot': lot.lot_id, 'aria-current': lot.lot_id === state.lotId ? 'true' : 'false', onclick: () => selectLot(lot.lot_id)},
      h('span', {class: 'lot-id', text: 'Лот ' + lot.lot_id}),
      h('span', {class: 'lot-subject', text: lot.subject}),
      h('span', {class: 'lot-count', title: 'Поставщиков в списке', text: String(lot[state.group].length)})))) :
    [h('li', {class: 'lots-empty', text: state.query ? 'Ничего не найдено. Попробуйте номер лота, часть предмета или ИНН.' : 'Нет лотов с результатом.'})]));
  $('lots-more').hidden = !state.hasMore;
}

function selectLot(lotId) {
  state.lotId = lotId;
  for (const btn of $('lots-list').querySelectorAll('.lot-btn')) btn.setAttribute('aria-current', btn.dataset.lot === lotId ? 'true' : 'false');
  renderLotDetail();
  if (matchMedia('(max-width: 1080px)').matches) $('lot-detail').scrollIntoView({block: 'start', behavior: 'smooth'});
}

function emptyState(title, text) {
  return h('div', {class: 'empty'}, h('strong', {text: title}), h('p', {text}));
}

function statusTone(status) {
  if (/^проверенн/i.test(status)) return 'ok';
  if (/^новый/i.test(status)) return 'new';
  if (/требует проверки/i.test(status)) return 'check';
  return null;
}

function renderLotDetail() {
  const lot = state.lots.find(l => l.lot_id === state.lotId);
  const box = $('lot-detail');
  if (!lot) { box.replaceChildren(emptyState(state.query ? 'Нет совпадений' : 'Результат пуст', state.query ? 'Измените запрос поиска.' : 'Ни для одного лота не нашлось поставщиков.')); return; }

  const meta = [];
  const price = lot.start_price ? fmtMoney(lot.start_price) : '';
  if (price) meta.push(['НМЦК', price]);
  if (lot.is_smp !== undefined && lot.is_smp !== '') meta.push(['Только для МСП', yes(lot.is_smp) ? 'Да' : 'Нет']);
  if (lot.items_total) meta.push(['Позиций ТРУ', fmt(lot.items_total)]);

  const children = [
    h('span', {class: 'lot-chip', text: 'Лот ' + lot.lot_id}),
    h('h2', {text: lot.subject}),
    meta.length ? h('dl', {class: 'lot-meta'}, meta.map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {text: v})))) : null,
  ];

  if (lot.items?.length) {
    const rest = lot.items_total - lot.items.length;
    children.push(h('details', {class: 'lot-items'},
      h('summary', {text: `Позиции ТРУ и коды ОКПД2 (${fmt(lot.items_total || lot.items.length)})`}),
      h('ul', {}, lot.items.map(i => h('li', {}, h('span', {text: i.name || 'Без названия'}), i.okpd2 ? h('code', {title: 'ОКПД2', text: i.okpd2}) : null))),
      rest > 0 ? h('p', {class: 'more', text: `Ещё ${fmt(rest)} — в выгрузке CSV.`}) : null));
  }

  const rows = lot[state.group];
  const verified = state.group === 'verified';
  const section = h('div', {class: 'suppliers', 'data-group': state.group});
  if (!rows.length) {
    section.append(verified
      ? emptyState('Модель не нашла поставщиков с историей', 'Посмотрите вкладку «Непроверенные» — там компании из открытых реестров.')
      : emptyState('Новых компаний не найдено', state.job?.mode === 'demo' ? 'В демо-режиме для этого лота новые компании не добавлены.' : 'Поиск в открытых реестрах не подключён или не дал результатов по кодам ОКПД2 лота.'));
  } else {
    const showRole = rows.some(r => r.role && !UNKNOWN_ROLE.test(r.role));
    section.append(h('div', {class: 'sup-head', 'aria-hidden': 'true'},
      h('span', {text: '№'}), h('span', {text: 'Поставщик'}), h('span', {text: showRole ? 'Роль' : 'Главная причина'}),
      verified ? [h('span', {text: 'Шанс победы'}), h('span', {text: 'Статус'})] : h('span', {text: 'Почему найден'}), h('span')));
    section.append(h('div', {class: 'sup-list', role: 'list'}, rows.map(c => h('div', {role: 'listitem'}, supplierRow(c, lot, showRole)))));
  }
  children.push(section);
  box.replaceChildren(...children);
}

function supplierRow(c, lot, showRole) {
  const verified = state.group === 'verified';
  const p = pWin(c);
  const best = Math.max(...lot[state.group].map(x => pWin(x) || 0));
  const pct = p != null && best > 0 ? Math.max(2, Math.round(100 * p / best)) : Math.max(2, Math.min(100, c.score));
  return h('button', {class: 'sup-row', type: 'button', 'aria-haspopup': 'dialog', onclick: e => openSheet(c, lot, e.currentTarget)},
    h('span', {class: 'rank' + (verified && c.rank <= 3 ? ' rank-' + c.rank : ''), text: String(c.rank)}),
    h('span', {class: 'sup-name'}, h('strong', {text: displayName(c)}),
      h('span', {text: hasName(c) ? (c.supplier_inn ? 'ИНН ' + c.supplier_inn : 'ИНН не указан') : noNameNote(c)})),
    showRole ? h('span', {class: 'sup-cell role', text: c.role || '—'}) : h('span', {class: 'sup-cell reason-cell', text: mainReason(c)}),
    verified
      ? [h('span', {class: 'score'}, h('b', {text: p != null ? fmtChance(p) : fmtScore(c.score)}), h('span', {class: 'meter', 'aria-hidden': 'true'}, h('span', {style: `width:${pct}%`})),
           c.explanation?.of ? h('small', {text: `место ${c.explanation.place} из ${fmt(c.explanation.of)}`}) : null),
         h('span', {class: 'chip', 'data-tone': statusTone(c.status), text: c.status})]
      : h('span', {class: 'sup-cell sup-reason', text: c.reasons?.[0] || 'Причина не указана'}),
    icon('chev'));
}

function selectGroup(group, render = true) {
  state.group = group;
  for (const tab of $('segmented').querySelectorAll('[role="tab"]')) {
    const active = tab.dataset.group === group;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  $('workspace').setAttribute('aria-labelledby', 'tab-' + group);
  $('group-caption').textContent = GROUP_CAPTION[group];
  if (render) { renderLots(); renderLotDetail(); }
}

/* ---------- Карточка поставщика ---------- */
const TAX_REGIMES = {usn: 'УСН', osn: 'ОСН', envd: 'ЕНВД', eshn: 'ЕСХН', psn: 'Патент', npd: 'НПД', srp: 'СРП', ausn: 'АУСН'};
const SOURCE_STATE = {ok: ['ok', 'получено'], error: ['check', 'ошибка'], captcha: ['check', 'капча'], timeout: ['check', 'таймаут']};
const fmtYears = y => {
  const n = Math.floor(Number(y));
  return Number.isFinite(n) ? (n < 1 ? 'меньше года' : `${n} ${plural(n, 'год', 'года', 'лет')}`) : '';
};
// Крупные суммы — в млн/млрд: «17,4 млн ₽» читается быстрее, чем «17 366 000 ₽»
function fmtRub(v) {
  const n = Number(v);
  if (v === null || v === undefined || !Number.isFinite(n)) return '';
  const a = Math.abs(n);
  const [d, unit] = a >= 1e9 ? [1e9, ' млрд ₽'] : a >= 1e6 ? [1e6, ' млн ₽'] : a >= 1e3 ? [1e3, ' тыс. ₽'] : [1, ' ₽'];
  return (n / d).toLocaleString('ru-RU', {maximumFractionDigits: d === 1 ? 0 : 1}) + unit;
}
const filled = v => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && !v.length);
const pairs = list => list.filter(([, v]) => filled(v));
const extLink = (url, text) => {
  try {
    const u = new URL(url);
    if (u.protocol === 'http:' || u.protocol === 'https:') return h('a', {class: 'link', href: u.href, target: '_blank', rel: 'noopener noreferrer', text});
  } catch { /* некорректный адрес */ }
  return h('span', {text});
};

function ring(score, label = 'из 100', text = fmtScore(score), aria = `Оценка модели ${fmtScore(score)} из 100`) {
  const r = 42, len = 2 * Math.PI * r, pct = Math.max(0, Math.min(100, score)) / 100;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 96 96');
  svg.setAttribute('aria-hidden', 'true');
  for (const [cls, dash] of [['track', 0], ['value', len * (1 - pct)]]) {
    const c = document.createElementNS(SVG_NS, 'circle');
    c.setAttribute('cx', '48'); c.setAttribute('cy', '48'); c.setAttribute('r', String(r)); c.setAttribute('class', cls);
    if (cls === 'value') { c.setAttribute('stroke-dasharray', String(len)); c.setAttribute('stroke-dashoffset', String(dash)); }
    svg.append(c);
  }
  return h('div', {class: 'ring', role: 'img', 'aria-label': aria}, svg,
    h('div', {class: 'ring-label'}, h('b', {text}), h('span', {text: label})));
}

/* ---------- Почему на этом месте: разбор оценки модели ---------- */
// Вклад признака — SHAP LightGBM: оценка кандидата равна сумме вкладов, поэтому разность оценок двух
// кандидатов равна сумме разностей вкладов. Признаки контекста лота одинаковы у всех и не показываются.
const FEATURE_LABEL = {
  s_n_win: 'Побед в закупках всего', s_n_part: 'Участий в закупках всего', s_win_rate: 'Доля побед',
  s_m_since_win: 'Последняя победа', s_m_since_part: 'Последнее участие', s_n_customers: 'Разных заказчиков',
  s_em_share: 'Доля участий в Электронном магазине', s_is_ip: 'Индивидуальный предприниматель',
  s_is_spb: 'Зарегистрирован в Санкт-Петербурге', platform_fit: 'Работает на площадке этого лота',
  price_dev: 'НМЦК относительно его обычных контрактов', price_in_range: 'НМЦК в его обычном диапазоне',
  code_win_l6: 'Побед с точно таким же кодом ОКПД2', code_win_l5: 'Побед с тем же видом ОКПД2',
  code_win_l4: 'Побед с той же подгруппой ОКПД2', code_win_l3: 'Побед с той же группой ОКПД2',
  code_part_l5: 'Участий с тем же видом ОКПД2', code_part_l3: 'Участий с той же группой ОКПД2',
  code_cover_l5: 'Покрытие видов ОКПД2 лота', code_cover_l3: 'Покрытие групп ОКПД2 лота',
  code_m_since_win: 'Последняя победа по кодам лота', group_share: 'Доля побед в группе ОКПД2 лота',
  cust_win: 'Побед у этого заказчика', cust_part: 'Участий у этого заказчика',
  cust_class_win: 'Побед у заказчика в классе ОКПД2 лота', cust_m_since_win: 'Последняя победа у этого заказчика',
  district_class_win: 'Побед в классе ОКПД2 у заказчиков района', text_cos: 'Похожесть предмета на его прошлые контракты',
};
const factorLabel = f => FEATURE_LABEL[f.feature] || f.title;
const MIN_PHI = 0.03;  // меньше — шум, не показываем

function explainSection(c, lot) {
  const e = c.explanation;
  const list = lot.verified || [];
  const i = list.indexOf(c);
  const parts = [h('h3', {text: c.rank === 1 ? 'Почему на первом месте' : `Почему на ${c.rank}-м месте`}),
    h('p', {class: 'score-explain', text: `Модель сравнила ${fmt(e.of)} ${plural(e.of, 'кандидата', 'кандидатов', 'кандидатов')} с историей закупок по этому лоту. Шанс победы ${fmtChance(e.p_win)} рассчитан из их оценок, а оценка складывается из вкладов признаков ниже.`})];
  const other = c.rank === 1 ? list[i + 1] : list[i - 1];
  if (other?.explanation?.factors) parts.push(contrast(c, other, c.rank === 1));
  const top = e.factors.filter(f => Math.abs(f.phi) >= MIN_PHI).sort((a, b) => Math.abs(b.phi) - Math.abs(a.phi)).slice(0, 7);
  const max = Math.max(...top.map(f => Math.abs(f.phi)), 1e-6);
  parts.push(h('h4', {class: 'explain-sub', text: 'Из чего сложилась оценка'}),
    h('ul', {class: 'factors'}, top.map(f => h('li', {},
      h('span', {class: 'factor-name'}, h('span', {text: factorLabel(f)}), h('small', {text: f.value})),
      h('span', {class: 'factor-bar', 'data-sign': f.phi > 0 ? 'up' : 'down', title: (f.phi > 0 ? 'Повышает' : 'Понижает') + ' оценку на ' + Math.abs(f.phi).toFixed(2)},
        h('span', {style: `width:${Math.max(4, Math.round(50 * Math.abs(f.phi) / max))}%`}))))),
    h('p', {class: 'factors-legend'}, h('span', {class: 'lg-up', text: 'повышает оценку'}), h('span', {class: 'lg-down', text: 'понижает'})),
    h('span', {class: 'chip', 'data-tone': statusTone(c.status), text: c.status}));
  return h('section', {class: 'sheet-section'}, ...parts);
}

// Чем кандидат отличается от соседа по списку: признаки с наибольшей разностью вкладов
function contrast(c, other, ahead) {
  const theirs = Object.fromEntries(other.explanation.factors.map(f => [f.feature, f]));
  const diffs = c.explanation.factors.filter(f => theirs[f.feature]).map(f => ({f, o: theirs[f.feature], d: f.phi - theirs[f.feature].phi}));
  const side = diffs.filter(x => ahead ? x.d >= MIN_PHI : x.d <= -MIN_PHI).sort((a, b) => Math.abs(b.d) - Math.abs(a.d));
  const sum = side.reduce((acc, x) => acc + Math.abs(x.d), 0);
  const who = `№${other.rank} — ${displayName(other)}, шанс ${fmtChance(pWin(other))}`;
  if (!side.length) return h('p', {class: 'note', text: `${ahead ? 'Опережает' : 'Уступает'} ${who} по совокупности мелких различий.`});
  return h('div', {class: 'contrast'},
    h('p', {class: 'contrast-head'}, h('strong', {text: ahead ? 'Опережает ' : 'Уступает '}), who, ahead ? ' — прежде всего за счёт:' : ' — прежде всего из-за:'),
    h('ul', {}, side.slice(0, 3).map(x => h('li', {},
      h('span', {class: 'contrast-name', text: factorLabel(x.f)}),
      h('span', {class: 'contrast-vals'}, h('b', {text: x.f.value}), ' против ', x.o.value),
      h('span', {class: 'contrast-share', text: `${Math.round(100 * Math.abs(x.d) / sum)}% разрыва`})))));
}

function openSheet(c, lot, trigger) {
  state.lastFocus = trigger || document.activeElement;
  const verified = !c.is_new;
  $('sheet-context').textContent = verified ? `${c.rank}-е место по лоту ${lot.lot_id}` : `Новая компания для лота ${lot.lot_id}`;
  $('sheet-title').textContent = displayName(c);
  const ids = [];
  if (c.supplier_inn) {
    const copy = h('button', {class: 'icon-btn', type: 'button', 'aria-label': 'Скопировать ИНН', title: 'Скопировать ИНН'}, icon('copy'));
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(c.supplier_inn); } catch { return; }
      copy.replaceChildren(icon('check')); copy.setAttribute('aria-label', 'ИНН скопирован');
      setTimeout(() => { copy.replaceChildren(icon('copy')); copy.setAttribute('aria-label', 'Скопировать ИНН'); }, 1400);
    });
    ids.push(hasName(c) ? h('span', {class: 'id-chip has-action'}, h('span', {text: 'ИНН'}), c.supplier_inn, copy)
      : h('span', {class: 'id-chip has-action'}, 'Скопировать ИНН', copy));
    if (!hasName(c)) ids.push(h('span', {class: 'id-chip'}, h('span', {text: noNameNote(c)})));
  }
  if (c.supplier_kpp) ids.push(h('span', {class: 'id-chip'}, h('span', {text: 'КПП'}), c.supplier_kpp));
  if (c.region) ids.push(h('span', {class: 'id-chip', text: c.region}));
  $('sheet-ids').replaceChildren(...ids);
  const p = pWin(c);
  $('sheet-score').replaceChildren(!verified ? h('div', {class: 'unscored', text: 'Моделью не оценён'})
    : p != null ? ring(p * 100, 'шанс победы', fmtChance(p), `Шанс победы ${fmtChance(p)}`) : ring(c.score));

  const sections = [];
  if (verified && c.explanation?.factors?.length) {
    sections.push(explainSection(c, lot));
  } else if (verified) {
    sections.push(h('section', {class: 'sheet-section'},
      h('h3', {text: 'Как получена оценка'}),
      h('p', {class: 'score-explain', text: 'Место среди всех кандидатов лота по шкале 0–100, где 100 — лучший. Модель учитывает победы по кодам ОКПД2, похожесть прошлых контрактов на предмет закупки и работу с этим заказчиком.'}),
      h('span', {class: 'chip', 'data-tone': statusTone(c.status), text: c.status})));
  } else {
    sections.push(h('section', {class: 'sheet-section'}, h('p', {class: 'note'}, h('strong', {text: 'Компании нет в истории закупок. '}),
      'Она найдена в открытых реестрах по кодам ОКПД2 лота, поэтому модель её не оценивала. Проверьте компанию вручную, прежде чем приглашать.')));
  }

  const smp = c.is_smp === null || c.is_smp === undefined ? 'Нет данных' : c.is_smp ? 'Да' : 'Нет';
  sections.push(h('section', {class: 'sheet-section'}, h('h3', {text: 'Характеристики'}),
    h('dl', {class: 'props'}, [...(winChance(c) && !c.explanation?.factors ? [['Вероятность победы', winChance(c) + '%']] : []), ['Роль', c.role], ['Регион', c.region || 'Не указан'], ['Субъект МСП', smp], ['Обогащение', c.enrichment_status]]
      .map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {text: v || '—'}))))));

  sections.push(h('section', {class: 'sheet-section'}, h('h3', {text: 'Почему в списке'}),
    c.reasons?.length ? h('ul', {class: 'reasons'}, c.reasons.map(r => h('li', {}, h('span', {class: 'tick'}, icon('check')), h('span', {text: r}))))
      : h('p', {class: 'muted', text: 'Объяснение не передано.'})));

  const companyBox = h('div', {class: 'company', id: 'company-box', 'aria-busy': 'true'});
  sections.push(companyBox);

  const sources = (c.sources || []).filter(s => s && (s.source || s.url));
  sections.push(h('section', {class: 'sheet-section', id: 'sources-section'}, h('h3', {text: 'Источники'}),
    sources.length ? h('ul', {class: 'sources'}, sources.map(sourceItem))
      : h('p', {class: 'muted', text: c.is_demo ? 'Демо-компания: источники не запрашивались.' : 'Источники не переданы.'})));

  if (c.supplier_inn) {
    sections.push(h('section', {class: 'sheet-section'}, h('div', {class: 'sheet-actions'},
      h('a', {class: 'btn btn-soft', href: 'https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=' + encodeURIComponent(c.supplier_inn), target: '_blank', rel: 'noopener noreferrer'}, 'Найти закупки в ЕИС', icon('external')))));
  }

  $('sheet-body').replaceChildren(...sections);
  $('sheet-body').scrollTop = 0;
  $('sheet').hidden = false;
  $('sheet-backdrop').hidden = false;
  document.body.style.overflow = 'hidden';
  $('sheet-close').focus();
  loadCompany(c, companyBox);
}

function sourceItem(s) {
  let link = null;
  try {
    const url = new URL(s.url);
    if (url.protocol === 'http:' || url.protocol === 'https:') link = h('a', {class: 'link', href: url.href, target: '_blank', rel: 'noopener noreferrer', text: s.source || url.hostname});
  } catch { /* некорректный адрес */ }
  const details = [s.field ? 'Поле: ' + s.field : '', s.checked_at ? 'проверено ' + fmtDate(s.checked_at) : ''].filter(Boolean).join(', ');
  return h('li', {}, link || h('span', {text: s.source}), details ? h('small', {text: details}) : null);
}

function metricsGrid(entries, mode) {
  return h('dl', {class: 'metrics' + (mode ? ' is-' + mode : '')}, entries.map(([label, value]) => h('div', {}, h('dt', {text: label}), h('dd', {text: value}))));
}

const section = (title, ...body) => h('section', {class: 'sheet-section'}, h('h3', {text: title}), ...body);
const SKELETON = ['Выручка', 'Сотрудников', 'На рынке', 'Участий в закупках', 'Побед', 'Заказчиков'];

// Карточка компании: GET /api/suppliers/{inn}/card → сервис обогащения (enrichment-api) по ИНН
async function loadCompany(c, box) {
  state.cardRequest?.abort();
  if (!c.supplier_inn) { box.replaceChildren(section('О компании', h('p', {class: 'muted', text: 'Для компании без ИНН сведения недоступны.'}))); box.removeAttribute('aria-busy'); return; }
  box.setAttribute('aria-busy', 'true');
  box.replaceChildren(section('О компании', metricsGrid(SKELETON.map(k => [k, '…']), 'loading'),
    h('p', {class: 'metrics-note', text: 'Собираем данные из ЕГРЮЛ, ФНС, ГИР БО, реестра МСП и РНП…'})));
  const controller = new AbortController();
  state.cardRequest = controller;
  try {
    // ИНН, которого нет в базе, сервис опрашивает в источниках — до ~30 с
    const card = await api(`/api/suppliers/${c.supplier_inn}/card`, {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(60000)])});
    if (controller.signal.aborted) return;
    renderCompany(c, card, box);
  } catch (error) {
    if (controller.signal.aborted) return;
    const retry = h('button', {class: 'link-btn', type: 'button', text: 'Повторить', onclick: () => loadCompany(c, box)});
    const text = error.status === 404 ? 'Открытые источники ничего не знают об этом ИНН.'
      : error.status === 503 ? 'Сервис обогащения не подключён.'
      : 'Не удалось загрузить сведения: ' + (error.name === 'TimeoutError' ? 'источники не ответили вовремя' : error.message) + '. ';
    box.replaceChildren(section('О компании', h('p', {class: 'note'}, text, error.status === 404 || error.status === 503 ? null : retry)));
  }
  box.removeAttribute('aria-busy');
}

function renderCompany(c, card, box) {
  const co = card.company || {};
  const name = co.name_short || co.name_full;
  // Модель отдаёт ИНН вместо названия — подставляем найденное
  if (name && !hasName(c)) $('sheet-title').textContent = name;
  const ids = $('sheet-ids');
  if (co.kpp && !c.supplier_kpp) ids.append(h('span', {class: 'id-chip'}, h('span', {text: 'КПП'}), co.kpp));
  if (co.region_name && !c.region) ids.append(h('span', {class: 'id-chip', text: co.region_name}));

  const parts = [];
  const flags = card.risk_flags || [];
  const active = co.is_active === true ? ['ok', co.status || 'Действующая'] : co.is_active === false ? ['check', co.status || 'Недействующая'] : ['check', 'Статус не подтверждён'];
  const role = card.role || {};
  const director = co.director?.name ? [co.director.name, co.director.post?.toLowerCase()].filter(Boolean).join(', ') : '';
  const reg = co.reg_date ? fmtDate(co.reg_date) + (co.age_years != null ? ` (${fmtYears(co.age_years)})` : '') : '';
  const okved = co.okved_main ? [co.okved_main, co.okved_main_name].filter(Boolean).join(' — ') : '';
  const regimes = (co.tax_regimes || []).map(r => TAX_REGIMES[r] || r.toUpperCase()).join(', ');
  const smp = co.is_smp === true ? (co.smp_category ? ['микро', 'малое', 'среднее'][co.smp_category - 1] + ' предприятие' : 'Да') : co.is_smp === false ? 'Нет' : '';
  parts.push(section('О компании',
    h('div', {class: 'company-tags'},
      h('span', {class: 'chip', 'data-tone': active[0], text: active[1]}),
      role.value && role.value !== 'unknown' ? h('span', {class: 'chip', 'data-tone': 'new', title: (role.evidence || []).join('\n'), text: role.label}) : null,
      co.in_rnp ? h('span', {class: 'chip', 'data-tone': 'danger', text: 'В реестре недобросовестных'}) : null),
    h('dl', {class: 'props'}, pairs([
      ['Полное наименование', co.name_full], ['ОГРН', co.ogrn], ['Руководитель', director], ['Зарегистрирована', reg],
      ['Основной ОКВЭД', okved], ['Субъект МСП', smp], ['Налоговый режим', regimes], ['Уставный капитал', fmtRub(co.charter_capital)],
    ]).map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {text: v})))),
    co.address ? h('dl', {class: 'props props-wide'}, h('div', {}, h('dt', {text: 'Адрес'}), h('dd', {text: co.address}))) : null));

  const year = co.finance_year ? ` за ${co.finance_year}` : '';
  const prev = co.revenue != null && co.revenue_prev ? Math.round((co.revenue / co.revenue_prev - 1) * 100) : null;
  const finance = pairs([
    ['Выручка' + year, fmtRub(co.revenue) + (prev != null && prev !== 0 ? ` (${prev > 0 ? '+' : '−'}${Math.abs(prev)}%)` : '')],
    ['Чистая прибыль', fmtRub(co.net_profit)], ['Капитал', fmtRub(co.equity)],
    ['Сотрудников', co.employees != null ? fmt(co.employees) : ''], ['Уплачено налогов' + (co.taxes_paid_year ? ` за ${co.taxes_paid_year}` : ''), fmtRub(co.taxes_paid)],
    ['Недоимка', co.tax_arrears_total ? fmtRub(co.tax_arrears_total) : ''],
  ]);
  const history = pairs([
    ['Участий в закупках', co.hist_lots != null ? fmt(co.hist_lots) : ''], ['Побед', co.hist_wins != null ? fmt(co.hist_wins) : ''],
    ['Заказчиков', co.hist_customers != null ? fmt(co.hist_customers) : ''], ['Последняя закупка', co.hist_last_date ? fmtDate(co.hist_last_date) : ''],
  ]);
  if (finance.length) parts.push(section('Финансы', metricsGrid(finance)));
  if (history.length && co.hist_lots) parts.push(section('Закупки СПб', metricsGrid(history)));

  parts.push(section('Риски', flags.length
    ? h('ul', {class: 'risks'}, flags.map(f => h('li', {text: f.text})))
    : h('p', {class: 'muted', text: co.is_active === true ? 'Признаков риска в открытых источниках не найдено.' : 'Признаков риска не найдено, но статус в ЕГРЮЛ не подтверждён — проверьте вручную.'})));

  const ct = card.contacts;
  const contacts = ct?.found ? pairs([['Телефон', (ct.phones || []).join(', ')], ['Эл. почта', (ct.emails || []).join(', ')], ['Почтовый адрес', ct.postal_address]]) : [];
  const links = (card.links || []).filter(l => l.url);
  parts.push(section('Контакты и ссылки',
    contacts.length ? h('dl', {class: 'props'}, contacts.map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {text: v})))) : h('p', {class: 'muted', text: 'Контакты в открытых контрактах ЕИС не найдены.'}),
    links.length ? h('ul', {class: 'company-links'}, links.map(l => h('li', {}, extLink(l.url, l.title), icon('external')))) : null));

  // Источники: какие реестры опрошены, с датой каждого факта
  const checked = card.sources_status || [];
  if (checked.length) {
    const dates = {};
    for (const f of Object.values(card.fields || {})) if (f?.source && f.fetched_at && (!dates[f.source] || f.fetched_at > dates[f.source])) dates[f.source] = f.fetched_at;
    $('sources-section').replaceChildren(h('h3', {text: 'Источники'}), h('ul', {class: 'sources'}, (c.sources || []).filter(s => s.field === 'score').map(sourceItem), checked.map(s => {
      const [tone, label] = SOURCE_STATE[s.status] || ['check', s.status];
      return h('li', {class: 'source-row'}, h('span', {}, extLink(s.url, s.name), dates[s.source] ? h('small', {text: 'данные от ' + fmtDate(dates[s.source])}) : null),
        h('span', {class: 'chip', 'data-tone': tone, text: label}));
    })));
  }
  box.replaceChildren(...parts);
}

function closeSheet() {
  if ($('sheet').hidden) return;
  state.cardRequest?.abort();
  $('sheet').hidden = true;
  $('sheet-backdrop').hidden = true;
  document.body.style.overflow = '';
  if (state.lastFocus?.isConnected) state.lastFocus.focus();
}

function trapFocus(e) {
  if (e.key !== 'Tab' || $('sheet').hidden) return;
  const focusable = [...$('sheet').querySelectorAll('button, a[href], input, [tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ---------- Перетаскивание файлов в окно ---------- */
let dragDepth = 0;
const canDrop = e => [...(e.dataTransfer?.types || [])].includes('Files') && !state.busy;
window.addEventListener('dragenter', e => {
  if (!canDrop(e)) return;
  e.preventDefault();
  if (++dragDepth === 1) { $('drop-overlay').hidden = false; $('dropzone').classList.add('is-dragging'); }
});
window.addEventListener('dragover', e => { e.preventDefault(); if (canDrop(e)) e.dataTransfer.dropEffect = 'copy'; });
window.addEventListener('dragleave', () => {
  if (dragDepth && --dragDepth === 0) { $('drop-overlay').hidden = true; $('dropzone').classList.remove('is-dragging'); }
});
window.addEventListener('drop', e => {
  e.preventDefault();
  dragDepth = 0;
  $('drop-overlay').hidden = true;
  $('dropzone').classList.remove('is-dragging');
  if (!canDrop(e) || !e.dataTransfer.files.length) return;
  if (document.body.dataset.view === 'results') resetToUpload(false);
  addIncoming(e.dataTransfer.files);
});

/* ---------- События ---------- */
function resetToUpload(focus = true) {
  closeSheet();
  state.selection = [];
  state.jobId = null;
  state.job = null;
  state.legacy = false;
  state.autoStart = true;
  remember(null);
  uploadError('');
  renderTray();
  show('upload');
  if (focus) $('pick').focus();
}

$('pick').addEventListener('click', () => { if (!state.busy) $('file-input').click(); });
$('file-input').addEventListener('change', e => addIncoming(e.target.files));
$('retry').addEventListener('click', () => { state.autoStart = true; startJob(); });
$('reconnect').addEventListener('click', () => poll());
$('new-upload').addEventListener('click', () => resetToUpload());
$('examples').addEventListener('click', async e => {
  const button = e.currentTarget;
  if (state.busy) return;
  button.setAttribute('aria-busy', 'true');
  try {
    const files = await Promise.all([['notices', 'Извещения_пример.csv'], ['items', 'ТРУ_пример.csv']].map(async ([kind, name]) => {
      const response = await fetch('/api/examples/' + kind);
      if (!response.ok) throw new Error('Не удалось получить пример');
      return new File([await response.blob()], name, {type: 'text/csv'});
    }));
    state.selection = [];
    await addIncoming(files);
  } catch (error) { uploadError(error.message, false); }
  finally { button.removeAttribute('aria-busy'); }
});

$('segmented').addEventListener('click', e => {
  const tab = e.target.closest('[role="tab"]');
  if (tab) selectGroup(tab.dataset.group);
});
$('segmented').addEventListener('keydown', e => {
  const tabs = [...$('segmented').querySelectorAll('[role="tab"]')];
  const index = tabs.indexOf(document.activeElement);
  const next = {ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1}[e.key];
  if (next === undefined || index < 0) return;
  e.preventDefault();
  const tab = tabs[(next + tabs.length) % tabs.length];
  tab.focus();
  selectGroup(tab.dataset.group);
});

let searchTimer = null;
$('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.query = e.target.value.trim(); loadLots(true); }, 250);
});
$('lots-more').addEventListener('click', e => {
  e.currentTarget.setAttribute('aria-busy', 'true');
  loadLots(false).finally(() => $('lots-more').removeAttribute('aria-busy'));
});

$('sheet-close').addEventListener('click', closeSheet);
$('sheet-backdrop').addEventListener('click', closeSheet);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSheet();
  trapFocus(e);
});

/* ---------- Старт ---------- */
async function checkHealth() {
  try {
    state.health = await api('/api/health');
    for (const el of document.querySelectorAll('.file-limit')) el.textContent = String(Math.floor(state.health.max_file_bytes / 1024 / 1024));
  } catch {
    uploadError('Сервер недоступен. Запустите его и обновите страницу.', false);
  }
}

async function init() {
  for (const box of document.querySelectorAll('[data-orbit]')) box.append($('orbit').cloneNode(true));
  for (const svg of document.querySelectorAll('[data-orbit] .orbit')) svg.removeAttribute('id');
  renderTray();
  await checkHealth();
  let saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch { /* приватный режим */ }
  if (!saved || !/^[0-9a-f]{32}$/.test(saved)) return;
  try {
    const job = await api('/api/jobs/' + saved);
    state.jobId = saved;
    if (job.status === 'completed') await showResults(job);
    else if (job.status === 'queued' || job.status === 'processing') { show('progress'); renderJobProgress(job); await poll(); }
    else remember(null);
  } catch { remember(null); }
}

init();
