'use strict';
(function (root) {
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
    if (quoted) throw new Error('Не удалось прочитать заголовки CSV. Проверьте кавычки в первой строке.');
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
    if (notices && items) throw new Error('В одном CSV найдены поля обоих типов. Нужны отдельные файлы «Извещения» и «ТРУ».');
    if (!notices && !items) throw new Error('Не удалось определить тип CSV по столбцам. Проверьте «Требования к CSV».');
    return notices ? 'notices' : 'items';
  }
  async function identify(file, maxBytes) {
    if (!file.name.toLowerCase().endsWith('.csv') || !file.size || file.size > maxBytes) {
      throw new Error('Выберите непустой CSV размером до ' + Math.floor(maxBytes / 1024 / 1024) + ' МБ.');
    }
    const bytes = await file.slice(0, 65536).arrayBuffer();
    let text;
    try { text = new TextDecoder('utf-8', {fatal: true}).decode(bytes, {stream: true}); }
    catch { text = new TextDecoder('windows-1251').decode(bytes); }
    return classify(text);
  }
  async function mergeFiles(current, incoming, maxBytes) {
    const batch = Array.from(incoming);
    if (!batch.length || batch.length > 2) throw new Error('Добавьте два CSV: «Извещения» и «ТРУ». Можно выбрать их одновременно.');
    const kinds = await Promise.all(batch.map(async file => {
      try { return await identify(file, maxBytes); }
      catch (error) { throw new Error(file.name + ': ' + error.message); }
    }));
    if (new Set(kinds).size !== kinds.length) throw new Error('Выбраны два файла одного типа. Нужны один файл «Извещения» и один файл «ТРУ».');
    const next = {...current};
    kinds.forEach((kind, index) => { next[kind] = batch[index]; });
    return next;
  }
  const api = {classify, identify, mergeFiles};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CsvSelection = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
