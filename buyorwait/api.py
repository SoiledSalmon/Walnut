"""FastAPI service layer (three-tier: web page -> this API -> PostgreSQL + MongoDB)."""
from __future__ import annotations

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

import psycopg
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import services
from .config import ROOT, Settings, load_settings
from .db import mongo_client, mongo_db, pg_connect
from .evidence import add_evidence, list_evidence
from .extract import get_extractor, safe_error
from .gate import confirm_held, process_evidence


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=150, pattern=r'^[^@\s]+@[^@\s]+$')
    currency: str = Field(default='INR', min_length=3, max_length=3)
    min_balance_threshold: Decimal = Field(ge=0)
    target_monthly_savings: Decimal = Field(ge=0)


class EvidenceIn(BaseModel):
    kind: Literal['SMS', 'EMAIL']
    raw_text: str = Field(min_length=1, max_length=2000)


class EventIn(BaseModel):
    event_type: Literal['INCOME', 'EXPENSE', 'BILL', 'TRANSFER', 'INVESTMENT']
    category: str | None = None
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)
    event_date: date
    status: Literal['CLEARED', 'PENDING', 'SCHEDULED']
    recurrence_interval: Literal['DAILY', 'WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY'] | None = None
    description: str = Field(default='', max_length=200)

    @field_validator('amount')
    @classmethod
    def non_zero(cls, v):
        if v == 0 or not v.is_finite():
            raise ValueError('amount must be non-zero')
        return v


class RequestIn(BaseModel):
    requested_amount: Decimal = Field(gt=0)
    category_id: int
    target_deadline: date | None = None
    description: str = Field(default='', max_length=200)


class OptionIn(BaseModel):
    plan_type: Literal['FULL', 'EMI', 'BNPL']
    tenure_months: int = Field(ge=0, le=120)
    interest_rate_bps: int = Field(ge=0, le=10000)
    processing_fee: Decimal = Field(ge=0)


def clean(value):
    """Make DB/Mongo values JSON-safe; money stays exact as strings."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, Decimal):
        return f'{value:.2f}'
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise ValueError('invalid id') from None


def create_app(settings: Settings | None = None, conn_factory=None, mongo=None, seed_on_start: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    conn_factory = conn_factory or (lambda: pg_connect(settings.postgres_url))
    if seed_on_start is None:
        seed_on_start = not settings.mongo_url
    state = {'mongo': mongo}

    @asynccontextmanager
    async def lifespan(app):
        if state['mongo'] is None:
            state['mongo'] = mongo_db(mongo_client(settings.mongo_url))
        if seed_on_start:
            # In-memory MongoDB starts empty, so rebuild Postgres with it to keep both stores consistent.
            from .seed import seed
            with conn_factory() as conn:
                seed(conn, state['mongo'], settings.as_of(), reset=True)
        yield

    app = FastAPI(title='Buy or Wait?', lifespan=lifespan)

    def db():
        conn = conn_factory()
        try:
            yield conn
        finally:
            conn.close()

    def m():
        return state['mongo']

    @app.exception_handler(ValueError)
    async def value_error(_: Request, error: ValueError):
        text = safe_error(error)
        return JSONResponse({'detail': text}, status_code=404 if 'not found' in text else 422)

    @app.exception_handler(psycopg.errors.IntegrityError)
    @app.exception_handler(psycopg.errors.RaiseException)
    async def db_error(_: Request, error: psycopg.Error):
        return JSONResponse({'detail': safe_error(error).splitlines()[0]}, status_code=422)

    api = '/api'

    @app.get(api + '/meta')
    def get_meta(conn=Depends(db)):
        return clean(services.meta(conn, settings))

    @app.get(api + '/users')
    def get_users(conn=Depends(db)):
        return clean(services.list_users(conn))

    @app.post(api + '/users')
    def post_user(body: UserIn, conn=Depends(db)):
        return {'user_id': services.register_user(conn, body.name, body.email, body.currency.upper(),
                                                  body.min_balance_threshold, body.target_monthly_savings)}

    @app.get(api + '/users/{uid}/profile')
    def get_profile(uid: int, conn=Depends(db)):
        p = services.get_profile(conn, uid)
        if p is None:
            raise ValueError('user not found')
        return clean(p)

    @app.get(api + '/users/{uid}/evidence')
    def get_evidence(uid: int):
        return clean(list_evidence(m(), uid))

    @app.post(api + '/users/{uid}/evidence')
    def post_evidence(uid: int, body: EvidenceIn, conn=Depends(db)):
        if services.get_profile(conn, uid) is None:
            raise ValueError('user not found')
        eid = add_evidence(m(), uid, body.kind, body.raw_text, datetime.now(timezone.utc))
        return clean(m().raw_evidence.find_one({'_id': eid}))

    @app.post(api + '/evidence/{eid}/process')
    def post_process(eid: str, conn=Depends(db)):
        return clean(process_evidence(conn, m(), get_extractor(settings), _oid(eid), settings.as_of()))

    @app.post(api + '/candidates/{cid}/confirm')
    def post_confirm(cid: str, conn=Depends(db)):
        return {'event_id': confirm_held(conn, m(), _oid(cid))}

    @app.get(api + '/users/{uid}/events')
    def get_events(uid: int, conn=Depends(db)):
        return clean(services.list_events(conn, m(), uid))

    @app.post(api + '/users/{uid}/events')
    def post_event(uid: int, body: EventIn, conn=Depends(db)):
        if services.get_profile(conn, uid) is None:
            raise ValueError('user not found')
        fields = body.model_dump(mode='json') | {'amount': f'{body.amount:.2f}', 'merchant': body.description or None}
        fields.pop('description')
        return clean(services.add_manual_event(conn, m(), uid, fields, settings.as_of()))

    @app.get(api + '/users/{uid}/requests')
    def get_requests(uid: int, conn=Depends(db)):
        return clean(services.list_requests(conn, uid))

    @app.post(api + '/users/{uid}/requests')
    def post_request(uid: int, body: RequestIn, conn=Depends(db)):
        return {'request_id': services.create_request(conn, m(), uid, body.requested_amount, body.category_id,
                                                      body.target_deadline, body.description, settings.as_of())}

    @app.get(api + '/requests/{rid}/options')
    def get_options(rid: int, conn=Depends(db)):
        return clean(services.list_options(conn, rid))

    @app.post(api + '/requests/{rid}/options')
    def post_option(rid: int, body: OptionIn, conn=Depends(db)):
        return {'option_id': services.add_payment_option(conn, rid, body.plan_type, body.tenure_months,
                                                         body.interest_rate_bps, body.processing_fee)}

    @app.post(api + '/requests/{rid}/decide')
    def post_decide(rid: int, conn=Depends(db)):
        return clean(services.decide(conn, rid, settings.as_of()))

    @app.get(api + '/requests/{rid}/decisions')
    def get_versions(rid: int, conn=Depends(db)):
        return clean(services.list_versions(conn, rid))

    @app.get(api + '/decisions/{did}')
    def get_decision(did: int, conn=Depends(db)):
        return clean(services.get_decision(conn, did))

    @app.post(api + '/decisions/{did}/tamper')
    def post_tamper(did: int, conn=Depends(db)):
        services.tamper_decision(conn, did)
        return {'tampered_decision_id': did}

    @app.get(api + '/audit')
    def get_audit(conn=Depends(db)):
        return clean(services.audit_chain(conn))

    @app.post(api + '/audit/verify')
    def post_verify(conn=Depends(db)):
        return clean(services.verify_chain(conn))

    @app.get(api + '/users/{uid}/explain')
    def get_explain(uid: int, start: date | None = None, end: date | None = None, conn=Depends(db)):
        d = settings.as_of()
        start = start or d.replace(day=1)
        end = end or d.replace(day=calendar.monthrange(d.year, d.month)[1])
        return {'start': start.isoformat(), 'end': end.isoformat(), 'plan': services.explain_events(conn, uid, start, end)}

    app.mount('/', StaticFiles(directory=ROOT / 'frontend', html=True), name='frontend')
    return app


def __getattr__(name):
    # `uvicorn buyorwait.api:app` builds the app lazily so importing this module needs no .env.
    if name == 'app':
        return create_app()
    raise AttributeError(name)
