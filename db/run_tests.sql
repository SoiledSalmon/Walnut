\echo === 1. Precedence resolution (V_EFFECTIVE_EVENTS)
SELECT event_id, root_id, amount, status, source_type FROM V_EFFECTIVE_EVENTS ORDER BY event_id;
\echo === 2. 90-day forecast summary
SELECT count(*) AS days, min(forecast_date) AS first_day, max(forecast_date) AS last_day, min(headroom_above_min) AS min_headroom FROM fn_forecast_90(1,'2026-11-20');
SELECT * FROM fn_forecast_90(1,'2026-11-20') WHERE forecast_date IN ('2026-11-20','2026-11-28','2026-11-30','2026-12-02','2027-02-17');
\echo === 3. Decisions for three requests
SELECT sp_decide(1,'2026-11-20') AS d1, sp_decide(2,'2026-11-20') AS d2, sp_decide(3,'2026-11-20') AS d3;
SELECT decision_id, request_id, chosen_option_id AS opt, affordability_status, amount_safe_to_pay, earliest_safe_date FROM DECISIONS ORDER BY 1;
SELECT decision_id, decision_summary FROM DECISIONS ORDER BY 1;
SELECT decision_id, count(*) AS snapshots, min(headroom_above_min) AS min_headroom FROM V_FORECAST_HEADROOM GROUP BY 1 ORDER BY 1;
SELECT decision_id, installment_number, installment_amount, due_date FROM PLAN_ITEMS ORDER BY 1,2;
SELECT decision_id, category_id, recommended_reduction, period_days FROM SPENDING_CHANGES ORDER BY 1,2;
\echo === 4. Bitemporal re-decision of request 2
SELECT sp_decide(2,'2026-11-20') AS new_version;
SELECT decision_id, request_id, valid_time_end IS NULL AS is_current FROM DECISIONS WHERE request_id = 2 ORDER BY 1;
\echo === 5. Hash chain and tamper detection
SELECT audit_id, decision_id, left(previous_hash,12) AS previous_hash, left(current_hash,12) AS current_hash FROM AUDIT_RECORDS ORDER BY 1;
SELECT sp_verify_audit_chain() AS tampered_before_edit;
UPDATE DECISIONS SET amount_safe_to_pay = 1 WHERE decision_id = 1;
SELECT sp_verify_audit_chain() AS tampered_after_edit;
SELECT audit_id, decision_id, tamper_flag FROM AUDIT_RECORDS ORDER BY 1;
\echo === 6. Constraint checks (each must fail)
\set ON_ERROR_STOP 0
UPDATE DECISIONS SET amount_safe_to_pay = 999999 WHERE decision_id = 2;
INSERT INTO FINANCIAL_EVENTS (user_id,event_type,amount,currency,event_date,source_doc_id,status) VALUES (1,'EXPENSE',-1,'INR','2026-11-20','not-a-mongo-id-00000000','CLEARED');
INSERT INTO FINANCIAL_EVENTS (user_id,event_type,amount,currency,event_date,source_doc_id,status) VALUES (1,'EXPENSE',-1,'INR','2026-11-20',(SELECT source_doc_id FROM SOURCE_DOCUMENTS LIMIT 1),'SETTLED');
INSERT INTO EXCHANGE_RATES (from_currency,to_currency,rate,rate_date) VALUES ('USD','INR',85,'2026-11-01');
INSERT INTO PAYMENT_OPTIONS (event_id,plan_type,tenure_months) VALUES (19,'FULL',3);
\echo === 7. Index used for the per-user date-range scan
SET enable_seqscan = off;
EXPLAIN SELECT * FROM FINANCIAL_EVENTS WHERE user_id = 1 AND event_date BETWEEN '2026-11-01' AND '2026-11-30';
