from datetime import date
from pathlib import Path

import pytest

from buyorwait.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_schema_loads_sixteen_tables(pg):
    n = pg.execute("SELECT count(*) AS n FROM information_schema.tables "
                   "WHERE table_schema='public' AND table_type='BASE TABLE'").fetchone()['n']
    assert n == 16


def test_mongo_in_memory_ping(mongo):
    assert mongo.command('ping')['ok'] == 1


def test_settings_defaults(monkeypatch):
    for k in ('MONGO_URL', 'EXTRACTOR', 'AS_OF_DATE'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('MONGO_URL', '')
    monkeypatch.setenv('AS_OF_DATE', '2026-11-20')
    s = load_settings()
    assert s.extractor == 'rules' and s.mongo_url == '' and s.as_of() == date(2026, 11, 20)


def test_settings_blank_as_of_is_today(monkeypatch):
    monkeypatch.setenv('AS_OF_DATE', '')
    assert load_settings().as_of() == date.today()


def test_sql_files_are_verbatim():
    src = Path(r'C:\Users\Ananth\Downloads')
    if not (src / 'schema_1.sql').exists():
        pytest.skip('master doc sources not on this machine')
    for a, b in [('schema_1.sql', 'schema.sql'), ('test_data.sql', 'test_data.sql'), ('run_tests.sql', 'run_tests.sql')]:
        assert (src / a).read_bytes() == (ROOT / 'db' / b).read_bytes()
