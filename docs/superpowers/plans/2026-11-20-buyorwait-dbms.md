# Buy or Wait? DBMS Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old hackathon CLI with the three-tier Buy or Wait? system from the master document: web page → FastAPI → PostgreSQL (all decision logic) + MongoDB (raw evidence), with a validation gate between them.

**Architecture:** `db/schema.sql` (verbatim from the master doc) holds all forecasting, ranking, classification and hashing. Python only moves data: evidence goes into Mongo, an extractor turns it into a candidate, the gate validates it, and a parameterised insert promotes it into Postgres. The API calls `sp_decide` and reads the results back. A static HTML/JS page talks only to the JSON API.

**Tech Stack:** Python 3.14, FastAPI + uvicorn, psycopg 3 (`psycopg[binary]`), pymongo (+ GridFS), pymongo-inmemory, python-dotenv, google-genai, pytest + httpx (TestClient), Chart.js from CDN, PostgreSQL 19 (user-installed).

**Spec:** `docs/superpowers/specs/2026-11-20-buyorwait-dbms-design.md`

## Global Constraints

- No affordability arithmetic in Python. Forecast, eligibility, ranking, status, safe amount, cuts and hashes come only from `sp_decide` / `fn_forecast_90` / `sp_verify_audit_chain`.
- `db/schema.sql`, `db/test_data.sql`, `db/run_tests.sql` are byte-for-byte copies of `C:\Users\Ananth\Downloads\schema_1.sql`, `test_data.sql`, `run_tests.sql`. Never edit them.
- Every SQL statement that carries a value uses psycopg parameters (`%s`). No string-formatting of values into SQL.
- Every `FINANCIAL_EVENTS` row has a real Mongo `raw_evidence` document; `SOURCE_DOCUMENTS.source_doc_id` = that document's `_id` as 24-char lowercase hex.
- Shortcuts are settings, read from `.env`: `POSTGRES_URL`, `MONGO_URL` (empty = in-memory mongod), `EXTRACTOR` (`rules` | `gemini`, default `rules`), `GEMINI_API_KEY`, `AS_OF_DATE` (default `2026-11-20`; empty = today).
- Outcome vocabulary everywhere: `SAFE_NOW`, `SPLIT_PLAN`, `WAIT` (UI labels: Safe Now, Split Plan, Wait).
- Gate thresholds: confidence ≥ 0.80 else HELD; `event_date` within −365/+365 days of the as-of date; duplicate window = date ± 1 day.
- Not built: exchange-rate provider fetch, reminders/alerts, ledger-file upload, login, Docker.

## Review Focus

1. **Processing the same evidence twice** (double-click "Process"): must not create a second event. → test in Task 4.
2. **Indian amount formats in messages** (`INR 5,00,000`, `Rs.1,500.00`, `₹2,400`): must parse to the right number, never 500 or 50000. → test in Task 3.
3. **Foreign-currency event with no exchange rate on or before its date**: `fn_to_base` returns NULL and `SUM` silently drops it, which gives a wrong decision. The gate must REJECT it with a readable error. (This is the one rule added beyond §4.3. It's flagged for the team.) → test in Task 4.
4. **Bad manual input** (zero or negative-when-income amount, unknown currency, malformed date): the user gets a 422 or a REJECTED result with a reason, never a 500, and no half-written rows. → test in Task 7.
5. **Running the seed on a non-empty database without `--reset`**: refuses with a clear message instead of piling up duplicate demo data. → test in Task 6.

---

### Task 1: Clean slate, config, database connections

**Files:**
- Delete: `buyorwait/agent.py`, `buyorwait/data.py`, `buyorwait/forecast.py`, `buyorwait/models.py`, `buyorwait/planner.py`, `buyorwait/runner.py`, `buyorwait/store.py`, `buyorwait/verify.py`, `evaluation/`, `tools/`, `prompts/`, `tests/test_agent.py`, `tests/test_finance.py`, `requirements.lock.txt`
- Keep: `tests/fixtures/media/images/image_attack.png` (used in Task 3)
- Create: `db/schema.sql`, `db/test_data.sql`, `db/run_tests.sql` (copies), `buyorwait/config.py`, `buyorwait/db.py`, `.env.example`, `tests/conftest.py`, `tests/test_setup.py`
- Modify: `requirements.txt`, `.gitignore` (add `.env`, `__pycache__/`, `.pytest_cache/`)

**Interfaces:**
- Produces:
  - `config.Settings` (frozen dataclass): `postgres_url: str`, `mongo_url: str`, `extractor: str`, `gemini_api_key: str`, `as_of_date: date | None`; method `as_of() -> date` (the configured date, or `date.today()`).
  - `config.load_settings() -> Settings`: reads `.env` at the repo root with `load_dotenv(override=False)`.
  - `db.pg_connect(url: str) -> psycopg.Connection`: `row_factory=dict_row`, `autocommit=False`.
  - `db.run_sql_file(conn, path: Path) -> None`: executes the whole file without parameters and commits.
  - `db.reset_postgres(conn) -> None`: `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` then `run_sql_file(db/schema.sql)`.
  - `db.mongo_client(url: str) -> pymongo.MongoClient`: real client when `url` is set; otherwise one process-wide `pymongo_inmemory.MongoClient()` (started lazily, reused).
  - `db.mongo_db(client) -> Database`: database name `buyorwait`.
  - `conftest` fixtures: `pg` (a connection to a fresh `buyorwait_test` database with `schema.sql` loaded, recreated per test); `mongo` (in-memory Mongo db `buyorwait_test`, all collections dropped per test; session-scoped client). The admin connection for create/drop uses `TEST_POSTGRES_URL` (from `.env`) with `autocommit=True` and connects to the `postgres` database on the same server.

- [ ] **Step 0 (user prerequisite): PostgreSQL 19 is running.** In psql as the superuser, the user runs:
  `CREATE ROLE buyorwait LOGIN PASSWORD '<their choice>' CREATEDB;` `CREATE DATABASE buyorwait OWNER buyorwait;`
  The user then copies `.env.example` to `.env` and fills in `POSTGRES_URL` and `TEST_POSTGRES_URL` (the same role; database `buyorwait_test`). Claude never types the password.
- [ ] **Step 1: Create branch** `git checkout -b feat/dbms-rebuild`.
- [ ] **Step 2: Delete the old files, copy the three SQL files byte-for-byte, write `requirements.txt`:** `fastapi`, `uvicorn`, `psycopg[binary]`, `pymongo`, `pymongo-inmemory`, `python-dotenv`, `google-genai`, `pytest`, `httpx`. Install into `.venv`. If any wheel is missing for Python 3.14 on Windows, stop and report it.
- [ ] **Step 3: Write failing tests** in `tests/test_setup.py`:

```python
def test_schema_loads_sixteen_tables(pg):
    n = pg.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").fetchone()['n']
    assert n == 16

def test_mongo_in_memory_ping(mongo):
    assert mongo.command('ping')['ok'] == 1

def test_settings_defaults(monkeypatch):
    for k in ('MONGO_URL', 'EXTRACTOR', 'AS_OF_DATE'): monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('AS_OF_DATE', '2026-11-20')
    s = load_settings()
    assert s.extractor == 'rules' and s.mongo_url == '' and s.as_of() == date(2026, 11, 20)

def test_sql_files_are_verbatim():
    src = Path(r'C:\Users\Ananth\Downloads')
    if not src.exists(): pytest.skip('master doc sources not on this machine')
    for a, b in [('schema_1.sql', 'schema.sql'), ('test_data.sql', 'test_data.sql'), ('run_tests.sql', 'run_tests.sql')]:
        assert (src / a).read_bytes() == (ROOT / 'db' / b).read_bytes()
```

- [ ] **Step 4: Run** `pytest tests/test_setup.py -v`. Expected: FAIL (modules missing).
- [ ] **Step 5: Implement** `config.py`, `db.py`, `conftest.py`, `.env.example` (the five keys from Global Constraints plus `TEST_POSTGRES_URL`, with comments).
- [ ] **Step 6: Run** `pytest tests/test_setup.py -v`. Expected: 4 PASS. The first run downloads mongod and may take a minute.
- [ ] **Step 7: Commit** `chore: replace hackathon CLI with DBMS project skeleton`.

---

### Task 2: SQL test suite reproduces Appendix B on PostgreSQL 19

**Files:**
- Test: `tests/test_sql.py`

**Interfaces:**
- Consumes: the `pg` fixture and `db.run_sql_file`.

- [ ] **Step 1: Write the tests.** Each one loads `db/test_data.sql` into `pg` first, then runs the same queries as `run_tests.sql`:

```python
def test_precedence_rows(pg):        # effective event_ids
    assert ids == [1,2,3,4,6,8,10,11,12,16,21]
def test_forecast_shape(pg):         # fn_forecast_90(1,'2026-11-20')
    assert (days, first, last, min_headroom) == (90, date(2026,11,20), date(2027,2,17), Decimal('20010.00'))
    assert balance_on['2026-11-20'] == Decimal('33510.00') and balance_on['2026-11-30'] == Decimal('90010.00')
def test_three_decisions(pg):        # sp_decide(1..3,'2026-11-20')
    assert rows == [(1,1,'SAFE_NOW',Decimal('5000.00'),date(2026,11,20)),
                    (2,3,'SPLIT_PLAN',Decimal('20010.00'),date(2027,1,20)),
                    (3,None,'WAIT',Decimal('20010.00'),date(2026,12,30))]
    assert plan_items_of_2 == [Decimal('15649.00'),Decimal('15450.00'),Decimal('15450.00')]
    assert cuts_of_3 == [(3,Decimal('2200.00'),90),(7,Decimal('3000.00'),90)]
    assert snapshots_per_decision == [90,90,90] and min_headroom_of_2 == Decimal('4361.00')
def test_redecide_versions(pg):      # second sp_decide(2) → old version closed, new one current
def test_tamper_detected(pg):        # verify → 0; UPDATE decision 1 amount_safe_to_pay=1; verify → 1; only audit 1 flagged
@pytest.mark.parametrize('stmt', FIVE_FAILING_STATEMENTS_FROM_RUN_TESTS)
def test_constraints_reject(pg, stmt):   # pytest.raises(psycopg.Error)
def test_index_scan(pg):             # SET enable_seqscan=off; EXPLAIN ... contains 'ix_events_user_date'
```

- [ ] **Step 2: Run** `pytest tests/test_sql.py -v`. Expected: all PASS. This is a characterisation test of SQL that already exists. **If anything fails on PG 19, stop and report to the user. Do not edit `schema.sql`.**
- [ ] **Step 3: Commit** `test: reproduce master-document Appendix B on PostgreSQL 19`.

---

### Task 3: Extractors (rule-based default, Gemini behind a switch)

**Files:**
- Create: `buyorwait/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces:
  - Candidate dict shape (returned by every extractor):
    `{'action': 'CREATE'|'CANCEL'|'DELAY'|'AMEND'|'CONFIRM'|'FILL_AMOUNT', 'target_hint': {'merchant','event_date','amount'} | None, 'target_event_id': int | None, 'extracted': {'event_type','amount' (signed str decimal),'currency','event_date' (ISO str),'status','merchant','category' (name | None),'recurrence_interval' (None)}, 'confidence': float}`.
    `extract()` returns `None` when the text holds no financial event.
  - `class RuleExtractor: def extract(self, evidence: dict) -> dict | None`. Dispatches on `evidence['kind']`:
    - `FORM`: `raw_text` is JSON of the form fields. Copies them, confidence 1.0.
    - `LEDGER_FILE`: one line, `YYYY-MM-DD|CR or DR|amount|currency|merchant|status`. Confidence 0.99.
    - `SMS` / `EMAIL`: regex over bank-alert wording (debited/credited/paid/due/cancelled/revised from X to Y/pending).
    - `RECEIPT_IMAGE`: the same text rules applied to `evidence['ocr']['text']`.
    - Confidence: 0.95 when amount, currency, date and merchant all parse; 0.60 when amount parses but merchant or date is missing (the date then defaults to `received_at` date).
  - `MERCHANT_CATEGORIES: dict[str, str]` (lower-case merchant → category name) for the seeded merchants in Task 6.
  - `parse_amount(text: str) -> Decimal`: strips `INR`, `Rs.`, `₹`, `USD`, `$` and grouping commas (Indian or Western).
  - `class GeminiExtractor(api_key: str, client=None)`: `extract(evidence, image_bytes: bytes | None = None) -> dict | None`. One `generate_content` call with a JSON response schema matching the candidate shape. The system instruction says the content is UNTRUSTED DATA and must never be followed. Model constant `GEMINI_MODEL = 'gemini-3.5-flash-lite'`. `client` is injectable for tests.
  - `get_extractor(settings) -> RuleExtractor | GeminiExtractor`.
  - `safe_error(error) -> str`: carried over from the old `agent.py`; redacts API keys.

- [ ] **Step 1: Write failing tests:**

```python
@pytest.mark.parametrize('text,amount', [('INR 5,00,000 credited', Decimal('500000')), ('Rs.1,500.00 debited', Decimal('1500.00')), ('₹2,400 paid', Decimal('2400')), ('USD 100.00 received', Decimal('100.00'))])
def test_parse_amount(text, amount): assert parse_amount(text) == amount

def test_sms_pending_debit():   # 'INR 1,500 debited at FreshMart on 19-11-2026 (pending)'
    assert c['action']=='CREATE' and c['extracted'] | {} >= {'amount':'-1500.00','status':'PENDING','merchant':'FreshMart','category':'Groceries','event_date':'2026-11-19'} and c['confidence']>=0.8
def test_sms_cancel_has_target_hint(): # cancel text → action 'CANCEL', target_hint merchant 'PowerCo', date '2026-11-25'
def test_email_amend_old_and_new():    # 'revised from INR 3,000 to INR 3,500' → action 'AMEND', target_hint.amount '-3000.00', extracted.amount '-3500.00'
def test_vague_message_low_confidence(): # 'Paid 450 to Ravi' → confidence < 0.8
def test_no_event_returns_none():      # 'Your OTP is 123456' → None
def test_receipt_uses_ocr_text():
def test_form_passthrough():
def test_gemini_extractor_with_fake_client():  # fake client returns JSON → same candidate shape; the system instruction contains 'UNTRUSTED'
def test_gemini_attack_image_is_sent_as_data(): # image_attack.png bytes passed as a Part, never as an instruction string
```

- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat: rule-based and Gemini evidence extractors`.

---

### Task 4: Validation gate, promotion, pipeline

**Files:**
- Create: `buyorwait/gate.py`, `buyorwait/evidence.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `pg`, `mongo`, the candidate shape and extractors from Task 3.
- Produces:
  - `evidence.add_evidence(mongo, user_id: int, kind: str, raw_text: str, received_at: datetime, ocr: dict | None = None, image: bytes | None = None) -> ObjectId`. Puts image bytes in GridFS and stores `file_ref`. Status `RECEIVED`.
  - `evidence.list_evidence(mongo, user_id) -> list[dict]`: each evidence doc with its latest candidate under `candidate`.
  - `gate.validate(conn, mongo, candidate: dict, user_id: int, as_of: date) -> tuple[str, list[str], dict]`: returns `(result, errors, resolved)`, where `result` is `PASSED|HELD|REJECTED` and `resolved` holds the final `status`, `parent_event_id` and `category_id` for insertion. Check order follows §4.3: type/format (amount numeric and non-zero, ISO date, currency in `CURRENCIES`, **non-base currency needs an `EXCHANGE_RATES` row on or before `event_date`**), range (`|amount|` ≤ 100 × the user's `min_balance_threshold`, minimum 100,000; date within ±365 days), ownership (an explicit `target_event_id` must belong to `user_id`), target resolution for non-CREATE actions (`resolve_target`; none found → REJECTED `'target event not found'`), duplicate detection for CREATE, action mapping (CANCEL→CANCELLED, DELAY/AMEND/FILL_AMOUNT→AMENDED, CONFIRM→CLEARED, parent = target), and confidence < 0.80 → HELD (only when nothing was rejected).
  - `gate.resolve_target(conn, mongo, user_id, hint) -> int | None`: Postgres rows for the same user with `event_date` within ±1 day of the hint date and the same amount when the hint has one. Keep those whose merchant (read from the Mongo candidate with `promoted_event_id`) matches case-insensitively. Return the lowest `event_id` with `parent_event_id IS NULL`.
  - Duplicate rule: find the same user, amount and merchant with date ± 1 day. If any match has the same source type, the new record is `DUPLICATE` with parent = that match's family root. Otherwise it keeps its extracted status with parent = the root of the lowest-id match.
  - `gate.promote(conn, mongo, candidate_id: ObjectId) -> int`: in one Postgres transaction, `INSERT SOURCE_DOCUMENTS (str(evidence_id), source_type)` then `INSERT FINANCIAL_EVENTS ... RETURNING event_id`. On commit, sets candidate `promoted_event_id` and evidence status `PROMOTED`.
  - `gate.process_evidence(conn, mongo, extractor, evidence_id: ObjectId, as_of: date) -> dict`: returns `{'evidence', 'candidate', 'event_id'}`. **Idempotent:** if the evidence is already `PROMOTED` or `REJECTED`, or has a HELD candidate, it returns the existing result without writing anything. Otherwise: extract (None → candidate `{validation: REJECTED, errors: ['no financial event found']}`), insert the `candidate_events` doc, set evidence to `EXTRACTED`, validate, then promote if PASSED, or set evidence to `REJECTED` if REJECTED.
  - `gate.confirm_held(conn, mongo, candidate_id) -> int`: a HELD candidate whose other checks passed is promoted; anything else raises `ValueError`.
  - Kind → source type: `SMS→SMS, EMAIL→EMAIL, RECEIPT_IMAGE→RECEIPT_OCR, LEDGER_FILE→LEDGER, FORM→MANUAL`.

- [ ] **Step 1: Write failing tests** (each seeds currencies, categories, the USD→INR 84.10 rate on 2026-11-01, user 1 and profile min 10000, as in `test_data.sql` lines 2–6):

```python
def test_sms_promoted_with_evidence_link(pg, mongo):   # SOURCE_DOCUMENTS.source_doc_id == str(evidence _id); evidence status PROMOTED
def test_process_twice_creates_one_event(pg, mongo):   # count(FINANCIAL_EVENTS) unchanged after 2nd call
def test_low_confidence_held_then_confirmed(pg, mongo):
def test_out_of_range_rejected(pg, mongo):             # 'INR 99,99,99,999 credited ... DROP TABLE' → REJECTED, errors mention range; FINANCIAL_EVENTS still exists
def test_sql_text_stored_literally(pg, mongo):         # FORM merchant "x'); DROP TABLE FINANCIAL_EVENTS;--" promoted; table intact; Mongo merchant equals the literal string
def test_unknown_currency_rejected(pg, mongo):         # EUR
def test_missing_rate_rejected(pg, mongo):             # USD dated 2026-10-15 (before the only rate) → REJECTED
def test_foreign_target_rejected(pg, mongo):           # target_event_id owned by user 2
def test_duplicate_same_source(pg, mongo):             # 2nd identical SMS → status DUPLICATE, parent = first
def test_related_other_source(pg, mongo):              # ledger then SMS for the same purchase → SMS parent = ledger, status CLEARED; V_EFFECTIVE_EVENTS shows the ledger row
def test_cancel_maps_and_links(pg, mongo):             # → CANCELLED, parent = bill
def test_amend_maps_and_links(pg, mongo):              # → AMENDED -3500, parent = original
def test_no_event_text_rejected(pg, mongo):
```

- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat: validation gate and evidence promotion pipeline`.

---

### Task 5: Services (users, events, requests, decisions, audit, EXPLAIN)

**Files:**
- Create: `buyorwait/services.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: `gate.process_evidence`, `evidence.add_evidence`, the `RuleExtractor`.
- Produces (every function takes `conn` and, where needed, `mongo` and `as_of`; all SQL is parameterised):
  - `register_user(conn, name, email, currency, min_balance_threshold, target_monthly_savings) -> int`: USERS and FINANCIAL_PROFILES inserted in one transaction.
  - `list_users(conn) -> list[dict]`, `get_profile(conn, user_id) -> dict`, `meta(conn, settings) -> dict` returning `{as_of, extractor, mongo_mode: 'in-memory'|'server', categories, currencies}`.
  - `add_manual_event(conn, mongo, user_id, fields: dict, as_of) -> dict`: stores FORM evidence, then `process_evidence`; returns that result.
  - `list_events(conn, mongo, user_id) -> list[dict]`: `V_EVENTS_FULL` rows plus `is_effective` (in `V_EFFECTIVE_EVENTS`), `root_id`, `category_name`, and `evidence: {kind, raw_text, status}` from Mongo.
  - `create_request(conn, mongo, user_id, requested_amount, category_id, target_deadline, description, as_of) -> int`: FORM evidence first, then one transaction with SOURCE_DOCUMENTS, the PROPOSED EXPENSE event (amount = −requested, date = as_of), the FULL option and REQUESTS. Evidence marked PROMOTED.
  - `add_payment_option(conn, request_id, plan_type, tenure_months, interest_rate_bps, processing_fee) -> int`.
  - `list_requests(conn, user_id)`, `list_options(conn, request_id)`.
  - `decide(conn, request_id, as_of) -> dict`: in a transaction, `SELECT sp_decide(%s, %s)`, timed with `perf_counter`. Returns `get_decision(...) | {'elapsed_ms': float}`.
  - `get_decision(conn, decision_id) -> dict`: `{decision, option, plan_items, forecast (90 rows from V_FORECAST_HEADROOM), spending_changes (with category_name), audit}`.
  - `list_versions(conn, request_id) -> list[dict]`: `decision_id, valid_time_start, valid_time_end, is_current, affordability_status`.
  - `audit_chain(conn)`, `verify_chain(conn) -> {'tampered': int, 'records': [...]}`, `tamper_decision(conn, decision_id) -> None` (the run_tests UPDATE; demo only).
  - `explain_events(conn, user_id, start: date, end: date) -> list[str]`: `SET LOCAL enable_seqscan = off` and the run_tests EXPLAIN, inside a transaction that is rolled back. Values are bound with `psycopg.ClientCursor` (server-side parameters are not reliable with EXPLAIN).

- [ ] **Step 1: Write failing tests:**

```python
def test_register_creates_user_and_profile(pg): ...
def test_register_duplicate_email_raises_and_writes_nothing(pg): ...
def test_create_request_links_event_option_evidence(pg, mongo):   # status PROPOSED, FULL option tenure 0, source_doc_id is a FORM evidence _id
def test_decide_returns_ninety_forecast_rows_and_elapsed(pg, mongo):
def test_versions_after_redecide(pg, mongo):                       # 2 versions, only the newest is current
def test_verify_and_tamper(pg, mongo):                             # 0 → tamper → 1
def test_explain_uses_index(pg): assert any('ix_events_user_date' in l for l in explain_events(pg, 1, date(2026,11,1), date(2026,11,30)))
```

- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat: service layer over sp_decide, audit chain and EXPLAIN`.

---

### Task 6: Demo seed and end-to-end flow equals Appendix B

**Files:**
- Create: `buyorwait/seed.py` (CLI: `python -m buyorwait.seed [--reset]`)
- Test: `tests/test_flow.py`

**Interfaces:**
- Consumes: `db.reset_postgres`, `services.*`, `gate.process_evidence`, `evidence.add_evidence`.
- Produces: `seed(conn, mongo, as_of: date, reset: bool) -> dict` returning `{'user_id', 'inbox': [evidence ids in processing order]}`. If `reset` is false and USERS is non-empty, it raises `RuntimeError('Database already seeded; run with --reset')`. With `reset`, it calls `reset_postgres` and drops the Mongo database.

Seed contents (wording is the builder's call; the extracted values are pinned). Reference rows come from `test_data.sql` lines 2–6 (same currencies, category order, rate, user "Test User", profile INR/10000/5000).

| test_data id | kind | source | amount | date | status | merchant → category | seeded as |
|---|---|---|---|---|---|---|---|
| 1 | LEDGER_FILE | LEDGER | +60000 | 2026-10-31 | CLEARED | ACME Corp → Salary | promoted |
| 2 | LEDGER_FILE | LEDGER | −20000 | 2026-11-02 | CLEARED | Landlord → Rent | promoted |
| 4 | SMS | SMS | −2000 | 2026-11-08 | CLEARED | Spice Route → Dining | promoted |
| 6 | LEDGER_FILE | LEDGER | −2400 | 2026-11-10 | CLEARED | Cafe Mocha → Dining | promoted |
| 11 | FORM | MANUAL | +60000 MONTHLY | 2026-11-30 | SCHEDULED | ACME Corp → Salary | promoted |
| 12 | FORM | MANUAL | −20000 MONTHLY | 2026-12-02 | SCHEDULED | Landlord → Rent | promoted |
| 17 | FORM | MANUAL | +10000 INVESTMENT | 2026-12-15 | SCHEDULED | Mutual fund → none | promoted |
| req 1–3 | FORM | MANUAL | 5000 / 45000 / 90000, category Electronics, deadlines 2026-12-31 / 2027-01-31 / 2027-02-28; options in test_data order (FULL; FULL, EMI 3 m 1200 bps fee 199, EMI 6 m 1400 bps fee 199; FULL, EMI 3 m 1200 bps fee 199) | | | | seeded |
| 3 | RECEIPT_IMAGE | RECEIPT_OCR | −3000 | 2026-11-05 | CLEARED | FreshMart → Groceries | inbox |
| 21 | RECEIPT_IMAGE | RECEIPT_OCR | −6000 | 2026-11-14 | CLEARED | StyleHub → Shopping | inbox |
| 5 | SMS | SMS | −2400 | 2026-11-10 | CLEARED | Cafe Mocha → Dining | inbox (→ related to 6) |
| 7 | SMS | SMS | −2400 | 2026-11-10 | CLEARED | Cafe Mocha (same text again) | inbox (→ DUPLICATE) |
| 10 | EMAIL | EMAIL | +100 USD | 2026-11-12 | CLEARED | Upwork → none | inbox |
| 8 | SMS | SMS | −1500 | 2026-11-19 | PENDING | FreshMart → Groceries | inbox |
| 9 | SMS | SMS | +5000 | 2026-11-19 | PENDING | refund → none | inbox |
| 13 | EMAIL | EMAIL | −999 bill | 2026-11-25 | SCHEDULED | PowerCo → Utilities | inbox |
| 14 | SMS | SMS | cancel of 13 | 2026-11-25 | → CANCELLED | PowerCo | inbox (after 13) |
| 15 | EMAIL | EMAIL | −3000 bill | 2026-11-28 | SCHEDULED | AirNet → Utilities | inbox |
| 16 | EMAIL | EMAIL | revise 15 to −3500 | 2026-11-28 | → AMENDED | AirNet | inbox (after 15) |
| — | SMS | SMS | INR 99,99,99,999 with injection text | | | | inbox (→ REJECTED) |
| — | SMS | SMS | "Paid 450 to Ravi" | | | | inbox (→ HELD) |

Receipts store a small generated PNG in GridFS (any image; Pillow is not required, so a static PNG fixture under `buyorwait/seed_assets/` will do) plus `ocr: {text, confidence: 0.91}`. `received_at` order = inbox processing order.

- [ ] **Step 1: Write failing tests:**

```python
def test_seed_refuses_without_reset(pg, mongo):      # second seed(reset=False) raises RuntimeError
def test_inbox_results(pg, mongo):                   # process all inbox items: 2nd-to-last REJECTED, last HELD, all others PROMOTED
def test_flow_matches_appendix_b(pg, mongo):         # after processing the inbox, decide requests 1..3 at 2026-11-20:
    assert opening_balance == Decimal('33510.00')     # fn_forecast_90 first-day balance
    assert [(d['affordability_status'], d['chosen_option_id'], d['amount_safe_to_pay'], d['earliest_safe_date']) for d in ds] == \
        [('SAFE_NOW',1,Decimal('5000.00'),date(2026,11,20)), ('SPLIT_PLAN',3,Decimal('20010.00'),date(2027,1,20)), ('WAIT',None,Decimal('20010.00'),date(2026,12,30))]
    assert cuts == {'Dining': Decimal('2200.00'), 'Shopping': Decimal('3000.00')}
    assert effective_amounts_multiset == Counter of test_data effective amounts (60000,-20000,-3000,-2000,-2400,-1500,100 USD,60000,-20000,-3500,-6000)
```

- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat: demo seed reproducing master-document scenario through the gate`.

---

### Task 7: FastAPI app

**Files:**
- Create: `buyorwait/api.py`; Modify: `main.py` (`uvicorn.run('buyorwait.api:app', host='127.0.0.1', port=8000)`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: everything in `services`, plus `gate.process_evidence`, `gate.confirm_held`, `evidence.add_evidence/list_evidence`.
- Produces: `create_app(settings: Settings | None = None, conn_factory=None, mongo=None) -> FastAPI` (the injectable pieces are for tests); module-level `app = create_app()`. One Postgres connection per request via a dependency. Serves `frontend/` at `/`. Request bodies are Pydantic models (amount `Decimal`, dates `date`). `ValueError` and `psycopg.errors.IntegrityError` / `CheckViolation` / `RaiseException` map to HTTP 422 with `{'detail': message}`. Routes:

```
GET  /api/meta
GET  /api/users                         POST /api/users
GET  /api/users/{uid}/profile
GET  /api/users/{uid}/evidence          POST /api/users/{uid}/evidence   {kind: SMS|EMAIL, raw_text}
POST /api/evidence/{eid}/process        POST /api/candidates/{cid}/confirm
GET  /api/users/{uid}/events            POST /api/users/{uid}/events     {event_type, category_id, amount, currency, event_date, status, recurrence_interval, description}
GET  /api/users/{uid}/requests          POST /api/users/{uid}/requests   {requested_amount, category_id, target_deadline, description}
GET  /api/requests/{rid}/options        POST /api/requests/{rid}/options {plan_type, tenure_months, interest_rate_bps, processing_fee}
POST /api/requests/{rid}/decide         GET  /api/requests/{rid}/decisions
GET  /api/decisions/{did}               POST /api/decisions/{did}/tamper
GET  /api/audit                         POST /api/audit/verify
GET  /api/users/{uid}/explain
```

- [ ] **Step 1: Write failing tests** with `TestClient`:

```python
def test_happy_path(client):        # register → add SMS → process → event listed with evidence → request → option → decide → 90 forecast rows
def test_bad_event_422(client):      # amount 0 → 422; currency 'EUR' → result REJECTED with reason; date 'not-a-date' → 422; no FINANCIAL_EVENTS row added
def test_option_full_with_tenure_422(client)
def test_duplicate_email_422(client)
def test_tamper_then_verify(client)
def test_frontend_served(client):    # GET / → 200, text/html
```

- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat: FastAPI service layer`.

---

### Task 8: Web frontend and README

**Files:**
- Create: `frontend/index.html`, `frontend/app.js`, `frontend/style.css`
- Modify: `README.md` (replace it fully: what the system is, architecture diagram in text, setup including the Step 0 SQL, `python -m buyorwait.seed --reset`, `python main.py`, `pytest`, the `.env` switches and the exam path: set `MONGO_URL`, `EXTRACTOR=gemini`, add docker-compose)

**Interfaces:**
- Consumes: the Task 7 routes only.

The page is one screen with these sections, in this order:
- Header showing `as_of`, extractor and Mongo mode (from `/api/meta`), plus a user picker and a register form.
- **Evidence inbox:** a table of raw evidence (kind, text, status). A Process button per item. After processing, a side-by-side card shows the Mongo doc JSON, the candidate (confidence, validation result, errors) and the promoted Postgres row. HELD items get a Confirm button. A box to add a new SMS or e-mail.
- **Events:** a table with effective / excluded badges, status, source, root family, and each row's Mongo `raw_text`. An add-manual-event form.
- **Requests:** a list, a new-request form, add-option, Decide and Re-decide buttons.
- **Decision panel:** a status badge, safe amount today, chosen option, instalment table, earliest full-payment date, spending cuts, the summary, `elapsed_ms`. A Chart.js line chart of `projected_balance` and `projected_savings` over 90 days with a horizontal line at `min_threshold_applied`. A version history table.
- **Audit:** the chain table (prev and current hash, first 12 characters), Verify chain, and Tamper (demo) per decision, which shows the flagged row.
- **Inside the database:** the EXPLAIN output.

Chart.js from `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`. Plain `fetch`, no framework. Every rendered value goes through `textContent`, never `innerHTML`, so evidence text can't inject markup.

- [ ] **Step 1: Build the page.**
- [ ] **Step 2: Verify in the browser:** `python -m buyorwait.seed --reset`, then `python main.py`, then open `http://127.0.0.1:8000`. Walk the full demo: process every inbox item (one REJECTED, one HELD then confirmed... **note:** confirming the HELD item adds a −450 expense, so the Appendix B numbers are reached *before* confirming; the demo script says to decide first and confirm afterwards, or leave it HELD). Decide requests 1, 2, 3 and check the statuses SAFE_NOW / SPLIT_PLAN (EMI 3 m, 15,649 + 15,450 + 15,450) / WAIT (30 Dec 2026, cuts Dining 2,200, Shopping 3,000). Re-decide request 2, tamper with decision 1, verify. Check the browser console for errors.
- [ ] **Step 3: Commit** `feat: web frontend and project README`.

---

### Task 9: Final verification

- [ ] **Step 1:** `pytest -q` must be all green. Paste the summary line.
- [ ] **Step 2:** Run the Task 8 Step 2 demo walk again from a fresh `seed --reset` and check every number against Appendix B.
- [ ] **Step 3:** `git status` must be clean, and `.env` must not be tracked.
