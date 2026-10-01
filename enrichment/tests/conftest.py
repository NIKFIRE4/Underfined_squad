import os

import pytest
from sqlalchemy import create_engine

from enrichment import storage


@pytest.fixture
def db_url(tmp_path):
    """SQLite во временной папке; ENRICHMENT_TEST_DB=postgresql+psycopg://... — прогон на PostgreSQL
    (SQLite не проверяет длину строк и типы, PostgreSQL — проверяет: гоняйте перед прогоном на стенде).
    Внимание: на PostgreSQL таблицы обогащения в этой БД пересоздаются."""
    url = os.environ.get("ENRICHMENT_TEST_DB")
    if not url:
        return f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(url)
    storage.metadata.drop_all(engine)
    engine.dispose()
    return url
