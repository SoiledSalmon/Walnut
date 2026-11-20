# Buy or Wait?

A hybrid SQL–NoSQL database system for safe financial affordability decisions and 90-day cash-flow forecasting.
DBMS Lab (CD252IA), RV College of Engineering. Team: Ananth G Karanth, Anurag G Kharvi, Manoj Gupta D B.

Given a purchase request, the system answers **Safe Now**, **Split Plan** or **Wait**. It also returns the safe amount to pay
today, the recommended payment plan, the earliest full-payment date, the 90-day balance and savings trajectory, and
any discretionary spending cuts. All decision logic runs **inside PostgreSQL**. Every decision is versioned and sealed in
a SHA-256 hash chain.

## Architecture

```
Web page (frontend/)  ──JSON──▶  FastAPI service (buyorwait/api.py)
                                    │
             ┌──────────────────────┴───────────────────────┐
             ▼                                              ▼
 MongoDB (untrusted evidence)                 PostgreSQL (verified facts + all decision logic)
   raw_evidence     SMS / e-mail / receipt      16 BCNF relations (db/schema.sql)
   candidate_events extractor output + gate     V_EFFECTIVE_EVENTS  precedence rules
   GridFS           receipt images              fn_forecast_90      recursive CTE + window functions
             │                                  sp_decide           ranking, status, plan, cuts, audit
             └── extractor ─▶ validation gate ─▶ parameterised INSERT (source_doc_id = Mongo _id)
```

| Module | Role (DFD process) |
|---|---|
| `buyorwait/extract.py` | NLP/OCR (2.0). `RuleExtractor` works offline. `GeminiExtractor` is used when `EXTRACTOR=gemini`. |
| `buyorwait/gate.py` | Validation gate and promotion (3.0): type, range, confidence, ownership, duplicates, action mapping. |
| `buyorwait/services.py` | Users (1.0), options (5.0), requests (6.0), decisions via `sp_decide` (7.0), audit and EXPLAIN (8.0). |
| `buyorwait/seed.py` | Demo data: the master document's test scenario, run through the real pipeline. |
| `db/schema.sql`, `db/test_data.sql`, `db/run_tests.sql` | The master document's SQL, unchanged. |

## Setup (Windows)

1. Install PostgreSQL (19, tested; 16+ works) with the command line tools. In `psql` as `postgres`, run:
   ```sql
   CREATE ROLE buyorwait LOGIN PASSWORD 'choose-one' CREATEDB;
   CREATE DATABASE buyorwait OWNER buyorwait;
   ```
2. Install the Python dependencies (Python 3.11+):
   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and put your password into `POSTGRES_URL` and `TEST_POSTGRES_URL`.

## Run the demo

```powershell
.venv\Scripts\python.exe main.py
```

Open http://127.0.0.1:8000.

With `MONGO_URL` empty, the app starts an in-memory MongoDB and **rebuilds both databases with the demo data every time it
starts**, so the two stores always match. Anything added during a session is lost on restart.

Suggested walk-through:
1. **Evidence inbox.** Press *Process* on each item from top to bottom. The pipeline cards show the MongoDB document,
   the extracted candidate with its gate result, and the PostgreSQL row. The second-to-last SMS (a prompt-injection
   attempt) is **rejected**. "Paid 450 to Ravi" is **held** for confirmation.
2. **Events.** Families show the precedence rules: the ledger beats SMS, the duplicate SMS is excluded, the pending
   credit is excluded, the cancelled bill is excluded, and the amended bill uses ₹3,500.
3. **Requests.** Decide #1, #2 and #3. You get Safe Now; Split Plan (EMI 3 months: ₹15,649 + ₹15,450 + ₹15,450); and
   Wait (earliest 30 Dec 2026, cut Dining by ₹2,200 and Shopping by ₹3,000). These are the numbers in Appendix B of the
   master document. Decide *before* confirming the held ₹450 message, which would change them.
4. **Re-decide** #2 to see the old version closed (bitemporal).
5. **Audit.** *Tamper (demo)* on a decision, then *Verify chain*. The edited record is flagged.
6. **Inside the database.** The EXPLAIN plan shows the `(user_id, event_date)` index scan.

## Settings (`.env`)

| Key | Meaning |
|---|---|
| `POSTGRES_URL` | App database. |
| `TEST_POSTGRES_URL` | Scratch database for `pytest`. It is dropped and recreated. |
| `MONGO_URL` | Empty means in-memory MongoDB (demo). Set it, e.g. `mongodb://localhost:27017`, to use a real server, then run `python -m buyorwait.seed --reset` once. |
| `EXTRACTOR` | `rules` (offline) or `gemini` (live NLP/OCR; needs `GEMINI_API_KEY`). |
| `AS_OF_DATE` | Decision date. `2026-11-20` reproduces Appendix B. Leave it empty to use today. |

**Moving to the practical exam setup** only changes settings. Run MongoDB as a server (or add a `docker-compose.yml`
with `postgres` and `mongo`), set `MONGO_URL`, seed once, and optionally set `EXTRACTOR=gemini`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

- `tests/test_sql.py` reproduces the master document's Appendix B directly on PostgreSQL.
- `tests/test_flow.py` checks that seed plus live inbox processing reaches the same Appendix B decisions through the gate.
- Gate, extractor, service and API tests cover the rest.
