# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env
uvicorn app.main:app --port 8000
```
Needs Python 3.11+ and MongoDB 6.0+ (uses `$densify` and `$setWindowFields`). Indexes are created at startup.

## Implemented
All endpoints in `openapi.yaml`: employees, punch-in / punch-out, attendance list, PATCH regularization with history, the four analytics endpoints, and `/admin/explain/{endpoint}`. `REVIEW.md` covers Part A and `DECISIONS.md` covers Part C.

## Assumptions
- Punch-out closes the most recent record whose `punch_in` is at or before `punched_at`: 404 if there is none, 409 if it is already closed.
- `attendance_pct` is rounded to 2 decimals (R8); `attendance_rate` and `moving_avg_7d` to 4. The moving average uses the unrounded daily rates.


