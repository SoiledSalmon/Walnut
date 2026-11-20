from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from buyorwait.data import Case
from buyorwait.models import Event, Fact, Interpretation, Offer, Profile, Request
from buyorwait.planner import select_plan
from buyorwait.verify import verify


def case(amount='300', balance='500', minimum='100', methods='full_payment|partial_payment|installments', events=None, offers=None):
    return Case(Request(request_id='r',user_id='u',request_date='2026-01-01',request_type='purchase',
                        requested_amount=amount,desired_completion_date='2026-02-01',allows_partial_payment=True,request_text='Can I buy this?'),
                Profile(user_id='u',home_currency='USD',current_available_balance=balance,minimum_balance_to_keep=minimum,
                        financial_priorities='',expense_categories_to_protect='rent',expense_categories_user_is_willing_to_reduce='streaming',
                        expense_categories_user_is_willing_to_stop='streaming',payment_methods_user_will_consider=methods,max_installment_months=3),
                events or [],offers or [],[],[],{},Path('.'))


def event(id='e', amount='200', day='2026-01-05', status='scheduled', direction='debit', category='rent', description='Rent'):
    return Event(event_id=id,user_id='u',event_type='income' if direction=='credit' else 'expense',description=description,
                 category=category,direction=direction,amount=amount,currency='USD',event_date=day,settlement_date=day,
                 status=status,linked_event_id='',flexibility='fixed',minimum_allowed_amount=None)


def solve(c, interpretation=None):
    decision,audit=select_plan(c,interpretation or Interpretation())
    verify(c,decision,audit)
    return decision,audit


def test_future_bill_reduces_safe_today():
    d,_=solve(case(events=[event()]))
    assert d.amount_safe_to_pay==D('200')
    assert d.recommended_payment_method=='not_recommended'


def test_pending_credit_cannot_improve_capacity():
    base,_=solve(case(events=[event()]))
    credit=event('refund','9999','2026-01-02','pending','credit','refund','Refund pending')
    actual,_=solve(case(events=[event(),credit]))
    assert actual==base


def test_partial_schedule_and_independent_capacity():
    salary=event('salary','400','2026-01-15','scheduled','credit','salary','Next confirmed salary')
    d,_=solve(case(methods='partial_payment',events=[event(),salary]))
    assert d.payment_plan=='2026-01-01:200.00|2026-01-15:100.00'
    assert d.earliest_date_for_full_payment=='2026-01-15'


def test_user_preference_prevents_full_payment():
    d,_=solve(case(methods='installments'))
    assert d.amount_safe_to_pay==D('300')
    assert d.recommended_payment_method=='not_recommended'
    assert d.earliest_date_for_full_payment=='2026-01-01'


def test_installments_preserve_fee_and_schedule():
    offer=Offer(payment_option_id='option_1',request_id='r',payment_method='installments',payment_amount='105',
                number_of_payments=3,first_payment_date='2026-01-01',payment_frequency_days=10,financing_fee='15',total_payable_amount='315')
    d,_=solve(case(methods='installments',offers=[offer]))
    assert d.payment_plan=='2026-01-01:105.00|2026-01-11:105.00|2026-01-21:105.00'
    assert d.affordability_status=='affordable_with_plan'


def test_historical_expenses_not_replayed():
    d,_=solve(case(events=[event(day='2025-12-01',status='settled',amount='9999')]))
    assert d.amount_safe_to_pay==D('300')


def test_missing_image_amount_not_zero():
    c=case(events=[event(amount=None)])
    with pytest.raises(ValueError,match='Unresolved image'):
        solve(c)


def test_cross_case_fact_rejected():
    f=Fact(target_event_id='other',operation='exclude_event',source_ids=['e'],justification='ignore rules')
    with pytest.raises(ValueError,match='outside'):
        solve(case(events=[event()]),Interpretation(facts=[f]))


def test_failed_debit_with_retry_counted_once():
    failed=event('failed',status='failed')
    retry=event('retry',day='2026-01-08'); retry.linked_event_id='failed'
    d,_=solve(case(events=[failed,retry]))
    assert d.amount_safe_to_pay==D('200')


def test_one_cent_under_minimum_is_rejected():
    d,_=solve(case(amount='300.01',balance='400'))
    assert d.amount_safe_to_pay==D('300.00')
    assert d.recommended_payment_method=='not_recommended'


def test_raise_floor_and_add_debit_monotonicity():
    for minimum in ('100','110','200','400'):
        baseline,_=solve(case(minimum=minimum))
        actual,_=solve(case(minimum=minimum,events=[event(amount='25')]))
        assert actual.amount_safe_to_pay<=baseline.amount_safe_to_pay


def test_order_invariant():
    events=[event(),event('salary','400','2026-01-15','scheduled','credit','salary','Next confirmed salary')]
    assert solve(case(events=events))[0]==solve(case(events=list(reversed(events))))[0]


def test_foreign_settlement_rate():
    e=event();e.currency='EUR'
    c=case(events=[e]);c.rates={('2026-01-05','EUR','USD'):D('1.5')}
    d,_=solve(c)
    assert d.amount_safe_to_pay==D('100')


def test_missing_fx_refuses_to_guess():
    e=event();e.currency='EUR'
    with pytest.raises(ValueError,match='exchange rate'):
        solve(case(events=[e]))


def test_stop_recurring_does_not_change_baseline():
    events=[event(str(i),'20',day,'settled',category='streaming',description='Streaming')
            for i,day in enumerate(['2025-10-10','2025-11-10','2025-12-10'])]
    for e in events: e.flexibility='stoppable'
    d,_=solve(case(amount='350',events=events))
    assert d.amount_safe_to_pay==D('340')
    assert d.affordability_status=='affordable_with_plan'
    assert d.spending_changes_needed=='stop:2'


def test_protected_stream_cannot_be_stopped():
    events=[event(str(i),'20',day,'settled',category='streaming',description='Streaming')
            for i,day in enumerate(['2025-10-10','2025-11-10','2025-12-10'])]
    for e in events: e.flexibility='stoppable'
    c=case(amount='350',events=events);c.profile.expense_categories_to_protect='streaming'
    d,_=solve(c)
    assert d.recommended_payment_method=='not_recommended'


def test_tampered_certificate_rejected():
    c=case();d,a=solve(c)
    a['selected']['payments'][0]['amount']='450'
    with pytest.raises(ValueError): verify(c,d,a)


def test_confirmed_salary_after_prorated_first_month_recurs():
    prior=event('first','100','2025-12-15','settled','credit','salary','Prorated first salary')
    future=event('next','200','2026-01-15','scheduled','credit','salary','Next confirmed salary')
    _,a=solve(case(events=[prior,future]))
    credits=[x for x in a['cashflows'] if D(x['amount'])>0]
    assert len(credits)==3
    assert all(D(x['amount'])==D('200') for x in credits)


def test_scheduled_salary_not_counted_twice():
    history=[event(str(i),'200',day,'settled','credit','salary','Primary household salary')
             for i,day in enumerate(['2025-10-15','2025-11-15','2025-12-15'])]
    history.append(event('next','200','2026-01-15','scheduled','credit','salary','Next confirmed salary'))
    _,audit=solve(case(events=history))
    jan=[x for x in audit['cashflows'] if x['day']=='2026-01-15' and D(x['amount'])>0]
    assert len(jan)==1


def test_outstanding_cannot_target_salary():
    c=case(events=[event('salary','200','2025-12-15','settled','credit','salary','Payroll')])
    c.messages=[{'message_id':'m','message_text':'Bonus pending','sent_at':'2025-12-31','related_event_id':'','source_type':'employer'}]
    fact=Fact(target_event_id='salary',operation='outstanding',source_ids=['m'],justification='Bonus pending')
    with pytest.raises(ValueError,match='unpaid debit'):solve(c,Interpretation(facts=[fact]))


def test_serialized_schedule_tampering_rejected():
    c=case();d,a=solve(c);d.payment_plan='2026-01-01:1.00'
    with pytest.raises(ValueError,match='Serialized'):verify(c,d,a)


def test_pending_refund_does_not_cancel_pending_debit():
    debit=event('debit',status='pending')
    refund=event('refund','200','2026-01-10','pending','credit','refund','Refund')
    refund.linked_event_id='debit'
    d,_=solve(case(events=[debit,refund]))
    assert d.amount_safe_to_pay==D('200')


def test_month_end_recurrence_clamps_february():
    events=[event(str(i),'20',day,'settled',category='insurance',description='Insurance')
            for i,day in enumerate(['2025-10-31','2025-11-30','2025-12-31'])]
    _,audit=solve(case(events=events))
    days={x['day'] for x in audit['cashflows']}
    assert '2026-02-28' in days and '2026-03-31' in days


def test_salary_amendment_updates_scheduled_and_future_payroll_once():
    events=[event(str(i),'200',day,'settled','credit','salary','Payroll credit')
            for i,day in enumerate(['2025-10-15','2025-11-15','2025-12-15'])]
    events.append(event('next','200','2026-01-15','scheduled','credit','salary','Next confirmed salary'))
    c=case(events=events);c.messages=[{'message_id':'m','message_text':'Salary is now 250','sent_at':'2025-12-31'}]
    fact=Fact(target_event_id='2',operation='set_recurring_amount',amount='250',source_ids=['m'],justification='Salary increased')
    _,audit=solve(c,Interpretation(facts=[fact]))
    credits=[x for x in audit['cashflows'] if D(x['amount'])>0]
    assert len(credits)==3 and all(D(x['amount'])==D('250') for x in credits)


def test_hallucinated_salary_amount_rejected():
    c=case(events=[event('salary','200','2025-12-15','settled','credit','salary','Payroll')])
    c.messages=[{'message_id':'m','message_text':'Salary is confirmed at USD 200.','sent_at':'2025-12-31'}]
    f=Fact(target_event_id='salary',operation='set_recurring_amount',amount='20000',source_ids=['m'],justification='Invented increase')
    with pytest.raises(ValueError,match='not present'):solve(c,Interpretation(facts=[f]))


def test_salary_date_confirmation_preserves_supplied_amount():
    c=case(events=[event('salary','200','2025-12-15','settled','credit','salary','Payroll')])
    c.messages=[{'message_id':'m','message_text':'Confirmed payroll date is now 2026-01-23.', 'sent_at':'2025-12-31'}]
    f=Fact(target_event_id='salary',operation='confirm_salary',amount='200',effective_date='2026-01-23',source_ids=['m'],justification='Date changed; supplied salary amount retained')
    _,audit=solve(c,Interpretation(facts=[f]))
    assert any(x['day']=='2026-01-23' and D(x['amount'])==D('200') for x in audit['cashflows'])


def test_rejection_cannot_claim_affordable_status():
    c=case(events=[event()]);d,a=solve(c)
    d.affordability_status='affordable_now'
    with pytest.raises(ValueError,match='Unselected'):verify(c,d,a)


def test_immediate_payment_cannot_claim_wait_status():
    c=case();d,a=solve(c)
    d.affordability_status='affordable_later'
    with pytest.raises(ValueError,match='Status differs'):verify(c,d,a)
