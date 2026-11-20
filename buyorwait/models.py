"""Typed contracts: monetary values never pass through binary floating point."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CENT = Decimal('0.01')
ZERO = Decimal('0')
OUTPUT_COLUMNS = ['request_id', 'amount_safe_to_pay', 'affordability_status',
                  'recommended_payment_method', 'payment_plan',
                  'earliest_date_for_full_payment', 'spending_changes_needed',
                  'decision_explanation']


def money(value: str | Decimal) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite monetary value')
    return result


def rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def floor_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def fmt(value: Decimal) -> str:
    return format(value.quantize(CENT), 'f')


def parts(value: str) -> set[str]:
    return set(filter(None, value.split('|')))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Request(StrictModel):
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal = Field(gt=0)
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


class Profile(StrictModel):
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal = Field(ge=0)
    financial_priorities: str
    expense_categories_to_protect: str
    expense_categories_user_is_willing_to_reduce: str
    expense_categories_user_is_willing_to_stop: str
    payment_methods_user_will_consider: str
    max_installment_months: int | None = Field(default=None,gt=0)

    @field_validator('max_installment_months', mode='before')
    @classmethod
    def blank_int(cls, value):
        return None if value == '' else value


class Event(StrictModel):
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: Literal['credit', 'debit', 'non_cash']
    amount: Decimal | None = Field(ge=0)
    currency: str
    event_date: date
    settlement_date: date | None
    status: Literal['settled', 'pending', 'scheduled', 'failed', 'cancelled', 'unrealized']
    linked_event_id: str
    flexibility: Literal['fixed', 'stoppable', 'reducible', 'reducible_or_stoppable']
    minimum_allowed_amount: Decimal | None = Field(ge=0)

    @field_validator('amount', 'minimum_allowed_amount', 'settlement_date', mode='before')
    @classmethod
    def blank(cls, value):
        return None if value == '' else value


class Offer(StrictModel):
    payment_option_id: str
    request_id: str
    payment_method: Literal['full_payment', 'installments']
    payment_amount: Decimal = Field(gt=0)
    number_of_payments: int = Field(gt=0)
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Decimal = Field(ge=0)
    total_payable_amount: Decimal = Field(gt=0)

    @field_validator('payment_frequency_days', mode='before')
    @classmethod
    def blank(cls, value):
        return None if value == '' else value


class Fact(StrictModel):
    target_event_id: str
    operation: Literal['extract_amount', 'set_recurring_amount', 'confirm_salary', 'shift_next',
                       'stop_recurring', 'exclude_event', 'outstanding', 'nonrecurring']
    source_ids: list[str] = Field(min_length=1)
    amount: Decimal | None = Field(default=None, ge=0)
    effective_date: date | None = None
    end_date: date | None = None
    field_label: str = ''
    justification: str = Field(min_length=1, max_length=600)


class Interpretation(StrictModel):
    facts: list[Fact] = Field(default_factory=list, max_length=30)


class Cashflow(StrictModel):
    day: date
    amount: Decimal
    event_id: str
    series_id: str = ''
    description: str


class Series(StrictModel):
    series_id: str
    events: list[Event]
    representative: Event
    amount: Decimal
    monthly_day: int | None = None
    interval_days: int | None = None
    month_days: list[int] = Field(default_factory=list)
    anchor: date


class Payment(StrictModel):
    day: date
    amount: Decimal = Field(gt=0)


class Change(StrictModel):
    event_id: str
    series_id: str
    action: Literal['stop', 'reduce_to']
    amount: Decimal = Field(ge=0)


class Candidate(StrictModel):
    method: str
    payments: list[Payment]
    changes: list[Change] = Field(default_factory=list)
    option_id: str = ''
    minimum_balance: Decimal = ZERO
    minimum_date: date | None = None


class Decision(StrictModel):
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: Literal['affordable_now', 'affordable_with_plan', 'affordable_later', 'not_affordable']
    recommended_payment_method: Literal['full_payment', 'partial_payment', 'installments', 'wait', 'not_recommended']
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str

    def csv_row(self) -> dict[str, str]:
        row = self.model_dump(mode='json')
        row['amount_safe_to_pay'] = fmt(self.amount_safe_to_pay)
        return row
