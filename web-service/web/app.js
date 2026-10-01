'use strict';
const $ = id => document.getElementById(id);
const files = {notices: null, items: null};
let health = null, busy = false, reading = false, currentJob = null, polling = false;
const storageKey = 'pool-local-job-v1';
const number = value => Number(value || 0).toLocaleString('ru-RU');
const errorMessage = message => { $('error').textContent = message; $('error').hidden = !message; };
function remember(id) { try { id ? localStorage.setItem(storageKey,id) : localStorage.removeItem(storageKey); } catch {} }
function selected(kind, file) {
  if (busy) return;
  files[kind] = file;
  $('selected-'+kind).hidden = !file;
  $('name-'+kind).textContent = file ? file.name : '';
  $('meta-'+kind).textContent = file ? (file.size < 1024*1024 ? (file.size/1024).toFixed(1)+' КБ' : (file.size/1024/1024).toFixed(1)+' МБ') : '';
  errorMessage(''); updateControls();
}
async function chooseFiles(incoming) {
  if (busy || reading) return;
  reading = true; updateControls();
  try {
    const next = await CsvSelection.mergeFiles(files, incoming, health?.max_file_bytes || 512*1024*1024);
    selected('notices', next.notices); selected('items', next.items);
  } catch (error) { errorMessage(error.message); }
  finally { reading = false; $('file-csv').value = ''; updateControls(); }
}
function updateControls() {
  const count = Object.values(files).filter(Boolean).length;
  $('file-counter').textContent = count+' из 2';
  $('start').disabled = busy || reading || count !== 2 || !health || (health.mode==='live' && !(health.recommender_ready && health.enricher_ready));
  $('examples').disabled = busy || reading;
  $('top-k').disabled = busy || reading;
  $('file-csv').disabled = busy || reading;
  $('zone-csv').classList.toggle('loaded',count===2);
  $('upload-label').textContent = reading ? 'Проверяем файлы…' : count===2 ? 'Заменить CSV-файлы' : 'Загрузить CSV-файлы';
  for(const kind of ['notices','items']) $('remove-'+kind).disabled = busy || reading;
  $('action-hint').textContent = reading ? 'Определяем типы по столбцам' : busy ? 'Обработка продолжается на локальном сервере' : count===2 ? '' : 'Добавьте «Извещения» и «ТРУ»';
}
function step(name) { document.body.dataset.phase=name; }
async function request(url, options={}) {
  const response = await fetch(url,{...options,signal:AbortSignal.timeout(15000)});
  const payload = await response.json();
  if(!response.ok) throw new Error(payload.error || 'Ошибка сервера');
  return payload;
}
function progress(value, message, detail) { $('progress-panel').hidden=false; $('progress-bar').value=value; $('progress-value').textContent=value+'%'; $('progress-heading').textContent=message; $('progress-detail').textContent=detail; }
async function checkHealth() {
  try {
    health = await request('/api/health');
    if (health.mode==='live' && !(health.recommender_ready && health.enricher_ready)) errorMessage('Подключите модель и обогащение перед запуском подбора.');
    document.querySelectorAll('.file-limit').forEach(el=>el.textContent='до '+Math.floor(health.max_file_bytes/1024/1024)+' МБ');
  } catch { errorMessage('Сервер недоступен. Запустите его и обновите страницу.'); }
  updateControls();
}
$('file-csv').addEventListener('change',event=>{if(event.target.files.length)chooseFiles(event.target.files);});
for(const kind of ['notices','items']) $('remove-'+kind).addEventListener('click',()=>selected(kind,null));
const zone=$('zone-csv');
['dragenter','dragover'].forEach(name=>zone.addEventListener(name,e=>{e.preventDefault();if(!busy&&!reading)zone.classList.add('dragging');}));
['dragleave','drop'].forEach(name=>zone.addEventListener(name,e=>{e.preventDefault();zone.classList.remove('dragging');}));
zone.addEventListener('drop',e=>chooseFiles(e.dataTransfer.files));
async function useExamples() {
  if(busy || reading)throw new Error('Дождитесь завершения обработки');
  reading=true;updateControls();
  try {
    const responses=await Promise.all(['notices','items'].map(async kind=>{const r=await fetch('/api/examples/'+kind);if(!r.ok)throw new Error('Не удалось получить примеры');return new File([await r.blob()],kind==='notices'?'Извещения_пример.csv':'ТРУ_пример.csv',{type:'text/csv'});}));
    const next=await CsvSelection.mergeFiles(files,responses,health?.max_file_bytes || 512*1024*1024);
    selected('notices',next.notices);selected('items',next.items);
    return {files:responses.map(f=>f.name)};
  } finally {reading=false;updateControls();}
}
$('examples').addEventListener('click',()=>useExamples().catch(e=>errorMessage(e.message)));
function upload(jobId,kind,file,completed,total) {
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('PUT',`/api/jobs/${jobId}/files/${kind}`);xhr.setRequestHeader('X-Filename',encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','text/csv');xhr.timeout=1800000;
    xhr.upload.onprogress=e=>{if(e.lengthComputable)progress(Math.min(100,Math.round((completed+e.loaded)/total*100)),'Загружаем файлы…',kind==='notices'?'Передаём извещения':'Передаём позиции ТРУ');};
    xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);xhr.status>=200&&xhr.status<300?resolve():reject(new Error(data.error||'Ошибка загрузки'));}catch{reject(new Error('Сервер вернул некорректный ответ'));}};
    xhr.onerror=()=>reject(new Error('Не удалось загрузить файл. Проверьте, запущен ли сервер.'));
    xhr.ontimeout=()=>reject(new Error('Истекло время загрузки. Попробуйте файл меньшего размера.'));
    xhr.send(file);
  });
}
$('start').addEventListener('click',async()=>{
  if(busy || reading || !files.notices || !files.items)return;
  busy=true;errorMessage('');updateControls();$('results').hidden=true;$('reconnect').hidden=true;
  try {
    const job=await request('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({top_k:Number($('top-k').value)})});currentJob=job.id;
    progress(0,'Загружаем файлы…','Передаём файлы локальному серверу');
    const total=files.notices.size+files.items.size;
    await upload(job.id,'notices',files.notices,0,total);
    await upload(job.id,'items',files.items,files.notices.size,total);
    await request(`/api/jobs/${job.id}/start`,{method:'POST'});remember(job.id);step('process');await pollJob();
  } catch(e) {errorMessage(e.message);busy=false;updateControls();$('progress-panel').hidden=true;}
});
async function pollJob() {
  if(polling || !currentJob)return;
  polling=true;$('reconnect').hidden=true;busy=true;updateControls();
  try {
    while(true) {
      const job=await request('/api/jobs/'+currentJob);
      if(job.status==='completed') {renderResults(job);busy=false;updateControls();return;}
      if(job.status==='failed') {errorMessage(job.message);progress(job.progress,'Обработка остановлена','Исправьте файлы или подключение модулей и запустите новую загрузку.');busy=false;remember(null);updateControls();return;}
      if(job.status==='uploading') {remember(null);busy=false;updateControls();return;}
      step('process');progress(job.progress,job.message,'Извещений: '+number(job.stats.notices)+' · Позиций ТРУ: '+number(job.stats.items));
      await new Promise(resolve=>setTimeout(resolve,900));
    }
  } catch(e) {errorMessage('Не удалось получить статус: '+e.message+'. Сервер мог продолжить обработку.');$('reconnect').hidden=false;}
  finally {polling=false;}
}
$('reconnect').addEventListener('click',()=>{errorMessage('');pollJob();});
function addText(parent,tag,text,className) {const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;parent.append(el);return el;}
function renderResults(job) {
  step('result');$('progress-panel').hidden=true;$('results').hidden=false;$('result-demo').hidden=job.mode!=='demo';
  $('details').hidden=true;
  $('download').href=`/api/jobs/${job.id}/download`;
  $('result-caption').textContent=job.mode==='demo'?'Демо: до трёх вымышленных поставщиков для каждого лота.':'Ранжированный список по каждому лоту.';
  $('stats').replaceChildren();
  for(const [label,value] of [['Лотов обработано',job.stats.lots],['Позиций ТРУ',job.stats.items],['Строк результата',job.stats.recommendations]]){const el=addText($('stats'),'div','','stat');addText(el,'strong',number(value));addText(el,'span',label);}
  $('warnings').replaceChildren();for(const warning of job.warnings)addText($('warnings'),'p',warning);
  $('result-rows').replaceChildren();
  for(const row of job.preview) {
    const tr=document.createElement('tr');
    const lot=addText(tr,'td',row.lot_id);lot.title=row.subject;
    const name=addText(tr,'td','');addText(name,'strong',row.supplier_name);addText(name,'small',row.supplier_inn?'ИНН '+row.supplier_inn:'ИНН не задан · демо');
    addText(tr,'td',row.role);const score=addText(tr,'td','');addText(score,'span',String(row.score),'score');
    addText(tr,'td',row.enrichment_status);const action=addText(tr,'td','');const button=addText(action,'button','Подробнее','text-button');button.setAttribute('aria-label','Подробнее: '+row.supplier_name+', лот '+row.lot_id);button.addEventListener('click',()=>showDetails(row));
    $('result-rows').append(tr);
  }
  $('preview-caption').textContent=job.stats.recommendations ? `Показано ${job.preview.length} из ${number(job.stats.recommendations)} строк. Все строки доступны в CSV.` : 'Кандидаты не найдены. CSV содержит заголовки столбцов.';
  $('results').scrollIntoView({block:'start'});
}
function showDetails(row) {
  $('details-title').textContent=row.supplier_name;const body=$('details-body');body.replaceChildren();
  addText(body,'p','Лот '+row.lot_id+' · '+row.subject);
  addText(body,'p','Оценка: '+row.score+'/100 · '+row.role);
  addText(body,'p','Статус: '+row.status);
  addText(body,'p','Регион: '+(row.region||'не указан')+' · МСП: '+(row.is_smp===null?'нет данных':row.is_smp?'да':'нет'));
  if(row.supplier_inn)addText(body,'p','ИНН: '+row.supplier_inn+' · КПП: '+(row.supplier_kpp||'не указан'));
  addText(body,'h3','Почему в списке');const list=addText(body,'ul','');for(const reason of row.reasons)addText(list,'li',reason);
  if(!row.reasons.length)addText(body,'p','Модель не передала объяснение.');
  addText(body,'h3','Источники данных');
  if(!row.sources.length)addText(body,'p',row.is_demo?'Демо: обращения к открытым источникам не выполнялись.':'Источники не предоставлены. '+row.enrichment_status);
  for(const source of row.sources) {const p=addText(body,'p',(source.field?source.field+' · ':'')+(source.source||'Источник')+' · '+(source.checked_at||'Дата не указана'));try{const url=new URL(source.url);if(['http:','https:'].includes(url.protocol)){const a=addText(p,'a',' Открыть источник');a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';}}catch{}}
  $('details').hidden=false;
  $('details').scrollIntoView({block:'nearest'});
}
$('close-details').addEventListener('click',()=>{$('details').hidden=true;});
$('reset').addEventListener('click',()=>{if(busy||reading)return;currentJob=null;remember(null);$('results').hidden=true;$('progress-panel').hidden=true;selected('notices',null);selected('items',null);step('upload');$('main').scrollIntoView({behavior:'smooth'});$('file-csv').focus();});
async function init() {await checkHealth();try{currentJob=localStorage.getItem(storageKey);}catch{}if(currentJob && /^[0-9a-f]{32}$/.test(currentJob))await pollJob();}
init();
