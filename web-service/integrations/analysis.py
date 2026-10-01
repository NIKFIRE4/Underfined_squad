"""JSON bridge to the team's separately running model service."""
import json
import os
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class AnalysisError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured():
    return bool(os.environ.get('MODEL_ANALYZE_URL', '').strip())


def reject_constant(value):
    raise ValueError('Недопустимое число в JSON: ' + value)


def analyze(fields, service_port):
    url = os.environ.get('MODEL_ANALYZE_URL', '').strip()
    if not url:
        raise AnalysisError(503, 'Адрес модели не настроен. Укажите MODEL_ANALYZE_URL и перезапустите сервер.')
    try:
        target = urlsplit(url)
        if target.scheme not in ('http', 'https') or not target.hostname or target.username or target.password or target.fragment:
            raise ValueError()
        if target.hostname in ('localhost', '127.0.0.1', '::1') and (target.port or (443 if target.scheme == 'https' else 80)) == service_port:
            raise ValueError()
        timeout = float(os.environ.get('MODEL_ANALYZE_TIMEOUT', '60'))
        if not 0 < timeout <= 300:
            raise ValueError()
    except ValueError:
        raise AnalysisError(503, 'Проверьте MODEL_ANALYZE_URL и MODEL_ANALYZE_TIMEOUT: модель должна работать отдельно от этого сервера.') from None
    request = Request(url, data=json.dumps(fields, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                      headers={'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json'}, method='POST')
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise AnalysisError(502, f'Сервис модели вернул HTTP {status}. Проверьте адрес и формат полей.') from None
    except (TimeoutError, socket.timeout):
        raise AnalysisError(504, 'Модель не ответила за отведённое время.') from None
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise AnalysisError(504, 'Модель не ответила за отведённое время.') from None
        raise AnalysisError(502, 'Не удалось подключиться к сервису модели. Проверьте, что он запущен.') from None
    except (OSError, HTTPException):
        raise AnalysisError(502, 'Соединение с сервисом модели прервано.') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AnalysisError(502, 'Ответ модели превышает 4 МБ.')
    try:
        result = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(result, (dict, list)):
            raise ValueError()
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise AnalysisError(502, 'Модель должна вернуть JSON-объект или список результатов.') from None
