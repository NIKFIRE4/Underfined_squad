"""Local MVP server. Python 3.10+, standard library only; binds to loopback."""
import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from inputs import prepare_sources

from exports import export_lot
from okpd_coverage import coverage
import okpd_check
from pipeline import read_csv, run_pipeline
from integrations import recommender, enricher, analysis

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
MAX_FILE = int(os.environ.get("MAX_FILE_MB", "512")) * 1024 * 1024
MODE = os.environ.get("PIPELINE_MODE", "live")  # live — модель v3 (models/); demo — вымышленные компании без модели
JOBS = {}
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1)
UPLOAD_EXT = (".csv", ".xlsx")
MAX_SOURCES = 4
# Кэш результатов: те же файлы с тем же top_k, режимом и версией модели не пересчитываются
CACHE_TTL = float(os.environ.get("CACHE_HOURS", "24")) * 3600
CACHE_FILES = ("suppliers.csv", "lots.jsonl")
CACHE_FIELDS = ("stats", "warnings", "preview", "formats", "detected")


def code_version():
    """Отпечаток кода и модели: после переобучения или правки конвейера кэш не используется."""
    paths = [ROOT / "pipeline.py", ROOT / "inputs.py", ROOT / "okpd_check.py", ROOT.parent / "models" / "okpd2_reference.json.gz", *sorted((ROOT / "integrations").glob("*.py")),
             *(ROOT.parent / "models" / n for n in ("meta.json", "ranker.txt", "fit_calibration.json"))]
    return hashlib.sha256("|".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in paths if p.exists()).encode()).hexdigest()[:16]


def cache_key(job):
    files = job["files"]
    if any("sha256" not in f for f in files.values()):
        return None
    # тип файла с автоопределением задаёт содержимое, а не порядок загрузки
    hashes = sorted(f["sha256"] if k.startswith("src-") else f"{k}:{f['sha256']}" for k, f in files.items())
    raw = json.dumps([hashes, job["top_k"], job["mode"], code_version(), job.get("okpd2_choice")], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def cached_job(key, exclude):
    """Последняя завершённая задача с тем же ключом, не старше CACHE_TTL и с файлами результата на диске."""
    now = time.time()
    found = [j for j in JOBS.values() if j["id"] != exclude and j.get("cache_key") == key and j["status"] == "completed"
             and now - j.get("updated_at", 0) < CACHE_TTL and all((DATA / j["id"] / n).is_file() for n in CACHE_FILES)]
    return max(found, key=lambda j: j.get("updated_at", 0), default=None)


def update_job(job_id, **changes):
    with LOCK:
        job = JOBS[job_id]
        job.update(changes, updated_at=time.time())
        temp = DATA / job_id / "status.tmp"
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temp.replace(DATA / job_id / "status.json")


def worker(job_id):
    job = JOBS[job_id]
    update_job(job_id, status="processing")
    try:
        sources = {k: v for k, v in job["files"].items() if k.startswith("src-")}
        if sources and not job.get("prepared"):  # после /check файлы уже разобраны в notices.csv и items.csv
            update_job(job_id, stage="reading", progress=2, message="Читаем файлы…")
            update_job(job_id, detected=prepare_sources(DATA / job_id, sources, lambda **kw: update_job(job_id, **kw)))
        run_pipeline(DATA / job_id, job["mode"], job["top_k"], lambda **kw: update_job(job_id, **kw))
    except (ValueError, UnicodeError, csv.Error, OSError) as exc:
        update_job(job_id, status="failed", stage="failed", message=str(exc))
    except Exception:
        logging.exception("Job %s failed", job_id)
        update_job(job_id, status="failed", stage="failed", message="Ошибка обработки. Подробности в журнале сервера; проверьте адаптеры команды.")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logging.info(fmt, *args)

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.headers_out("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def headers_out(self, content_type, size):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

    def body_json(self, max_size=16384):
        size = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or not 0 < size <= max_size:
            raise ValueError("Некорректный размер запроса")
        data = json.loads(self.rfile.read(size), parse_constant=analysis.reject_constant)
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект")
        return data

    def safe_origin(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin in {"http://"+h for h in allowed})

    def serve_file(self, path, content_type, download=None):
        if not path.is_file():
            self.reply(404, {"error": "Файл не найден"})
            return
        self.send_response(200)
        self.headers_out(content_type, path.stat().st_size)
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename=suppliers.csv; filename*=UTF-8''{quote(download)}")
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024*1024)

    def reply_lots(self, job, query):
        """Результат по лотам для интерфейса: постранично, с поиском по лоту, предмету, названию и ИНН."""
        path = DATA / job["id"] / "lots.jsonl"
        if job["status"] != "completed":
            self.reply(409, {"error": "Результат ещё не готов"})
            return
        if not path.is_file():
            self.reply(404, {"error": "Результат сохранён в старом формате, доступен только CSV"})
            return
        try:
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(200, max(1, int(query.get("limit", ["50"])[0])))
        except ValueError:
            self.reply(400, {"error": "offset и limit — целые числа"})
            return
        needle = query.get("q", [""])[0].strip().lower()[:200]
        lots, matched = [], 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                lot = None
                if needle:
                    lot = json.loads(line)
                    hay = " ".join([lot["lot_id"], lot["subject"]] + [c["supplier_name"] + " " + c["supplier_inn"] for g in ("verified", "unverified") for c in lot[g]]).lower()
                    if needle not in hay:
                        continue
                if matched >= offset and len(lots) < limit:
                    lots.append(lot or json.loads(line))
                matched += 1
                if matched > offset + limit:
                    break
        self.reply(200, {"offset": offset, "lots": lots, "has_more": matched > offset + len(lots), "total": None if needle else job.get("stats", {}).get("lots")})

    def okpd2_choice(self, payload):
        """Выбор из окна проверки: {"mode": "auto"|"manual", "rows": {строка: {okpd2_code, product_name}},
        "trust": {строка: "code"|"name"}, "trust_all": "code"|"name"}."""
        raw = payload.get("okpd2") if isinstance(payload, dict) else None
        if not raw:
            return None
        if not isinstance(raw, dict) or raw.get("mode") not in ("auto", "manual"):
            raise ValueError("okpd2.mode должен быть auto или manual")
        rows = {str(k): {f: str(v[f])[:500] for f in ("okpd2_code", "product_name") if f in v}
                for k, v in (raw.get("rows") or {}).items() if isinstance(v, dict) and str(k).isdigit()}
        trust = {str(k): v for k, v in (raw.get("trust") or {}).items() if str(k).isdigit() and v in ("code", "name")}
        return {"mode": raw["mode"], "rows": rows, "trust": trust,
                "trust_all": raw.get("trust_all") if raw.get("trust_all") in ("code", "name") else "code"}

    def reply_check(self, job_id):
        """Проверка ТРУ до запуска подбора: строки с некорректным кодом ОКПД2 и/или наименованием."""
        with LOCK:
            job = JOBS.get(job_id)
            if job is None:
                self.reply(404, {"error": "Задача не найдена"})
                return
            if job["status"] != "uploading":
                self.reply(409, {"error": "Проверка доступна до запуска подбора"})
                return
            files = dict(job["files"])
        folder = DATA / job_id
        sources = {k: v for k, v in files.items() if k.startswith("src-")}
        if sources and not job.get("prepared"):
            detected = prepare_sources(folder, sources)
            update_job(job_id, prepared=True, detected=detected)
        if not (folder / "items.csv").exists():
            self.reply(409, {"error": "Загрузите файлы извещений и ТРУ"})
            return
        rows = ((line, row["lot_id"], row) for row, line, _, _ in read_csv(folder / "items.csv", "items"))
        result = okpd_check.analyze(rows)
        update_job(job_id, okpd2_check=result["counts"])
        self.reply(200, result)

    def do_GET(self):
        if not self.safe_origin():
            self.reply(403, {"error": "Доступ разрешён только с локального адреса сервиса"})
            return
        route = urlparse(self.path).path
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/file-selection.js": ("file-selection.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if route in static:
            name, mime = static[route]
            self.serve_file(ROOT / "web" / name, mime)
        elif re.fullmatch(r"/fonts/[a-z0-9-]+\.woff2", route):
            self.serve_file(ROOT / "web" / route.lstrip("/"), "font/woff2")
        elif route == "/api/health":
            self.reply(200, {"status": "ok", "mode": MODE, "recommender_ready": recommender.READY, "enricher_ready": enricher.READY, "max_file_bytes": MAX_FILE, "analyze_configured": analysis.configured()})
        elif match := re.fullmatch(r"/api/suppliers/(\d{10}|\d{12})/card", route):
            # Карточка компании из сервиса обогащения (enrichment-api): реквизиты, финансы, риски, контакты, источники
            if not enricher.READY:
                self.reply(503, {"error": "Сервис обогащения не подключён (ENRICH_API_URL)"})
                return
            refresh = parse_qs(urlparse(self.path).query).get("refresh", [""])[0] == "true"
            try:
                self.reply(200, enricher.fetch_card(match[1], refresh))
            except enricher.EnrichmentError as exc:
                self.reply(exc.status if exc.status in (400, 404) else 502, {"error": str(exc)})
        elif match := re.fullmatch(r"/api/suppliers/(\d{10}|\d{12})/stats", route):
            # Статистика поставщика (успешные контракты, характеристики) — отдельный сервис, подключается позже.
            # Контракт ответа описан в INTEGRATION.md; пока available=false, и интерфейс показывает заглушку.
            self.reply(200, {"inn": match[1], "available": False})
        elif route in ("/api/examples/notices", "/api/examples/items"):
            # «Запустить на примере»: реальные лоты октября 2025 (scripts/make_test_set.py), модель их не видела;
            # без набора — два синтетических лота
            notices = route.endswith("notices")
            test_set = ROOT / "examples" / "test-set" / ("Извещения.csv" if notices else "ТРУ.csv")
            path = test_set if test_set.exists() else ROOT / "examples" / ("notices.csv" if notices else "items.csv")
            self.serve_file(path, "text/csv; charset=utf-8", path.name)
        else:
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(/download|/lots|/coverage)?", route)
            if not match or match[1] not in JOBS:
                self.reply(404, {"error": "Задача не найдена"})
                return
            with LOCK:
                job = dict(JOBS[match[1]])
            if match[2] == "/coverage":
                query = parse_qs(urlparse(self.path).query)
                lot_id = query.get('lot_id', [''])[0]
                inn = query.get('inn', [''])[0]
                if job['status'] != 'completed':
                    self.reply(409, {'error': 'Подбор ещё не завершён'})
                elif not lot_id or not re.fullmatch(r'\d{10}|\d{12}', inn):
                    self.reply(400, {'error': 'Укажите лот и ИНН поставщика'})
                else:
                    try:
                        # результат из кэша копирует только выдачу — позиции лота лежат в исходной задаче
                        folder = DATA / (job.get('cached_from') if (DATA / str(job.get('cached_from')) / 'input.sqlite').exists() else match[1])
                        self.reply(200, coverage(folder, lot_id, inn, job['mode']))
                    except LookupError as error:
                        self.reply(404, {'error': str(error)})
                    except Exception:
                        logging.exception('Не удалось сопоставить коды ОКПД2')
                        self.reply(503, {'error': 'Профиль ОКПД2 временно недоступен'})
            elif match[2] == "/lots":
                self.reply_lots(job, parse_qs(urlparse(self.path).query))
            elif match[2]:
                if job["status"] != "completed":
                    self.reply(409, {"error": "Файл ещё не готов"})
                else:
                    query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if "lot_id" in query:
                        lot_id = query["lot_id"][0]
                        file_format = query.get("format", ["csv"])[0]
                        if not lot_id or file_format not in ("csv", "xlsx"):
                            self.reply(400, {"error": "Укажите лот и формат csv или xlsx"})
                            return
                        try:
                            payload, content_type = export_lot(DATA / match[1], lot_id, file_format)
                        except LookupError as error:
                            self.reply(404, {"error": str(error)})
                            return
                        except ValueError as error:
                            self.reply(400, {"error": str(error)})
                            return
                        from urllib.parse import quote
                        label = re.sub(r'[^\w.-]', '_', lot_id)[:80]
                        demo = "_ДЕМО" if job["mode"] == "demo" else ""
                        filename = f"Поставщики_лот_{label}{demo}.{file_format}"
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(payload)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Disposition", f"attachment; filename=suppliers.{file_format}; filename*=UTF-8''{quote(filename)}")
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    self.serve_file(DATA / match[1] / "suppliers.csv", "text/csv; charset=utf-8", "Поставщики_ДЕМО.csv" if job["mode"] == "demo" else "Поставщики.csv")
            else:
                self.reply(200, job)

    def do_POST(self):
        try:
            if not self.safe_origin():
                self.close_connection = True
                self.reply(403, {"error": "Недопустимый источник запроса"})
                return
            route = urlparse(self.path).path
            if route == "/api/suppliers/roles":
                payload = self.body_json(max_size=16384)
                inns = payload.get('inns') if isinstance(payload, dict) else None
                if not isinstance(inns, list) or not 1 <= len(inns) <= 50 or any(not isinstance(inn, str) or not re.fullmatch(r'\d{10}|\d{12}', inn) for inn in inns):
                    raise ValueError('Передайте от 1 до 50 корректных ИНН')
                if not enricher.READY:
                    self.reply(503, {'error': 'Сервис обогащения не подключён'})
                    return
                try:
                    cards = enricher._request('POST', '/api/suppliers/batch', {'inns': list(dict.fromkeys(inns)), 'offline': True})
                    roles = {card['inn']: card['role']['label'] for card in cards.get('items', [])
                             if card.get('role', {}).get('value') not in (None, 'unknown') and card['role'].get('label')}
                    self.reply(200, {'roles': roles})
                except enricher.EnrichmentError as error:
                    self.reply(503, {'error': str(error)})
                return
            if route == "/api/recommendations":
                # Одна закупка из формы → топ поставщиков модели со скором, причинами и признаками
                payload = self.body_json(max_size=2 * 1024 * 1024)
                if not getattr(recommender, "READY", False) or not hasattr(recommender, "recommend_detailed"):
                    self.reply(503, {"error": "Модель не подключена. См. INTEGRATION.md"})
                    return
                fixes = []
                for item in payload.get("items") or [] if isinstance(payload, dict) else []:
                    if isinstance(item, dict) and item.get("okpd2"):
                        row = {"okpd2_code": item["okpd2"], "product_name": item.get("name", "")}
                        if fix := okpd_check.fix_item(row):
                            item["okpd2"] = row["okpd2_code"]
                            fixes.append(fix)
                try:
                    result = recommender.recommend_detailed(payload)
                    if fixes and isinstance(result, dict):
                        result.setdefault("warnings", []).extend(okpd_check.summary(fixes, len(payload["items"]), 0))
                except ValueError:
                    raise
                except Exception:
                    logging.exception("Recommendation failed")
                    self.reply(500, {"error": "Ошибка модели. Подробности в журнале сервера."})
                    return
                self.reply(200, result)
                return
            if route in ("/analyze", "/api/analyze"):
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self.close_connection = True
                    self.reply(415, {"error": "Передайте поля модели как application/json"})
                    return
                if int(self.headers.get("Content-Length", "0")) > 1024 * 1024:
                    self.close_connection = True
                    self.reply(413, {"error": "JSON-запрос не должен превышать 1 МБ"})
                    return
                self.connection.settimeout(30)
                payload = self.body_json(max_size=1024 * 1024)
                if not payload:
                    raise ValueError("Передайте непустой JSON-объект с полями модели")
                self.reply(200, analysis.analyze(payload, self.server.server_port))
                return
            if route == "/api/jobs":
                payload = self.body_json()
                top_k = payload.get("top_k", 10)
                if type(top_k) is not int or not 1 <= top_k <= 50:
                    raise ValueError("top_k должен быть целым числом от 1 до 50")
                # Обогащение необязательно: без него модель работает, но названия и новые компании не подставляются
                if MODE == "live" and not recommender.READY:
                    self.reply(503, {"error": "Модель ещё не подключена. См. INTEGRATION.md"})
                    return
                job_id = uuid.uuid4().hex
                (DATA / job_id).mkdir(parents=True)
                with LOCK:
                    JOBS[job_id] = {"id": job_id, "status": "uploading", "stage": "upload", "progress": 0, "message": "Ожидаем два CSV", "mode": MODE, "top_k": top_k, "files": {}, "preview": [], "warnings": [], "stats": {}, "created_at": time.time()}
                    update_job(job_id)
                self.reply(201, JOBS[job_id])
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/check", route)
            if match:
                self.reply_check(match[1])
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/start", route)
            if not match or match[1] not in JOBS:
                self.reply(404, {"error": "Задача не найдена"})
                return
            choice = self.okpd2_choice(self.body_json(max_size=4 * 1024 * 1024) if int(self.headers.get("Content-Length") or 0) else {})
            with LOCK:
                job_id = match[1]
                job = JOBS[job_id]
                files = job["files"]
                ready = set(files) == {"notices", "items"} or (files and all(k.startswith("src-") for k in files))
                if job["status"] != "uploading" or not ready:
                    self.reply(409, {"error": "Загрузите файлы извещений и ТРУ; повторный запуск задачи недоступен"})
                    return
                if choice:
                    (DATA / job_id / "okpd2_choice.json").write_text(json.dumps(choice, ensure_ascii=False), encoding="utf-8")
                    job["okpd2_choice"] = choice
                key = cache_key(job)
                source = cached_job(key, job_id) if key and CACHE_TTL > 0 else None
                if not source and any(j["status"] in {"queued", "processing"} for j in JOBS.values()):
                    self.reply(409, {"error": "Сервер обрабатывает другую задачу. Повторите запуск после её завершения."})
                    return
                if source:
                    for name in CACHE_FILES:
                        shutil.copyfile(DATA / source["id"] / name, DATA / job_id / name)
                    when = time.strftime("%d.%m %H:%M", time.localtime(source.get("updated_at", 0)))
                    update_job(job_id, cache_key=key, cached_from=source["id"], status="completed", stage="completed", progress=100,
                               message=f"Готово: эти файлы уже обрабатывались {when}, результат взят из кэша",
                               **{f: source[f] for f in CACHE_FIELDS if f in source})
                else:
                    update_job(job_id, cache_key=key, status="queued", message="Начинаем обработку…")
                    POOL.submit(worker, job_id)
            self.reply(202, {"id": job_id})
        except analysis.AnalysisError as exc:
            self.reply(exc.status, {"error": str(exc)})
        except TimeoutError:
            self.close_connection = True
            self.reply(408, {"error": "Истекло время чтения запроса"})
        except (ValueError, KeyError, RecursionError) as exc:
            self.close_connection = True
            self.reply(400, {"error": str(exc)})

    def do_PUT(self):
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/files/(notices|items|auto)", urlparse(self.path).path)
        if not self.safe_origin():
            self.close_connection = True
            self.reply(403, {"error": "Недопустимый источник запроса"})
            return
        if not match or match[1] not in JOBS:
            self.close_connection = True
            self.reply(404, {"error": "Задача не найдена"})
            return
        job_id, kind = match.groups()
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < size <= MAX_FILE:
                raise ValueError(f"Файл должен быть непустым, не больше {MAX_FILE // 1024 // 1024} МБ")
            filename = unquote(self.headers.get("X-Filename", "file.csv"))
            ext = Path(filename.lower()).suffix
            if ext == ".xls":
                raise ValueError("Формат .xls не поддерживается. Сохраните книгу как .xlsx или CSV.")
            if ext not in (UPLOAD_EXT if kind == "auto" else (".csv",)):
                raise ValueError("Поддерживаются файлы .csv и .xlsx" if kind == "auto" else "Поддерживаются только файлы .csv")
            with LOCK:
                files = JOBS[job_id]["files"]
                if kind == "auto":
                    # Тип файла (извещения/ТРУ) определит сервер по столбцам при запуске
                    if any(not k.startswith("src-") for k in files) or len(files) >= MAX_SOURCES:
                        raise ValueError(f"В одну задачу можно загрузить не больше {MAX_SOURCES} файлов")
                    kind = f"src-{len(files) + 1}"
                elif any(k.startswith("src-") for k in files):
                    raise ValueError("В эту задачу файлы загружаются без указания типа")
                if JOBS[job_id]["status"] != "uploading" or kind in files:
                    raise ValueError("Файл уже загружен или задача запущена. Создайте новую загрузку.")
                marker = DATA / job_id / f"{kind}.part"
                # Exclusive creation prevents two concurrent writes to the same upload.
                stream = marker.open("xb")
            self.connection.settimeout(120)
            remaining = size
            digest = hashlib.sha256()
            with stream:
                while remaining:
                    chunk = self.rfile.read(min(1024*1024, remaining))
                    if not chunk:
                        raise ValueError("Загрузка прервана. Создайте новую загрузку.")
                    stream.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            marker.replace(DATA / job_id / f"{kind}{ext}")
            with LOCK:
                files = dict(JOBS[job_id]["files"])
                files[kind] = {"name": filename[:240], "bytes": size, "ext": ext, "sha256": digest.hexdigest()}
                update_job(job_id, files=files)
            self.reply(200, {"ok": True})
        except (ValueError, OSError) as exc:
            self.close_connection = True
            self.reply(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if MODE not in {"demo", "live"}:
        raise SystemExit("PIPELINE_MODE must be demo or live")
    DATA.mkdir(parents=True, exist_ok=True)
    for status in DATA.glob("*/status.json"):
        try:
            job = json.loads(status.read_text(encoding="utf-8"))
            JOBS[job["id"]] = job
            if job["status"] in {"queued", "processing"}:
                update_job(job["id"], status="failed", stage="failed", message="Обработка прервана перезапуском сервера. Загрузите файлы заново.")
        except (ValueError, KeyError, OSError):
            logging.warning("Cannot load %s", status)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if getattr(recommender, "READY", False) and hasattr(recommender, "warmup"):
        threading.Thread(target=recommender.warmup, name="model-warmup", daemon=True).start()
    if MODE == "live" and getattr(enricher, "READY", False):
        # пул новых компаний для «Непроверенных» (~8 с), чтобы первый лот не ждал загрузки
        threading.Thread(target=enricher.new_pool.pool, name="new-pool-warmup", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Local URL: http://127.0.0.1:{args.port} | mode={MODE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server", flush=True)
    finally:
        server.server_close()
        POOL.shutdown(wait=True)


if __name__ == "__main__":
    main()
