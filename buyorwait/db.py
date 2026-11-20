"""PostgreSQL and MongoDB connections."""
from __future__ import annotations

import atexit
import threading
from pathlib import Path

import psycopg
import pymongo
from psycopg.rows import dict_row

from .config import ROOT

SCHEMA = ROOT / 'db' / 'schema.sql'
_inmemory = None
_lock = threading.Lock()


def pg_connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=False)


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text(encoding='utf-8'))
    conn.commit()


def reset_postgres(conn: psycopg.Connection) -> None:
    conn.execute('DROP SCHEMA public CASCADE')
    conn.execute('CREATE SCHEMA public')
    conn.commit()
    run_sql_file(conn, SCHEMA)


def mongo_client(url: str) -> pymongo.MongoClient:
    if url:
        return pymongo.MongoClient(url)
    global _inmemory
    with _lock:
        if _inmemory is None:
            # shortcut: in-memory mongod (data lost on exit); set MONGO_URL for a real server.
            import pymongo_inmemory
            _inmemory = pymongo_inmemory.MongoClient()
            atexit.register(_inmemory.close)  # stop mongod before its temp dir is removed (Windows file lock)
        return _inmemory


def mongo_db(client: pymongo.MongoClient):
    return client['buyorwait']
