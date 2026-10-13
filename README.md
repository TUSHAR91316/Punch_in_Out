# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing an employee attendance lifecycle, audit tracking, and analytical reporting, strictly compliant with OpenAPI specifications and business rules R1–R10.

---

## Quickstart

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 recommended)

### 1. Environment Setup
```bash
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Ensure `MONGO_URI` and `MONGO_DB` are configured in `.env` (or set as environment variables).

### 2. Seed Database (Sample Data)
```bash
python sample_seed.py
```

### 3. Run the Server
```bash
uvicorn app.main:app --port 8000 --reload
```
The application initializes database indexes idempotently at startup via FastAPI lifespan events and is ready to handle requests once `GET /health` returns HTTP 200.

---

## Architecture & Implementation Notes

- **Application Structure**: All application code resides in `app/main.py` per assignment requirements.
- **Index Lifecycle**: All unique constraints (`emp_code`, `(emp_code, date)`) and multi-key query indexes are created idempotently on startup.
- **Concurrency & Atomicity**: Punch-in concurrency races are resolved at the database index level (`DuplicateKeyError` -> HTTP 409). Punch-out uses atomic `find_one_and_update` with status pre-conditions to prevent duplicate punch-outs. Regularizations employ optimistic locking against audit history.
- **Pure Database Aggregations**: All analytics (`/analytics/...`) are implemented as pure MongoDB aggregation pipelines (`$group`, `$lookup`, `$densify`, `$setWindowFields`) without Python-level iteration.
- **Explain Tooling**: The `/admin/explain/{endpoint}` route executes with `executionStats` verbosity on target query plans, confirming index coverage (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

---

## Project Artifacts
- `app/main.py`: Complete application implementation.
- `requirements.txt`: Minimal dependencies for the service.
- `REVIEW.md`: Detailed defect analysis and remediations for starter code.
- `DECISIONS.md`: Design rationale for indexing, concurrency, competition ranking, headcount aggregation, and 100x scaling.
