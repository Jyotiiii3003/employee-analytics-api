# REVIEW.md

Defects found in the starter `app/main.py`. "Fix" names the function or model in the new file.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `health` | Always returns 200 and never contacts MongoDB. | Stop MongoDB: `/health` still says ok, but the contract requires 503. | Ping the database; `PyMongoError` becomes 503. |
| 2 | `create_employee` | Check-then-insert (`find_one`, then `insert_one`) with no unique index. | Two parallel creates with the same `emp_code` both return 201. | Unique index `uq_emp_code`; `DuplicateKeyError` becomes 409. |
| 3 | `EmployeeIn` | No validation: any `emp_code`, email, shift or `joined_on` string is accepted, and `shift_start == shift_end` is allowed. | `emp_code="X"`, `shift_start="25:99"` or `joined_on="nope"` returns 201 instead of 422. Bad dates later break analytics. | Pydantic patterns and length limits, a real-date validator for `joined_on`, and a model validator for equal shifts. |
| 4 | `create_employee` | `created_at` is `datetime.now()`: naive local server time, with microseconds, returned as an ISO string instead of epoch milliseconds. | Response shows a string, and the stored value depends on the server time zone. | UTC time truncated to seconds, serialized as epoch ms. |
| 5 | `list_employees` | `skip = page * page_size` skips the whole first page. | `page=1` never returns the first 20 employees. | `(page - 1) * page_size`. |
| 6 | `list_employees` | `total` counts the whole collection (`count_documents({})`), and there is no sort. | `department=X` returns the wrong `total`; page order is unstable. | Count with the same filter; sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100`. |
| 7 | `punch_in` | Unknown employee: `emp["shift_start"]` on `None` raises `TypeError`. | Punch in with an unknown `emp_code` returns 500 instead of 404. | `get_employee` raises 404 first. |
| 8 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` gives naive local time and keeps fractional seconds; the date comes from that, not from IST; overnight shifts are ignored. | On a UTC server, a 23:30 UTC punch-in gets the wrong IST date. A 01:00 punch-in on a 22:00-06:00 shift gets the wrong date. | `ms_to_dt` (UTC, truncated to whole seconds) and `attendance_date` (IST, R1, overnight rule). |
| 9 | `punch_in` | `if body.punched_at` treats `0` as missing; the value is not range-checked; `status` accepts any string, including `ABSENT`. | `punched_at=5` is accepted; `status="ABSENT"` returns 201. | `EpochMs` strict integer between 1e11 and 4102444800000; `status` limited to PRESENT, WFH, ON_DUTY. |
| 10 | `punch_in` | Check-then-insert race, and the check is not backed by an index. | Two simultaneous requests both return 201 and create two records. | Unique `(emp_code, date)` index; duplicate key becomes 409. |
| 11 | `punch_in` response | Returns `id` (the internal `_id`) and `punch_in` as a datetime. | Response has `id` and an ISO string, which breaks the contract. | `serialize_record` with an explicit field list; instants become epoch ms. |
| 12 | `compute_late_minutes` | Floors to whole minutes first, then tests `> 10`. It also uses the punch-in's own date and time zone, and ignores overnight shifts. | Shift 09:30, punch-in 09:40:01 gives 0, but R2 says 10. | Compare seconds with `> 600`, then floor; use IST shift start on the attendance date. |
| 13 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus float error. | 09:00:00 to 09:07:30 is 0.125 h; `round` gives 0.12, but half-up (R4) gives 0.13. | `Decimal` division with `ROUND_HALF_UP`. |
| 14 | `compute_overtime` | No 30-minute minimum, no overnight shifts (the end is placed on the record date), and it is never called by any endpoint. | 5 minutes after shift end returns 5, but R3 says 0. | One `compute_overtime`: next-day end for overnight shifts, counted only if at least 30 minutes. |
| 15 | `list_attendance` | Loads every matching document into memory, then sorts and paginates in Python. | Slow and memory-heavy at 100,000 records; no database index is used. | Filter, sort, skip and limit in MongoDB, backed by indexes. |
| 16 | `list_attendance` | Sorts by `date` only (no `emp_code` tie-break), leaks `id`, returns datetimes, does not validate dates, status or paging, and does not default missing legacy fields. | Same-date rows change order between pages; `date_from=banana` is accepted. | Sort `(date desc, emp_code asc)`; shared `serialize_record` with defaults; 422 on bad input. |
| 17 | startup | No indexes are created at all. | The grader's seeded database has only `_id`, so every query is a collection scan. | `create_indexes()` runs from the app lifespan; names and keys are fixed so it is idempotent. |
| 18 | `MongoClient` | Default PyMongo returns naive datetimes, so UTC and local time get mixed. | Epoch values off by the server's UTC offset. | `tz_aware=True` plus `dt_to_ms` / `ms_to_dt` at the boundary. |

## Looked at and decided it was not a defect

- `load_dotenv()` uses its default `override=False`, so real environment variables already win over `.env`.
- Plain `def` endpoints with PyMongo: FastAPI runs them in a thread pool, so they do not block the event loop.
- The `IST` fixed offset is correct because India has no daylight saving time.
