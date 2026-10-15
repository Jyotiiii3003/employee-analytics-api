# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee punch-in / punch-out, manual attendance corrections with an audit trail, and monthly analytics built on MongoDB aggregation pipelines. This is my submission for the HROne Software Engineer Trainee assignment.

All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (the analytics use `$densify`, `$setWindowFields` and `$rank`)
- Git

## Setup and run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` file is also loaded, but real environment variables take priority. No `.env` is committed.
- A local MongoDB can be started with `docker run -d -p 27017:27017 mongo:7`, or use a free Atlas cluster.
- Indexes are created automatically at startup. Creation is idempotent, so restarting is safe.
- `GET /health` returns `{"status": "ok"}` when MongoDB is reachable, and 503 otherwise.
- Interactive docs: `http://localhost:8000/docs`.

## What is implemented

| Area | Endpoints |
|---|---|
| System | `GET /health` |
| Employees | `POST /employees`, `GET /employees` |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}` |
| Analytics | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` |
| Admin | `GET /admin/explain/{endpoint}` |

The business rules R1–R10 from the contract are implemented: IST attendance dates including overnight shifts and truncation to whole seconds, late minutes with the grace period, overtime with the 30-minute minimum, work hours and half days with half-up rounding, presence counting, Monday–Friday working days (counted from `joined_on` for mid-month joiners), headcount that includes employees with no logs, and pagination (`page` from 1, `page_size` up to 100, `total` reflecting the filters).

## Design notes

- **Concurrency.** Uniqueness is enforced by unique indexes (`emp_code`; `emp_code` + `date`), not by check-then-insert. Punch-out and corrections use atomic compare-and-set updates, so simultaneous requests produce one success and 409 for the rest. Correction history is appended with `$push`.
- **Time handling.** Instants are stored as BSON dates (UTC) and exchanged as epoch milliseconds. PyMongo runs with `tz_aware=True`, and conversion happens in one place at the API boundary.
- **Analytics** run as aggregation pipelines inside MongoDB and read the stored derived values (`work_hours`, `late_minutes`, ...) rather than recomputing them. The trend endpoint uses `$densify` for gap filling and `$setWindowFields` for the 7-day moving average. The leaderboard uses `$rank` for tied ranks.
- **Explain.** `/admin/explain/{endpoint}` builds the query or pipeline with the same builder functions as the real endpoint, so the plan shown is the plan that runs.

Written answers: `REVIEW.md` (defects found in the starter code) and `DECISIONS.md` (design decisions).

## Assumptions

- Punch-out closes the most recent record whose `punch_in` is at or before `punched_at`. If there is no such record (including a `punched_at` earlier than every punch-in) the response is 404; if that record is already closed it is 409; if the duration is zero or over 24 hours it is 422.
- `attendance_pct` is rounded to 2 decimals (R8); `attendance_rate` and `moving_avg_7d` to 4 decimals. The moving average is computed from the unrounded daily rates.
- A PATCH that sends `null` for `punch_in` or `punch_out` is treated as "not supplied".

## Missing / not verified

- There is no automated test suite. I checked behaviour manually against the sample data (`sample_data/`) on MongoDB 6 in Docker.

