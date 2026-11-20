"""Validation gate and promotion (DFD 3.0, master document section 4.3).

Candidates are untrusted. Only values that pass every check reach PostgreSQL, and
only through parameterised statements.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from bson import ObjectId

from .evidence import SOURCE_TYPE, image_bytes, latest_candidate, merchant_of
from .extract import safe_error

MIN_CONFIDENCE = 0.80
WINDOW_DAYS = 365
EVENT_TYPES = {'INCOME', 'EXPENSE', 'BILL', 'TRANSFER', 'INVESTMENT'}
INTERVALS = {None, 'DAILY', 'WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY'}
SYSTEM_STATUSES = {'PROPOSED', 'DUPLICATE'}  # set by the system, never accepted from evidence
ACTION_STATUS = {'CANCEL': 'CANCELLED', 'DELAY': 'AMENDED', 'AMEND': 'AMENDED', 'FILL_AMOUNT': 'AMENDED', 'CONFIRM': 'CLEARED'}


def _now():
    return datetime.now(timezone.utc)


def _same(a, b) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def _root(conn, event_id: int) -> int:
    while True:
        parent = conn.execute('SELECT parent_event_id FROM FINANCIAL_EVENTS WHERE event_id=%s', (event_id,)).fetchone()['parent_event_id']
        if parent is None:
            return event_id
        event_id = parent


def resolve_target(conn, mongo, user_id: int, hint: dict | None) -> int | None:
    if not hint or not hint.get('event_date'):
        return None
    day = date.fromisoformat(hint['event_date'])
    sql = ('SELECT event_id FROM FINANCIAL_EVENTS WHERE user_id=%s AND parent_event_id IS NULL '
           'AND event_date BETWEEN %s AND %s')
    params = [user_id, day - timedelta(days=1), day + timedelta(days=1)]
    if hint.get('amount'):
        sql += ' AND amount=%s'
        params.append(Decimal(hint['amount']))
    for row in conn.execute(sql + ' ORDER BY event_id', params):
        if _same(merchant_of(mongo, row['event_id']), hint.get('merchant')):
            return row['event_id']
    return None


def _duplicate(conn, mongo, user_id, amount, currency, day, merchant, source_type):
    rows = conn.execute(
        'SELECT e.event_id, sd.source_type FROM FINANCIAL_EVENTS e JOIN SOURCE_DOCUMENTS sd USING (source_doc_id) '
        'WHERE e.user_id=%s AND e.amount=%s AND e.currency=%s AND e.event_date BETWEEN %s AND %s ORDER BY e.event_id',
        (user_id, amount, currency, day - timedelta(days=1), day + timedelta(days=1))).fetchall()
    matches = [r for r in rows if _same(merchant_of(mongo, r['event_id']), merchant)]
    if not matches:
        return None, None
    same = [r for r in matches if r['source_type'] == source_type]
    if same:
        return 'DUPLICATE', _root(conn, same[0]['event_id'])
    return 'RELATED', _root(conn, matches[0]['event_id'])


def validate(conn, mongo, candidate: dict, user_id: int, as_of: date, source_type: str) -> tuple[str, list[str], dict]:
    x, errors = candidate.get('extracted') or {}, []
    profile = conn.execute('SELECT currency, min_balance_threshold FROM FINANCIAL_PROFILES WHERE user_id=%s', (user_id,)).fetchone()
    if not profile:
        return 'REJECTED', ['user has no financial profile'], {}
    # type and format
    try:
        amount = Decimal(str(x.get('amount')))
        if not amount.is_finite() or amount == 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        amount = None
        errors.append('amount must be a non-zero number')
    try:
        day = date.fromisoformat(str(x.get('event_date')))
    except ValueError:
        day = None
        errors.append('event_date must be an ISO date (YYYY-MM-DD)')
    currency = x.get('currency')
    if not conn.execute('SELECT 1 FROM CURRENCIES WHERE currency_code=%s', (currency,)).fetchone():
        errors.append(f'unknown currency {currency!r}')
    elif day and currency != profile['currency'] and not conn.execute(
            'SELECT 1 FROM EXCHANGE_RATES WHERE from_currency=%s AND to_currency=%s AND rate_date<=%s',
            (currency, profile['currency'], min(day, as_of))).fetchone():
        # fn_forecast_90 converts future flows at the as-of date, so the rate must exist by the earlier of the two
        errors.append(f'no exchange rate {currency}->{profile["currency"]} on or before {min(day, as_of)}')
    if x.get('event_type') not in EVENT_TYPES:
        errors.append(f'unknown event_type {x.get("event_type")!r}')
    if x.get('recurrence_interval') not in INTERVALS:
        errors.append(f'unknown recurrence_interval {x.get("recurrence_interval")!r}')
    status = x.get('status')
    if candidate.get('action', 'CREATE') == 'CREATE' and (status in SYSTEM_STATUSES or not conn.execute(
            'SELECT 1 FROM EVENT_STATUSES WHERE status=%s', (status,)).fetchone()):
        errors.append(f'status {status!r} not allowed')
    category_id = None
    if x.get('category'):
        row = conn.execute('SELECT category_id FROM CATEGORIES WHERE category_name=%s', (x['category'],)).fetchone()
        if row:
            category_id = row['category_id']
        else:
            errors.append(f'unknown category {x["category"]!r}')
    # range
    # shortcut: limit compared in the event's own currency, not converted; fine for INR-based demo users.
    limit = max(100 * profile['min_balance_threshold'], Decimal('100000'))
    if amount is not None and abs(amount) > limit:
        errors.append(f'amount out of plausible range (|amount| > {limit})')
    if day and abs((day - as_of).days) > WINDOW_DAYS:
        errors.append(f'event_date out of range (more than {WINDOW_DAYS} days from {as_of})')
    if errors:
        return 'REJECTED', errors, {}
    # ownership, action mapping, duplicates
    action, parent = candidate.get('action', 'CREATE'), None
    if action not in ('CREATE', *ACTION_STATUS):
        return 'REJECTED', [f'unknown action {action!r}'], {}
    if candidate.get('target_event_id') is not None:
        owner = conn.execute('SELECT user_id FROM FINANCIAL_EVENTS WHERE event_id=%s', (candidate['target_event_id'],)).fetchone()
        if not owner:
            return 'REJECTED', ['target event not found'], {}
        if owner['user_id'] != user_id:
            return 'REJECTED', ['target event belongs to another user'], {}
        parent = candidate['target_event_id']
    elif action != 'CREATE':
        parent = resolve_target(conn, mongo, user_id, candidate.get('target_hint'))
        if parent is None:
            return 'REJECTED', ['target event not found'], {}
    if action != 'CREATE':
        status = ACTION_STATUS[action]
    else:
        kind, root = _duplicate(conn, mongo, user_id, amount, currency, day, x.get('merchant'), source_type)
        if kind == 'DUPLICATE':
            status, parent = 'DUPLICATE', root
        elif kind == 'RELATED':
            parent = root
    resolved = {'event_type': x['event_type'], 'category_id': category_id, 'amount': str(amount), 'currency': currency,
                'event_date': day.isoformat(), 'status': status, 'parent_event_id': parent,
                'recurrence_interval': x.get('recurrence_interval')}
    if float(candidate.get('confidence', 0)) < MIN_CONFIDENCE:
        return 'HELD', [f'confidence {candidate.get("confidence")} below {MIN_CONFIDENCE}; needs user confirmation'], resolved
    return 'PASSED', [], resolved


def promote(conn, mongo, candidate_id: ObjectId) -> int:
    c = mongo.candidate_events.find_one({'_id': candidate_id})
    r = c['resolved']
    with conn.transaction():
        conn.execute('INSERT INTO SOURCE_DOCUMENTS (source_doc_id, source_type) VALUES (%s, %s)',
                     (str(c['evidence_id']), c['source_type']))
        event_id = conn.execute(
            'INSERT INTO FINANCIAL_EVENTS (user_id, event_type, category_id, amount, currency, event_date, source_doc_id, '
            'parent_event_id, status, recurrence_interval) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING event_id',
            (c['user_id'], r['event_type'], r['category_id'], Decimal(r['amount']), r['currency'],
             date.fromisoformat(r['event_date']), str(c['evidence_id']), r['parent_event_id'], r['status'],
             r['recurrence_interval'])).fetchone()['event_id']
    mongo.candidate_events.update_one({'_id': candidate_id}, {'$set': {'promoted_event_id': event_id}})
    mongo.raw_evidence.update_one({'_id': c['evidence_id']}, {'$set': {'status': 'PROMOTED'}})
    return event_id


def _result(mongo, evidence_id):
    candidate = latest_candidate(mongo, evidence_id)
    return {'evidence': mongo.raw_evidence.find_one({'_id': evidence_id}), 'candidate': candidate,
            'event_id': (candidate or {}).get('promoted_event_id')}


def _reject(mongo, candidate_id, evidence_id, errors):
    mongo.candidate_events.update_one({'_id': candidate_id}, {'$set': {
        'validation': {'result': 'REJECTED', 'errors': errors, 'checked_at': _now()}}})
    mongo.raw_evidence.update_one({'_id': evidence_id}, {'$set': {'status': 'REJECTED'}})


def process_evidence(conn, mongo, extractor, evidence_id: ObjectId, as_of: date) -> dict:
    # Claim atomically so a double-click or a second request cannot promote twice.
    evidence = mongo.raw_evidence.find_one_and_update({'_id': evidence_id, 'status': 'RECEIVED'},
                                                      {'$set': {'status': 'EXTRACTING'}})
    if evidence is None:
        if not mongo.raw_evidence.find_one({'_id': evidence_id}):
            raise ValueError('evidence not found')
        return _result(mongo, evidence_id)
    try:
        candidate = extractor.extract(evidence, image_bytes=image_bytes(mongo, evidence))
    except Exception as error:
        mongo.raw_evidence.update_one({'_id': evidence_id}, {'$set': {'status': 'RECEIVED', 'last_error': safe_error(error)}})
        raise
    source_type = SOURCE_TYPE[evidence['kind']]
    doc = {'evidence_id': evidence_id, 'user_id': evidence['user_id'], 'source_type': source_type,
           'promoted_event_id': None, **(candidate or {'action': None, 'target_hint': None, 'target_event_id': None,
                                                       'extracted': {}, 'confidence': 0.0})}
    doc['_id'] = mongo.candidate_events.insert_one(doc).inserted_id
    mongo.raw_evidence.update_one({'_id': evidence_id}, {'$set': {'status': 'EXTRACTED'}})
    if candidate is None:
        _reject(mongo, doc['_id'], evidence_id, ['no financial event found'])
        return _result(mongo, evidence_id)
    try:
        result, errors, resolved = validate(conn, mongo, doc, evidence['user_id'], as_of, source_type)
    except Exception as error:  # malformed untrusted values must end REJECTED, never stuck half-processed
        result, errors, resolved = 'REJECTED', [f'validation failed: {safe_error(error)}'], {}
    finally:
        conn.rollback()  # validation only reads; end its transaction
    mongo.candidate_events.update_one({'_id': doc['_id']}, {'$set': {
        'resolved': resolved, 'validation': {'result': result, 'errors': errors, 'checked_at': _now()}}})
    if result == 'REJECTED':
        _reject(mongo, doc['_id'], evidence_id, errors)
    elif result == 'PASSED':
        try:
            promote(conn, mongo, doc['_id'])
        except Exception as error:  # a database constraint is the last line of the gate
            _reject(mongo, doc['_id'], evidence_id, [f'database rejected the record: {safe_error(error)}'])
    return _result(mongo, evidence_id)


def confirm_held(conn, mongo, candidate_id: ObjectId) -> int:
    c = mongo.candidate_events.find_one({'_id': candidate_id})
    if not c or (c.get('validation') or {}).get('result') != 'HELD' or c.get('promoted_event_id'):
        raise ValueError('only a HELD candidate can be confirmed')
    mongo.candidate_events.update_one({'_id': candidate_id}, {'$set': {
        'validation': {'result': 'PASSED', 'errors': [], 'checked_at': _now(), 'confirmed_by_user': True}}})
    return promote(conn, mongo, candidate_id)
