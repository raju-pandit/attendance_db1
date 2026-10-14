"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

Contract: openapi.yaml. Stored shapes: DATA_MODEL.md. Business rules are referenced inline as R1-R10.

How time is handled in this file
- At the API boundary every instant is an integer of epoch milliseconds.
- Inside, every instant is a timezone-aware UTC datetime. The Mongo client uses tz_aware=True, so PyMongo
  never hands us a naive datetime.
- Calendar dates (date, joined_on) and shift times (HH:MM) stay plain strings, exactly as stored.
- Rounding is always half-up through Decimal, never Python's round() (which rounds half to even).
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# =========================================================================== #
# 1. Configuration
# =========================================================================== #
load_dotenv()  # never overrides variables that are already set in the environment

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # datetimes come back aware (UTC), never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60             # R2
MIN_OVERTIME_MINUTES = 30           # R3
HALF_DAY_HOURS = 4.5                # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]   # R6

# strict=True rejects floats, strings and booleans; the bounds reject seconds-looking values.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

# [0-9] instead of \d: pydantic's regex engine treats \d as any Unicode digit.
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# Index names, so queries can hint the exact index they were designed for.
IDX_EMP_CODE = "uniq_employee_emp_code"
IDX_EMP_DEPT_CODE = "department_emp_code"
IDX_EMP_JOINED = "joined_on"
IDX_EMP_DEPT_JOINED = "department_joined_on"
IDX_ATT_EMP_DATE = "uniq_attendance_employee_date"
IDX_ATT_DATE = "date_desc_emp_code"
IDX_ATT_STATUS = "status_date_desc_emp_code"

ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


# =========================================================================== #
# 2. Startup, indexes and error handling
# =========================================================================== #
def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    emp, att = db.employees, db.attendance_logs
    emp.create_index([("emp_code", 1)], unique=True, name=IDX_EMP_CODE)
    emp.create_index([("department", 1), ("emp_code", 1)], name=IDX_EMP_DEPT_CODE)
    emp.create_index([("joined_on", 1)], name=IDX_EMP_JOINED)
    emp.create_index([("department", 1), ("joined_on", 1)], name=IDX_EMP_DEPT_JOINED)
    att.create_index([("emp_code", 1), ("date", 1)], unique=True, name=IDX_ATT_EMP_DATE)
    att.create_index([("date", -1), ("emp_code", 1)], name=IDX_ATT_DATE)
    att.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name=IDX_ATT_STATUS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": f"Database error: {type(exc).__name__}"})


def invalid(msg: str, *loc: str) -> RequestValidationError:
    """A 422 with FastAPI's default body shape, for rules a schema cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": loc or ("body",), "msg": msg, "input": None}])


# =========================================================================== #
# 3. Time helpers (R1)
# =========================================================================== #
def from_ms(ms: int) -> datetime:
    """Epoch millis -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:            # defensive: BSON dates are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate(dt: datetime) -> datetime:
    """Seeded values may carry milliseconds; R1 says calculations use whole seconds."""
    return dt.replace(microsecond=0)


def check_date(value: Optional[str], *loc: str) -> Optional[str]:
    """Pattern is checked by FastAPI; this rejects impossible dates such as 2026-02-30."""
    if value is None:
        return None
    try:
        Date.fromisoformat(value)
    except ValueError:
        raise invalid("must be a real calendar date YYYY-MM-DD", *loc)
    return value


def hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def is_overnight(emp: dict) -> bool:
    return hhmm(emp["shift_end"]) <= hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST date of the punch-in, except an overnight-shift punch before shift_end
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end for the shift that starts on date_str (end moves to the next day if overnight)."""
    day = Date.fromisoformat(date_str)
    start = datetime.combine(day, hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, hhmm(emp["shift_end"]), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


# =========================================================================== #
# 4. Business rules R2-R5 and rounding (R8)
# =========================================================================== #
def half_up(value: Any, places: int) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    shift_start, _ = shift_bounds(date_str, emp)
    seconds = int((truncate(punch_in) - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded half-up to 2 decimals."""
    seconds = int((truncate(punch_out) - truncate(punch_in)).total_seconds())
    hours = half_up(Decimal(seconds) / Decimal(3600), 2)
    if hours is None:
        raise ValueError("work hours could not be calculated")
    return hours


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: floor minutes after shift end, counted only when at least 30."""
    _, shift_end = shift_bounds(date_str, emp)
    minutes = int((truncate(punch_out) - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   date_str: str, emp: dict) -> dict[str, Any]:
    """All four derived fields, per the table in DATA_MODEL.md section 3."""
    out: dict[str, Any] = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp)
        out["half_day"] = hours < HALF_DAY_HOURS        # R5 uses the rounded value
    return out


# =========================================================================== #
# 5. Serialization (Mongo document -> API JSON)
# =========================================================================== #
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def stored_state(rec: dict) -> dict[str, Any]:
    """The tracked fields of a stored record, with legacy gaps filled in (missing -> defaults)."""
    return {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


def _api_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def serialize_history(entry: dict) -> dict:
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def serialize_record(rec: dict) -> dict:
    state = stored_state(rec)
    return {
        "emp_code": rec["emp_code"],
        "date": rec["date"],
        "status": state["status"],
        "punch_in": to_ms(state["punch_in"]),
        "punch_out": to_ms(state["punch_out"]),
        "work_hours": state["work_hours"],
        "late_minutes": int(state["late_minutes"]),
        "overtime_minutes": int(state["overtime_minutes"]),
        "half_day": state["half_day"],
        "history": [serialize_history(h) for h in rec.get("history") or []],
    }


def serialize_employee(emp: dict) -> dict:
    return {
        "emp_code": emp["emp_code"],
        "name": emp.get("name"),
        "email": emp.get("email"),
        "department": emp.get("department"),
        "shift_start": emp.get("shift_start"),
        "shift_end": emp.get("shift_end"),
        "joined_on": emp.get("joined_on"),
        "created_at": to_ms(emp.get("created_at")),
    }


# =========================================================================== #
# 6. Request models
# =========================================================================== #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, value: str) -> str:
        Date.fromisoformat(value)     # ValueError -> 422
        return value

    @model_validator(mode="after")
    def shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = Field(
        default=None,
        description="Epoch milliseconds, not seconds. Omit this field to use the current time.",
    )


class RegularizeRequest(BaseModel):
    """Derived fields sent by a client are ignored (unknown keys are dropped)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        # Runs only for fields the client actually sent. Omit a field to leave it unchanged.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


# =========================================================================== #
# 7. Lookups
# =========================================================================== #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp


def department_members(department: str) -> list[dict]:
    return list(
        db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1})
        .hint(IDX_EMP_DEPT_CODE)
    )


def month_bounds(month: str) -> tuple[str, str]:
    first = Date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(first: str, last: str, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri between first and last inclusive, starting from joined_on for mid-month joiners."""
    start = Date.fromisoformat(max(first, joined_on or first))
    end = Date.fromisoformat(last)
    days = 0
    while start <= end:
        days += start.weekday() < 5
        start += timedelta(days=1)
    return days


# =========================================================================== #
# 8. Aggregation expressions. `p` is the field prefix: "$" for a log document,
#    "$logs." for a log joined onto an employee.
# =========================================================================== #
def is_presence(p: str) -> dict:
    return {"$in": [{"$ifNull": [f"{p}status", None]}, PRESENCE_STATUSES]}


def presence_value(p: str) -> dict:
    """R5/R6: 1 for a present record, 0.5 for a present half day, 0 otherwise."""
    return {"$cond": [is_presence(p), {"$cond": [{"$eq": [f"{p}half_day", True]}, 0.5, 1]}, 0]}


def weekday_presence_value(p: str) -> dict:
    """R7: present_days only counts Mon-Fri records."""
    weekday = {"$isoDayOfWeek": {"$dateFromString": {"dateString": f"{p}date", "format": "%Y-%m-%d"}}}
    return {"$cond": [{"$and": [is_presence(p), {"$lte": [weekday, 5]}]}, presence_value(p), 0]}


def is_late(p: str) -> dict:
    return {"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}


def has_work_hours(p: str) -> dict:
    return {"$and": [is_presence(p), {"$isNumber": f"{p}work_hours"}]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding in the database, on Decimal128 so 0.12345 does not become 0.1234 through float error."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


# =========================================================================== #
# 9. Query builders. The endpoints AND /admin/explain call these, so the explained
#    query is always exactly the one the endpoint runs.
# =========================================================================== #
def attendance_list_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                          status: Optional[str]) -> tuple[dict, str]:
    check_date(date_from, "query", "date_from")
    check_date(date_to, "query", "date_to")
    if date_from and date_to and date_from > date_to:
        raise invalid("date_from must be on or before date_to", "query", "date_from")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    # Pick the index deliberately: most selective equality first, and the sort comes from the index.
    hint = IDX_ATT_EMP_DATE if emp_code else IDX_ATT_STATUS if status else IDX_ATT_DATE
    return q, hint


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": weekday_presence_value("$")},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(is_late("$")),
            "total_late_minutes": {"$sum": {"$cond": [is_late("$"), "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Runs on `employees`, so people with zero logs are still there (R9). Each employee pulls in
    their own logs for the month through an indexed $lookup; $unwind keeps employees with none."""
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    L = "$logs."
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": weekday_presence_value(L)},
            # Average over records (not of per-employee averages): keep a sum and a count.
            "work_hours_sum": {"$sum": {"$cond": [has_work_hours(L), {"$toDecimal": f"{L}work_hours"}, 0]}},
            "work_hours_n": count_if(has_work_hours(L)),
            "late_count": count_if(is_late(L)),
            "total_late_minutes": {"$sum": {"$cond": [is_late(L), f"{L}late_minutes", 0]}},
            "leave_count": count_if({"$eq": [f"{L}status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": [f"{L}status", "ON_DUTY"]}),
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$employees"},
            "present_days": 1, "work_hours_sum": 1, "work_hours_n": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str],
                              dept_codes: Optional[list[str]]) -> list:
    match: dict = {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}
    if dept_codes is not None:
        match["emp_code"] = {"$in": dept_codes}
    pipeline: list = [
        {"$match": match},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                    # logs of unknown employees drop out here, before ranking
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(members: list[dict], start: str, end: str) -> list:
    """One row per calendar day: group logs by day, add a seed row so an empty range still
    produces output, fill the gaps with $densify, then a 7-row moving window."""
    codes = [m["emp_code"] for m in members]
    join_dates = [m.get("joined_on") or "0000-01-01" for m in members]
    lower = datetime.fromisoformat(start).replace(tzinfo=UTC)
    upper = datetime.fromisoformat(end).replace(tzinfo=UTC) + timedelta(days=1)   # $densify upper bound is exclusive
    rate = {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": presence_value("$")},   # any day, weekends included
                    "late_count": count_if(is_late("$"))}},
        # Seed row for `from`, so $densify has something to fill from even when there are no logs.
        # It is read from an indexed employee lookup, so the plan stays free of collection scans.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"emp_code": codes[0]}},
            {"$project": {"_id": {"$literal": start}, "present_count": {"$literal": 0},
                          "late_count": {"$literal": 0}}},
        ]}},
        {"$group": {"_id": "$_id",
                    "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": join_dates, "cond": {"$lte": ["$$this", "$date"]}}}}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr(rate, 4),
            None,
        ]}}},
        # Only rows inside the range exist, so the first rows get a shorter window automatically.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": round_half_up_expr("$moving_avg_7d", 4)}},
    ]


def trend_range(start: str, end: str) -> tuple[str, str]:
    check_date(start, "query", "from")
    check_date(end, "query", "to")
    if end < start:
        raise invalid("to must be on or after from", "query", "to")
    if (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1 > MAX_TREND_DAYS:
        raise invalid(f"range cannot exceed {MAX_TREND_DAYS} days", "query", "to")
    return start, end


def leaderboard_department_codes(department: Optional[str]) -> Optional[list[str]]:
    return [m["emp_code"] for m in department_members(department)] if department is not None else None


# =========================================================================== #
# 10. Endpoints: system and employees
# =========================================================================== #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)        # the unique index decides duplicates, even under a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    hint = IDX_EMP_DEPT_CODE if department is not None else IDX_EMP_CODE
    total = db.employees.count_documents(q, hint=hint)
    cursor = (db.employees.find(q).sort("emp_code", 1).hint(hint)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(e) for e in cursor], "total": total, "page": page, "page_size": page_size}


# =========================================================================== #
# 11. Endpoints: attendance
# =========================================================================== #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("date", -1)],
        hint=IDX_ATT_EMP_DATE,
    )
    if rec is None:
        raise HTTPException(404, "No punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "That record is already punched out")
    if ts <= truncate(rec["punch_in"]):
        raise invalid("punched_at must be after punch_in", "body", "punched_at")
    if ts - truncate(rec["punch_in"]) > MAX_SHIFT:
        raise invalid("punched_at is more than 24 hours after punch_in", "body", "punched_at")

    fields = derived_fields(rec["status"], rec["punch_in"], ts, rec["date"], emp)
    update = {"punch_out": ts, "work_hours": fields["work_hours"],
              "overtime_minutes": fields["overtime_minutes"], "half_day": fields["half_day"]}
    doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},    # only one of two concurrent punch-outs can match
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(409, "That record is already punched out")
    return serialize_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(
        None, pattern=DATE_PATTERN, description="Inclusive calendar date in YYYY-MM-DD format, e.g. 2026-07-01."
    ),
    date_to: Optional[str] = Query(
        None, pattern=DATE_PATTERN, description="Inclusive calendar date in YYYY-MM-DD format, e.g. 2026-07-31."
    ),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, hint = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q, hint=hint)
    cursor = (db.attendance_logs.find(q).sort(ATTENDANCE_SORT).hint(hint)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_record(r) for r in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, body: RegularizeRequest,
                          date: str = Path(..., pattern=DATE_PATTERN)):
    check_date(date, "path", "date")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "No attendance record for this date")

    sent = body.model_fields_set & {"status", "punch_in", "punch_out"}
    if not sent:
        raise invalid("send at least one of status, punch_in, punch_out")

    old = stored_state(rec)
    status = body.status if "status" in sent else old["status"]
    if status is None:
        raise invalid("status cannot be null; send a valid status", "body", "status")

    if status not in PRESENCE_STATUSES:
        # ABSENT / LEAVE: both punch times are cleared, so sending one is a contradiction.
        if "punch_in" in sent or "punch_out" in sent:
            raise invalid(f"punch times cannot be set with status {status}")
        pin = pout = None
    else:
        if "punch_in" in sent:
            if body.punch_in is None:
                raise invalid("must not be null; omit the field to leave it unchanged", "body", "punch_in")
            pin = from_ms(body.punch_in)
        else:
            pin = old["punch_in"]
        if "punch_out" in sent:
            if body.punch_out is None:
                raise invalid("must not be null; omit the field to leave it unchanged", "body", "punch_out")
            pout = from_ms(body.punch_out)
        else:
            pout = old["punch_out"]
        if pin is None:
            raise invalid(f"status {status} requires a punch_in", "body", "punch_in")
        if attendance_date(pin, emp) != date:
            raise invalid("punch_in must stay on the record's attendance date", "body", "punch_in")
        if pout is not None:
            if truncate(pout) <= truncate(pin):
                raise invalid("punch_out must be after punch_in", "body", "punch_out")
            if truncate(pout) - truncate(pin) > MAX_SHIFT:
                raise invalid("punch_out must be within 24 hours of punch_in", "body", "punch_out")

    new = {"status": status, "punch_in": pin, "punch_out": pout,
           **derived_fields(status, pin, pout, date, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise invalid("the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(rec.get("history") or [])
    doc = db.attendance_logs.find_one_and_update(
        {   # Optimistic lock: write only if nobody changed the record since we read it.
            "_id": rec["_id"],
            "status": rec.get("status"),
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(409, "The record was changed by another request; reload and retry")
    return serialize_record(doc)


# =========================================================================== #
# 12. Endpoints: analytics
# =========================================================================== #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last),
                                             hint=IDX_ATT_EMP_DATE))
    agg = rows[0] if rows else {}
    working_days = count_working_days(first, last, emp.get("joined_on"))
    present = Decimal(str(agg.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(present * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first, last, department), hint=hint):
        n = r["work_hours_n"]
        avg = r["work_hours_sum"].to_decimal() / n if n else None
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": half_up(avg, 2),
            "late_count": r["late_count"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    codes = leaderboard_department_codes(department)
    hint = IDX_ATT_EMP_DATE if codes is not None else IDX_ATT_DATE
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department, codes), hint=hint)
    items = [{**r, "total_late_minutes": int(r["total_late_minutes"])} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    start, end = trend_range(from_, to)
    members = department_members(department)
    if not members:
        raise HTTPException(404, "Department not found")
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": half_up(r["attendance_rate"], 4),
            "moving_avg_7d": half_up(r["moving_avg_7d"], 4),
        }
        for r in db.attendance_logs.aggregate(department_trend_pipeline(members, start, end),
                                              hint=IDX_ATT_EMP_DATE)
    ]
    return {"department": department, "items": items}


# =========================================================================== #
# 13. Admin: explain
# =========================================================================== #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def require(value: Optional[str], name: str) -> str:
    if value is None:
        raise invalid(f"{name} is required for this endpoint", "query", name)
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explains (executionStats) the main query of an endpoint, built by the same builder the endpoint uses."""
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        q, hint = attendance_list_query(emp_code, date_from, date_to, status)
        command = {"find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT), "hint": hint,
                   "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        code, m = require(emp_code, "emp_code"), require(month, "month")
        get_employee_or_404(code)
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(code, *month_bounds(m)),
                   "hint": IDX_ATT_EMP_DATE, "cursor": {}}
    elif endpoint == "department_summary":
        collection = "employees"
        command = {"aggregate": collection,
                   "pipeline": department_summary_pipeline(*month_bounds(require(month, "month")), department),
                   "hint": IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED, "cursor": {}}
    elif endpoint == "late_leaderboard":
        first, last = month_bounds(require(month, "month"))
        codes = leaderboard_department_codes(department)
        command = {"aggregate": collection,
                   "pipeline": late_leaderboard_pipeline(first, last, limit, department, codes),
                   "hint": IDX_ATT_EMP_DATE if codes is not None else IDX_ATT_DATE, "cursor": {}}
    else:
        dept = require(department, "department")
        start, end = trend_range(require(from_, "from"), require(to, "to"))
        members = department_members(dept)
        if not members:
            raise HTTPException(404, "Department not found")
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(members, start, end),
                   "hint": IDX_ATT_EMP_DATE, "cursor": {}}

    raw = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
