import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buyorwait.config import load_settings  # noqa: E402
from buyorwait.db import mongo_client, pg_connect, reset_postgres  # noqa: E402

load_settings()  # loads .env into the environment


def _test_url():
    url = os.getenv('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('TEST_POSTGRES_URL not set (see .env.example)')
    return url


@pytest.fixture
def pg():
    url = _test_url()
    name = conninfo_to_dict(url)['dbname']
    admin = make_conninfo(url, dbname='postgres')
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        a.execute(f'CREATE DATABASE "{name}"')
    conn = pg_connect(url)
    reset_postgres(conn)
    yield conn
    conn.close()


@pytest.fixture(scope='session')
def _mongo_client():
    return mongo_client('')


@pytest.fixture
def mongo(_mongo_client):
    _mongo_client.drop_database('buyorwait_test')
    return _mongo_client['buyorwait_test']
