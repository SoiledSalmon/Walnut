"""Independent chronological replay and contract checks before any export."""
from __future__ import annotations

from datetime import date, timedelta

from .data import Case
from .models import Candidate, Cashflow, Decision, ZERO, money, parts
from .models import fmt


def verify(case: Case, decision: Decision, audit: dict) -> None:
    request, profile = case.request, case.profile
    if decision.request_id != request.request_id:
        raise ValueError('Request mismatch')
    if not ZERO <= decision.amount_safe_to_pay <= request.requested_amount:
        raise ValueError('Safe amount outside bounds')
    if not decision.decision_explanation.strip():
        raise ValueError('Missing explanation')
    selected = audit['selected']
    if selected is None:
        if (decision.recommended_payment_method != 'not_recommended' or decision.payment_plan != 'none' or
                decision.affordability_status != 'not_affordable' or decision.spending_changes_needed != 'none'):
            raise ValueError('Unselected plan exported')
        if audit['safe_candidates']:
            raise ValueError('Safe candidate ignored')
        return
    candidate = Candidate.model_validate(selected)
    if candidate.method != decision.recommended_payment_method:
        raise ValueError('Method mismatch')
    methods = parts(profile.payment_methods_user_will_consider)
    if ('full_payment' if candidate.method == 'wait' else candidate.method) not in methods:
        raise ValueError('User does not accept method')
    payments = candidate.payments
    if not payments or any(p.amount <= ZERO for p in payments):
        raise ValueError('Empty or nonpositive payment schedule')
    if candidate.method in ('full_payment','wait'):
        if len(payments)!=1 or payments[0].amount!=request.requested_amount:
            raise ValueError('Full payment must be a single complete payment')
        if candidate.method=='full_payment' and payments[0].day!=request.request_date:
            raise ValueError('Full payment must start today')
        if candidate.method=='wait' and payments[0].day<=request.request_date:
            raise ValueError('Wait must start in the future')
        expected_status='affordable_with_plan' if candidate.changes else ('affordable_now' if candidate.method=='full_payment' else 'affordable_later')
        if decision.affordability_status!=expected_status:
            raise ValueError('Status differs from payment method')
    expected_plan='|'.join(f'{p.day}:{fmt(p.amount)}' for p in payments)
    expected_changes='|'.join(f'stop:{c.event_id}' if c.action=='stop' else f'reduce_to:{c.event_id}:{fmt(c.amount)}' for c in candidate.changes) or 'none'
    if decision.payment_plan!=expected_plan or decision.spending_changes_needed!=expected_changes:
        raise ValueError('Serialized plan differs from certificate')
    if payments != sorted(payments, key=lambda p: p.day):
        raise ValueError('Unordered payments')
    if payments[0].day < request.request_date or payments[-1].day > request.desired_completion_date:
        raise ValueError('Payment outside allowed dates')
    if candidate.method == 'installments':
        offer = next((o for o in case.offers if o.payment_option_id == candidate.option_id), None)
        if not offer or not profile.max_installment_months or len(payments) > profile.max_installment_months:
            raise ValueError('Invalid installment eligibility')
        expected = [(offer.first_payment_date+timedelta(days=i*offer.payment_frequency_days), offer.payment_amount)
                    for i in range(offer.number_of_payments)]
        if [(p.day,p.amount) for p in payments] != expected:
            raise ValueError('Installments differ from supplied offer')
    elif sum((p.amount for p in payments), ZERO) != request.requested_amount:
        raise ValueError('Payments do not complete request')
    if candidate.method == 'partial_payment':
        if (not request.allows_partial_payment or len(payments) != 2 or
                not ZERO < decision.amount_safe_to_pay < request.requested_amount or
                payments[0].day != request.request_date or payments[0].amount != decision.amount_safe_to_pay or
                str(payments[1].day) != decision.earliest_date_for_full_payment):
            raise ValueError('Partial payment contract violation')
    if decision.affordability_status == 'affordable_now' and (candidate.changes or candidate.method != 'full_payment' or
            decision.earliest_date_for_full_payment != str(request.request_date)):
        raise ValueError('Invalid affordable_now status')
    if (candidate.changes or candidate.method in ('partial_payment','installments')) and decision.affordability_status != 'affordable_with_plan':
        raise ValueError('Plan must use affordable_with_plan')
    by_id = {e.event_id:e for e in case.events}
    if len(candidate.changes)>3 or len({c.series_id for c in candidate.changes}) != len(candidate.changes):
        raise ValueError('Invalid change count or duplicate stream')
    changes = {c.series_id:c for c in candidate.changes}
    series_info = {s['series_id']:s for s in audit['series']}
    for change in changes.values():
        event = by_id[change.event_id]
        if event.category in parts(profile.expense_categories_to_protect):
            raise ValueError('Protected expense changed')
        if change.action == 'stop':
            if event.category not in parts(profile.expense_categories_user_is_willing_to_stop) or event.flexibility not in ('stoppable','reducible_or_stoppable'):
                raise ValueError('Stop not permitted')
        elif (event.category not in parts(profile.expense_categories_user_is_willing_to_reduce) or
              event.flexibility not in ('reducible','reducible_or_stoppable') or
              event.minimum_allowed_amount is None or change.amount < event.minimum_allowed_amount):
            raise ValueError('Reduction not permitted')
    timeline = []
    for raw in audit['cashflows']:
        flow = Cashflow.model_validate(raw)
        amount = flow.amount
        change = changes.get(flow.series_id)
        original = by_id[flow.event_id]
        if change and amount < 0 and original.status not in ('pending','scheduled'):
            if change.action == 'stop':
                continue
            value = change.amount
            if '(daily variable-spending reserve)' in flow.description:
                value /= money(str(series_info[flow.series_id]['interval_days']))
            if original.currency != profile.home_currency:
                value *= case.rates[(str(flow.day),original.currency,profile.home_currency)]
                value = value.quantize(money('.01'))
            amount = -min(-amount,value)
        timeline.append((flow.day, 0 if amount>0 else 1, amount))
    timeline.extend((p.day,2,-p.amount) for p in payments)
    balance = profile.current_available_balance
    if balance < profile.minimum_balance_to_keep:
        raise ValueError('Opening balance already below minimum')
    for day, _, amount in sorted(timeline):
        balance += amount
        if balance < profile.minimum_balance_to_keep:
            raise ValueError(f'Independent replay violates minimum on {day}')
    # Check winner order without importing the planner's ranking implementation.
    def key(raw):
        c = Candidate.model_validate(raw)
        return (bool(c.changes),sum((p.amount for p in c.payments),ZERO),c.payments[0].day,
                len(c.payments),c.option_id,'|'.join(f'{x.action}:{x.event_id}:{x.amount}' for x in c.changes))
    if key(selected) != min(key(c) for c in audit['safe_candidates']):
        raise ValueError('Selected plan is not highest ranked')
