from datetime import date, datetime
from decimal import Decimal

import psycopg
import pytest

from buyorwait import services as s
from buyorwait.evidence import add_evidence
from buyorwait.extract import RuleExtractor
from buyorwait.gate import process_evidence

AS_OF = date(2026, 11, 20)
REF_SQL = """
INSERT INTO CURRENCIES VALUES ('INR','Indian Rupee'), ('USD','US Dollar');
INSERT INTO CATEGORIES (category_name,is_discretionary) VALUES ('Electronics',TRUE),('Groceries',FALSE),('Dining',TRUE),('Rent',FALSE),('Utilities',FALSE),('Salary',FALSE),('Shopping',TRUE);
"""


@pytest.fixture
def db(pg, mongo):
    pg.execute(REF_SQL)
    pg.commit()
    return pg, mongo


def user_with_income(pg, mongo):
    uid = s.register_user(pg, 'Asha', 'asha@example.com', 'INR', Decimal('10000'), Decimal('5000'))
    eid = add_evidence(mongo, uid, 'LEDGER_FILE', '2026-11-01|CR|50000.00|INR|ACME Corp|CLEARED', datetime(2026, 11, 1))
    process_evidence(pg, mongo, RuleExtractor(), eid, AS_OF)
    return uid


def test_register_creates_user_and_profile(db):
    pg, _ = db
    uid = s.register_user(pg, 'Asha', 'asha@example.com', 'INR', Decimal('10000'), Decimal('5000'))
    p = s.get_profile(pg, uid)
    assert (p['name'], p['currency'], p['min_balance_threshold']) == ('Asha', 'INR', Decimal('10000.00'))
    assert [u['user_id'] for u in s.list_users(pg)] == [uid]


def test_register_duplicate_email_raises_and_writes_nothing(db):
    pg, _ = db
    s.register_user(pg, 'Asha', 'asha@example.com', 'INR', Decimal('10000'), Decimal('5000'))
    with pytest.raises(psycopg.errors.UniqueViolation):
        s.register_user(pg, 'Asha 2', 'asha@example.com', 'INR', Decimal('10000'), Decimal('5000'))
    assert pg.execute('SELECT count(*) n FROM FINANCIAL_PROFILES').fetchone()['n'] == 1


def test_manual_event_goes_through_gate(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    r = s.add_manual_event(pg, mongo, uid, {'event_type': 'EXPENSE', 'amount': '-500.00', 'currency': 'INR',
                                            'event_date': '2026-11-15', 'status': 'CLEARED', 'category': 'Dining',
                                            'merchant': 'Cafe', 'recurrence_interval': None}, AS_OF)
    assert r['event_id'] and r['evidence']['kind'] == 'FORM'
    events = s.list_events(pg, mongo, uid)
    row = next(e for e in events if e['event_id'] == r['event_id'])
    assert row['is_effective'] and row['source_type'] == 'MANUAL' and row['category_name'] == 'Dining'
    assert row['evidence']['kind'] == 'FORM'


def test_create_request_links_event_option_evidence(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    rid = s.create_request(pg, mongo, uid, Decimal('5000'), 1, date(2026, 12, 31), 'Headphones', AS_OF)
    req = pg.execute('SELECT * FROM REQUESTS r JOIN FINANCIAL_EVENTS e USING (event_id) WHERE request_id=%s', (rid,)).fetchone()
    assert (req['status'], req['amount'], req['event_date']) == ('PROPOSED', Decimal('-5000.00'), AS_OF)
    opts = s.list_options(pg, rid)
    assert [(o['plan_type'], o['tenure_months']) for o in opts] == [('FULL', 0)]
    ev = mongo.raw_evidence.find_one({'_id': __import__('bson').ObjectId(req['source_doc_id'])})
    assert ev['kind'] == 'FORM' and ev['status'] == 'PROMOTED'
    oid = s.add_payment_option(pg, rid, 'EMI', 3, 1200, Decimal('199'))
    assert len(s.list_options(pg, rid)) == 2 and oid
    assert [r['request_id'] for r in s.list_requests(pg, uid)] == [rid]


def test_decide_returns_ninety_forecast_rows_and_elapsed(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    rid = s.create_request(pg, mongo, uid, Decimal('5000'), 1, date(2026, 12, 31), 'Headphones', AS_OF)
    d = s.decide(pg, rid, AS_OF)
    assert d['decision']['affordability_status'] == 'SAFE_NOW'
    assert len(d['forecast']) == 90 and d['elapsed_ms'] >= 0
    assert d['audit']['current_hash'] and d['option']['plan_type'] == 'FULL'
    assert [p['installment_amount'] for p in d['plan_items']] == [Decimal('5000.00')]


def test_versions_after_redecide(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    rid = s.create_request(pg, mongo, uid, Decimal('5000'), 1, date(2026, 12, 31), 'Headphones', AS_OF)
    s.decide(pg, rid, AS_OF)
    s.decide(pg, rid, AS_OF)
    assert [v['is_current'] for v in s.list_versions(pg, rid)] == [False, True]


def test_verify_and_tamper(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    rid = s.create_request(pg, mongo, uid, Decimal('5000'), 1, date(2026, 12, 31), 'Headphones', AS_OF)
    did = s.decide(pg, rid, AS_OF)['decision']['decision_id']
    assert s.verify_chain(pg)['tampered'] == 0
    s.tamper_decision(pg, did)
    v = s.verify_chain(pg)
    assert v['tampered'] == 1 and v['records'][0]['tamper_flag'] is True
    assert len(s.audit_chain(pg)) == 1


def test_explain_uses_index(db):
    pg, mongo = db
    uid = user_with_income(pg, mongo)
    plan = s.explain_events(pg, uid, date(2026, 11, 1), date(2026, 11, 30))
    assert any('ix_events_user_date' in line for line in plan)


def test_meta(db):
    pg, _ = db
    from buyorwait.config import Settings
    m = s.meta(pg, Settings('', '', 'rules', '', AS_OF))
    assert m['as_of'] == AS_OF and m['mongo_mode'] == 'in-memory' and len(m['categories']) == 7
