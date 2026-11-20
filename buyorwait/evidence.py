"""MongoDB evidence store (DFD M1 raw_evidence, M2 candidate_events)."""
from __future__ import annotations

from datetime import datetime

import gridfs
from bson import ObjectId

KINDS = ('SMS', 'EMAIL', 'RECEIPT_IMAGE', 'LEDGER_FILE', 'FORM')
SOURCE_TYPE = {'SMS': 'SMS', 'EMAIL': 'EMAIL', 'RECEIPT_IMAGE': 'RECEIPT_OCR', 'LEDGER_FILE': 'LEDGER', 'FORM': 'MANUAL'}


def add_evidence(mongo, user_id: int, kind: str, raw_text: str, received_at: datetime,
                 ocr: dict | None = None, image: bytes | None = None, status: str = 'RECEIVED') -> ObjectId:
    if kind not in KINDS:
        raise ValueError(f'unknown evidence kind {kind}')
    file_ref = gridfs.GridFS(mongo).put(image, filename=f'{kind.lower()}.png') if image else None
    return mongo.raw_evidence.insert_one({'user_id': user_id, 'kind': kind, 'received_at': received_at,
                                          'raw_text': raw_text, 'file_ref': file_ref, 'ocr': ocr,
                                          'status': status}).inserted_id


def image_bytes(mongo, evidence: dict) -> bytes | None:
    return gridfs.GridFS(mongo).get(evidence['file_ref']).read() if evidence.get('file_ref') else None


def latest_candidate(mongo, evidence_id: ObjectId) -> dict | None:
    return mongo.candidate_events.find_one({'evidence_id': evidence_id}, sort=[('_id', -1)])


def list_evidence(mongo, user_id: int) -> list[dict]:
    docs = list(mongo.raw_evidence.find({'user_id': user_id}).sort([('received_at', 1), ('_id', 1)]))
    for doc in docs:
        doc['candidate'] = latest_candidate(mongo, doc['_id'])
    return docs


def merchant_of(mongo, event_id: int) -> str | None:
    c = mongo.candidate_events.find_one({'promoted_event_id': event_id})
    return (c or {}).get('extracted', {}).get('merchant')
