const test = require('node:test');
const assert = require('node:assert/strict');
const {classify, addFiles, missing, isReady} = require('../web/file-selection.js');
const maxBytes = 512 * 1024 * 1024;
const file = (name, text) => new File([text], name, {type:'text/csv'});
const notices = (name='random-a.csv') => file(name, 'lot_id;subject\n01;Бумага\n');
const items = (name='random-b.csv') => file(name, 'lot_id;product_name;okpd2_code\n01;Бумага;17\n');
const xlsx = name => new File(['PK'], name);

test('recognizes CSV kinds by headers rather than names or order', async () => {
  const a=notices(), b=items();
  const result=await addFiles([],[b,a],maxBytes);
  assert.deepEqual(result.map(e=>e.kind),['items','notices']);
  assert.ok(isReady(result));
});
test('accepts BOM, quoted comma headers and procedure_name alias', () => {
  assert.equal(classify('﻿"LOT_ID","procedure_name"\r\n01,A'),'notices');
  assert.equal(classify('"lot_id";"product_name";"okpd2_code"\n'),'items');
});
test('one CSV is not ready and reports what is missing; adding the other completes the set', async () => {
  const first=await addFiles([],[notices()],maxBytes);
  assert.deepEqual(missing(first),['items']);
  assert.equal(isReady(first),false);
  const second=await addFiles(first,[items()],maxBytes);
  assert.equal(second[0].file,first[0].file);
  assert.ok(isReady(second));
});
test('a new CSV of the same kind replaces the previous one', async () => {
  const first=await addFiles([],[notices('old.csv'),items()],maxBytes);
  const second=await addFiles(first,[notices('fixed.csv')],maxBytes);
  assert.equal(second.length,2);
  assert.ok(second.some(e=>e.file.name==='fixed.csv'));
  assert.ok(!second.some(e=>e.file.name==='old.csv'));
});
test('xlsx workbook is accepted without reading and is ready on its own (sheets are detected by the server)', async () => {
  const result=await addFiles([],[xlsx('Закупки.xlsx')],maxBytes);
  assert.equal(result[0].kind,'workbook');
  assert.ok(isReady(result));
  assert.deepEqual(missing(result),[]);
});
test('rejects legacy xls, other extensions, empty files and too many files', async () => {
  await assert.rejects(addFiles([],[xlsx('old.xls')],maxBytes),/xlsx или CSV/);
  await assert.rejects(addFiles([],[file('data.txt','lot_id;subject')],maxBytes),/CSV или Excel/);
  await assert.rejects(addFiles([],[file('empty.csv','')],maxBytes),/пустой/);
  await assert.rejects(addFiles([],[1,2,3,4,5].map(i=>xlsx(`b${i}.xlsx`)),maxBytes),/не больше 4/);
});
test('rejects ambiguous and unrelated headers', () => {
  assert.throws(()=>classify('lot_id;subject;product_name;okpd2_code\n'),/и извещений, и ТРУ/);
  assert.throws(()=>classify('lot_id;supplier_inn;supplier_kpp\n'),/не похоже/);
});
test('checks size before reading a large file', async () => {
  const large={name:'large.csv',size:maxBytes+1,slice(){throw Error('must not read');}};
  await assert.rejects(addFiles([],[large],maxBytes),/больше/);
});
test('does not change the current selection when a file is rejected', async () => {
  const current=await addFiles([],[notices()],maxBytes);
  const before=current.slice();
  await assert.rejects(addFiles(current,[file('bad.csv','a;b\n')],maxBytes));
  assert.deepEqual(current,before);
});
