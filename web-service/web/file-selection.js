'use strict';
// Набор выбранных файлов: CSV распознаются по заголовкам прямо в браузере,
// книги Excel (.xlsx) распознаёт сервер — в одной книге могут быть оба листа.
(function (root) {
  const MAX_FILES = 4;
  const LABEL = {notices: 'Извещения', items: 'ТРУ', workbook: 'Excel'};

  function headerValues(text, delimiter) {
    const values = [];
    let value = '', quoted = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (char === '"') {
        if (quoted && text[i + 1] === '"') { value += '"'; i++; }
        else quoted = !quoted;
      } else if (!quoted && char === delimiter) { values.push(value.trim().toLowerCase()); value = ''; }
      else if (!quoted && (char === '\n' || char === '\r')) break;
      else value += char;
    }
    if (quoted) throw new Error('не удалось прочитать заголовки — проверьте кавычки в первой строке');
    values.push(value.trim().toLowerCase());
    return values;
  }

  function classify(text) {
    text = text.replace(/^\uFEFF/, '');
    const variants = [';', ','].map(delimiter => headerValues(text, delimiter));
    const headers = variants.sort((a, b) => b.length - a.length)[0];
    const has = name => headers.includes(name);
    const notices = has('lot_id') && (has('subject') || has('procedure_name'));
    const items = has('lot_id') && has('product_name') && has('okpd2_code');
    if (notices && items) throw new Error('в одном CSV столбцы и извещений, и ТРУ — нужны отдельные таблицы');
    if (!notices && !items) throw new Error('не похоже ни на извещения (lot_id, subject), ни на ТРУ (lot_id, product_name, okpd2_code)');
    return notices ? 'notices' : 'items';
  }

  function extension(name) {
    const dot = name.lastIndexOf('.');
    return dot < 0 ? '' : name.slice(dot).toLowerCase();
  }

  async function identify(file, maxBytes) {
    const ext = extension(file.name);
    if (ext === '.xls') throw new Error('формат .xls не поддерживается — сохраните книгу как .xlsx или CSV');
    if (ext !== '.csv' && ext !== '.xlsx') throw new Error('нужен файл CSV или Excel (.xlsx)');
    if (!file.size) throw new Error('файл пустой');
    if (file.size > maxBytes) throw new Error('файл больше ' + Math.floor(maxBytes / 1024 / 1024) + ' МБ');
    if (ext === '.xlsx') return 'workbook';
    const bytes = await file.slice(0, 65536).arrayBuffer();
    let text;
    try { text = new TextDecoder('utf-8', {fatal: true}).decode(bytes, {stream: true}); }
    catch { text = new TextDecoder('windows-1251').decode(bytes); }
    return classify(text);
  }

  // current: [{file, kind}]. Новый CSV того же типа заменяет прежний — так проще поправить ошибку в файле.
  async function addFiles(current, incoming, maxBytes) {
    const batch = Array.from(incoming);
    const next = current.slice();
    for (const file of batch) {
      let kind;
      try { kind = await identify(file, maxBytes); }
      catch (error) { throw new Error(file.name + ': ' + error.message); }
      const same = next.findIndex(entry => entry.file.name === file.name && entry.file.size === file.size);
      const sameKind = kind === 'workbook' ? -1 : next.findIndex(entry => entry.kind === kind);
      const replace = same >= 0 ? same : sameKind;
      if (replace >= 0) next.splice(replace, 1, {file, kind});
      else next.push({file, kind});
    }
    if (next.length > MAX_FILES) throw new Error('Можно загрузить не больше ' + MAX_FILES + ' файлов за раз.');
    return next;
  }

  // Готово к запуску: есть обе CSV-таблицы или хотя бы одна книга Excel (её листы проверит сервер).
  function missing(list) {
    if (list.some(entry => entry.kind === 'workbook')) return [];
    return ['notices', 'items'].filter(kind => !list.some(entry => entry.kind === kind));
  }
  const isReady = list => list.length > 0 && missing(list).length === 0;

  const api = {classify, identify, addFiles, missing, isReady, LABEL, MAX_FILES};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.FileSelection = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
