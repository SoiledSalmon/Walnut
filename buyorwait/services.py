"""Service layer: parameterised SQL around the in-database decision engine.

No affordability arithmetic lives here; sp_decide does all of it.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
from bson import ObjectId

from .evidence import add_evidence
from .extract import RuleExtractor
from .gate import process_evidence


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


# ---------- users and profiles (DFD 1.0) ----------

def register_user(conn, name, email, currency, min_balance_threshold, target_monthly_savings) -> int:
    with conn.transaction():
        uid = _one(conn, 'INSERT INTO USERS (name, email) VALUES (%s, %s) RETURNING user_id', (name, email))['user_id']
        conn.execute('INSERT INTO FINANCIAL_PROFILES (user_id, currency, min_balance_threshold, target_monthly_savings) '
                     'VALUES (%s, %s, %s, %s)', (uid, currency, min_balance_threshold, target_monthly_savings))
    return uid


def list_users(conn) -> list[dict]:
    rows = _rows(conn, 'SELECT user_id, name, email FROM USERS ORDER BY user_id')
    conn.rollback()
    return rows


def get_profile(conn, user_id) -> dict | None:
    row = _one(conn, 'SELECT u.user_id, u.name, u.email, p.currency, p.min_balance_threshold, p.target_monthly_savings '
                     'FROM USERS u JOIN FINANCIAL_PROFILES p USING (user_id) WHERE u.user_id=%s', (user_id,))
    conn.rollback()
    return row


def meta(conn, settings) -> dict:
    out = {'as_of': settings.as_of(), 'extractor': settings.extractor,
           'mongo_mode': 'server' if settings.mongo_url else 'in-memory',
           'categories': _rows(conn, 'SELECT * FROM CATEGORIES ORDER BY category_id'),
           'currencies': _rows(conn, 'SELECT * FROM CURRENCIES ORDER BY currency_code')}
    conn.rollback()
    return out


# ---------- events ----------

def add_manual_event(conn, mongo, user_id, fields: dict, as_of: date) -> dict:
    eid = add_evidence(mongo, user_id, 'FORM', json.dumps(fields, default=str), datetime.now(timezone.utc))
    return process_evidence(conn, mongo, RuleExtractor(), eid, as_of)


def list_events(conn, mongo, user_id) -> list[dict]:
    rows = _rows(conn, """
        WITH RECURSIVE fam AS (
          SELECT event_id, event_id AS root_id FROM FINANCIAL_EVENTS WHERE parent_event_id IS NULL AND user_id=%s
          UNION ALL
          SELECT e.event_id, f.root_id FROM FINANCIAL_EVENTS e JOIN fam f ON e.parent_event_id = f.event_id)
        SELECT v.event_id, v.event_type, v.amount, v.currency, v.event_date, v.status, v.source_type, v.source_doc_id,
               v.parent_event_id, v.recurrence_interval, v.is_recurring, v.status_rank, v.source_rank, v.forecast_rule,
               f.root_id, c.category_name, (ee.event_id IS NOT NULL) AS is_effective
        FROM V_EVENTS_FULL v JOIN fam f USING (event_id)
        LEFT JOIN CATEGORIES c ON c.category_id = v.category_id
        LEFT JOIN V_EFFECTIVE_EVENTS ee ON ee.event_id = v.event_id
        WHERE v.user_id=%s ORDER BY f.root_id, v.event_id""", (user_id, user_id))
    conn.rollback()
    docs = {str(d['_id']): d for d in mongo.raw_evidence.find(
        {'_id': {'$in': [ObjectId(r['source_doc_id']) for r in rows]}}, {'kind': 1, 'raw_text': 1, 'status': 1, 'ocr': 1})}
    for r in rows:
        d = docs.get(r['source_doc_id'], {})
        r['evidence'] = {'kind': d.get('kind'), 'raw_text': d.get('raw_text') or (d.get('ocr') or {}).get('text'),
                         'status': d.get('status')}
    return rows


# ---------- requests and payment options (DFD 5.0, 6.0) ----------

def create_request(conn, mongo, user_id, requested_amount, category_id, target_deadline, description, as_of) -> int:
    form = {'request': description, 'requested_amount': str(requested_amount), 'category_id': category_id,
            'target_deadline': str(target_deadline) if target_deadline else None}
    eid = add_evidence(mongo, user_id, 'FORM', json.dumps(form), datetime.now(timezone.utc), status='EXTRACTED')
    try:
        with conn.transaction():
            conn.execute("INSERT INTO SOURCE_DOCUMENTS (source_doc_id, source_type) VALUES (%s, 'MANUAL')", (str(eid),))
            event_id = _one(conn, "INSERT INTO FINANCIAL_EVENTS (user_id, event_type, category_id, amount, currency, event_date, "
                                  "source_doc_id, status) SELECT %s, 'EXPENSE', %s, %s, currency, %s, %s, 'PROPOSED' "
                                  "FROM FINANCIAL_PROFILES WHERE user_id=%s RETURNING event_id",
                            (user_id, category_id, -Decimal(requested_amount), as_of, str(eid), user_id))
            if event_id is None:
                raise ValueError('user has no financial profile')
            event_id = event_id['event_id']
            conn.execute("INSERT INTO PAYMENT_OPTIONS (event_id, plan_type) VALUES (%s, 'FULL')", (event_id,))
            rid = _one(conn, 'INSERT INTO REQUESTS (user_id, event_id, requested_amount, category_id, target_deadline) '
                             'VALUES (%s, %s, %s, %s, %s) RETURNING request_id',
                       (user_id, event_id, requested_amount, category_id, target_deadline))['request_id']
    except Exception:
        mongo.raw_evidence.update_one({'_id': eid}, {'$set': {'status': 'REJECTED'}})
        raise
    mongo.raw_evidence.update_one({'_id': eid}, {'$set': {'status': 'PROMOTED'}})
    return rid


def add_payment_option(conn, request_id, plan_type, tenure_months, interest_rate_bps, processing_fee) -> int:
    with conn.transaction():
        row = _one(conn, 'INSERT INTO PAYMENT_OPTIONS (event_id, plan_type, tenure_months, interest_rate_bps, processing_fee) '
                         'SELECT event_id, %s, %s, %s, %s FROM REQUESTS WHERE request_id=%s RETURNING option_id',
                   (plan_type, tenure_months, interest_rate_bps, processing_fee, request_id))
        if row is None:
            raise ValueError('request not found')
    return row['option_id']


def list_requests(conn, user_id) -> list[dict]:
    rows = _rows(conn, """
        SELECT r.*, c.category_name, d.decision_id AS current_decision_id, d.affordability_status
        FROM REQUESTS r JOIN CATEGORIES c USING (category_id)
        LEFT JOIN DECISIONS d ON d.request_id = r.request_id AND d.valid_time_end IS NULL
        WHERE r.user_id=%s ORDER BY r.request_id""", (user_id,))
    conn.rollback()
    return rows


def list_options(conn, request_id) -> list[dict]:
    rows = _rows(conn, 'SELECT o.* FROM PAYMENT_OPTIONS o JOIN REQUESTS r USING (event_id) WHERE r.request_id=%s '
                       'ORDER BY o.option_id', (request_id,))
    conn.rollback()
    return rows


# ---------- decisions (DFD 7.0) ----------

def decide(conn, request_id, as_of: date) -> dict:
    start = time.perf_counter()
    # Check inside the transaction: a read before it would make conn.transaction() a savepoint that never commits.
    with conn.transaction():
        if not _one(conn, 'SELECT 1 FROM REQUESTS WHERE request_id=%s', (request_id,)):
            raise ValueError('request not found')
        decision_id = _one(conn, 'SELECT sp_decide(%s, %s) AS d', (request_id, as_of))['d']
    elapsed = (time.perf_counter() - start) * 1000
    return get_decision(conn, decision_id) | {'elapsed_ms': round(elapsed, 2)}


def get_decision(conn, decision_id) -> dict:
    d = _one(conn, 'SELECT d.*, r.requested_amount, r.user_id FROM DECISIONS d JOIN REQUESTS r USING (request_id) '
                   'WHERE decision_id=%s', (decision_id,))
    if d is None:
        conn.rollback()
        raise ValueError('decision not found')
    out = {'decision': d,
           'option': _one(conn, 'SELECT * FROM PAYMENT_OPTIONS WHERE option_id=%s', (d['chosen_option_id'],)),
           'plan_items': _rows(conn, 'SELECT * FROM PLAN_ITEMS WHERE decision_id=%s ORDER BY installment_number', (decision_id,)),
           'forecast': _rows(conn, 'SELECT forecast_date, projected_balance, projected_savings, headroom_above_min '
                                   'FROM V_FORECAST_HEADROOM WHERE decision_id=%s ORDER BY forecast_date', (decision_id,)),
           'spending_changes': _rows(conn, 'SELECT s.*, c.category_name FROM SPENDING_CHANGES s JOIN CATEGORIES c '
                                           'USING (category_id) WHERE decision_id=%s ORDER BY category_id', (decision_id,)),
           'audit': _one(conn, 'SELECT * FROM AUDIT_RECORDS WHERE decision_id=%s', (decision_id,))}
    conn.rollback()
    return out


def list_versions(conn, request_id) -> list[dict]:
    rows = _rows(conn, 'SELECT decision_id, affordability_status, valid_time_start, valid_time_end, created_at, '
                       '(valid_time_end IS NULL) AS is_current FROM DECISIONS WHERE request_id=%s ORDER BY decision_id',
                 (request_id,))
    conn.rollback()
    return rows


# ---------- audit (DFD 7.8, 8.0) ----------

def audit_chain(conn) -> list[dict]:
    rows = _rows(conn, 'SELECT * FROM AUDIT_RECORDS ORDER BY audit_id')
    conn.rollback()
    return rows


def verify_chain(conn) -> dict:
    with conn.transaction():
        n = _one(conn, 'SELECT sp_verify_audit_chain() AS n')['n']
    return {'tampered': n, 'records': audit_chain(conn)}


def tamper_decision(conn, decision_id) -> None:
    """Demo only: edit a sealed decision directly, as run_tests.sql does, so verify can catch it."""
    with conn.transaction():
        if conn.execute('UPDATE DECISIONS SET amount_safe_to_pay = 1 WHERE decision_id = %s', (decision_id,)).rowcount == 0:
            raise ValueError('decision not found')


# ---------- NFR1: index use ----------

def explain_events(conn, user_id, start: date, end: date) -> list[str]:
    # EXPLAIN cannot take server-side parameters; ClientCursor binds them safely on the client.
    with conn.transaction():
        cur = psycopg.ClientCursor(conn)
        cur.execute('SET LOCAL enable_seqscan = off')
        cur.execute('EXPLAIN SELECT * FROM FINANCIAL_EVENTS WHERE user_id = %s AND event_date BETWEEN %s AND %s',
                    (user_id, start, end))
        return [r['QUERY PLAN'] for r in cur.fetchall()]
