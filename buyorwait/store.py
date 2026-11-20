"""Durable case checkpoints and actual model-call accounting."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS cases (
          run TEXT, request TEXT, fingerprint TEXT, status TEXT, payload TEXT,
          PRIMARY KEY(run,request));
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY, stamp TEXT, run TEXT, request TEXT, model TEXT,
          input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
          thinking_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
          status TEXT, error TEXT);
        ''')
        self.db.commit()

    def cached(self, run, request, fingerprint):
        with self.lock:
            row = self.db.execute('SELECT payload FROM cases WHERE run=? AND request=? AND fingerprint=? AND status=?',
                                  (run,request,fingerprint,'complete')).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run, request, fingerprint, status, payload):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?)',
                            (run,request,fingerprint,status,json.dumps(payload,ensure_ascii=False)))

    def reserve_call(self, run, request, model):
        stamp = datetime.now(timezone.utc).isoformat()
        # Persist quota accounting across restarts; rolling 24h is conservative at reset boundaries.
        with self.lock, self.db:
            used = self.db.execute("SELECT COUNT(*) FROM calls WHERE model=? AND julianday(stamp)>julianday('now','-1 day')",(model,)).fetchone()[0]
            if used >= 450:
                raise RuntimeError(f'Local daily call budget exhausted for {model}')
            cursor = self.db.execute('INSERT INTO calls(stamp,run,request,model,status) VALUES(?,?,?,?,?)',
                                     (stamp,run,request,model,'attempted'))
            return cursor.lastrowid

    def finish_call(self, call_id, usage=None, error=''):
        usage = usage or {}
        with self.lock, self.db:
            self.db.execute('UPDATE calls SET input_tokens=?,output_tokens=?,thinking_tokens=?,total_tokens=?,status=?,error=? WHERE id=?',
                            (usage.get('prompt_token_count',0) or 0, usage.get('candidates_token_count',0) or 0,
                             usage.get('thoughts_token_count',0) or 0, usage.get('total_token_count',0) or 0,
                             'error' if error else 'complete',error,call_id))

    def report(self, run):
        with self.lock:
            rows = self.db.execute("SELECT model,COUNT(*),SUM(input_tokens),SUM(output_tokens),SUM(thinking_tokens),SUM(total_tokens),SUM(status='error') FROM calls WHERE run=? GROUP BY model",(run,)).fetchall()
        return [dict(zip(['model','calls','input_tokens','output_tokens','thinking_tokens','total_tokens','errors'],r)) for r in rows]


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
    temporary.replace(path)
