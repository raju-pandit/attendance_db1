# attendance_db1

## Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (real env vars win)
uvicorn app.main:app --port 8000
```

Indexes are created at startup; no manual steps.

Notes
- Instants are epoch ms in the API and aware UTC datetimes in MongoDB (`tz_aware=True`).
- Rounding is half-up via `Decimal` (R4, R8); in pipelines it uses Decimal128 so float error cannot flip a half.
- Trend: a range of more than 92 days (counted inclusively) is a 422. Moving average is the mean of the already
  rounded daily rates.

Testing: `tests/check_api.py` seeds awkward data into a **throwaway** database, compares every analytics answer
with an independent Python implementation of the rules, and checks the races and explain plans.
See the docstring at the top of the file for how to run it.

Windows PowerShell setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set MONGO_URI to a reachable MongoDB instance.
uvicorn app.main:app --port 8000
```

MongoDB must be running and reachable before the API starts; startup creates its indexes.
