"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

Layout of this single file:
  1. configuration and indexes
  2. pure helpers (time zone, R1-R5 rules, serialization)
  3. request models
  4. employees / attendance endpoints
  5. analytics pipelines + endpoints
  6. admin explain endpoint (re-uses the exact same query/pipeline builders)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# tz_aware=True makes PyMongo hand back timezone-aware (UTC) datetimes, so we never
# mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE = ["ABSENT", "LEAVE"]
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # contract bounds for instants


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def ensure_index(collection, keys: list, name: str, unique: bool = False) -> None:
    """Create an index; tolerate an equivalent one that already exists under another name."""
    try:
        collection.create_index(keys, unique=unique, name=name)
    except OperationFailure as exc:
        if exc.code != 85:  # 85 = IndexOptionsConflict: same keys, different name/options
            raise
        for info in collection.index_information().values():
            same_keys = [tuple(k) for k in info["key"]] == [tuple(k) for k in keys]
            if same_keys and bool(info.get("unique", False)) == unique:
                return  # an equivalent index is already there
        raise


def create_indexes() -> None:
    """Idempotent: safe to run on every start."""
    # employees
    ensure_index(db.employees, [("emp_code", 1)], "uq_emp_code", unique=True)
    ensure_index(db.employees, [("department", 1), ("emp_code", 1)], "ix_dept_code")
    ensure_index(db.employees, [("department", 1), ("joined_on", 1)], "ix_dept_joined")
    ensure_index(db.employees, [("joined_on", 1)], "ix_joined")
    # attendance_logs
    ensure_index(db.attendance_logs, [("emp_code", 1), ("date", 1)], "uq_emp_date", unique=True)
    ensure_index(db.attendance_logs, [("date", -1), ("emp_code", 1)], "ix_date_emp")
    ensure_index(
        db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)], "ix_status_date_emp"
    )
    ensure_index(db.attendance_logs, [("emp_code", 1), ("punch_in", -1)], "ix_emp_punchin")


# --------------------------------------------------------------------------- #
# Helpers: instants, calendar values
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1). Range is checked by the models."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> epoch ms using integer arithmetic (no float rounding)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _valid_date(value: str) -> date_type:
    """Strict YYYY-MM-DD. Raises ValueError otherwise."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date_type.fromisoformat(value)


def parse_date(value: str, name: str) -> date_type:
    try:
        return _valid_date(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be a valid YYYY-MM-DD date")


def month_bounds(month: str) -> tuple[str, str, int, int]:
    """'2026-07' -> ('2026-07-01', '2026-07-31', 2026, 7). 422 on anything else."""
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}", year, mon


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding, done on Decimal(str(x)) so binary floats cannot interfere."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers: business rules R1-R5 (the ONLY place they are implemented)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: time, shift_end: time) -> date_type:
    """R1: IST date of the punch-in; overnight early-morning punches belong to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.time() < shift_end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, att_date: date_type, shift_start: time) -> int:
    """R2: strictly more than 10:00 after shift start -> floor minutes since shift start."""
    start = datetime.combine(att_date, shift_start, tzinfo=IST)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals half-up (Decimal, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, att_date: date_type, shift_start: time, shift_end: time) -> int:
    """R3: floor minutes after shift end, only if >= 30. Overnight shifts end the next day."""
    end = datetime.combine(att_date, shift_end, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    seconds = int((punch_out - end).total_seconds())
    return seconds // 60 if seconds >= 1800 else 0


def derive(emp: dict, status: str, punch_in, punch_out, att_date: date_type) -> dict:
    """All derived fields for a record (R2-R5); matches the table in DATA_MODEL.md."""
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return zero
    s, e = hhmm(emp["shift_start"]), hhmm(emp["shift_end"])
    zero["late_minutes"] = compute_late_minutes(punch_in, att_date, s)
    if punch_out is None:
        return zero
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": zero["late_minutes"],
        "overtime_minutes": compute_overtime(punch_out, att_date, s, e),
        "half_day": hours < Decimal("4.50"),  # R5 uses the ROUNDED value
    }


def check_duration(punch_in: datetime, punch_out: datetime) -> None:
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if seconds > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# Helpers: serialization (Mongo document -> API shape). One place, used everywhere.
# --------------------------------------------------------------------------- #
def _api_value(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_history_entry(entry: dict) -> dict:
    changes = {
        key: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
        for key, change in (entry.get("changes") or {}).items()
    }
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_record(doc: dict) -> dict:
    """Explicit field list: never leaks _id, applies defaults for legacy documents."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def page_args(page: int, page_size: int) -> tuple[int, int]:
    return min((page - 1) * page_size, 10**12), page_size  # clamp: huge page numbers -> empty page, not a 500


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Request models (validation = 422, before any database work)
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # no floats, strings or bools
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        _valid_date(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields (work_hours, ...) are not declared, so a client cannot set them (ignored)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    skip, limit = page_args(page, page_size)
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", 1).skip(skip).limit(limit)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    s, e = hhmm(emp["shift_start"]), hhmm(emp["shift_end"])
    att_date = attendance_date(ts, s, e)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, ts, None, att_date),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): one 201, the rest 409
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    check_duration(record["punch_in"], ts)  # 422 if <= punch_in or > 24h

    att_date = _valid_date(record["date"])
    derived = derive(emp, record["status"], record["punch_in"], ts, att_date)
    derived.pop("late_minutes")  # late_minutes was fixed at punch-in; punch-out leaves it alone

    # Atomic compare-and-set: only a still-open record can be closed, so of two simultaneous
    # punch-outs exactly one matches and the other gets None back -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")
    return serialize_record(updated)


def attendance_find_args(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Filter/sort/pagination of GET /attendance. Shared with the explain endpoint."""
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    if status is not None and status not in PRESENCE + NON_PRESENCE:
        raise HTTPException(status_code=422, detail="invalid status")

    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    skip, limit = page_args(page, page_size)
    return {"filter": query, "sort": [("date", -1), ("emp_code", 1)], "skip": skip, "limit": limit}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    args = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(args["filter"])
    cursor = (
        db.attendance_logs.find(args["filter"])
        .sort(args["sort"])
        .skip(args["skip"])
        .limit(args["limit"])
    )
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    record_date = parse_date(date, "date")
    emp = get_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record["status"]
    old_in, old_out = record.get("punch_in"), record.get("punch_out")
    new_status = body.status or old_status

    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot carry punch times")
        new_in = new_out = None  # these statuses clear both punch times
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires punch_in")
        s, e = hhmm(emp["shift_start"]), hhmm(emp["shift_end"])
        if attendance_date(new_in, s, e) != record_date:  # R1, incl. overnight shifts
            raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
        if new_out is not None:
            check_duration(new_in, new_out)

    # Fields the client actually changed.
    changes: dict = {}
    for field, old, new in (
        ("status", old_status, new_status),
        ("punch_in", old_in, new_in),
        ("punch_out", old_out, new_out),
    ):
        if old != new:
            changes[field] = {"from": old, "to": new}  # datetimes stay BSON dates in MongoDB
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # Recompute derived fields from the final values and record those that changed too.
    derived = derive(emp, new_status, new_in, new_out, record_date)
    old_derived = {
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    for field, new in derived.items():
        if old_derived[field] != new:
            changes[field] = {"from": old_derived[field], "to": new}

    entry = {
        "at": now_utc(),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }

    # Compare-and-set on the fields we read: if a concurrent punch-out or correction changed
    # the record meanwhile, nothing matches and the loser gets 409. $push is atomic, so a
    # history entry can never be lost or overwritten.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out},
        {
            "$set": {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived},
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared aggregation expressions
# --------------------------------------------------------------------------- #
def _f(prefix: str, field: str) -> str:
    return f"${prefix}{field}"


def late_expr(prefix: str = ""):
    return {"$ifNull": [_f(prefix, "late_minutes"), 0]}  # missing means 0


def is_present_expr(prefix: str = ""):
    return {"$in": [_f(prefix, "status"), PRESENCE]}  # R6


def is_weekday_expr(prefix: str = ""):
    day = {"$dateFromString": {"dateString": _f(prefix, "date"), "format": "%Y-%m-%d"}}
    return {"$in": [{"$dayOfWeek": day}, [2, 3, 4, 5, 6]]}  # Sunday = 1, so Mon-Fri = 2..6


def present_value_expr(prefix: str = "", weekdays_only: bool = True):
    """1 per present record, 0.5 for a half day (R5, R6); R7 limits it to Mon-Fri when asked."""
    cond = is_present_expr(prefix)
    if weekdays_only:
        cond = {"$and": [cond, is_weekday_expr(prefix)]}
    half = {"$ifNull": [_f(prefix, "half_day"), False]}  # missing means false
    return {"$cond": [cond, {"$cond": [half, 0.5, 1]}, 0]}


def is_late_expr(prefix: str = ""):
    return {"$cond": [{"$gt": [late_expr(prefix), 0]}, 1, 0]}


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_value_expr()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": is_late_expr()},
                "total_late_minutes": {"$sum": late_expr()},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def count_working_days(first: date_type, last: date_type) -> int:
    """R7: Monday-Friday between two dates, inclusive."""
    if first > last:
        return 0
    days = (last - first).days + 1
    return sum(1 for i in range(days) if (first + timedelta(days=i)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    _, end, year, mon = month_bounds(month)
    emp = get_employee(emp_code)

    joined = _valid_date(emp["joined_on"])
    working_days = count_working_days(max(joined, date_type(year, mon, 1)), _valid_date(end))

    result = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    stats = result[0] if result else {}
    present = stats.get("present_days", 0)

    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present)) * 100 / Decimal(working_days), 2)  # R8

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary (employees -> lookup -> attendance_logs)
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end, _, _ = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the end of the month
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Keep employees with zero logs: they still count towards headcount.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": present_value_expr("logs.")},
                # $avg skips nulls: average over RECORDS that are present-status with a work_hours value
                "avg_work_hours": {
                    "$avg": {"$cond": [is_present_expr("logs."), "$logs.work_hours", None]}
                },
                "late_count": {"$sum": is_late_expr("logs.")},
                "total_late_minutes": {"$sum": late_expr("logs.")},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    items = list(db.employees.aggregate(build_department_summary_pipeline(month, department)))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard (standard competition ranking via $rank)
# --------------------------------------------------------------------------- #
def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end, _, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": late_expr()},
                "late_count": {"$sum": is_late_expr()},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},  # logs whose employee does not exist are dropped
    ]
    if department is not None:
        # filter BEFORE ranking so ranks are computed inside the department
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # $rank = 1, 2, 2, 4: ties share a rank and the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend ($densify gap filling + $setWindowFields moving average)
# --------------------------------------------------------------------------- #
def department_emp_codes(department: str) -> list:
    cursor = db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
    return [d["emp_code"] for d in cursor]


def parse_trend_range(from_date: str, to_date: str) -> tuple[date_type, date_type]:
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")
    return start, end


def build_trend_pipeline(department: str, start: date_type, end: date_type, emp_codes: list) -> list:
    day0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    day_end_exclusive = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    return [
        # 1. index-backed scan of this department's records in the range
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                "emp_code": {"$in": emp_codes},
            }
        },
        # 2. one row per day that has records
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": present_value_expr(weekdays_only=False)},
                "late_count": {"$sum": is_late_expr()},
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # 3. gap filling: one row for EVERY calendar day in [from, to]
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [day0, day_end_exclusive]},
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
            }
        },
        # 4. headcount per day (R9): department employees who had joined by that day
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 5. 7-day moving average over the current row and up to 6 earlier rows in the range.
        # $avg ignores null rates; the first rows naturally have a shorter window.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(from_date, to_date)
    codes = department_emp_codes(department)
    if not codes:
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(db.attendance_logs.aggregate(build_trend_pipeline(department, start, end, codes)))
    for item in items:
        item["present_count"] = round_half_up(item["present_count"], 2)
        for field in ("attendance_rate", "moving_avg_7d"):
            if item.get(field) is not None:
                item[field] = round_half_up(item[field], 4)  # rates: 4 decimals
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain. Uses the SAME builders as the endpoints above.
# --------------------------------------------------------------------------- #
def _run_explain(command: dict) -> dict:
    plan = db.command({"explain": command, "verbosity": "executionStats"})
    # Explain output can hold BSON types (Timestamp, Binary, ...) that plain JSON cannot encode.
    return json.loads(json_util.dumps(plan))


def _require(**values) -> None:
    missing = [k for k, v in values.items() if v is None or v == ""]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        args = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
        command = {
            "find": "attendance_logs",
            "filter": args["filter"],
            "sort": dict(args["sort"]),
            "skip": args["skip"],
            "limit": args["limit"],
        }
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            pipeline, collection = build_employee_monthly_pipeline(emp_code, month), "attendance_logs"
        elif endpoint == "department_summary":
            _require(month=month)
            pipeline, collection = build_department_summary_pipeline(month, department), "employees"
        elif endpoint == "late_leaderboard":
            _require(month=month)
            pipeline, collection = build_leaderboard_pipeline(month, limit, department), "attendance_logs"
        else:  # department_trend
            _require(department=department, **{"from": from_date, "to": to_date})
            start, end = parse_trend_range(from_date, to_date)
            pipeline = build_trend_pipeline(department, start, end, department_emp_codes(department))
            collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    return {"endpoint": endpoint, "collection": collection, "explain": _run_explain(command)}