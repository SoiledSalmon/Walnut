-- Sample data used to test schema.sql (as_of date = 2026-11-20)
INSERT INTO CURRENCIES VALUES ('INR','Indian Rupee'), ('USD','US Dollar');
INSERT INTO CATEGORIES (category_name,is_discretionary) VALUES ('Electronics',TRUE),('Groceries',FALSE),('Dining',TRUE),('Rent',FALSE),('Utilities',FALSE),('Salary',FALSE),('Shopping',TRUE);
INSERT INTO EXCHANGE_RATES (from_currency,to_currency,rate,rate_date) VALUES ('USD','INR',84.10,'2026-11-01');
INSERT INTO USERS (name,email) VALUES ('Test User','test@example.com');
INSERT INTO FINANCIAL_PROFILES (user_id,currency,min_balance_threshold,target_monthly_savings) VALUES (1,'INR',10000,5000);

-- evidence documents (ids would be MongoDB ObjectIds)
CREATE TEMP TABLE d(n INT, t VARCHAR);
INSERT INTO d VALUES (1,'LEDGER'),(2,'SMS'),(3,'EMAIL'),(4,'MANUAL'),(5,'RECEIPT_OCR');
INSERT INTO SOURCE_DOCUMENTS SELECT substr(md5(n::text),1,24), t FROM d;
CREATE TEMP TABLE doc AS SELECT t, substr(md5(n::text),1,24) AS id FROM d;

INSERT INTO FINANCIAL_EVENTS (event_id,user_id,event_type,category_id,amount,currency,event_date,source_doc_id,parent_event_id,status,recurrence_interval)
OVERRIDING SYSTEM VALUE VALUES
 ( 1,1,'INCOME',6, 60000,'INR','2026-10-31',(SELECT id FROM doc WHERE t='LEDGER'),NULL,'CLEARED',NULL),
 ( 2,1,'EXPENSE',4,-20000,'INR','2026-11-02',(SELECT id FROM doc WHERE t='LEDGER'),NULL,'CLEARED',NULL),
 ( 3,1,'EXPENSE',2, -3000,'INR','2026-11-05',(SELECT id FROM doc WHERE t='RECEIPT_OCR'),NULL,'CLEARED',NULL),
 ( 4,1,'EXPENSE',3, -2000,'INR','2026-11-08',(SELECT id FROM doc WHERE t='SMS'),NULL,'CLEARED',NULL),
 (21,1,'EXPENSE',7, -6000,'INR','2026-11-14',(SELECT id FROM doc WHERE t='RECEIPT_OCR'),NULL,'CLEARED',NULL),
 -- same purchase reported by SMS and by the ledger: ledger wins the tie, counted once
 ( 5,1,'EXPENSE',3, -2400,'INR','2026-11-10',(SELECT id FROM doc WHERE t='SMS'),NULL,'CLEARED',NULL),
 ( 6,1,'EXPENSE',3, -2400,'INR','2026-11-10',(SELECT id FROM doc WHERE t='LEDGER'),5,'CLEARED',NULL),
 -- exact duplicate SMS: never counted
 ( 7,1,'EXPENSE',3, -2400,'INR','2026-11-10',(SELECT id FROM doc WHERE t='SMS'),5,'DUPLICATE',NULL),
 -- pending debit counts, pending credit does not
 ( 8,1,'EXPENSE',2, -1500,'INR','2026-11-19',(SELECT id FROM doc WHERE t='SMS'),NULL,'PENDING',NULL),
 ( 9,1,'INCOME',NULL, 5000,'INR','2026-11-19',(SELECT id FROM doc WHERE t='SMS'),NULL,'PENDING',NULL),
 -- foreign-currency income
 (10,1,'INCOME',NULL,  100,'USD','2026-11-12',(SELECT id FROM doc WHERE t='EMAIL'),NULL,'CLEARED',NULL),
 -- recurring schedule (expanded by the recursive CTE)
 (11,1,'INCOME',6, 60000,'INR','2026-11-30',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'SCHEDULED','MONTHLY'),
 (12,1,'EXPENSE',4,-20000,'INR','2026-12-02',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'SCHEDULED','MONTHLY'),
 -- future bill cancelled by SMS: excluded
 (13,1,'BILL',5, -999,'INR','2026-11-25',(SELECT id FROM doc WHERE t='EMAIL'),NULL,'SCHEDULED',NULL),
 (14,1,'BILL',5, -999,'INR','2026-11-25',(SELECT id FROM doc WHERE t='SMS'),13,'CANCELLED',NULL),
 -- future bill amended from 3000 to 3500: amended value used
 (15,1,'BILL',5,-3000,'INR','2026-11-28',(SELECT id FROM doc WHERE t='EMAIL'),NULL,'SCHEDULED',NULL),
 (16,1,'BILL',5,-3500,'INR','2026-11-28',(SELECT id FROM doc WHERE t='EMAIL'),15,'AMENDED',NULL),
 -- unrealised investment return: excluded
 (17,1,'INVESTMENT',NULL,10000,'INR','2026-12-15',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'SCHEDULED',NULL),
 -- prospective expenses for three requests (PROPOSED = not yet in the forecast)
 (18,1,'EXPENSE',1, -5000,'INR','2026-11-20',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'PROPOSED',NULL),
 (19,1,'EXPENSE',1,-45000,'INR','2026-11-20',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'PROPOSED',NULL),
 (20,1,'EXPENSE',1,-90000,'INR','2026-11-20',(SELECT id FROM doc WHERE t='MANUAL'),NULL,'PROPOSED',NULL);
SELECT setval(pg_get_serial_sequence('financial_events','event_id'), 21);

INSERT INTO PAYMENT_OPTIONS (event_id,plan_type,tenure_months,interest_rate_bps,processing_fee) VALUES
 (18,'FULL',0,0,0),
 (19,'FULL',0,0,0),(19,'EMI',3,1200,199),(19,'EMI',6,1400,199),
 (20,'FULL',0,0,0),(20,'EMI',3,1200,199);

INSERT INTO REQUESTS (user_id,event_id,requested_amount,category_id,target_deadline) VALUES
 (1,18, 5000,1,'2026-12-31'), (1,19,45000,1,'2027-01-31'), (1,20,90000,1,'2027-02-28');
