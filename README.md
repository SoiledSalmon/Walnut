# Buy or Wait?

An AI-powered financial decision agent for the HackerRank Orchestrate hackathon. Given a user's purchase or payment request, it determines whether the user should pay in full, pay partially, use installments, wait, or not proceed — while ensuring the balance never drops below the user's minimum over a 90-day forecast.

## Architecture

The system uses a **hybrid architecture**: a Gemini LLM interprets unstructured evidence (messages and images), while a deterministic Python engine handles all financial calculations, plan enumeration, and verification. Cases without unstructured evidence bypass the LLM entirely.

```
main.py → runner.py → data.py (load & validate)
                     → agent.py (Gemini evidence loop, 5 bounded tools)
                     → forecast.py (recurrence inference, 90-day cashflow)
                     → planner.py (capacity, plan enumeration, ranking)
                     → verify.py (independent schedule replay)
                     → store.py (SQLite checkpoints, token accounting)
```

### Core Modules

| Module | Responsibility |
|---|---|
| `data.py` | Typed CSV loading, user/request joins, SHA-256 case fingerprinting |
| `forecast.py` | Evidence validation, lifecycle reconciliation, recurrence detection, 90-day cashflow construction |
| `planner.py` | Baseline capacity calculation, exhaustive spending-change combinations (up to 3 streams), payment candidate enumeration and deterministic ranking |
| `verify.py` | Independent day-by-day balance replay, preference/schedule/amount contract checks |
| `agent.py` | Bounded Gemini tool loop with 5 allowlisted tools, rate limiting, retries |
| `store.py` | SQLite checkpointing, resumable runs, token usage accounting |
| `runner.py` | CLI orchestrator: run, replay, validate, package, check |

### Key Design Decisions

- **Decimal arithmetic everywhere** — Python `Decimal` with 2-decimal-place rounding eliminates IEEE 754 floating-point errors in monetary calculations.
- **Deterministic fast path** — Cases without messages or images skip the LLM entirely, producing verified results in sub-milliseconds.
- **Independent verification** — Every decision is replayed through a separate verifier before export, checking balance floor preservation at every day in the 90-day window.
- **Strict trust boundary** — The LLM cannot modify profile balances, preferences, minimums, or request amounts. Amended amounts must appear in cited evidence.
- **Certificate-based finalization** — The agent must obtain a cryptographic evaluation certificate from the deterministic engine before finalizing any decision.

## Setup

Python 3.11+ required. Development used Python 3.14.6.

```sh
python3 -m venv .venv
# Activate the environment
python3 -m pip install -r code/requirements.txt
```

Set `GEMINI_API_KEY` in the environment or in `code/.env`.

## Run

```sh
# Full evaluation run (250 requests)
python3 code/main.py

# Other commands
python3 code/main.py check                              # Validate inputs, no API calls
python3 code/main.py run --samples --run-id samples      # Run on 25 sample requests
python3 code/main.py validate --run-id final             # Validate output.csv
python3 code/main.py replay --run-id final               # Replay saved evidence, no API calls
python3 code/main.py package --run-id final               # Create code.zip
python3 -m pytest -c code/pytest.ini code/tests -q       # Run 32 regression tests
```

On Windows: use `.venv/Scripts/python.exe` instead of `python3`.

## Financial Assumptions

- The opening balance already reflects historical settled events — they inform forecasting but are not replayed.
- Monthly, semimonthly, and fixed-interval recurrence is inferred from 3+ observations.
- Pending/scheduled debits are reserved; pending credits and unrealized investments are excluded.
- Confirmed salary is counted on its settlement date. Final payroll terminates the prior stream.
- Variable expenses (groceries, transport, dining) use daily reserves at the 75th percentile rate.
- Variable earned income uses lower-quartile estimates. Bonuses, commissions, and windfalls are excluded from recurring salary.
- Foreign cash events use the supplied dated exchange rate with Decimal rounding.
- Capacity rounds down to cents; installment amounts and fees are preserved exactly from offers.

## Model Budget

| Parameter | Value |
|---|---|
| Models | `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite` |
| Max turns per case | 4 |
| Max tool calls per case | 8 |
| Max API attempts per case | 6 (including 2 transient retries) |
| Rate limiting | 12 RPM, 200K TPM per model |
| Daily limit | 450 attempts per model |

## Test Suite

32 regression tests covering: cash flow states, protected spending, financing fees, partial schedules, foreign currency conversion, one-cent boundaries, model-loop caps, cache invalidation, and certificate tampering.

```sh
python3 -m pytest -c code/pytest.ini code/tests -q
```

## Evaluation Results

Final run: 250/250 requests resolved. 615 API attempts (87 failed/retried), 6,866,898 tokens.

Sample accuracy (25 public examples): safe amount within 1¢: 3/25, status: 18/25, payment method: 19/25, schedule: 18/25, earliest date: 15/25, spending changes: 18/25. See `evaluation/sample_report.json` for details.

## Submission Artifacts

| File | Description |
|---|---|
| `code.zip` | Complete runnable solution with `evaluation/usage_report.md` |
| `output.csv` | 250 validated predictions |
| `chat_transcript.txt` | Development conversation log |
