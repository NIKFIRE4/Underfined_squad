const test = require('node:test');
const assert = require('node:assert/strict');
const {classify, mergeFiles} = require('../web/file-selection.js');
const maxBytes = 512 * 1024 * 1024;
const file = (name, text) => new File([text], name, {type:'text/csv'});
const notices = () => file('random-a.csv', 'lot_id;subject\n01;Бумага\n');
const items = () => file('random-b.csv', 'lot_id;product_name;okpd2_code\n01;Бумага;17\n');

test('recognizes reversed files by headers rather than names or order', async () => {
  const a=notices(), b=items();
  const result=await mergeFiles({notices:null,items:null},[b,a],maxBytes);
  assert.equal(result.notices,a);assert.equal(result.items,b);
});
test('accepts BOM, quoted comma headers and procedure_name alias', () => {
  assert.equal(classify('\uFEFF"LOT_ID","procedure_name"\r\n01,A'),'notices');
  assert.equal(classify('"lot_id";"product_name";"okpd2_code"\n'),'items');
});
test('adding one file later preserves the first file', async () => {
  const first=await mergeFiles({},[notices()],maxBytes);
  const second=await mergeFiles(first,[items()],maxBytes);
  assert.equal(second.notices,first.notices);assert.ok(second.items);
});
test('rejects duplicate types without changing the selected pair', async () => {
  const current={notices:notices(),items:items()};
  const before={...current};
  await assert.rejects(mergeFiles(current,[notices(),notices()],maxBytes),/одного типа/);
  assert.deepEqual(current,before);
});
test('rejects third file, empty file and non-CSV extension', async () => {
  await assert.rejects(mergeFiles({},[notices(),items(),notices()],maxBytes),/два CSV/);
  await assert.rejects(mergeFiles({},[file('empty.csv','')],maxBytes),/непустой/);
  await assert.rejects(mergeFiles({},[file('data.txt','lot_id;subject')],maxBytes),/CSV/);
});
test('rejects ambiguous and unrelated headers', () => {
  assert.throws(()=>classify('lot_id;subject;product_name;okpd2_code\n'),/обоих типов/);
  assert.throws(()=>classify('lot_id;supplier_inn;supplier_kpp\n'),/определить/);
});
test('checks size before reading a large file', async () => {
  const large={name:'large.csv',size:maxBytes+1,slice(){throw Error('must not read');}};
  await assert.rejects(mergeFiles({},[large],maxBytes),/размером/);
});
