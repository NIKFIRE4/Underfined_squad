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
const visibleReasons = c => (c.reasons || []).filter(r => !/(?:вероятность|шанс).*побед/i.test(r)).map(r => r.replace(/Соответствие/g, 'Оценка').replace(/соответствие/g, 'оценка'));
const mainReason = c => visibleReasons(c).find(r => !/^(?:Соответствие|Оценка) лоту:/.test(r)) || '—';
// «Соответствие» 0–100 (score модели): оценка выше, чем у стольких процентов реальных победителей похожих закупок.
// В отличие от шанса победы не делится между ~300 кандидатами лота
const hasFit = c => c.explanation?.fit != null;
const fmtFit = v => String(Math.round(Number(v)));
const fitTone = v => v >= 70 ? 'high' : v >= 40 ? 'mid' : 'low';
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
    // проверка позиций ТРУ до подбора: код ОКПД2 и наименование (окно выбора сценария при ошибках)
    setProgress(20, 'Проверяем коды ОКПД2 и наименования…', 'check', fmtSize(total));
    const check = await api(`/api/jobs/${job.id}/check`, {method: 'POST', signal: AbortSignal.timeout(600000)});
    let body = null;
    if (Object.values(check.counts).some(Boolean)) {
      const choice = await checkDialog(check);
      if (!choice) {
        failUpload(check.counts.both
          ? `Файл не принят: в ${fmt(check.counts.both)} ${plural(check.counts.both, 'строке', 'строках', 'строках')} ТРУ некорректны и код ОКПД2, и наименование. Исправьте хотя бы одно поле в каждой такой строке и загрузите файл снова.`
          : 'Подбор не запущен: выберите, как исправить позиции ТРУ, или загрузите исправленный файл.', !check.counts.both);
        return;
      }
      body = JSON.stringify({okpd2: choice});
    }
    await api(`/api/jobs/${job.id}/start`, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body} : {method: 'POST'});
    remember(job.id);
    setProgress(20, 'Ставим в очередь…', 'check', fmtSize(total));
    await poll();
  } catch (error) {
    failUpload(error.message);
  }
}

/* ---------- Проверка позиций ТРУ: код ОКПД2 и наименование ---------- */
// Сервер (/check, web-service/okpd_check.py) сверяет строки со справочником кодов закупок СПб.
// Одно поле некорректно — пользователь выбирает: исправить самому (с подсказками) или автоматически.
// Код противоречит наименованию — выбор, на что ориентироваться. Оба поля некорректны — файл не принимается.
const CODE_RE = /^\d{2}(\.\d{1,2}(\.\d{1,2}(\.\d{1,3})?)?)?$/;
const normCode = v => String(v || '').replace(/\s+/g, '').replace(/,/g, '.').replace(/^\.+|\.+$/g, '');
const nameValid = v => (String(v).toLowerCase().match(/[а-яёa-z]/g) || []).length >= 3 && /[а-яёa-z]{3,}/i.test(String(v));
const rowLabel = r => `Строка ${r.line} · лот ${r.lot_id}`;

function checkDialog(check) {
  return new Promise(resolve => {
    const c = check.counts;
    const dialog = h('dialog', {class: 'check-dialog', 'aria-labelledby': 'check-title'});
    const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener('cancel', e => { e.preventDefault(); finish(null); });
    const parts = [
      c.both ? `${fmt(c.both)} — некорректны и код, и наименование` : '',
      c.code ? `${fmt(c.code)} — некорректный код ОКПД2` : '',
      c.name ? `${fmt(c.name)} — некорректное наименование` : '',
      c.conflict ? `${fmt(c.conflict)} — код не соответствует наименованию` : '',
    ].filter(Boolean);
    const head = h('header', {class: 'check-head'},
      h('h2', {id: 'check-title', text: c.both ? 'Файл не принят' : 'Проверьте позиции ТРУ'}),
      h('p', {text: `Строк ТРУ: ${fmt(check.total)}. Найдено: ${parts.join('; ')}.`}));

    if (c.both) {  // оба поля некорректны — только исправление файла
      const bad = check.rows.filter(r => r.kind === 'both');
      dialog.append(head,
        h('div', {class: 'check-body'},
          h('p', {class: 'check-note bad', text: 'В этих строках неверны и код ОКПД2, и наименование — по ним нельзя ни подобрать поставщиков, ни восстановить данные. Исправьте файл: в каждой строке должно быть хотя бы одно корректное поле.'}),
          h('ul', {class: 'check-rows'}, bad.map(r => h('li', {class: 'check-row'},
            h('p', {class: 'check-row-head', text: rowLabel(r)}),
            h('p', {class: 'check-problem', text: r.problem}),
            h('p', {class: 'check-hint', text: r.hint})))),
          bad.length < c.both ? h('p', {class: 'muted', text: `Показаны первые ${fmt(bad.length)} из ${fmt(c.both)}.`}) : null,
          h('p', {class: 'check-hint', text: 'Как исправить: наименование — название товара или услуги словами («Кефир 2,5%»), код ОКПД2 — цифры вида 32.50.13.110. Коды можно найти в классификаторе ОКПД2 или в прошлых закупках.'})),
        h('footer', {class: 'check-foot'}, h('button', {class: 'btn btn-primary', type: 'button', text: 'Исправить файл', onclick: () => finish(null)})));
      document.body.append(dialog);
      dialog.showModal();
      return;
    }

    // одно поле или противоречие: выбор сценария
    const fixRows = check.rows.filter(r => r.kind === 'code' || r.kind === 'name');
    const conflictRows = check.rows.filter(r => r.kind === 'conflict');
    const state = {mode: null, trustAll: 'code', edits: {}, trust: {}};
    const startBtn = h('button', {class: 'btn btn-primary', type: 'button', text: 'Запустить подбор', disabled: true});
    const manualBox = h('div', {class: 'check-manual', hidden: true});
    const validNote = h('p', {class: 'check-valid', 'aria-live': 'polite'});

    const valueOf = r => state.edits[r.line]?.[r.kind === 'code' ? 'okpd2_code' : 'product_name'] ?? (r.kind === 'code' ? r.code : r.name);
    const original = r => r.kind === 'code' ? normCode(r.code) : String(r.name || '').trim();
    const current = r => r.kind === 'code' ? normCode(valueOf(r)) : String(valueOf(r)).trim();
    // «исправлено» — только если значение изменено и стало корректным: исходный неверный код таким не считается
    const rowFixed = r => current(r) !== original(r) && (r.kind === 'code' ? CODE_RE.test(current(r)) : nameValid(current(r)));
    const rowBad = r => current(r) !== '' && current(r) !== original(r) && !rowFixed(r);  // введено, но не похоже на код/наименование
    function refresh() {
      const fixed = fixRows.filter(rowFixed).length;
      startBtn.disabled = !state.mode;
      // счёт — от всех строк с ошибкой в файле, а не только показанных в окне
      validNote.textContent = state.mode === 'manual' && fixRows.length ? `Исправлено ${fmt(fixed)} из ${fmt(c.code + c.name)}` : '';
      for (const el of dialog.querySelectorAll('[data-line]')) {
        const r = fixRows.find(x => String(x.line) === el.dataset.line);
        if (r) el.dataset.valid = rowFixed(r) ? 'yes' : rowBad(r) ? 'no' : '';
      }
    }
    function editRow(r) {
      const field = r.kind === 'code' ? 'okpd2_code' : 'product_name';
      const input = h('input', {class: 'check-input', type: 'text', value: valueOf(r), 'aria-label': `${rowLabel(r)}: ${r.kind === 'code' ? 'код ОКПД2' : 'наименование'}`,
        placeholder: r.kind === 'code' ? 'Код в формате XX.XX.XX.XXX' : 'Наименование товара или услуги', oninput: e => { state.edits[r.line] = {[field]: e.target.value}; refresh(); }});
      const options = r.kind === 'code' ? (r.options || []).map(o => ({value: o.code, text: `${o.code} — ${o.title || 'код из справочника'}`, why: o.why}))
        : r.suggest?.name ? [{value: r.suggest.name, text: r.suggest.name, why: 'типичное наименование для этого кода'}] : [];
      return h('li', {class: 'check-row', 'data-line': String(r.line)},
        h('p', {class: 'check-row-head'}, rowLabel(r), h('span', {class: 'check-mark', 'aria-hidden': 'true'})),
        h('p', {class: 'check-context', text: r.kind === 'code' ? `Наименование: ${r.name}` : `Код ОКПД2: ${r.code}`}),
        h('p', {class: 'check-problem', text: r.problem}),
        h('label', {class: 'check-field'}, h('span', {text: r.kind === 'code' ? 'Код ОКПД2' : 'Наименование'}), input),
        options.length ? h('div', {class: 'check-suggest'}, h('span', {class: 'muted', text: 'Подсказка:'}),
          options.map(o => h('button', {class: 'check-chip', type: 'button', title: o.why || '', text: o.text,
            onclick: () => { input.value = o.value; state.edits[r.line] = {[field]: o.value}; refresh(); }}))) : null,
        h('p', {class: 'check-hint', text: r.hint}));
    }
    function trustRow(r) {
      const name = `trust-${r.line}`;
      const radio = (value, text) => h('label', {class: 'check-radio'},
        h('input', {type: 'radio', name, value, checked: (state.trust[r.line] || state.trustAll) === value ? true : null,
          onchange: () => { state.trust[r.line] = value; }}), h('span', {text}));
      return h('li', {class: 'check-row'},
        h('p', {class: 'check-row-head', text: rowLabel(r)}),
        h('p', {class: 'check-context', text: `Наименование: ${r.name}`}),
        h('p', {class: 'check-problem', text: r.problem}),
        h('div', {class: 'check-radios', role: 'radiogroup', 'aria-label': rowLabel(r)},
          radio('code', `Ориентироваться на код ${r.code}`), radio('name', `На наименование → ${r.suggest.code}`)));
    }
    const trustAll = c.conflict ? h('div', {class: 'check-trust'},
      h('p', {}, h('strong', {text: 'Код не соответствует наименованию. '}), 'На что ориентироваться при подборе?'),
      h('div', {class: 'segmented check-seg', role: 'radiogroup'}, [['code', 'На код ОКПД2'], ['name', 'На наименование']].map(([v, t]) =>
        h('button', {type: 'button', role: 'radio', 'aria-checked': String(v === state.trustAll), 'data-trust': v, text: t, onclick: e => {
          state.trustAll = v;
          for (const b of e.currentTarget.parentNode.children) b.setAttribute('aria-checked', String(b.dataset.trust === v));
          for (const r of conflictRows) if (!(r.line in state.trust)) { const el = dialog.querySelector(`input[name="trust-${r.line}"][value="${v}"]`); if (el) el.checked = true; }
        }}))),
      h('p', {class: 'check-hint', text: 'По умолчанию — код: заказчики нередко пишут к коду общее или неточное наименование. «На наименование» заменит код подходящим по названию из справочника.'})) : null;

    const scenario = (mode, title, text) => h('button', {class: 'check-scenario', type: 'button', role: 'radio', 'aria-checked': 'false', 'data-mode': mode, onclick: e => {
      state.mode = mode;
      for (const b of dialog.querySelectorAll('.check-scenario')) b.setAttribute('aria-checked', String(b.dataset.mode === mode));
      manualBox.hidden = mode !== 'manual';
      refresh();
    }}, h('strong', {text: title}), h('span', {text}));
    if (fixRows.length) {
      manualBox.append(h('ul', {class: 'check-rows'}, fixRows.map(editRow)));
      if (check.truncated) manualBox.append(h('p', {class: 'muted', text: `Показаны первые ${fmt(fixRows.length)} из ${fmt(c.code + c.name)}; остальные будут исправлены автоматически.`}));
    }
    if (conflictRows.length) manualBox.append(h('h3', {class: 'check-sub', text: 'Код и наименование не соответствуют'}), h('ul', {class: 'check-rows'}, conflictRows.map(trustRow)));

    dialog.append(head, h('div', {class: 'check-body'},
      h('div', {class: 'check-scenarios', role: 'radiogroup', 'aria-label': 'Как исправить'},
        scenario('auto', 'Исправить автоматически', 'По справочнику кодов закупок СПб: код — по наименованию позиции, наименование — по коду. Все исправления будут видны в результате.'),
        scenario('manual', 'Исправлю сам', 'Список строк с ошибками и подсказками. Что не успеете исправить — перед запуском предложим исправить автоматически или оставить как есть.')),
      trustAll, manualBox),
      h('footer', {class: 'check-foot'}, validNote,
        h('button', {class: 'btn btn-soft', type: 'button', text: 'Отмена', onclick: () => finish(null)}), startBtn));
    startBtn.addEventListener('click', async () => {
      const rows = {};
      if (state.mode === 'manual') {
        for (const r of fixRows.filter(rowFixed)) rows[r.line] = r.kind === 'code' ? {okpd2_code: current(r)} : {product_name: current(r)};
        // не все исправлены — показать оставшиеся и спросить: исправить автоматически или оставить как есть
        const left = fixRows.filter(r => !rowFixed(r));
        if (left.length) {
          const keep = await confirmUnresolved(left, c.code + c.name - fixRows.length);
          if (keep === null) return;  // «Назад к исправлению»
          for (const line of keep) rows[line] = {keep: true};
        }
      }
      finish({mode: state.mode, rows, trust: state.trust, trust_all: state.trustAll});
    });
    document.body.append(dialog);
    dialog.showModal();
    refresh();
  });
}

// Подтверждение при неисправленных строках: для всех и для каждой — исправить автоматически или оставить как есть.
// Возвращает множество строк «оставить как есть» или null («Назад к исправлению»).
function confirmUnresolved(rows, hidden = 0) {
  return new Promise(resolve => {
    const dialog = h('dialog', {class: 'check-dialog check-confirm', 'aria-labelledby': 'confirm-title'});
    const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.addEventListener('cancel', e => { e.preventDefault(); finish(null); });
    const choice = {all: 'auto', row: {}};
    const pick = r => choice.row[r.line] || choice.all;
    const radios = r => h('div', {class: 'check-radios', role: 'radiogroup', 'aria-label': rowLabel(r)}, [['auto', 'Исправить автоматически'], ['keep', 'Оставить как есть']].map(([v, t]) =>
      h('label', {class: 'check-radio'}, h('input', {type: 'radio', name: `left-${r.line}`, value: v, checked: pick(r) === v ? true : null,
        onchange: () => { choice.row[r.line] = v; }}), h('span', {text: t}))));
    const seg = h('div', {class: 'segmented check-seg', role: 'radiogroup', 'aria-label': 'Для всех строк'}, [['auto', 'Для всех: исправить автоматически'], ['keep', 'Для всех: оставить как есть']].map(([v, t]) =>
      h('button', {type: 'button', role: 'radio', 'aria-checked': String(v === choice.all), 'data-v': v, text: t, onclick: e => {
        choice.all = v; choice.row = {};
        for (const b of e.currentTarget.parentNode.children) b.setAttribute('aria-checked', String(b.dataset.v === v));
        for (const el of dialog.querySelectorAll(`input[type=radio][value="${v}"]`)) el.checked = true;
      }})));
    dialog.append(
      h('header', {class: 'check-head'}, h('h2', {id: 'confirm-title', text: `Не все строки исправлены: ${fmt(rows.length + hidden)}`}),
        h('p', {text: 'Ознакомьтесь со строками ниже и выберите, что с ними сделать при подборе.'})),
      h('div', {class: 'check-body'},
        h('div', {class: 'check-trust'}, seg,
          h('p', {class: 'check-hint', text: '«Исправить автоматически» — код по наименованию позиции, наименование — типичное для кода (по справочнику закупок СПб). «Оставить как есть» — значение из файла: неверный код не участвует в поиске по коду, подбор идёт по тексту наименования.'})),
        h('ul', {class: 'check-rows'}, rows.map(r => h('li', {class: 'check-row'},
          h('p', {class: 'check-row-head', text: rowLabel(r)}),
          h('p', {class: 'check-context', text: r.kind === 'code' ? `Наименование: ${r.name}` : `Код ОКПД2: ${r.code}`}),
          h('p', {class: 'check-problem', text: r.problem}),
          r.suggest ? h('p', {class: 'check-hint', text: `Автоматически: ${r.kind === 'code' ? r.suggest.code : `«${r.suggest.name}»`}`})
            : h('p', {class: 'check-hint', text: 'Автоматически подобрать не удалось — строка останется как есть.'}),
          radios(r)))),
        hidden > 0 ? h('p', {class: 'muted', text: `Ещё ${fmt(hidden)} ${plural(hidden, 'строка', 'строки', 'строк')} не поместились в окно — они будут исправлены автоматически.`}) : null),
      h('footer', {class: 'check-foot'},
        h('button', {class: 'btn btn-soft', type: 'button', text: 'Назад к исправлению', onclick: () => finish(null)}),
        h('button', {class: 'btn btn-primary', type: 'button', text: 'Подтвердить и запустить', onclick: () => finish(new Set(rows.filter(r => pick(r) === 'keep').map(r => String(r.line))))})));
    document.body.append(dialog);
    dialog.showModal();
  });
}

function failUpload(message, canRetry = true) {
  state.busy = false;
  state.autoStart = false;
  remember(null);
  show('upload');
  renderTray();
  // файл с ошибкой «оба поля» повторять бессмысленно — только исправить и загрузить заново
  uploadError(message, canRetry && FileSelection.isReady(state.selection));
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
  verified: 'Компании с историей закупок, ранжированные моделью. Оценка 0–100 — насколько компания похожа на реальных победителей таких закупок; нажмите на компанию, чтобы увидеть, почему она на этом месте.',
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
  const warnings = [...(job.cached_from ? [job.message] : []), ...(job.warnings || [])];
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
    h('div', {class: 'lot-toolbar'},
      h('span', {class: 'lot-chip', text: 'Лот ' + lot.lot_id}),
      h('div', {class: 'lot-downloads', role: 'group', 'aria-label': `Скачать поставщиков лота ${lot.lot_id}`},
        h('span', {class: 'lot-download-label', text: 'Скачать лот'}),
        ['csv', 'xlsx'].map(format => h('a', {
          class: 'btn btn-soft', text: format.toUpperCase(), download: '',
          href: `/api/jobs/${state.jobId}/download?${new URLSearchParams({lot_id: lot.lot_id, format})}`,
          'aria-label': `Скачать поставщиков лота ${lot.lot_id} в ${format.toUpperCase()}`,
          title: 'Все поставщики этого лота: проверенные и непроверенные',
        })))),
    h('h2', {text: lot.subject}),
    meta.length ? h('dl', {class: 'lot-meta'}, meta.map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {text: v})))) : null,
  ];

  if (lot.okpd2_fixed || lot.names_filled) {
    // проверка при загрузке (окно «Проверьте позиции ТРУ», web-service/okpd_check.py) исправила коды или наименования
    const what = [lot.okpd2_fixed ? `коды ОКПД2 — ${fmt(lot.okpd2_fixed)}` : '', lot.names_filled ? `наименования — ${fmt(lot.names_filled)}` : ''].filter(Boolean).join(', ');
    children.push(h('div', {class: 'alert alert-fix', role: 'note'}, h('p', {},
      h('strong', {text: `Исправлены позиции ТРУ: ${what} из ${fmt(lot.items_total)}. `}),
      'В файле были пустые, несуществующие или не соответствующие друг другу коды и наименования. Исправили их вручную или по справочнику кодов закупок СПб — подбор шёл по исправленным данным. Исходные значения зачёркнуты в списке позиций, причина — в подсказке.')));
  }

  if (lot.items?.length) {
    const rest = lot.items_total - lot.items.length;
    children.push(h('details', {class: 'lot-items', open: lot.okpd2_fixed || lot.names_filled ? true : null},
      h('summary', {text: `Позиции ТРУ и коды ОКПД2 (${fmt(lot.items_total || lot.items.length)})`}),
      h('ul', {}, lot.items.map(i => h('li', {},
        h('span', {class: 'item-name'}, i.name || 'Без названия',
          i.name_original !== undefined ? h('small', {class: 'okpd-fixed', title: 'Наименование исправлено при загрузке', text: `в файле «${i.name_original || 'пусто'}»`}) : null),
        i.okpd2 ? h('span', {class: 'item-code'}, h('code', {title: i.okpd2_original !== undefined ? `ОКПД2 исправлен: в файле «${i.okpd2_original || 'пусто'}»` : 'ОКПД2', text: i.okpd2}),
          i.okpd2_original !== undefined ? h('small', {class: 'okpd-fixed', title: 'Почему исправлен: ' + (i.okpd2_fix_reason || 'кода нет в классификаторе'), text: `в файле ${i.okpd2_original || 'без кода'}`}) : null) : null))),
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
    section.append(h('div', {class: 'sup-head', 'aria-hidden': 'true'},
      h('span', {text: '№'}), h('span', {text: 'Поставщик'}), h('span', {text: 'Главная причина'}),
      ...(verified ? [h('span', {text: 'Оценка'})] : []), h('span', {text: 'Роль'}), h('span')));
    section.append(h('div', {class: 'sup-list', role: 'list'}, rows.map(c => h('div', {role: 'listitem'}, supplierRow(c, lot)))));
    loadRoles(rows);
  }
  children.push(section);
  box.replaceChildren(...children);
}

const roleCache = new Map();
const rolePending = new Set();
const roleLabel = c => roleCache.get(c.supplier_inn) || c.role || 'Не определена';
function applyRole(inn, label) {
  if (!label || UNKNOWN_ROLE.test(label)) return;
  roleCache.set(inn, label);
  for (const lot of state.lots) for (const group of ['verified', 'unverified']) {
    for (const c of lot[group]) if (c.supplier_inn === inn) c.role = label;
  }
  for (const el of document.querySelectorAll('[data-role-inn]')) {
    if (el.dataset.roleInn === inn) el.textContent = label;
  }
}
async function loadRoles(rows) {
  const inns = [...new Set(rows.map(c => c.supplier_inn).filter(inn => /^\d{10}(\d{2})?$/.test(inn) && !roleCache.has(inn) && !rolePending.has(inn)))];
  if (!inns.length) return;
  inns.forEach(inn => rolePending.add(inn));
  try {
    const data = await api('/api/suppliers/roles', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({inns})});
    for (const [inn, label] of Object.entries(data.roles || {})) applyRole(inn, label);
  } catch { /* Сохраняем роли из результата; недоступные остаются «Не определена». */ }
  finally { inns.forEach(inn => rolePending.delete(inn)); }
}

function supplierRow(c, lot) {
  const verified = state.group === 'verified';
  const pct = Math.max(2, Math.min(100, c.score));
  return h('button', {class: 'sup-row', type: 'button', 'aria-haspopup': 'dialog', onclick: e => openSheet(c, lot, e.currentTarget)},
    h('span', {class: 'rank' + (verified && c.rank <= 3 ? ' rank-' + c.rank : ''), text: String(c.rank)}),
    h('span', {class: 'sup-name'}, h('strong', {text: displayName(c)}),
      h('span', {text: hasName(c) ? (c.supplier_inn ? 'ИНН ' + c.supplier_inn : 'ИНН не указан') : noNameNote(c)})),
    h('span', {class: 'sup-cell reason-cell', text: mainReason(c)}),
    verified ? h('span', {class: 'score', 'data-tone': hasFit(c) ? fitTone(c.score) : null},
      h('b', {text: hasFit(c) ? fmtFit(c.score) : fmtScore(c.score)}), h('span', {class: 'meter', 'aria-hidden': 'true'}, h('span', {style: `width:${pct}%`})),
      c.explanation?.of ? h('small', {text: `место ${c.explanation.place} из ${fmt(c.explanation.of)}`}) : null) : null,
    h('span', {class: 'sup-cell role', 'data-role-inn': c.supplier_inn, text: roleLabel(c)}),
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
const SOURCE_STATE = {ok: ['ok', 'получено'], error: ['check', 'ошибка'], captcha: ['check', 'ФНС просит капчу'], timeout: ['check', 'таймаут']};
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

/* ---------- Источники полей: квадратик со стрелкой рядом со значением ---------- */
// Каждое значение карточки — с первоисточником и датой: видно, что сведения собраны из реестров, а не выдуманы
const HISTORY_SOURCE = 'Выгрузка организаторов: закупки СПб 2024–2025 (АИС ГЗ и Электронный магазин)';
function srcBtn(src) {
  if (!src?.url) return null;
  const title = `Источник: ${src.name}${src.date ? ', данные от ' + fmtDate(src.date) : ''}`;
  try {
    const u = new URL(src.url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return h('a', {class: 'src-link', href: u.href, target: '_blank', rel: 'noopener noreferrer', title, 'aria-label': title}, icon('external'));
  } catch { return null; }
}
// field → {name, url, date}: код источника поля из card.fields, адрес для ручной проверки — из sources_status
// Контракты компании в ЕИС по ИНН (не по названию: у ИП это ФИО, находит однофамильцев)
const eisContracts = inn => 'https://zakupki.gov.ru/epz/contract/search/results.html?fz44=on&searchString=' + encodeURIComponent(inn);
function sourceResolver(card) {
  const byCode = Object.fromEntries((card.sources_status || []).map(s => [s.source, s]));
  return field => {
    const f = card.fields?.[field];
    if (!f?.source) return null;
    // Числа — участия в лотах СПб 2024–2025, включая Электронный магазин; в ЕИС — контракты 44-ФЗ за все годы и регионы
    if (f.source === 'history') return {name: HISTORY_SOURCE + '. Ссылка — контракты 44-ФЗ компании в ЕИС за все годы и регионы, без малых закупок Электронного магазина: числа не совпадут', url: eisContracts(card.inn), date: f.fetched_at};
    const s = byCode[f.source];
    return s ? {name: s.name, url: s.url, date: f.fetched_at} : null;
  };
}
// Источник доказательства роли: ОКВЭД — реестр/ФНС, широта кодов — история закупок, продукция — реестры
function roleEvidenceField(text) {
  if (/ОКВЭД/i.test(text)) return 'okved_main';
  if (/ГИСП|промышленн|719/i.test(text)) return 'in_gisp';
  if (/программ|реестр.*ПО/i.test(text)) return 'in_software_registry';
  if (/истори|заказчик|код|поставлял|лот/i.test(text)) return 'hist_okpd2_codes';
  return null;
}
// Блок «Источники раздела»: все разные источники полей раздела
function sectionSources(src, fields) {
  const seen = new Map();
  for (const f of fields) { const s = src(f); if (s?.url && !seen.has(s.name)) seen.set(s.name, s); }
  if (!seen.size) return null;
  return h('p', {class: 'section-sources'}, h('span', {text: 'Источники: '}),
    [...seen.values()].map(s => h('span', {class: 'section-source'}, s.name, s.date ? ` · ${fmtDate(s.date)}` : '', srcBtn(s))));
}
const propRow = (k, v, src) => h('div', {}, h('dt', {text: k}), h('dd', {}, h('span', {text: v}), srcBtn(src)));

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
  s_win_3m: 'Побед за последние 3 месяца', s_win_6m: 'Побед за последние 6 месяцев', s_part_6m: 'Участий за 6 месяцев',
  code_win_l5_6m: 'Побед с тем же видом ОКПД2 за 6 месяцев', code_win_l3_6m: 'Побед с той же группой ОКПД2 за 6 месяцев',
  code_win_l3_3m: 'Побед с той же группой ОКПД2 за 3 месяца', code_part_l3_6m: 'Участий с той же группой ОКПД2 за 6 месяцев',
  cust_win_12m: 'Побед у этого заказчика за год', spec_l5: 'Специализация: доля его побед в виде ОКПД2 лота',
  spec_l3: 'Специализация: доля его побед в группе ОКПД2 лота', spec_part_l3: 'Доля его участий в группе ОКПД2 лота',
  rel_code_win_l5: 'Победы в виде ОКПД2 относительно лидера лота', rel_code_win_l3: 'Победы в группе ОКПД2 относительно лидера лота',
  rel_code_win_l3_6m: 'Свежие победы в группе ОКПД2 относительно лидера', rel_cust_win: 'Победы у заказчика относительно лидера лота',
  rel_text: 'Похожесть предмета относительно лучшей в лоте',
};
const factorLabel = f => FEATURE_LABEL[f.feature] || f.title;
const MIN_PHI = 0.03;  // меньше — шум, не показываем

function explainSection(c, lot) {
  const e = c.explanation;
  const list = lot.verified || [];
  const i = list.indexOf(c);
  const parts = [h('h3', {text: c.rank === 1 ? 'Почему на первом месте' : `Почему на ${c.rank}-м месте`}),
    h('p', {class: 'score-explain', text: e.fit != null
      ? `Оценка ${fmtFit(e.fit)} из 100: оценка модели выше, чем у ${fmtFit(e.fit)}% реальных победителей похожих закупок. Оценка складывается из вкладов признаков ниже.`
      : `Модель сравнила ${fmt(e.of)} ${plural(e.of, 'кандидата', 'кандидатов', 'кандидатов')} с историей закупок по этому лоту. Оценка складывается из вкладов признаков ниже.`})];
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
  const who = `№${other.rank} — ${displayName(other)}, ` + `оценка ${hasFit(other) ? fmtFit(other.score) : fmtScore(other.score)}`;
  if (!side.length) return h('p', {class: 'note', text: `${ahead ? 'Опережает' : 'Уступает'} ${who} по совокупности мелких различий.`});
  return h('div', {class: 'contrast'},
    h('p', {class: 'contrast-head'}, h('strong', {text: ahead ? 'Опережает ' : 'Уступает '}), who, ahead ? ' — прежде всего за счёт:' : ' — прежде всего из-за:'),
    h('ul', {}, side.slice(0, 3).map(x => h('li', {},
      h('span', {class: 'contrast-name', text: factorLabel(x.f)}),
      h('span', {class: 'contrast-vals'}, h('b', {text: x.f.value}), ' против ', x.o.value),
      h('span', {class: 'contrast-share', text: `${Math.round(100 * Math.abs(x.d) / sum)}% разрыва`})))));
}

// Плашки под названием: ИНН с копированием, КПП, регион. Перерисовываются, когда карточка нашла название
function sheetIds(c) {
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
  return ids;
}

// Совпадение кода лота с историей поставщика: точное, частичное (общий уровень ОКПД2) или нет
const OKPD_LEVEL = {'вид': 'вид', 'подгруппа': 'подгруппу', 'группа': 'группу', 'класс': 'класс'};
const okpdState = i => i.present === null || i.present === undefined ? 'unknown' : i.present ? 'present' : i.match ? 'partial' : 'absent';
const okpdLabel = i => i.present === null || i.present === undefined ? 'Нет данных' : i.present ? 'Точное совпадение'
  : i.match ? `Частично: поставлял ${OKPD_LEVEL[i.match] || i.match} ${i.match_code}` : 'Нет в профиле';

async function loadOkpdCoverage(c, lot, box) {
  const count = h('span', {class: 'okpd-count', text: '…'});
  const content = h('div', {}, h('p', {class: 'muted', text: 'Сверяем коды лота с профилем поставщика…'}));
  box.append(h('h3', {class: 'okpd-heading'}, 'ОКПД2', count), content);
  if (!c.supplier_inn) {
    count.textContent = 'Нет данных';
    content.replaceChildren(h('p', {class: 'muted', text: 'Для поставщика без ИНН сравнение недоступно.'}));
    return;
  }
  try {
    const data = await api(`/api/jobs/${state.jobId}/coverage?${new URLSearchParams({lot_id: lot.lot_id, inn: c.supplier_inn})}`);
    if (!box.isConnected) return;
    count.textContent = data.available ? `${data.matched} из ${data.total}` + (data.partial ? ` · частично ${data.partial}` : '') : `— из ${data.total}`;
    content.replaceChildren(
      h('ul', {class: 'okpd-list'}, data.items.map(item => h('li', {'data-state': okpdState(item)},
        h('span', {class: 'okpd-code'}, item.code, item.original ? h('small', {class: 'okpd-fixed', title: 'Код исправлен при загрузке: в файле такого кода нет', text: ` в файле ${item.original}`}) : null),
        h('span', {text: okpdLabel(item)})))),
      h('p', {class: 'okpd-note', text: !data.total ? 'В позициях лота коды ОКПД2 не указаны.' : data.available
        ? 'Сверка с историей побед и участий поставщика в закупках СПб (выгрузка организаторов 2024–2025). Частичное совпадение — общий вид, подгруппа, группа или класс ОКПД2. Отсутствие кода не означает, что поставщик не может поставить товар.'
        : 'В модели нет истории кодов этого поставщика — подтвердить наличие или отсутствие нельзя.'}));
  } catch {
    if (!box.isConnected) return;
    count.textContent = 'Нет данных';
    content.replaceChildren(h('p', {class: 'muted', text: 'Не удалось загрузить коды поставщика. '}),
      h('button', {class: 'link-btn', type: 'button', text: 'Повторить', onclick: () => { box.replaceChildren(); loadOkpdCoverage(c, lot, box); }}));
  }
}

// Сноска: откуда в выдаче сам поставщик (а не сведения о нём)
function originNote(c) {
  if (c.is_demo) return h('aside', {class: 'origin-note'}, h('strong', {text: 'Откуда в списке: '}), 'демо-компания, вымышлена для проверки интерфейса.');
  const eis = c.supplier_inn ? {name: 'ЕИС: контракты 44-ФЗ компании (все годы и регионы)', url: eisContracts(c.supplier_inn)} : null;
  if (!c.is_new) {
    return h('aside', {class: 'origin-note'}, h('strong', {text: 'Откуда в списке: '}),
      'история закупок Санкт-Петербурга — ', h('span', {class: 'origin-src'}, HISTORY_SOURCE, srcBtn(eis)),
      '. Компания участвовала или побеждала в закупках, модель отобрала её среди кандидатов лота по кодам ОКПД2, заказчику и описанию.');
  }
  const regs = (c.sources || []).filter(s => s && s.source && s.url);
  return h('aside', {class: 'origin-note'}, h('strong', {text: 'Откуда в списке: '}),
    'компании нет в истории закупок СПб — она найдена в открытых реестрах',
    regs.length ? [': ', regs.map((s, i) => h('span', {class: 'origin-src'}, i ? '; ' : '', s.source, s.checked_at ? ` (${fmtDate(s.checked_at)})` : '', srcBtn({name: s.source, url: s.url, date: s.checked_at})))] : '',
    '. Отобрана по группе ОКПД2 лота как действующая: в 2025 году платила налоги или имела сотрудников, не в РНП.');
}

function openSheet(c, lot, trigger) {
  state.lastFocus = trigger || document.activeElement;
  const verified = !c.is_new;
  $('sheet-context').textContent = verified ? `${c.rank}-е место по лоту ${lot.lot_id}` : `Новая компания для лота ${lot.lot_id}`;
  $('sheet-title').textContent = displayName(c);
  $('sheet-ids').replaceChildren(...sheetIds(c));
  $('sheet-score').replaceChildren(!verified ? h('div', {class: 'unscored', text: 'Моделью не оценён'})
    : ring(c.score, 'оценка', hasFit(c) ? fmtFit(c.score) : fmtScore(c.score)));

  const okpdBox = h('section', {class: 'sheet-section okpd-coverage', 'aria-live': 'polite'});
  const sections = [originNote(c), okpdBox].filter(Boolean);
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
    h('dl', {class: 'props'}, [['Роль', roleLabel(c)], ['Статус', c.status], ['Регион', c.region || 'Не указан'], ['Субъект МСП', smp], ['Обогащение', c.enrichment_status]]
      .map(([k, v]) => h('div', {}, h('dt', {text: k}), h('dd', {'data-prop': {'Роль': 'role', 'Субъект МСП': 'smp'}[k]},
        h('span', {text: v || '—', ...(k === 'Роль' ? {'data-role-inn': c.supplier_inn} : {})})))))));

  sections.push(h('section', {class: 'sheet-section'}, h('h3', {text: 'Почему в списке'}),
    visibleReasons(c).length ? h('ul', {class: 'reasons'}, visibleReasons(c).map(r => h('li', {}, h('span', {class: 'tick'}, icon('check')), h('span', {text: r}))))
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
  loadOkpdCoverage(c, lot, okpdBox);
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
  return h('dl', {class: 'metrics' + (mode ? ' is-' + mode : '')}, entries.map(([label, value, src]) => h('div', {}, h('dt', {text: label}), h('dd', {}, h('span', {text: value}), srcBtn(src)))));
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
    if (card.pending?.length) watchPending(c, box, controller);
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

// ФНС «Прозрачный бизнес» и ЕГРЮЛ закрываются капчей — сервис догружает их в фоне (сначала кандидатов
// с высшей оценкой). Пока карточка открыта, переспрашиваем базу и перерисовываем, когда данные пришли.
const PENDING_POLL_MS = 8000, PENDING_MAX_MS = 5 * 60000;
const PENDING_NAMES = {pb: 'ФНС «Прозрачный бизнес»', egrul: 'ЕГРЮЛ'};
async function watchPending(c, box, controller) {
  const started = Date.now();
  while (!controller.signal.aborted && box.isConnected && Date.now() - started < PENDING_MAX_MS) {
    await new Promise(r => setTimeout(r, PENDING_POLL_MS));
    if (controller.signal.aborted || !box.isConnected) return;
    try {
      const card = await api(`/api/suppliers/${c.supplier_inn}/card`, {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)])});
      if (controller.signal.aborted) return;
      if (!card.pending?.length) { renderCompany(c, card, box); return; }
    } catch { /* сеть или сервис — попробуем в следующий раз */ }
  }
  box.querySelector('.pending-note')?.replaceChildren('Фоновая догрузка ещё идёт — откройте карточку позже.');
}

function renderCompany(c, card, box) {
  const co = card.company || {};
  if (card.role?.value !== 'unknown') applyRole(c.supplier_inn, card.role?.label);
  const name = co.name_short || co.name_full;
  // Модель отдаёт ИНН вместо названия — подставляем найденное в кандидата: шапку карточки и строку в списке
  if (name && !hasName(c)) {
    c.supplier_name = name;
    $('sheet-title').textContent = name;
    $('sheet-ids').replaceChildren(...sheetIds(c));
    renderLotDetail();
  }
  const ids = $('sheet-ids');
  if (co.kpp && !c.supplier_kpp) ids.append(h('span', {class: 'id-chip'}, h('span', {text: 'КПП'}), co.kpp));
  if (co.region_name && !c.region) ids.append(h('span', {class: 'id-chip', text: co.region_name}));

  const parts = [];
  const src = sourceResolver(card);
  if (card.pending?.length) {
    parts.push(h('p', {class: 'pending-note', role: 'status'}, h('span', {class: 'pending-dot', 'aria-hidden': 'true'}),
      `${card.pending.map(s => PENDING_NAMES[s] || s).join(' и ')} — догружаются в фоне (у ФНС капча), карточка обновится сама.`));
  }
  const flags = card.risk_flags || [];
  const active = co.is_active === true ? ['ok', co.status || 'Действующая'] : co.is_active === false ? ['check', co.status || 'Недействующая'] : ['check', 'Статус не подтверждён'];
  const role = card.role || {};
  const director = co.director?.name ? [co.director.name, co.director.post?.toLowerCase()].filter(Boolean).join(', ') : '';
  const reg = co.reg_date ? fmtDate(co.reg_date) + (co.age_years != null ? ` (${fmtYears(co.age_years)})` : '') : '';
  const okved = co.okved_main ? [co.okved_main, co.okved_main_name].filter(Boolean).join(' — ') : '';
  const regimes = (co.tax_regimes || []).map(r => TAX_REGIMES[r] || r.toUpperCase()).join(', ');
  const smp = co.is_smp === true ? (co.smp_category ? ['микро', 'малое', 'среднее'][co.smp_category - 1] + ' предприятие' : 'Да') : co.is_smp === false ? 'Нет' : '';
  const hasRole = role.value && role.value !== 'unknown';
  // Роль: доказательства с первоисточником каждого
  const roleProof = hasRole && role.evidence?.length ? h('div', {class: 'role-proof'},
    h('p', {class: 'role-proof-title'}, `Почему «${role.label}»`, role.confidence ? h('small', {text: ` · уверенность ${{high: 'высокая', medium: 'средняя', low: 'низкая'}[role.confidence] || role.confidence}`}) : null),
    h('ul', {}, role.evidence.map(e => h('li', {}, h('span', {text: e}), srcBtn(src(roleEvidenceField(e))))))) : null;
  const roleSrc = hasRole ? src(roleEvidenceField(role.evidence?.[0] || '') || 'okved_main') : null;
  const about = [
    ['Полное наименование', co.name_full, 'name_full'], ['ОГРН', co.ogrn, 'ogrn'], ['Руководитель', director, 'director'], ['Зарегистрирована', reg, 'reg_date'],
    ['Основной ОКВЭД', okved, 'okved_main'], ['Субъект МСП', smp, 'is_smp'], ['Налоговый режим', regimes, 'tax_regimes'], ['Уставный капитал', fmtRub(co.charter_capital), 'charter_capital'],
  ];
  parts.push(section('О компании',
    h('div', {class: 'company-tags'},
      h('span', {class: 'chip-src'}, h('span', {class: 'chip', 'data-tone': active[0], text: active[1]}), srcBtn(src('status'))),
      hasRole ? h('span', {class: 'chip-src'}, h('span', {class: 'chip', 'data-tone': 'new', title: (role.evidence || []).join('\n'), text: role.label}), srcBtn(roleSrc)) : null,
      co.in_rnp ? h('span', {class: 'chip-src'}, h('span', {class: 'chip', 'data-tone': 'danger', text: 'В реестре недобросовестных'}), srcBtn(src('in_rnp'))) : null),
    roleProof,
    h('dl', {class: 'props'}, pairs(about).map(([k, v, f]) => propRow(k, v, src(f)))),
    co.address ? h('dl', {class: 'props props-wide'}, propRow('Адрес', co.address, src('address'))) : null,
    sectionSources(src, [...about.map(a => a[2]), 'status', 'address'])));
  // Те же ссылки — у роли и МСП в «Характеристиках» над карточкой
  for (const [prop, s2] of [['role', roleSrc], ['smp', src('is_smp')]]) {
    const dd = $('sheet-body').querySelector(`[data-prop="${prop}"]`);
    if (dd && s2 && !dd.querySelector('.src-link')) dd.append(srcBtn(s2));
  }

  const year = co.finance_year ? ` за ${co.finance_year}` : '';
  const prev = co.revenue != null && co.revenue_prev ? Math.round((co.revenue / co.revenue_prev - 1) * 100) : null;
  const financeRows = [
    ['Выручка' + year, fmtRub(co.revenue) + (prev != null && prev !== 0 ? ` (${prev > 0 ? '+' : '−'}${Math.abs(prev)}%)` : ''), 'revenue'],
    ['Чистая прибыль', fmtRub(co.net_profit), 'net_profit'], ['Капитал', fmtRub(co.equity), 'equity'],
    ['Сотрудников', co.employees != null ? fmt(co.employees) : '', 'employees'], ['Уплачено налогов' + (co.taxes_paid_year ? ` за ${co.taxes_paid_year}` : ''), fmtRub(co.taxes_paid), 'taxes_paid'],
    ['Недоимка', co.tax_arrears_total ? fmtRub(co.tax_arrears_total) : '', 'tax_arrears_total'],
  ];
  const historyRows = [
    ['Участий в закупках', co.hist_lots != null ? fmt(co.hist_lots) : '', 'hist_lots'], ['Побед', co.hist_wins != null ? fmt(co.hist_wins) : '', 'hist_wins'],
    ['Заказчиков', co.hist_customers != null ? fmt(co.hist_customers) : '', 'hist_customers'], ['Последняя закупка', co.hist_last_date ? fmtDate(co.hist_last_date) : '', 'hist_last_date'],
  ];
  const finance = pairs(financeRows), history = pairs(historyRows);
  if (finance.length) parts.push(section('Финансы', metricsGrid(finance.map(([k, v, f]) => [k, v, src(f)])), sectionSources(src, finance.map(r => r[2]))));
  if (history.length && co.hist_lots) parts.push(section('Закупки СПб', metricsGrid(history.map(([k, v, f]) => [k, v, src(f)])), sectionSources(src, history.map(r => r[2]))));

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
      // источник в фоновой очереди — не ошибка, а «догружается»: ФНС пускает после паузы
      const [tone, label] = card.pending?.includes(s.source) && s.status !== 'ok' ? ['new', 'догружается в фоне'] : SOURCE_STATE[s.status] || ['check', s.status];
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
    const files = await Promise.all([['notices', 'Извещения_октябрь_2025.csv'], ['items', 'ТРУ_октябрь_2025.csv']].map(async ([kind, name]) => {
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
