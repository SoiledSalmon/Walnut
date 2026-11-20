# Buy or Wait? — DBMS project build design

Date: 2026-11-20. Team: Ananth G Karanth, Anurag G Kharvi, Manoj Gupta D B (RVCE, DBMS Lab CD252IA).

## 1. Intent

Repurpose this repository (an old hackathon CLI agent) into the working system described by the
approved **Synopsis** and the **Master Design Document** (`BuyOrWait_Master_Document.pdf`).

- **Tomorrow (2026-11-21):** a full working demo where the team can show that *every* objective in the
  Synopsis and SRS is met.
- **Practical exam (a few months later):** the same system on a proper setup (Docker, real
  MongoDB server, optionally live Gemini extraction).

**Governing rule:** every shortcut taken for tomorrow must be a *configuration switch*, not a fake.
Moving to the exam setup means changing `.env` (and adding `docker-compose.yml`), never rewriting code.

## 2. Goals

Each Synopsis/SRS objective is demonstrably met in the running app:

| Objective | Demonstrated by |
|---|---|
| Hybrid PostgreSQL + MongoDB | Both run for real. Every event in the UI shows its Postgres row beside the MongoDB evidence document it came from, joined by `source_doc_id` = Mongo `_id`. |
| 3NF/BCNF schema, constraints, triggers (FR1) | `db/schema.sql` = the master document's tested `schema_1.sql`, **unchanged**. |
| Message/receipt ingestion + NLP/OCR (FR2) | Pre-loaded SMS / e-mail / receipt evidence sits in Mongo `raw_evidence` as `RECEIVED`. Processing it runs: extractor → `candidate_events` doc with confidence → validation gate → Postgres. |
| Validation gate + injection safety (FR2, NFR3) | Gate shows PASSED / HELD (confidence < 0.80) / REJECTED with recorded errors. A malicious SMS is rejected on screen. Only parameterised SQL reaches Postgres. |
| Precedence rules (FR3) | Event list marks winners and excluded records (SMS + ledger counted once, duplicate dropped, pending credit ignored, bill cancelled / amended, unrealised investment excluded), read from `V_EFFECTIVE_EVENTS`. |
| 90-day in-database forecast (FR4) | Decisions are made by calling `sp_decide`; nothing is computed in Python. Chart of the 90 `FORECAST_SNAPSHOTS` with the minimum-balance line. |
| Expected outputs (SRS) | Status (Safe Now / Split Plan / Wait), safe amount today, recommended option + installment schedule, earliest full-payment date, 90-day balance and savings trajectory with headroom, spending changes, explanation. |
| Bitemporal + SHA-256 audit (FR5, NFR4) | "Re-decide" shows the previous version closed (`valid_time_end`) and the new one current. "Tamper (demo)" edits a decision row directly; "Verify chain" calls `sp_verify_audit_chain()` and shows the flagged record. |
| Three-tier FastAPI + web frontend (NFR5), FR1 | Register user + profile (one transaction), add manual events, add payment options to a request, submit requests. No login. |
| NFR1 performance | Each decision shows its elapsed time; an EXPLAIN panel shows the index scan on `ix_events_user_date`. |

## 3. Non-goals / anti-goals

- **Anti-goal:** any affordability arithmetic in Python. The forecast, ranking, classification and
  hashing live in PostgreSQL only. (The old `forecast.py` / `planner.py` are deleted for this reason.)
- **Anti-goal:** placeholders that must be rewritten later. Shortcuts are switches.
- **Anti-goal:** diverging from the master document. `schema.sql`, `test_data.sql`, `run_tests.sql` are
  copied verbatim; if a change ever becomes necessary, the team decides and the document is updated.
- No authentication / login, no multi-tenant security (not in the Synopsis or SRS).
- No Docker tonight (exam-phase task).
- **Not in the agreed demo flow, so not built** (they appear in the master DFD; the team will decide
  later whether to add them): exchange-rate provider fetch (process 4.0 — rates are seeded),
  installment reminders / alerts (part of 8.0), ledger-file upload (ledger records are seeded).

## 4. Constraints

- Windows 11, Python 3.14. No Postgres, MongoDB or Docker currently installed.
- PostgreSQL 19, installed natively by the user (no embedded-Postgres wheel exists for Python
  3.14/Windows). The master document's SQL was tested on 16.15; the SQL test suite confirms it on 19.
  App role `buyorwait` with `CREATEDB` (so tests can create a scratch database), database `buyorwait`.
- MongoDB tonight via `pymongo-inmemory` (downloads and runs a real `mongod`), selected by leaving
  `MONGO_URL` empty.
- Demo must work offline-from-AI: default extractor needs no API key.

## 5. Approaches considered

**Frontend / service shape**

1. **Chosen — FastAPI JSON API + static HTML/JS frontend served by FastAPI (Chart.js from CDN, no
   build step).** Clean three-tier split the SRS asks for (frontend talks only to the API), nothing to
   compile, quick to build tonight, survives unchanged to the exam.
2. Server-rendered Jinja templates. Fewer files, but blurs the "web frontend ↔ service layer" tier the
   SRS and architecture figure describe.
3. React/Vite SPA. Nicer to extend, but a build toolchain is friction tonight and adds nothing the
   rubric marks.

**Where the decision logic runs** — one sensible way: `sp_decide` in Postgres, as the whole design
requires. A Python engine (the old code) contradicts the project's central claim.

**MongoDB tonight** — `pymongo-inmemory` (chosen: real mongod, zero install) vs `mongomock` (a fake —
violates the governing rule) vs native MongoDB install (more setup tonight; that is the exam path,
reachable by setting `MONGO_URL`).

The team handed the "how" to Claude; these are Claude's calls.

## 6. Design

### 6.1 Layout

```
db/schema.sql            master doc schema_1.sql, verbatim
db/test_data.sql         master doc test data, verbatim
db/run_tests.sql         master doc test script, verbatim
buyorwait/config.py      .env settings: POSTGRES_URL, MONGO_URL, EXTRACTOR, GEMINI_API_KEY, AS_OF_DATE
buyorwait/db.py          psycopg (v3) connection + Mongo client (in-memory when MONGO_URL empty)
buyorwait/extract.py     Extractor interface; RuleExtractor (regex, default); GeminiExtractor
buyorwait/gate.py        validation gate (master doc §4.3) + action mapping + parameterised promote
buyorwait/api.py         FastAPI app: JSON endpoints + serves frontend/
buyorwait/seed.py        builds the demo database (Postgres + Mongo)
frontend/                index.html, app.js, style.css
tests/                   pytest
```

Old modules removed: `forecast.py`, `planner.py`, `runner.py`, `verify.py`, `store.py`, `data.py`,
`models.py` (CSV contracts), `agent.py` (its Gemini client / safety pieces move into
`GeminiExtractor`), `evaluation/`, `tools/`, hackathon README sections. Kept and reused: the
image-injection fixture, `safe_error` key redaction, the untrusted-data prompt wording.

### 6.2 MongoDB (master doc §4.2)

- `raw_evidence` (M1): `{_id, user_id, kind: SMS|EMAIL|RECEIPT_IMAGE|LEDGER_FILE|FORM, received_at,
  raw_text, file_ref, ocr: {text, confidence}|null, status: RECEIVED|EXTRACTED|PROMOTED|REJECTED}`.
  Receipt images stored in GridFS (`file_ref`).
- `candidate_events` (M2): `{_id, evidence_id, user_id, source_type, action, target_event_id,
  extracted: {...}, confidence, validation: {result, errors, checked_at}, promoted_event_id}`.

### 6.3 Ingestion pipeline

1. **Ingest:** evidence document inserted into `raw_evidence` (status `RECEIVED`). Pre-loaded by seed;
   the UI can also add a new SMS / e-mail text.
2. **Extract:** `EXTRACTOR=rules` (default) parses SMS/e-mail text with regex into a candidate
   (action, amount, currency, date, status, merchant, target) and a confidence score; receipts use the
   stored `ocr.text`. `EXTRACTOR=gemini` sends text/image to Gemini with the untrusted-data instruction
   and a strict JSON response schema. Both return the same candidate shape. Status → `EXTRACTED`.
3. **Gate** (§4.3): type/format, range (±365 days, plausible amount), confidence ≥ 0.80 else HELD,
   ownership of `target_event_id`, duplicate detection (same user, amount, merchant, date ±1 day →
   DUPLICATE if same source, else related record via `parent_event_id`), action mapping
   (CANCEL→CANCELLED, DELAY/AMEND/FILL_AMOUNT→AMENDED, CONFIRM→CLEARED). Result written to the candidate.
4. **Promote:** in one Postgres transaction: `SOURCE_DOCUMENTS` row (id = Mongo `_id` hex) then
   `FINANCIAL_EVENTS` row, parameterised. Evidence → `PROMOTED` (or `REJECTED`); candidate gets
   `promoted_event_id`. HELD candidates can be confirmed by the user in the UI, then promoted.

Manual events and request forms are stored as `FORM` evidence (source type `MANUAL`) so every event
still has a real Mongo document behind its `source_doc_id`.

### 6.4 Requests and decisions

- Submit request (process 6.0): form stored as `FORM` evidence; one transaction inserts the
  `PROPOSED` expense event, its `FULL` payment option, and the `REQUESTS` row. Extra EMI/BNPL options
  can be added (process 5.0).
- Decide: `SELECT sp_decide(%s, %s)` with the configured as-of date, then read `DECISIONS`,
  `PLAN_ITEMS`, `V_FORECAST_HEADROOM`, `SPENDING_CHANGES`, `AUDIT_RECORDS`. Elapsed time measured
  around the call.
- Re-decide = calling `sp_decide` again (bitemporal versioning is inside the procedure).
- Audit: "Verify chain" → `sp_verify_audit_chain()`. "Tamper (demo)" → a clearly labelled endpoint
  that runs `UPDATE DECISIONS SET amount_safe_to_pay = 1 WHERE decision_id = %s` (same as run_tests).
- EXPLAIN panel: runs the master document's EXPLAIN query (with `enable_seqscan = off`, as in
  run_tests) and shows the plan.

### 6.5 Demo data

- `AS_OF_DATE=2026-11-20` in `.env` (blank = today), so the demo reproduces the master document's
  tested numbers (Appendix B: opening balance 33,510; Request 1 SAFE_NOW; Request 2 SPLIT_PLAN EMI 3 m;
  Request 3 WAIT, earliest 30 Dec 2026).
- Seed loads `schema.sql`, then builds the same user, profile, categories, currencies, rate and events
  as `test_data.sql`, but with one real Mongo document per event. The ledger/recurring/manual
  baseline is promoted at seed time. The SMS / e-mail / receipt records that exercise the precedence
  rules (duplicate SMS, pending debit/credit, bill cancellation, amendment, ...) are left in the inbox
  as `RECEIVED`, so the examiner processes them live through extractor + gate. After processing, the
  database state equals `test_data.sql` and decisions match Appendix B. Plus one prompt-injection SMS
  and one low-confidence message to show REJECTED and HELD.
- **What "matches Appendix B" means (Claude's call):** the *results* match — effective amounts, opening
  balance, the 90-day forecast, the three decisions, plan items, cuts, re-decide and tamper detection.
  Event ids and family roots may differ, because each event now has its own Mongo document and live
  SMS records attach to the already-seeded ledger row (the ledger record still wins every family, as in
  Appendix B). The three requests and their payment options are seeded in the same order as
  `test_data.sql`, so request ids 1–3 and option ids 1–6 (including "option 3" in decision 2) match.
  The examiner can still create new requests live.
- **Receipts (agreed earlier):** seeded receipts store the image in GridFS *and* its OCR output in
  `ocr.text`. `EXTRACTOR=rules` reads the stored OCR output; `EXTRACTOR=gemini` OCRs the image live.
  No local OCR engine (Tesseract) is installed.
- `seed --reset` drops and rebuilds both databases, so the demo can be rerun.

### 6.6 Frontend (one page, sections)

User picker / register form · Evidence inbox (raw doc → candidate → gate result → promoted event,
with Mongo doc and Postgres row side by side) · Events (from `V_EVENTS_FULL` joined with effective
flag) · Requests (create, add options, decide, re-decide) · Decision panel (outputs, schedule,
spending cuts, Chart.js 90-day chart with minimum line, elapsed ms, version history) · Audit (chain
table, verify, tamper) · EXPLAIN panel.

### 6.7 Testing

- `tests/test_sql.py`: loads `db/schema.sql` + `db/test_data.sql` into a scratch database and asserts
  the Appendix B results (precedence rows, 90 rows, three decisions, plan items, cuts, re-decide,
  tamper detection, the five constraint failures, index scan).
- `tests/test_gate.py`: each gate rule incl. injection text, low confidence, foreign ownership,
  duplicate same/other source, action mapping.
- `tests/test_extract.py`: rule extractor on each seeded message.
- `tests/test_flow.py`: seed → process the inbox through the API → decisions equal Appendix B.
- Tests need a reachable Postgres (`TEST_POSTGRES_URL`); Mongo uses in-memory.

### 6.8 Configuration (`.env`)

```
POSTGRES_URL=postgresql://buyorwait:buyorwait@localhost:5432/buyorwait
MONGO_URL=                 # empty = in-memory mongod (tonight); set for a real server (exam)
EXTRACTOR=rules            # rules | gemini
GEMINI_API_KEY=
AS_OF_DATE=2026-11-20      # empty = today
```

## 7. Left to the builder

Exact regex patterns and confidence scoring of the rule extractor; message wording of seeded
evidence; UI styling; endpoint names; Gemini model id and response schema details.
