"""Local MVP server. Python 3.10+, standard library only; binds to loopback."""
import argparse
import csv
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
from urllib.parse import unquote, urlparse

from pipeline import run_pipeline
from integrations import recommender, enricher

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
MAX_FILE = int(os.environ.get("MAX_FILE_MB", "512")) * 1024 * 1024
MODE = os.environ.get("PIPELINE_MODE", "demo")
JOBS = {}
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1)


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
        run_pipeline(DATA / job_id, job["mode"], job["top_k"], lambda **kw: update_job(job_id, **kw))
    except (ValueError, UnicodeError, csv.Error) as exc:
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
        if not 0 < size <= max_size:
            raise ValueError("Некорректный размер запроса")
        data = json.loads(self.rfile.read(size))
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

    def do_GET(self):
        if not self.safe_origin():
            self.reply(403, {"error": "Доступ разрешён только с локального адреса сервиса"})
            return
        route = urlparse(self.path).path
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/file-selection.js": ("file-selection.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if route in static:
            name, mime = static[route]
            self.serve_file(ROOT / "web" / name, mime)
        elif route == "/api/health":
            self.reply(200, {"status": "ok", "mode": MODE, "recommender_ready": recommender.READY, "enricher_ready": enricher.READY, "max_file_bytes": MAX_FILE})
        elif route in ("/api/examples/notices", "/api/examples/items"):
            name = "notices.csv" if route.endswith("notices") else "items.csv"
            self.serve_file(ROOT / "examples" / name, "text/csv; charset=utf-8", name)
        else:
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(/download)?", route)
            if not match or match[1] not in JOBS:
                self.reply(404, {"error": "Задача не найдена"})
                return
            with LOCK:
                job = dict(JOBS[match[1]])
            if match[2]:
                if job["status"] != "completed":
                    self.reply(409, {"error": "Файл ещё не готов"})
                else:
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
            if route == "/api/recommendations":
                # Одна закупка из формы → топ поставщиков модели со скором, причинами и признаками
                payload = self.body_json(max_size=2 * 1024 * 1024)
                if not getattr(recommender, "READY", False) or not hasattr(recommender, "recommend_detailed"):
                    self.reply(503, {"error": "Модель не подключена. См. INTEGRATION.md"})
                    return
                try:
                    result = recommender.recommend_detailed(payload)
                except ValueError:
                    raise
                except Exception:
                    logging.exception("Recommendation failed")
                    self.reply(500, {"error": "Ошибка модели. Подробности в журнале сервера."})
                    return
                self.reply(200, result)
                return
            if route == "/api/jobs":
                payload = self.body_json()
                top_k = payload.get("top_k", 10)
                if type(top_k) is not int or not 1 <= top_k <= 50:
                    raise ValueError("top_k должен быть целым числом от 1 до 50")
                if MODE == "live" and not (recommender.READY and enricher.READY):
                    self.reply(503, {"error": "Модель и обогащение ещё не подключены. См. INTEGRATION.md"})
                    return
                job_id = uuid.uuid4().hex
                (DATA / job_id).mkdir(parents=True)
                with LOCK:
                    JOBS[job_id] = {"id": job_id, "status": "uploading", "stage": "upload", "progress": 0, "message": "Ожидаем два CSV", "mode": MODE, "top_k": top_k, "files": {}, "preview": [], "warnings": [], "stats": {}, "created_at": time.time()}
                    update_job(job_id)
                self.reply(201, JOBS[job_id])
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/start", route)
            if not match or match[1] not in JOBS:
                self.reply(404, {"error": "Задача не найдена"})
                return
            with LOCK:
                job_id = match[1]
                job = JOBS[job_id]
                if job["status"] != "uploading" or set(job["files"]) != {"notices", "items"}:
                    self.reply(409, {"error": "Загрузите оба файла; повторный запуск задачи недоступен"})
                    return
                if any(j["status"] in {"queued", "processing"} for j in JOBS.values()):
                    self.reply(409, {"error": "Сервер обрабатывает другую задачу. Повторите запуск после её завершения."})
                    return
                update_job(job_id, status="queued", message="Начинаем обработку…")
                POOL.submit(worker, job_id)
            self.reply(202, {"id": job_id})
        except (ValueError, KeyError) as exc:
            self.close_connection = True
            self.reply(400, {"error": str(exc)})

    def do_PUT(self):
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/files/(notices|items)", urlparse(self.path).path)
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
            if not filename.lower().endswith(".csv"):
                raise ValueError("Поддерживаются только файлы .csv")
            with LOCK:
                if JOBS[job_id]["status"] != "uploading" or kind in JOBS[job_id]["files"]:
                    raise ValueError("Файл уже загружен или задача запущена. Создайте новую загрузку.")
                marker = DATA / job_id / f"{kind}.part"
                # Exclusive creation prevents two concurrent writes to the same upload.
                stream = marker.open("xb")
            self.connection.settimeout(120)
            remaining = size
            with stream:
                while remaining:
                    chunk = self.rfile.read(min(1024*1024, remaining))
                    if not chunk:
                        raise ValueError("Загрузка прервана. Создайте новую загрузку.")
                    stream.write(chunk)
                    remaining -= len(chunk)
            marker.replace(DATA / job_id / f"{kind}.csv")
            with LOCK:
                files = dict(JOBS[job_id]["files"])
                files[kind] = {"name": filename[:240], "bytes": size}
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
