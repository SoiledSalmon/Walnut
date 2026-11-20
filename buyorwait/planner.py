"""Finite candidate enumeration and deterministic ordering."""
from __future__ import annotations

import itertools
from datetime import timedelta
from decimal import Decimal

from .data import Case
from .models import (Candidate, Cashflow, Change, Decision, Interpretation, Payment,
                     Series, ZERO, floor_money, fmt, parts)
from .forecast import build_forecast, convert


def balances(case: Case, flow: list[Cashflow], payments: list[Payment] = ()):
    today = case.request.request_date
    credits = [ZERO] * 91
    debits = [ZERO] * 91
    for event in flow:
        index = (event.day-today).days
        if event.amount > 0:
            credits[index] += event.amount
        else:
            debits[index] += event.amount
    for payment in payments:
        index = (payment.day-today).days
        if not 0 <= index <= 90:
            raise ValueError('Payment outside forecast')
        debits[index] -= payment.amount
    value = case.profile.current_available_balance
    result = []
    for index in range(91):
        value += credits[index] + debits[index]
        result.append(value)
    return result


def capacity(case: Case, flow: list[Cashflow]):
    path = balances(case, flow)
    minimum = case.profile.minimum_balance_to_keep
    opening = case.profile.current_available_balance
    safe = floor_money(min(case.request.requested_amount, max(ZERO, min([opening]+path)-minimum)))
    earliest = None
    if min([opening]+path) >= minimum:
        suffix = path[-1]
        for index in range(90, -1, -1):
            suffix = min(suffix, path[index])
            if suffix-minimum >= case.request.requested_amount:
                earliest = case.request.request_date+timedelta(days=index)
    return safe, earliest, path


def change_options(case: Case, streams: list[Series]) -> list[list[Change]]:
    profile = case.profile
    protected = parts(profile.expense_categories_to_protect)
    stop = parts(profile.expense_categories_user_is_willing_to_stop)
    reduce = parts(profile.expense_categories_user_is_willing_to_reduce)
    groups = []
    for stream in streams:
        event = stream.representative
        if event.direction != 'debit' or event.category in protected:
            continue
        choices = []
        if event.category in stop and event.flexibility in ('stoppable', 'reducible_or_stoppable'):
            choices.append(Change(event_id=event.event_id, series_id=stream.series_id, action='stop', amount=ZERO))
        if (event.category in reduce and event.flexibility in ('reducible', 'reducible_or_stoppable') and
                event.minimum_allowed_amount is not None and event.minimum_allowed_amount < stream.amount):
            choices.append(Change(event_id=event.event_id, series_id=stream.series_id,
                                  action='reduce_to', amount=event.minimum_allowed_amount))
        if choices:
            groups.append(choices)
    return groups


def changed_flow(case: Case, flow: list[Cashflow], changes: list[Change], streams: list[Series]):
    by_series = {c.series_id: c for c in changes}
    representatives = {s.series_id: s.representative for s in streams}
    stream_map = {s.series_id:s for s in streams}
    explicit_ids = {e.event_id for e in case.events if e.status in ('pending', 'scheduled')}
    result = []
    for event in flow:
        change = by_series.get(event.series_id)
        # Existing pending debts cannot be cancelled by reducing future spending.
        if change and event.amount < 0 and event.event_id not in explicit_ids:
            if change.action == 'stop':
                continue
            adjusted=change.amount
            if '(daily variable-spending reserve)' in event.description:
                adjusted=adjusted/Decimal(stream_map[event.series_id].interval_days)
            value = convert(case, adjusted, representatives[event.series_id].currency, event.day)
            result.append(event.model_copy(update={'amount': -min(value, -event.amount)}))
        else:
            result.append(event)
    return result


def schedules(case: Case, safe: Decimal, earliest, flow: list[Cashflow], changes: list[Change]):
    request, profile = case.request, case.profile
    methods = parts(profile.payment_methods_user_will_consider)
    today, deadline, amount = request.request_date, request.desired_completion_date, request.requested_amount
    if 'full_payment' in methods:
        yield Candidate(method='full_payment', payments=[Payment(day=today, amount=amount)], changes=changes)
        _, changed_earliest, _ = capacity(case, flow)
        if changed_earliest and today < changed_earliest <= deadline:
            yield Candidate(method='wait', payments=[Payment(day=changed_earliest, amount=amount)], changes=changes)
    if ('partial_payment' in methods and request.allows_partial_payment and ZERO < safe < amount and
            earliest and today < earliest <= deadline):
        yield Candidate(method='partial_payment', payments=[Payment(day=today, amount=safe),
                        Payment(day=earliest, amount=amount-safe)], changes=changes)
    if 'installments' in methods and profile.max_installment_months:
        for offer in sorted(case.offers, key=lambda o: o.payment_option_id):
            if offer.payment_method != 'installments' or offer.number_of_payments > profile.max_installment_months:
                continue
            if not offer.payment_frequency_days or offer.first_payment_date < today:
                continue
            payments = [Payment(day=offer.first_payment_date+timedelta(days=i*offer.payment_frequency_days),
                                amount=offer.payment_amount) for i in range(offer.number_of_payments)]
            if payments[-1].day <= min(deadline, today+timedelta(days=90)):
                yield Candidate(method='installments', payments=payments, changes=changes, option_id=offer.payment_option_id)


def rank(candidate: Candidate):
    signature = '|'.join(f'{x.action}:{x.event_id}:{x.amount}' for x in candidate.changes)
    return (bool(candidate.changes), sum((p.amount for p in candidate.payments), ZERO),
            candidate.payments[0].day, len(candidate.payments), candidate.option_id, signature)


def select_plan(case: Case, interpretation: Interpretation):
    flow, streams, notes = build_forecast(case, interpretation)
    safe, earliest, path = capacity(case, flow)
    rejected = []

    def evaluate(changes):
        modified = changed_flow(case, flow, changes, streams)
        valid = []
        for candidate in schedules(case, safe, earliest, modified, changes):
            if candidate.payments[-1].day > case.request.desired_completion_date:
                rejected.append({'method': candidate.method, 'option_id': candidate.option_id, 'reason': 'deadline'})
                continue
            values = balances(case, modified, candidate.payments)
            low = min([case.profile.current_available_balance]+values)
            if low < case.profile.minimum_balance_to_keep:
                rejected.append({'method': candidate.method, 'option_id': candidate.option_id,
                                 'reason': 'minimum_balance', 'minimum': str(low)})
                continue
            candidate.minimum_balance = low
            candidate.minimum_date = case.request.request_date+timedelta(days=values.index(min(values)))
            valid.append(candidate)
        return valid

    candidates = evaluate([])
    if not candidates:
        options = change_options(case, streams)
        for count in range(1, min(3, len(options))+1):
            for groups in itertools.combinations(options, count):
                for combination in itertools.product(*groups):
                    candidates.extend(evaluate(list(combination)))
    candidates.sort(key=rank)
    chosen = candidates[0] if candidates else None
    decision = make_decision(case, chosen, safe, earliest, notes)
    audit = {'baseline': [{'date': str(case.request.request_date+timedelta(days=i)), 'balance': str(v)} for i,v in enumerate(path)],
             'cashflows': [x.model_dump(mode='json') for x in flow],
             'series': [x.model_dump(mode='json') for x in streams], 'evidence': notes,
             'selected': chosen.model_dump(mode='json') if chosen else None,
             'safe_candidates': [x.model_dump(mode='json') for x in candidates],
             'rejected': rejected, 'interpretation': interpretation.model_dump(mode='json')}
    return decision, audit


def make_decision(case: Case, chosen: Candidate | None, safe: Decimal, earliest, notes):
    request, profile = case.request, case.profile
    currency = profile.home_currency
    changes, plan, method = 'none', 'none', 'not_recommended'
    status = 'not_affordable'
    if chosen:
        method = chosen.method
        plan = '|'.join(f'{p.day}:{fmt(p.amount)}' for p in chosen.payments)
        changes = '|'.join(f'stop:{c.event_id}' if c.action == 'stop' else f'reduce_to:{c.event_id}:{fmt(c.amount)}' for c in chosen.changes) or 'none'
        status = ('affordable_with_plan' if chosen.changes or method in ('partial_payment', 'installments')
                  else 'affordable_now' if method == 'full_payment' else 'affordable_later')
        explanation = (f'Use {method.replace("_", " ")} totaling {currency} {fmt(sum((p.amount for p in chosen.payments), ZERO))}; '
                       f'complete by {chosen.payments[-1].day}. The 90-day projected low is {currency} {fmt(chosen.minimum_balance)} '
                       f'on {chosen.minimum_date}, protecting the {fmt(profile.minimum_balance_to_keep)} minimum.')
        if chosen.changes:
            explanation += f' Required changes: {changes}.'
    else:
        explanation = (f'Do not proceed by {request.desired_completion_date}: no eligible complete plan preserves the '
                       f'{currency} {fmt(profile.minimum_balance_to_keep)} minimum over 90 days. '
                       f'Today\'s safe capacity is {currency} {fmt(safe)}.')
        if earliest:
            explanation += f' Baseline full-payment capacity first occurs on {earliest}; deadline or payment preferences prevent an eligible plan.'
    if notes:
        priority={'stop_recurring':0,'confirm_salary':1,'set_recurring_amount':1,'shift_next':2,'outstanding':3,'extract_amount':4}
        for note in sorted(notes,key=lambda n:priority.get(n['operation'],5))[:2]:
            evidence=f' Evidence {",".join(note["sources"])}: {note["operation"].replace("_"," ")} for {note["target"]}'
            if note.get('amount') is not None: evidence+=f' ({note["currency"]} {note["amount"]})'
            if note.get('effective_date'): evidence+=f' effective {note["effective_date"]}'
            explanation+=evidence+'.'
    return Decision(request_id=request.request_id, amount_safe_to_pay=safe, affordability_status=status,
                    recommended_payment_method=method, payment_plan=plan,
                    earliest_date_for_full_payment=str(earliest) if earliest else '',
                    spending_changes_needed=changes, decision_explanation=explanation)
