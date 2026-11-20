"""Read only explicit participant-facing inputs; labels never enter case context."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .models import Event, Offer, Profile, Request, money


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f'Invalid CSV header: {path.name}')
        result = list(reader)
        if any(None in row for row in result):
            raise ValueError(f'Extra CSV fields: {path.name}')
        return result


def unique(items, key):
    indexed = {getattr(item, key): item for item in items}
    if len(indexed) != len(items):
        raise ValueError(f'Duplicate {key}')
    return indexed


@dataclass
class Case:
    request: Request
    profile: Profile
    events: list[Event]
    offers: list[Offer]
    messages: list[dict]
    images: list[dict]
    rates: dict
    dataset: Path

    def bundle(self) -> dict:
        return {'request': self.request.model_dump(mode='json'),
                'profile': self.profile.model_dump(mode='json'),
                'events': [x.model_dump(mode='json') for x in self.events],
                'offers': [x.model_dump(mode='json') for x in self.offers],
                'messages': self.messages, 'images': self.images}

    def image_path(self, image_id: str) -> Path:
        if image_id not in {x['image_id'] for x in self.images}:
            raise ValueError('Image is outside this case')
        path = (self.dataset / 'media' / 'images' / (image_id + '.png')).resolve()
        parent = (self.dataset / 'media' / 'images').resolve()
        if path.parent != parent or not path.is_file():
            raise ValueError('Mapped image unavailable')
        return path

    def fingerprint(self) -> str:
        digest = hashlib.sha256(json.dumps(self.bundle(), sort_keys=True).encode())
        digest.update(str(sorted(self.rates.items())).encode())
        for image in self.images:
            digest.update(self.image_path(image['image_id']).read_bytes())
        return digest.hexdigest()


class Dataset:
    def __init__(self, path: Path, samples: bool = False):
        self.path = path.resolve()
        raw_requests = rows(self.path / ('sample_requests.csv' if samples else 'requests.csv'))
        self.labels = raw_requests if samples else []
        self.requests = [Request(**{k: x[k] for k in Request.model_fields}) for x in raw_requests]
        unique(self.requests, 'request_id')
        self.profiles = unique([Profile(**x) for x in rows(self.path / 'financial_profiles.csv')], 'user_id')
        self.events = unique([Event(**x) for x in rows(self.path / 'financial_events.csv')], 'event_id')
        self.offers = unique([Offer(**x) for x in rows(self.path / 'request_payment_options.csv')], 'payment_option_id')
        self.messages = rows(self.path / 'messages.csv')
        self.images = rows(self.path / 'images.csv')
        self.rates = {}
        for x in rows(self.path / 'exchange_rates.csv'):
            key = (x['rate_date'], x['from_currency'], x['to_currency'])
            if key in self.rates or money(x['rate']) <= 0:
                raise ValueError('Invalid or duplicate exchange rate')
            self.rates[key] = money(x['rate'])
        for event in self.events.values():
            if event.user_id not in self.profiles:
                raise ValueError('Unknown event owner')
            if event.linked_event_id:
                linked = self.events.get(event.linked_event_id)
                if linked is None or linked.user_id != event.user_id:
                    raise ValueError('Invalid event lifecycle link')
        for offer in self.offers.values():
            if offer.payment_amount * offer.number_of_payments != offer.total_payable_amount:
                raise ValueError('Offer payment total does not reconcile')
        for group, identifier in [(self.messages, 'message_id'), (self.images, 'image_id')]:
            if len({x[identifier] for x in group}) != len(group):
                raise ValueError(f'Duplicate {identifier}')
            for item in group:
                if item['user_id'] not in self.profiles:
                    raise ValueError('Unknown evidence owner')
                target = item['related_event_id']
                if target and (target not in self.events or self.events[target].user_id != item['user_id']):
                    raise ValueError('Invalid evidence link')

    def case(self, request: Request) -> Case:
        def relevant(item):
            return item['user_id'] == request.user_id and item['request_id'] in ('', request.request_id)
        return Case(request, self.profiles[request.user_id],
                    sorted([x for x in self.events.values() if x.user_id == request.user_id], key=lambda x: x.event_id),
                    [x for x in self.offers.values() if x.request_id == request.request_id],
                    [x for x in self.messages if relevant(x) and x['sent_at'][:10] <= str(request.request_date)],
                    [x for x in self.images if relevant(x)], self.rates, self.path)
