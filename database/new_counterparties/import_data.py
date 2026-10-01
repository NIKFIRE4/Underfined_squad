import argparse
import os
import csv
import gzip
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

sys.stdout.reconfigure(encoding='utf-8')
parser = argparse.ArgumentParser(description='Import new counterparties into PostgreSQL without replacing existing tables.')
parser.add_argument('--archive', type=Path, help='Path to new_counterparties_pg.zip')
parser.add_argument('--verify-only', action='store_true', help='Check counts and constraints without changing the database')
parser.add_argument('--report', type=Path, help='Optional JSON report path')
args = parser.parse_args()
if not args.verify_only and (not args.archive or not args.archive.is_file()):
    parser.error('--archive must point to an existing ZIP file')
archive = args.archive
# libpq also supports PGHOST, PGPORT, PGDATABASE, PGUSER and PGPASSFILE.
dsn = os.environ.get('DATABASE_URL', '')
expected = {'new_cp_sources': 6, 'new_cp_companies': 499996,
            'new_cp_okpd2': 3372099, 'new_cp_contracts': 1266157}
started = time.monotonic()
report = {'archive': archive.name if archive else None, 'schema': 'public', 'counts': {}}

def log(message):
    print(f'[{time.monotonic() - started:.1f}s] {message}', flush=True)

if args.verify_only:
    with psycopg.connect(dsn, connect_timeout=10, options='-c default_transaction_read_only=on') as conn:
        report['database'] = conn.execute('SELECT current_database()').fetchone()[0]
        for table, count in expected.items():
            actual = conn.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(table))).fetchone()[0]
            if actual != count:
                raise RuntimeError(f'{table}: expected {count}, got {actual}')
            report['counts'][table] = actual
        report['constraints'] = conn.execute("SELECT c.conname,c.contype,c.convalidated FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(%s)", (list(expected),)).fetchall()
        required_keys = {('new_cp_sources_pkey', 'p'), ('new_cp_companies_pkey', 'p'), ('new_cp_okpd2_pkey', 'p'), ('new_cp_contracts_pkey', 'p'), ('new_cp_okpd2_inn_fkey', 'f'), ('new_cp_contracts_supplier_inn_fkey', 'f')}
        if not required_keys.issubset({(name, kind) for name, kind, valid in report['constraints'] if valid}):
            raise RuntimeError('Missing or unvalidated primary/foreign keys')
        indexes = conn.execute("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid=ANY(ARRAY['public.new_cp_okpd2'::regclass,'public.new_cp_companies'::regclass,'public.new_cp_contracts'::regclass]) AND indisvalid").fetchall()
        if not {'ix_new_cp_okpd2_search', 'ix_new_cp_companies_region', 'ix_new_cp_contracts_supplier'}.issubset({r[0].split('.')[-1] for r in indexes}):
            raise RuntimeError('Missing search indexes')
    report['verified'] = True
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0)

with zipfile.ZipFile(archive) as package, psycopg.connect(dsn, connect_timeout=10) as conn:
    report['database'] = conn.execute('SELECT current_database()').fetchone()[0]
    log('Target database: ' + report['database'])
    conn.execute("SET LOCAL search_path = public")
    conn.execute("SET LOCAL lock_timeout = '10s'")
    conn.execute("SET LOCAL statement_timeout = '30min'")
    if not conn.execute('SELECT pg_try_advisory_xact_lock(720261002)').fetchone()[0]:
        raise RuntimeError('Another import is running')
    original_tables = conn.execute("SELECT oid, relname, relfilenode FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY oid").fetchall()
    object_names = list(expected) + ['ix_new_cp_okpd2_search', 'ix_new_cp_companies_region', 'ix_new_cp_contracts_supplier']
    for name in object_names:
        if conn.execute('SELECT to_regclass(%s)', ('public.' + name,)).fetchone()[0]:
            raise RuntimeError(f'Object already exists: {name}; import cancelled without replacing data')
    schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
    # Only the reviewed new-table schema is used, never the archive's shell commands.
    indexes = re.findall(r'CREATE INDEX\s+[^;]+;', schema, re.I)
    schema = re.sub(r'CREATE INDEX\s+[^;]+;', '', schema, flags=re.I)
    conn.execute(schema)
    log('Four new tables created inside transaction')
    for table, count in expected.items():
        columns = [r[0] for r in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', ('public', table))]
        filename = table + ('.csv' if table == 'new_cp_sources' else '.csv.gz')
        with package.open('new_counterparties_pg/' + filename) as entry:
            if filename.endswith('.gz'):
                stream = gzip.GzipFile(fileobj=entry)
            else:
                # This small CSV has CRCRLF line endings; preserve quoted empty values.
                stream = io.BytesIO(entry.read().replace(b'\r\r\n', b'\n'))
            with stream:
                header = stream.readline()
                if next(csv.reader([header.decode('utf-8-sig').strip()])) != columns:
                    raise RuntimeError('Column mismatch: ' + table)
                total = 0
                last_log = time.monotonic()
                log('Loading ' + table)
                with conn.cursor() as cursor:
                    statement = sql.SQL('COPY public.{} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, ENCODING \'UTF8\')').format(sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)))
                    with cursor.copy(statement) as copy:
                        copy.write(header)
                        while chunk := stream.read(1024 * 1024):
                            copy.write(chunk)
                            total += len(chunk)
                            if time.monotonic() - last_log > 20:
                                log(f'{table}: {total // (1024*1024)} MiB transferred')
                                last_log = time.monotonic()
                    log(f'{table}: COPY finished')
        actual = conn.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(table))).fetchone()[0]
        if actual != count:
            raise RuntimeError(f'{table}: expected {count}, got {actual}')
        report['counts'][table] = actual
        log(f'{table}: verified {actual:,} rows')
    for statement in indexes:
        conn.execute(statement)
        log('Created index ' + statement.split()[2])
    for table in expected:
        conn.execute(sql.SQL('ANALYZE public.{}').format(sql.Identifier(table)))
    report['constraints'] = conn.execute("SELECT c.conname, c.contype, c.convalidated FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(%s) ORDER BY c.conname", (list(expected),)).fetchall()
    if any(not row[2] for row in report['constraints']):
        raise RuntimeError('Unvalidated constraint')
    report['indexes'] = conn.execute('SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=ANY(%s) ORDER BY tablename,indexname', ('public', list(expected))).fetchall()
    preserved = conn.execute('SELECT oid, relname, relfilenode FROM pg_class WHERE oid=ANY(%s) ORDER BY oid', ([row[0] for row in original_tables],)).fetchall()
    if preserved != original_tables:
        raise RuntimeError('Existing table structure changed during import')
    report['existing_tables_preserved'] = [row[1] for row in original_tables]
    report['table_sizes'] = conn.execute('SELECT relname, pg_total_relation_size(oid) FROM pg_class WHERE relnamespace=\'public\'::regnamespace AND relname=ANY(%s) ORDER BY relname', (list(expected),)).fetchall()
    report['search_plan'] = [row[0] for row in conn.execute("EXPLAIN (ANALYZE, BUFFERS) SELECT inn,tier,priority FROM public.new_cp_okpd2 WHERE okpd2_group='32.50' ORDER BY tier,priority DESC LIMIT 10")]
    log('Counts, keys, indexes and existing tables checked; committing')

report['committed_at'] = datetime.now(timezone.utc).isoformat()
report['duration_seconds'] = round(time.monotonic() - started, 1)
if args.report:
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
log('COMMITTED')
print(json.dumps(report, ensure_ascii=False, indent=2))
