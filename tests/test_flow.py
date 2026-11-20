"""Seed + live inbox processing ends in the master document's Appendix B results."""
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from buyorwait import services as s
from buyorwait.extract import RuleExtractor
from buyorwait.gate import process_evidence
from buyorwait.seed import seed

AS_OF = date(2026, 11, 20)


@pytest.fixture
def seeded(pg, mongo):
    info = seed(pg, mongo, AS_OF, reset=True)
    return pg, mongo, info


def process_inbox(pg, mongo, info):
    return [process_evidence(pg, mongo, RuleExtractor(), eid, AS_OF) for eid in info['inbox']]


def test_seed_refuses_without_reset(seeded):
    pg, mongo, _ = seeded
    with pytest.raises(RuntimeError, match='--reset'):
        seed(pg, mongo, AS_OF, reset=False)


def test_seed_reset_is_repeatable(seeded):
    pg, mongo, _ = seeded
    info = seed(pg, mongo, AS_OF, reset=True)
    assert pg.execute('SELECT count(*) n FROM USERS').fetchone()['n'] == 1
    assert [r['request_id'] for r in s.list_requests(pg, info['user_id'])] == [1, 2, 3]
    assert [o['option_id'] for o in s.list_options(pg, 2)] == [2, 3, 4]


def test_inbox_results(seeded):
    pg, mongo, info = seeded
    results = [r['candidate']['validation']['result'] for r in process_inbox(pg, mongo, info)]
    assert results[-2:] == ['REJECTED', 'HELD']
    assert set(results[:-2]) == {'PASSED'}


def test_every_event_has_a_mongo_document(seeded):
    pg, mongo, info = seeded
    process_inbox(pg, mongo, info)
    from bson import ObjectId
    for r in pg.execute('SELECT source_doc_id FROM FINANCIAL_EVENTS'):
        assert mongo.raw_evidence.find_one({'_id': ObjectId(r['source_doc_id'])})


def test_flow_matches_appendix_b(seeded):
    pg, mongo, info = seeded
    process_inbox(pg, mongo, info)
    effective = Counter((r['amount'], r['currency']) for r in pg.execute('SELECT amount, currency FROM V_EFFECTIVE_EVENTS'))
    assert effective == Counter([(Decimal(a), c) for a, c in [
        ('60000.00', 'INR'), ('-20000.00', 'INR'), ('-3000.00', 'INR'), ('-2000.00', 'INR'), ('-2400.00', 'INR'),
        ('-1500.00', 'INR'), ('100.00', 'USD'), ('60000.00', 'INR'), ('-20000.00', 'INR'), ('-3500.00', 'INR'),
        ('-6000.00', 'INR')]])
    first = pg.execute('SELECT projected_balance FROM fn_forecast_90(%s,%s) LIMIT 1', (info['user_id'], AS_OF)).fetchone()
    assert first['projected_balance'] == Decimal('33510.00')
    pg.rollback()
    ds = [s.decide(pg, rid, AS_OF) for rid in (1, 2, 3)]
    assert [(d['decision']['affordability_status'], d['decision']['chosen_option_id'], d['decision']['amount_safe_to_pay'],
             d['decision']['earliest_safe_date']) for d in ds] == [
        ('SAFE_NOW', 1, Decimal('5000.00'), date(2026, 11, 20)),
        ('SPLIT_PLAN', 3, Decimal('20010.00'), date(2027, 1, 20)),
        ('WAIT', None, Decimal('20010.00'), date(2026, 12, 30))]
    assert [p['installment_amount'] for p in ds[1]['plan_items']] == [Decimal('15649.00'), Decimal('15450.00'), Decimal('15450.00')]
    assert {c['category_name']: c['recommended_reduction'] for c in ds[2]['spending_changes']} == \
        {'Dining': Decimal('2200.00'), 'Shopping': Decimal('3000.00')}
