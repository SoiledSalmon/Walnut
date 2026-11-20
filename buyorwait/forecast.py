"""Evidence reconciliation and conservative recurrence; no model arithmetic."""
from __future__ import annotations

import calendar
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .data import Case
from .models import Cashflow, Event, Fact, Interpretation, Series, ZERO, rounded

ONE_OFF = re.compile(r'bonus|commission|arrears|prorat|reimburse|windfall|prize|lottery|final employer|final settlement', re.I)
VARIABLE_INCOME = re.compile(r'freelance|contract payment|invoice payment|platform|app earnings|marketplace|independent work|milestone|project payment|retainer', re.I)
VARIABLE = {'groceries', 'transport', 'dining'}


def month_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_month(day: date, anchor: int) -> date:
    month = day.month % 12 + 1
    return month_date(day.year + (day.month == 12), month, anchor)


def series_key(event: Event) -> str:
    # Variable merchants represent one spending stream, unlike separate salaries.
    detail = '' if event.category in VARIABLE else re.sub(r'\s+', ' ', event.description.lower()).strip()
    if event.category == 'salary' and not ONE_OFF.search(event.description):
        if VARIABLE_INCOME.search(event.description):
            detail = 'variable earned income'
        elif not re.search(r'household|previous employer|new employer', event.description, re.I):
            detail = 'regular salary'
    return '|'.join((event.direction, event.category, event.currency, detail))


def convert(case: Case, value: Decimal, currency: str, day: date) -> Decimal:
    if currency == case.profile.home_currency:
        return value
    key = (str(day), currency, case.profile.home_currency)
    if key not in case.rates:
        raise ValueError(f'Missing dated exchange rate: {key}')
    return rounded(value * case.rates[key])


def supported_amount(amount: Decimal, target: Event, sources: list[dict]) -> bool:
    """Reject uncited numeric inventions; this is not proof of semantic truth."""
    text=' '.join(x.get('message_text','') for x in sources)
    values=set()
    for token in re.findall(r'\d[\d.,]*',text):
        for candidate in (token.replace(',',''),token.replace('.','').replace(',','.')):
            try: values.add(Decimal(candidate.rstrip('.')))
            except Exception: pass
    if amount in values: return True
    if target.amount is not None:
        for token in re.findall(r'(\d+(?:\.\d+)?)\s*%',text):
            percentage=Decimal(token)/100
            if amount in (rounded(target.amount*(1+percentage)),rounded(target.amount*(1-percentage))):
                return True
    # Images need visual interpretation; textual numeric matching cannot verify pixels.
    return any('image_id' in source for source in sources)


def validate_facts(case: Case, interpretation: Interpretation) -> list[Fact]:
    events = {e.event_id: e for e in case.events}
    sources = {x['message_id']: x for x in case.messages}
    sources.update({x['image_id']: x for x in case.images})
    allowed = set(events) | set(sources)
    seen = set()
    for fact in interpretation.facts:
        if fact.target_event_id not in events or not set(fact.source_ids) <= allowed:
            raise ValueError('Fact targets or cites a record outside this case')
        key = (fact.target_event_id, fact.operation, fact.effective_date)
        if key in seen:
            raise ValueError('Duplicate/conflicting fact operations')
        seen.add(key)
        if fact.operation in ('extract_amount', 'set_recurring_amount', 'confirm_salary') and fact.amount is None:
            raise ValueError('Amount operation requires amount')
        if fact.operation == 'extract_amount':
            mapped = {x['image_id'] for x in case.images if x['related_event_id'] == fact.target_event_id}
            if not mapped.intersection(fact.source_ids) or not fact.field_label:
                raise ValueError('Amount extraction requires mapped image and field label')
            if events[fact.target_event_id].amount is not None:
                raise ValueError('Image extraction cannot overwrite a supplied amount')
        if fact.operation in ('set_recurring_amount', 'confirm_salary', 'shift_next', 'stop_recurring', 'outstanding'):
            if not set(fact.source_ids).intersection(sources):
                raise ValueError('Amendments need a message or image source')
        if fact.operation in ('set_recurring_amount','confirm_salary'):
            cited=[sources[s] for s in fact.source_ids if s in sources]
            unchanged_confirmation=(fact.operation=='confirm_salary' and fact.amount==events[fact.target_event_id].amount)
            if not unchanged_confirmation and not supported_amount(fact.amount,events[fact.target_event_id],cited):
                raise ValueError('Amended amount is not present in cited evidence or supported by an explicit percentage')
        if fact.operation=='confirm_salary' and (events[fact.target_event_id].category!='salary' or fact.effective_date is None):
            raise ValueError('Salary confirmation requires a salary target and confirmed future date')
        if fact.operation == 'outstanding' and (events[fact.target_event_id].direction!='debit' or events[fact.target_event_id].status not in ('failed','pending','scheduled')):
            raise ValueError('Outstanding applies only to an unpaid debit, never settled cash or salary')
        if fact.operation == 'nonrecurring' and fact.source_ids==[fact.target_event_id]:
            target=events[fact.target_event_id]
            if target.category=='salary' and not ONE_OFF.search(target.description) and not target.linked_event_id:
                raise ValueError('Regular salary cannot be removed without supporting evidence')
        if fact.operation == 'shift_next' and fact.effective_date is None:
            raise ValueError('Date amendment requires effective_date')
        if fact.end_date and (fact.effective_date is None or fact.end_date < fact.effective_date):
            raise ValueError('Invalid amendment date interval')
    # Preserve chronological evidence precedence rather than model list order.
    def stamp(fact):
        return max((sources[s].get('sent_at', '') for s in fact.source_ids if s in sources), default='')
    return sorted(interpretation.facts, key=lambda f: (stamp(f), f.target_event_id, f.operation))


def infer_series(events: list[Event], today: date, excluded: set[str]) -> list[Series]:
    groups = defaultdict(list)
    for event in events:
        if (event.event_id in excluded or event.status != 'settled' or
                event.settlement_date is None or event.settlement_date > today or
                event.direction == 'non_cash' or event.amount is None):
            continue
        if event.direction == 'credit' and (event.category != 'salary' or ONE_OFF.search(event.description)):
            continue
        if event.linked_event_id or event.event_type in ('refund', 'investment_sale', 'investment_purchase'):
            continue
        groups[series_key(event)].append(event)
    result = []
    for key, history in sorted(groups.items()):
        history.sort(key=lambda e: (e.settlement_date, e.event_id))
        dates = sorted({e.settlement_date for e in history})
        if len(dates) < 3:
            continue
        gaps = [(b-a).days for a, b in zip(dates, dates[1:])]
        days = Counter(d.day for d in dates)
        anchor_day = min(days, key=lambda d: (-days[d], d))
        months = {(d.year, d.month) for d in dates}
        monthly = (len(months) == len(dates) and len(months) >= 3 and
                   sum(abs(d.day-anchor_day) <= 3 for d in dates) / len(dates) >= .8)
        semi = sorted(d for d,count in days.items() if count >= 3)
        semimonthly = (not monthly and len(semi)==2 and sum(days[d] for d in semi)==len(dates))
        interval = int(statistics.median(gaps))
        if not monthly and not semimonthly and (interval <= 0 or sum(abs(g-interval) <= max(2, interval*.2) for g in gaps)/len(gaps) < .8):
            continue
        recent = [e.amount for e in history if e.settlement_date >= today-timedelta(days=90)]
        if not recent:
            continue  # stale history is not evidence of an ongoing stream
        if history[-1].direction == 'credit':
            next_due = next_month(dates[-1],dates[-1].day) if monthly else dates[-1]+timedelta(days=interval)
            if semimonthly:
                next_due=min(dates[-1]+timedelta(days=i) for i in range(1,33) if (dates[-1]+timedelta(days=i)).day in semi)
            if next_due < today:
                continue  # a missed historical pay cycle is not presumed to resume
        if history[-1].direction == 'credit':
            estimate = min(history[-1].amount,sorted(recent)[max(0,math.ceil(.25*len(recent))-1)]) if VARIABLE_INCOME.search(history[-1].description) else history[-1].amount
            if monthly:
                anchor_day = dates[-1].day  # do not assume a recent delayed payday reverts earlier
        else:
            values = sorted(recent)
            percentile = values[math.ceil(.75*len(values))-1]
            # An isolated bulk invoice is not a new amount for every grocery trip.
            regular = [v for v in recent if v <= percentile*3]
            latest_regular = regular[-1] if regular else percentile
            estimate = max(latest_regular, percentile)
        result.append(Series(series_id=key, events=history, representative=history[-1],
                             amount=estimate, monthly_day=anchor_day if monthly else None,
                             interval_days=None if monthly else interval, month_days=semi if semimonthly else [], anchor=dates[-1]))
    return result


def occurrence_dates(series: Series, today: date, end: date):
    if series.month_days:
        for offset in range((end-today).days+1):
            day=today+timedelta(days=offset)
            if day.day in series.month_days: yield day
        return
    if series.monthly_day:
        current = month_date(today.year, today.month, series.monthly_day)
        if current < today:
            current = next_month(current, series.monthly_day)
    else:
        current = series.anchor + timedelta(days=series.interval_days)
        while current < today:
            current += timedelta(days=series.interval_days)
    while current <= end:
        yield current
        current = next_month(current, series.monthly_day) if series.monthly_day else current+timedelta(days=series.interval_days)


def build_forecast(case: Case, interpretation: Interpretation):
    today = case.request.request_date
    end = today + timedelta(days=90)
    facts = validate_facts(case, interpretation)
    events = [e.model_copy(deep=True) for e in case.events]
    by_id = {e.event_id: e for e in events}
    excluded = {f.target_event_id for f in facts if f.operation == 'exclude_event'}
    nonrecurring = {f.target_event_id for f in facts if f.operation == 'nonrecurring'}
    for fact in facts:
        if fact.operation == 'extract_amount':
            by_id[fact.target_event_id].amount = fact.amount
    missing = [e.event_id for e in events if e.amount is None and e.event_id not in excluded]
    if missing:
        raise ValueError('Unresolved image amounts: ' + ', '.join(missing))
    series = infer_series(events, today, excluded | nonrecurring)
    for fact in facts:
        if fact.operation not in ('set_recurring_amount','confirm_salary'): continue
        target=by_id[fact.target_event_id]
        if target.category!='salary' or any(s.series_id==series_key(target) for s in series): continue
        history=sorted([e for e in events if series_key(e)==series_key(target) and e.status=='settled' and e.settlement_date and e.settlement_date<=today],key=lambda e:e.settlement_date)
        if len(history)>=3 or (fact.operation=='confirm_salary' and history):
            # Explicit resumption restores a historically supported payroll cadence.
            series.append(Series(series_id=series_key(target),events=history,representative=history[-1],
                                 amount=fact.amount if fact.operation=='confirm_salary' else history[-1].amount,
                                 monthly_day=fact.effective_date.day if fact.operation=='confirm_salary' else history[-1].settlement_date.day,anchor=history[-1].settlement_date))
    # A scheduled next salary plus a prior salary establishes an explicit ongoing
    # monthly payroll even when the only prior paycheck was prorated.
    for pending in events:
        if pending.category!='salary' or pending.status!='scheduled' or pending.event_id in excluded:
            continue
        history=[e for e in events if e.category=='salary' and e.status=='settled' and
                 e.currency==pending.currency and e.settlement_date and e.settlement_date<=today and
                 not re.search(r'bonus|commission|arrears',e.description,re.I)]
        matching=[s for s in series if s.representative.direction=='credit' and s.representative.currency==pending.currency and
                  (s.amount==pending.amount or s.series_id==series_key(pending))]
        if not matching and history and pending.settlement_date:
            prior=max(history,key=lambda e:e.settlement_date)
            series.append(Series(series_id=series_key(pending),events=[prior],representative=prior,
                                 amount=pending.amount,monthly_day=pending.settlement_date.day,anchor=prior.settlement_date))
    event_series = {e.event_id: s.series_id for s in series for e in s.events}
    for e in events:
        if e.event_id not in event_series and any(s.series_id == series_key(e) for s in series):
            event_series[e.event_id] = series_key(e)
        elif e.category=='salary' and e.status=='scheduled':
            matches=[s for s in series if s.representative.direction=='credit' and s.representative.currency==e.currency and s.amount==e.amount]
            if len(matches)==1: event_series[e.event_id]=matches[0].series_id
        elif e.category=='salary' and re.search(r'final employer|final settlement',e.description,re.I):
            matches=[s for s in series if s.representative.direction=='credit' and s.representative.category=='salary']
            if len(matches)==1: event_series[e.event_id]=matches[0].series_id
    # A final payroll terminates the historical stream even without a separate message.
    ended={event_series[e.event_id] for e in events if e.event_id in event_series and e.category=='salary' and
           re.search(r'final employer|final settlement',e.description,re.I)}
    flow: list[Cashflow] = []
    notes = []
    outstanding = {f.target_event_id for f in facts if f.operation == 'outstanding'}
    # A refund links to its purchase but does not replace the debit. Only a
    # same-direction retry/settlement can supersede a pending/failed attempt.
    successors = set()
    for child in events:
        parent = by_id.get(child.linked_event_id)
        if (parent and parent.status in ('pending','failed','cancelled') and
                child.status in ('settled','scheduled','pending') and child.event_id not in excluded and
                child.direction==parent.direction and child.currency==parent.currency and
                child.amount==parent.amount):
            successors.add(parent.event_id)
    explicit_slots = set()
    for event in events:
        if event.event_id in excluded or event.direction == 'non_cash':
            continue
        day = event.settlement_date
        if event.status == 'settled' and day and day <= today:
            explicit_slots.add((event_series.get(event.event_id, ''), day))
            continue
        is_debit = event.direction == 'debit'
        reserve = is_debit and event.status in ('pending', 'scheduled')
        if event.event_id in outstanding and event.event_id not in successors:
            reserve = True
        salary = (event.direction == 'credit' and event.category == 'salary' and
                  event.status in ('scheduled', 'settled') and not ONE_OFF.search(event.description))
        if not reserve and not salary:
            continue
        if event.event_id in successors and event.status == 'pending':
            continue
        if reserve and (day is None or day < today):
            day = today
        if day is None:
            raise ValueError('Confirmed cash event missing settlement date')
        sid = event_series.get(event.event_id, '')
        amount = event.amount
        active = True
        for fact in facts:
            same_stream = sid and event_series.get(fact.target_event_id)==sid
            same_target = fact.target_event_id==event.event_id
            if (same_target or same_stream) and fact.operation == 'shift_next':
                day = fact.effective_date
            if same_target or same_stream:
                in_period=day >= (fact.effective_date or today) and (fact.end_date is None or day<=fact.end_date)
                if fact.operation in ('set_recurring_amount','confirm_salary') and in_period: amount=fact.amount
                if fact.operation=='stop_recurring' and in_period: active=False
        if not active: continue
        if not today <= day <= end:
            continue
        value = convert(case, amount, event.currency, day)
        flow.append(Cashflow(day=day, amount=-value if is_debit else value,
                             event_id=event.event_id, series_id=sid, description=event.description))
        explicit_slots.add((sid, day))
    for stream in series:
        relevant = [f for f in facts if event_series.get(f.target_event_id) == stream.series_id]
        if stream.series_id in ended and not any(f.operation in ('set_recurring_amount','confirm_salary') for f in relevant):
            continue
        if stream.representative.direction=='debit' and stream.representative.category in VARIABLE and stream.interval_days:
            # Variable necessities are a daily reserve, not a promise that the next
            # grocery purchase occurs on a particular historical weekday.
            daily=stream.amount / Decimal(stream.interval_days)
            for offset in range(91):
                day=today+timedelta(days=offset)
                value=convert(case,daily,stream.representative.currency,day)
                flow.append(Cashflow(day=day,amount=-value,event_id=stream.representative.event_id,
                                     series_id=stream.series_id,description=stream.representative.description+' (daily variable-spending reserve)'))
            continue
        first = True
        for original_day in occurrence_dates(stream, today, end):
            if (stream.series_id, original_day) in explicit_slots:
                continue
            day, amount, active = original_day, stream.amount, True
            for fact in relevant:
                start = fact.effective_date or today
                in_period = original_day >= start and (fact.end_date is None or original_day <= fact.end_date)
                if fact.operation == 'stop_recurring' and in_period:
                    active = False
                if fact.operation in ('set_recurring_amount','confirm_salary') and in_period:
                    amount = fact.amount
                if fact.operation == 'shift_next' and first:
                    day = fact.effective_date
            first = False
            if not active or not today <= day <= end or (stream.series_id, day) in explicit_slots:
                continue
            value = convert(case, amount, stream.representative.currency, day)
            flow.append(Cashflow(day=day, amount=value if stream.representative.direction == 'credit' else -value,
                                 event_id=stream.representative.event_id, series_id=stream.series_id,
                                 description=stream.representative.description))
    for fact in facts:
        notes.append({'target': fact.target_event_id, 'operation': fact.operation,
                      'sources': fact.source_ids, 'reason': fact.justification,
                      'amount':str(fact.amount) if fact.amount is not None else None,
                      'currency':by_id[fact.target_event_id].currency,
                      'effective_date':str(fact.effective_date) if fact.effective_date else None,
                      'description':by_id[fact.target_event_id].description})
    flow.sort(key=lambda f: (f.day, f.amount < ZERO, f.event_id))
    return flow, series, notes
