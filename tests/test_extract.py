import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from buyorwait.extract import GeminiExtractor, RuleExtractor, parse_amount

RX = RuleExtractor()
WHEN = datetime(2026, 11, 20, 10, 0)


def ev(kind, text, ocr=None):
    return {'kind': kind, 'raw_text': text, 'received_at': WHEN, 'ocr': ocr}


@pytest.mark.parametrize('text,amount', [
    ('INR 5,00,000 credited', Decimal('500000')),
    ('Rs.1,500.00 debited', Decimal('1500.00')),
    ('₹2,400 paid', Decimal('2400')),
    ('USD 100.00 received', Decimal('100.00')),
    ('USD 1,234.50 received', Decimal('1234.50')),
])
def test_parse_amount(text, amount):
    assert parse_amount(text) == amount


def test_sms_pending_debit():
    c = RX.extract(ev('SMS', 'INR 1,500 debited at FreshMart on 19-11-2026 (pending). Avl bal INR 12,000.'))
    assert c['action'] == 'CREATE'
    assert {k: c['extracted'][k] for k in ('event_type', 'amount', 'currency', 'event_date', 'status', 'merchant', 'category')} == \
        {'event_type': 'EXPENSE', 'amount': '-1500.00', 'currency': 'INR', 'event_date': '2026-11-19',
         'status': 'PENDING', 'merchant': 'FreshMart', 'category': 'Groceries'}
    assert c['confidence'] >= 0.8


def test_sms_pending_credit_is_income():
    c = RX.extract(ev('SMS', 'INR 5,000 credited from Refund Desk on 19-11-2026 (pending).'))
    assert c['extracted']['event_type'] == 'INCOME' and c['extracted']['amount'] == '5000.00'
    assert c['extracted']['status'] == 'PENDING'


def test_email_bill_is_scheduled():
    c = RX.extract(ev('EMAIL', 'Your PowerCo bill of INR 999.00 is due on 25-11-2026.'))
    assert c['extracted']['event_type'] == 'BILL' and c['extracted']['status'] == 'SCHEDULED'
    assert c['extracted']['amount'] == '-999.00' and c['extracted']['category'] == 'Utilities'
    assert c['extracted']['merchant'] == 'PowerCo'


def test_sms_cancel_has_target_hint():
    c = RX.extract(ev('SMS', 'Your PowerCo bill of INR 999.00 due on 25-11-2026 has been cancelled.'))
    assert c['action'] == 'CANCEL'
    assert c['target_hint'] == {'merchant': 'PowerCo', 'event_date': '2026-11-25', 'amount': '-999.00'}


def test_email_amend_old_and_new():
    c = RX.extract(ev('EMAIL', 'Your AirNet bill due on 28-11-2026 has been revised from INR 3,000 to INR 3,500.'))
    assert c['action'] == 'AMEND'
    assert c['target_hint']['amount'] == '-3000.00' and c['target_hint']['merchant'] == 'AirNet'
    assert c['extracted']['amount'] == '-3500.00' and c['extracted']['event_date'] == '2026-11-28'


def test_usd_email_income():
    c = RX.extract(ev('EMAIL', 'Payment received: USD 100.00 from Upwork on 12-11-2026.'))
    assert c['extracted']['currency'] == 'USD' and c['extracted']['amount'] == '100.00'
    assert c['extracted']['event_type'] == 'INCOME' and c['extracted']['category'] is None


def test_vague_message_low_confidence():
    c = RX.extract(ev('SMS', 'Paid 450 to Ravi'))
    assert c['confidence'] < 0.8
    assert c['extracted']['amount'] == '-450.00' and c['extracted']['event_date'] == '2026-11-20'


def test_no_event_returns_none():
    assert RX.extract(ev('SMS', 'Your OTP is 123456. Do not share it.')) is None


def test_receipt_uses_ocr_text():
    c = RX.extract(ev('RECEIPT_IMAGE', '', ocr={'text': 'StyleHub\nTOTAL INR 6,000.00\nPaid at StyleHub on 14-11-2026', 'confidence': 0.91}))
    assert c['extracted']['amount'] == '-6000.00' and c['extracted']['category'] == 'Shopping'
    assert c['extracted']['event_date'] == '2026-11-14'


def test_ledger_line():
    c = RX.extract(ev('LEDGER_FILE', '2026-10-31|CR|60000.00|INR|ACME Corp|CLEARED'))
    assert c['extracted'].items() >= {'amount': '60000.00', 'event_type': 'INCOME', 'status': 'CLEARED',
                                      'category': 'Salary', 'event_date': '2026-10-31'}.items()
    assert c['confidence'] >= 0.99


def test_form_passthrough():
    fields = {'event_type': 'INCOME', 'amount': '60000.00', 'currency': 'INR', 'event_date': '2026-11-30',
              'status': 'SCHEDULED', 'merchant': 'ACME Corp', 'category': 'Salary', 'recurrence_interval': 'MONTHLY'}
    c = RX.extract(ev('FORM', json.dumps(fields)))
    assert c['action'] == 'CREATE' and c['extracted'] == fields and c['confidence'] == 1.0


class FakeModels:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        return type('R', (), {'text': json.dumps(self.reply)})()


class FakeClient:
    def __init__(self, reply):
        self.models = FakeModels(reply)


GEMINI_REPLY = {'action': 'CREATE', 'target_hint': None,
                'extracted': {'event_type': 'EXPENSE', 'amount': '-1500.00', 'currency': 'INR', 'event_date': '2026-11-19',
                              'status': 'PENDING', 'merchant': 'FreshMart', 'category': 'Groceries'},
                'confidence': 0.93}


def test_gemini_extractor_with_fake_client():
    client = FakeClient(GEMINI_REPLY)
    c = GeminiExtractor('k', client=client).extract(ev('SMS', 'INR 1,500 debited at FreshMart'))
    assert c['extracted']['amount'] == '-1500.00' and c['confidence'] == 0.93
    assert c['target_event_id'] is None and c['extracted']['recurrence_interval'] is None
    _, _, config = client.models.calls[0]
    assert 'UNTRUSTED' in config.system_instruction


def test_gemini_attack_image_is_sent_as_data():
    image = (Path(__file__).parent / 'fixtures/media/images/image_attack.png').read_bytes()
    client = FakeClient(GEMINI_REPLY)
    GeminiExtractor('k', client=client).extract(ev('RECEIPT_IMAGE', ''), image_bytes=image)
    _, contents, _ = client.models.calls[0]
    blobs = [p.inline_data.data for p in contents if getattr(p, 'inline_data', None)]
    assert blobs == [image]
