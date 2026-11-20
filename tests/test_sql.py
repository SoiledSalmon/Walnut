"""Characterisation of the master document's SQL (Appendix B) on the installed PostgreSQL."""
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from buyorwait.db import ROOT, run_sql_file

AS_OF = date(2026, 11, 20)


@pytest.fixture
def data(pg):
    run_sql_file(pg, ROOT / 'db' / 'test_data.sql')
    return pg


def decide_all(conn):
    conn.execute('SELECT sp_decide(1,%s), sp_decide(2,%s), sp_decide(3,%s)', (AS_OF, AS_OF, AS_OF))
    conn.commit()


def test_precedence_rows(data):
    ids = [r['event_id'] for r in data.execute('SELECT event_id FROM V_EFFECTIVE_EVENTS ORDER BY event_id')]
    assert ids == [1, 2, 3, 4, 6, 8, 10, 11, 12, 16, 21]
    root = data.execute('SELECT root_id FROM V_EFFECTIVE_EVENTS WHERE event_id=6').fetchone()['root_id']
    assert root == 5


def test_forecast_shape(data):
    s = data.execute('SELECT count(*) n, min(forecast_date) a, max(forecast_date) b, min(headroom_above_min) h '
                     'FROM fn_forecast_90(1,%s)', (AS_OF,)).fetchone()
    assert (s['n'], s['a'], s['b'], s['h']) == (90, date(2026, 11, 20), date(2027, 2, 17), Decimal('20010.00'))
    bal = {r['forecast_date']: r['projected_balance'] for r in data.execute('SELECT * FROM fn_forecast_90(1,%s)', (AS_OF,))}
    assert bal[date(2026, 11, 20)] == Decimal('33510.00')
    assert bal[date(2026, 11, 30)] == Decimal('90010.00')
    assert bal[date(2027, 2, 17)] == Decimal('150010.00')


def test_three_decisions(data):
    decide_all(data)
    rows = [tuple(r.values()) for r in data.execute(
        'SELECT request_id, chosen_option_id, affordability_status, amount_safe_to_pay, earliest_safe_date '
        'FROM DECISIONS ORDER BY decision_id')]
    assert rows == [(1, 1, 'SAFE_NOW', Decimal('5000.00'), date(2026, 11, 20)),
                    (2, 3, 'SPLIT_PLAN', Decimal('20010.00'), date(2027, 1, 20)),
                    (3, None, 'WAIT', Decimal('20010.00'), date(2026, 12, 30))]
    plan = [r['installment_amount'] for r in data.execute(
        'SELECT installment_amount FROM PLAN_ITEMS WHERE decision_id=2 ORDER BY installment_number')]
    assert plan == [Decimal('15649.00'), Decimal('15450.00'), Decimal('15450.00')]
    cuts = [tuple(r.values()) for r in data.execute(
        'SELECT category_id, recommended_reduction, period_days FROM SPENDING_CHANGES WHERE decision_id=3 ORDER BY 1')]
    assert cuts == [(3, Decimal('2200.00'), 90), (7, Decimal('3000.00'), 90)]
    snaps = [tuple(r.values()) for r in data.execute(
        'SELECT decision_id, count(*), min(headroom_above_min) FROM V_FORECAST_HEADROOM GROUP BY 1 ORDER BY 1')]
    assert snaps == [(1, 90, Decimal('15010.00')), (2, 90, Decimal('4361.00')), (3, 90, Decimal('20010.00'))]


def test_redecide_versions(data):
    decide_all(data)
    data.execute('SELECT sp_decide(2,%s)', (AS_OF,))
    data.commit()
    rows = [tuple(r.values()) for r in data.execute(
        'SELECT decision_id, valid_time_end IS NULL FROM DECISIONS WHERE request_id=2 ORDER BY 1')]
    assert rows == [(2, False), (4, True)]


def test_tamper_detected(data):
    decide_all(data)
    assert data.execute('SELECT sp_verify_audit_chain() n').fetchone()['n'] == 0
    data.execute('UPDATE DECISIONS SET amount_safe_to_pay = 1 WHERE decision_id = 1')
    assert data.execute('SELECT sp_verify_audit_chain() n').fetchone()['n'] == 1
    flags = [r['tamper_flag'] for r in data.execute('SELECT tamper_flag FROM AUDIT_RECORDS ORDER BY audit_id')]
    assert flags == [True, False, False]


FAILING = [
    "UPDATE DECISIONS SET amount_safe_to_pay = 999999 WHERE decision_id = 2",
    "INSERT INTO FINANCIAL_EVENTS (user_id,event_type,amount,currency,event_date,source_doc_id,status) VALUES (1,'EXPENSE',-1,'INR','2026-11-20','not-a-mongo-id-00000000','CLEARED')",
    "INSERT INTO FINANCIAL_EVENTS (user_id,event_type,amount,currency,event_date,source_doc_id,status) VALUES (1,'EXPENSE',-1,'INR','2026-11-20',(SELECT source_doc_id FROM SOURCE_DOCUMENTS LIMIT 1),'SETTLED')",
    "INSERT INTO EXCHANGE_RATES (from_currency,to_currency,rate,rate_date) VALUES ('USD','INR',85,'2026-11-01')",
    "INSERT INTO PAYMENT_OPTIONS (event_id,plan_type,tenure_months) VALUES (19,'FULL',3)",
]


@pytest.mark.parametrize('stmt', FAILING)
def test_constraints_reject(data, stmt):
    decide_all(data)
    with pytest.raises(psycopg.Error):
        data.execute(stmt)


def test_index_scan(data):
    data.execute('SET enable_seqscan = off')
    plan = ' '.join(r['QUERY PLAN'] for r in data.execute(
        "EXPLAIN SELECT * FROM FINANCIAL_EVENTS WHERE user_id = 1 AND event_date BETWEEN '2026-11-01' AND '2026-11-30'"))
    assert 'ix_events_user_date' in plan
