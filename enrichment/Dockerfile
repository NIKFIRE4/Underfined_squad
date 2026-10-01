# Сервис обогащения контрагентов. Собирается из корня репозитория:
#   docker compose build enrichment
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY enrichment/requirements.txt enrichment/requirements.txt
RUN pip install -r enrichment/requirements.txt
COPY enrichment/ enrichment/
COPY scripts/enrich_all.sh scripts/enrich_all.sh

# root, а не отдельный пользователь: data/ монтируется с хоста, и uid владельца там заранее неизвестен

ENTRYPOINT ["python", "-m", "enrichment"]
CMD ["stats"]
