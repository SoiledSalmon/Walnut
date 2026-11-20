import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from buyorwait.api import create_app
from buyorwait.config import Settings
from buyorwait.db import pg_connect
from buyorwait.seed import REFERENCE_SQL

AS_OF = date(2026, 11, 20)


@pytest.fixture
def client(pg, mongo):
    pg.execute(REFERENCE_SQL)
    pg.commit()
    url = os.environ['TEST_POSTGRES_URL']
    app = create_app(Settings(url, '', 'rules', '', AS_OF), conn_factory=lambda: pg_connect(url), mongo=mongo,
                     seed_on_start=False)
    with TestClient(app) as c:
        yield c


def register(client, email='asha@example.com'):
    r = client.post('/api/users', json={'name': 'Asha', 'email': email, 'currency': 'INR',
                                        'min_balance_threshold': '10000', 'target_monthly_savings': '5000'})
    assert r.status_code == 200, r.text
    return r.json()['user_id']


def event_count(pg):
    n = pg.execute('SELECT count(*) n FROM FINANCIAL_EVENTS').fetchone()['n']
    pg.rollback()
    return n


def test_happy_path(client):
    uid = register(client)
    ev = client.post(f'/api/users/{uid}/evidence', json={'kind': 'SMS', 'raw_text': 'INR 50,000 credited from ACME Corp on 01-11-2026.'}).json()
    processed = client.post(f'/api/evidence/{ev["_id"]}/process').json()
    assert processed['candidate']['validation']['result'] == 'PASSED' and processed['event_id']
    events = client.get(f'/api/users/{uid}/events').json()
    assert events[0]['evidence']['raw_text'].startswith('INR 50,000') and events[0]['amount'] == '50000.00'
    assert client.get(f'/api/users/{uid}/evidence').json()[0]['status'] == 'PROMOTED'
    rid = client.post(f'/api/users/{uid}/requests', json={'requested_amount': '5000', 'category_id': 1,
                                                          'target_deadline': '2026-12-31', 'description': 'Phone'}).json()['request_id']
    client.post(f'/api/requests/{rid}/options', json={'plan_type': 'EMI', 'tenure_months': 3, 'interest_rate_bps': 1200, 'processing_fee': '199'})
    assert len(client.get(f'/api/requests/{rid}/options').json()) == 2
    d = client.post(f'/api/requests/{rid}/decide').json()
    assert d['decision']['affordability_status'] == 'SAFE_NOW' and len(d['forecast']) == 90 and 'elapsed_ms' in d
    assert client.get(f'/api/decisions/{d["decision"]["decision_id"]}').json()['decision']['request_id'] == rid
    assert len(client.get(f'/api/requests/{rid}/decisions').json()) == 1
    assert client.get(f'/api/users/{uid}/requests').json()[0]['affordability_status'] == 'SAFE_NOW'
    assert any('ix_events_user_date' in l for l in client.get(f'/api/users/{uid}/explain').json()['plan'])
    m = client.get('/api/meta').json()
    assert m['as_of'] == '2026-11-20' and m['mongo_mode'] == 'in-memory'
    assert client.get(f'/api/users/{uid}/profile').json()['min_balance_threshold'] == '10000.00'


def test_held_confirm(client):
    uid = register(client)
    ev = client.post(f'/api/users/{uid}/evidence', json={'kind': 'SMS', 'raw_text': 'Paid 450 to Ravi'}).json()
    c = client.post(f'/api/evidence/{ev["_id"]}/process').json()['candidate']
    assert c['validation']['result'] == 'HELD'
    assert client.post(f'/api/candidates/{c["_id"]}/confirm').json()['event_id']
    assert client.post(f'/api/candidates/{c["_id"]}/confirm').status_code == 422


def test_bad_event_422(client, pg):
    uid = register(client)
    good = {'event_type': 'EXPENSE', 'category': 'Dining', 'amount': '-100', 'currency': 'INR', 'event_date': '2026-11-15',
            'status': 'CLEARED', 'recurrence_interval': None, 'description': 'Lunch'}
    assert client.post(f'/api/users/{uid}/events', json=good | {'amount': '0'}).status_code == 422
    assert client.post(f'/api/users/{uid}/events', json=good | {'event_date': 'not-a-date'}).status_code == 422
    r = client.post(f'/api/users/{uid}/events', json=good | {'currency': 'GBP'}).json()
    assert r['candidate']['validation']['result'] == 'REJECTED' and r['event_id'] is None
    assert event_count(pg) == 0
    assert client.post(f'/api/users/{uid}/events', json=good).json()['event_id']


def test_option_full_with_tenure_422(client):
    uid = register(client)
    rid = client.post(f'/api/users/{uid}/requests', json={'requested_amount': '5000', 'category_id': 1,
                                                          'target_deadline': None, 'description': 'x'}).json()['request_id']
    r = client.post(f'/api/requests/{rid}/options', json={'plan_type': 'FULL', 'tenure_months': 3, 'interest_rate_bps': 0, 'processing_fee': '0'})
    assert r.status_code == 422


def test_duplicate_email_422(client):
    register(client)
    r = client.post('/api/users', json={'name': 'B', 'email': 'asha@example.com', 'currency': 'INR',
                                        'min_balance_threshold': '0', 'target_monthly_savings': '0'})
    assert r.status_code == 422


def test_unknown_things_404(client):
    assert client.post('/api/requests/999/decide').status_code == 404
    assert client.post('/api/evidence/not-an-id/process').status_code == 422


def test_tamper_then_verify(client):
    uid = register(client)
    rid = client.post(f'/api/users/{uid}/requests', json={'requested_amount': '5000', 'category_id': 1,
                                                          'target_deadline': None, 'description': 'x'}).json()['request_id']
    did = client.post(f'/api/requests/{rid}/decide').json()['decision']['decision_id']
    assert client.post('/api/audit/verify').json()['tampered'] == 0
    assert client.post(f'/api/decisions/{did}/tamper').status_code == 200
    v = client.post('/api/audit/verify').json()
    assert v['tampered'] == 1 and client.get('/api/audit').json()[0]['tamper_flag'] is True


def test_frontend_served(client):
    r = client.get('/')
    assert r.status_code == 200 and 'text/html' in r.headers['content-type']


def test_frontend_never_served_stale(client):
    assert client.get('/app.js').headers.get('cache-control') == 'no-cache'
