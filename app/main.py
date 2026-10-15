import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # real environment variables take precedence over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_indexes_ready = False


@app.on_event("startup")
def startup_indexes() -> None:
    try:
        create_indexes()
    except PyMongoError:
        # Keep the service alive so /health can correctly report 503 while MongoDB is down.
        return


def create_indexes() -> None:
    """Idempotently create indexes required by the contract."""
    global _indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    _indexes_ready = True


def truncate_seconds(value: datetime) -> datetime:
    """Return an aware UTC instant truncated to whole seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_seconds(value).timestamp() * 1000)


def parse_epoch_ms(value: Optional[int], *, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return truncate_seconds(datetime.now(UTC))
        raise HTTPException(422, "timestamp is required")
    # Pydantic rejects bool/float/string for the request model; keep a boundary check too.
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try:
        return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid epoch milliseconds")


def api_instant(value: Optional[datetime]) -> Optional[int]:
    return epoch_ms(value)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_instant(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST).astimezone(UTC)


def shift_start_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local = truncate_seconds(punch_in).astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    start_minutes, end_minutes = start_h * 60 + start_m, end_h * 60 + end_m
    shift_day = local.date()
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        shift_day -= timedelta(days=1)
    return shift_instant(shift_day, shift_start)


def shift_end_for_record(record_date: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(record_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if eh * 60 + em <= sh * 60 + sm:
        day += timedelta(days=1)
    return shift_instant(day, shift_end)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    local = truncate_seconds(punch_in).astimezone(IST)
    # Older callers can omit shift_end; for normal shifts this still uses the local calendar day.
    start_day = local.date()
    if shift_end is not None:
        start = shift_start_for_punch(punch_in, shift_start, shift_end)
    else:
        start = shift_instant(start_day, shift_start)
    seconds_late = int((truncate_seconds(punch_in) - start).total_seconds())
    if seconds_late <= 600:
        return 0
    return max(0, seconds_late // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal("3600"), 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    end = shift_end_for_record(date_str, shift_start, shift_end)
    seconds_after = int((truncate_seconds(punch_out) - end).total_seconds())
    minutes = seconds_after // 60
    return int(minutes) if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        if key in result:
            result[key] = epoch_ms(result[key])
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_ms(change[side])
    return result


def validate_date_string(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")


def month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    next_first = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return first, next_first - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Request models: strict instant typing prevents floats, strings and seconds.
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Starter endpoints: corrected to follow the contract.
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            create_indexes()
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
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
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at, default_now=True)
    local = ts.astimezone(IST)
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    if end_h * 60 + end_m <= start_h * 60 + start_m and local.hour * 60 + local.minute < end_h * 60 + end_m:
        attendance_date = local.date() - timedelta(days=1)
    else:
        attendance_date = local.date()
    d = attendance_date.isoformat()
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        validate_date_string(date_from, "date_from")
    if date_to:
        validate_date_string(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Remaining contract endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at, default_now=True)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    pin = as_utc(record.get("punch_in"))
    if pin is None:
        raise HTTPException(404, "no punch-in found")
    elapsed = (ts - pin).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = work_hours < 4.50
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record was punched out concurrently")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_date_string(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    old = dict(record)
    old.setdefault("history", [])
    old.setdefault("late_minutes", 0)
    old.setdefault("overtime_minutes", 0)
    old.setdefault("half_day", False)
    old.setdefault("work_hours", None)
    new_status = body.status if body.status is not None else old["status"]
    pin = as_utc(old.get("punch_in"))
    pout = as_utc(old.get("punch_out"))
    if "punch_in" in body.model_fields_set:
        pin = parse_epoch_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set:
        pout = parse_epoch_ms(body.punch_out) if body.punch_out is not None else None

    if new_status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pin, pout = None, None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")

    if pin is not None:
        local = pin.astimezone(IST)
        sh, sm = map(int, emp["shift_start"].split(":"))
        eh, em = map(int, emp["shift_end"].split(":"))
        if eh * 60 + em <= sh * 60 + sm and local.hour * 60 + local.minute < eh * 60 + em:
            attendance_day = local.date() - timedelta(days=1)
        else:
            attendance_day = local.date()
        if attendance_day.isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout is not None:
        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"]) if pin is not None and new_status in PRESENT_STATUSES else 0
    hours = compute_work_hours(pin, pout) if pin is not None and pout is not None and new_status in PRESENT_STATUSES else None
    overtime = compute_overtime(pout, emp["shift_end"], date, emp["shift_start"]) if pout is not None and new_status in PRESENT_STATUSES else 0
    half = bool(hours is not None and hours < 4.50)
    final = {"status": new_status, "punch_in": pin, "punch_out": pout, "work_hours": hours,
             "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for key, value in final.items():
        before = old.get(key)
        if key in {"punch_in", "punch_out"}:
            before = as_utc(before) if before is not None else None
        if before != value:
            changes[key] = {"from": before, "to": value}
    if not changes:
        raise HTTPException(422, "regularization does not change anything")
    history_entry = {"at": truncate_seconds(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    query = {"_id": record["_id"]}
    if "history" in record:
        query["history"] = record["history"]
    else:
        query["history"] = {"$exists": False}
    update = {"$set": final, "$push": {"history": history_entry}}
    result = db.attendance_logs.update_one(query, update)
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    return serialize_attendance(db.attendance_logs.find_one({"_id": record["_id"]}))


def _round_expr(value_expr: Any, places: int = 2) -> dict:
    """MongoDB expression for positive-number half-up rounding (not $round's half-even)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value_expr, scale]}, 0.5]}}, scale]}


def _working_days_expr(start_expr: Any, end_exclusive_expr: Any) -> dict:
    """Aggregation expression counting Mon-Fri in [start, end-exclusive)."""
    return {"$size": {"$filter": {
        "input": {"$map": {"input": {"$range": [0, {"$max": [0, {"$dateDiff": {"startDate": start_expr, "endDate": end_exclusive_expr, "unit": "day"}}]}]},
                            "as": "offset", "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$offset"}}}},
        "as": "day", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$$day"}, 6]}]}
    }}}


def _month_dates(month: str) -> tuple[date, date, date]:
    first, last = month_bounds(month)
    return first, last, last + timedelta(days=1)


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last, next_day = _month_dates(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"_month_start": {"$dateFromString": {"dateString": first.isoformat()}},
                   "_month_end_exclusive": {"$dateFromString": {"dateString": next_day.isoformat()}},
                   "_join_date": {"$dateFromString": {"dateString": "$joined_on"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lt": ["$date", next_day.isoformat()]}]}}},
            {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}}},
            {"$set": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": "$_day"}, 2]}, {"$lte": [{"$dayOfWeek": "$_day"}, 6]}]}}}
        ], "as": "logs"}},
        {"$set": {"_work_start": {"$max": ["$_join_date", "$_month_start"]}}},
        {"$set": {"working_days": _working_days_expr("$_work_start", "$_month_end_exclusive")}},
        {"$set": {"_present": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", list(PRESENT_STATUSES)]}, "$$l._weekday"]}, {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]}}}},
                   "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                   "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
                   "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
                   "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
                      "present_days": _round_expr("$_present", 2), "leave_days": 1, "late_count": 1,
                      "total_late_minutes": 1, "total_overtime_minutes": 1,
                      "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, _round_expr({"$multiply": [{"$divide": ["$_present", "$working_days"]}, 100]}, 4), None]}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    rows = list(db.employees.aggregate(pipeline))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    first, last, next_day = _month_dates(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lt": ["$date", next_day.isoformat()]}]}}},
            {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}}},
            {"$set": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": "$_day"}, 2]}, {"$lte": [{"$dayOfWeek": "$_day"}, 6]}]}}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, "$logs._weekday"]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "avg_hours_raw": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$set": {"headcount": {"$size": "$headcount"}, "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, _round_expr("$avg_hours_raw", 2), None]}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": _round_expr("$present_days", 2), "avg_work_hours": 1,
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    return {"month": month, "items": list(db.employees.aggregate(build_department_summary_pipeline(month, department)))}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    first, last, next_day = _month_dates(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": next_day.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    start = validate_date_string(from_date, "from")
    end = validate_date_string(to_date, "to")
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "n", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$n"}}, "format": "%Y-%m-%d"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"date": "$days", "_date_dt": {"$dateFromString": {"dateString": "$days"}}, "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "codes": {"$map": {"input": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}}, "as": "e", "in": "$$e.emp_code"}}}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}
        ], "as": "logs"}},
        {"$set": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_date_dt"}, 2]}, {"$lte": [{"$dayOfWeek": "$_date_dt"}, 6]}]},
                   "present_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$in": ["$$l.status", list(PRESENT_STATUSES)]}, {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]}}}},
                   "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, _round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"_date_dt": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": _round_expr("$present_count", 2), "late_count": 1, "attendance_rate": 1, "moving_avg_7d": _round_expr("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = validate_date_string(from_date, "from")
    end = validate_date_string(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "to must be >= from and range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.employees.aggregate(build_department_trend_pipeline(department, from_date, to_date)))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    """Explain the same query/pipeline builder used by the corresponding endpoint."""
    if endpoint == "attendance_list":
        if date_from:
            validate_date_string(date_from, "date_from")
        if date_to:
            validate_date_string(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status
        explain = db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = validate_date_string(from_date, "from")
        end = validate_date_string(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "to must be >= from and range must not exceed 92 days")
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, "department not found")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain = db.command({"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if not month:
        raise HTTPException(422, "month is required for this endpoint")
    month_bounds(month)  # validate month before constructing a pipeline
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        coll = "employees"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
    elif endpoint == "department_summary":
        coll = "employees"
        pipeline = build_department_summary_pipeline(month, department)
    elif endpoint == "late_leaderboard":
        coll = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
    else:
        raise HTTPException(422, "unsupported explain endpoint")
    explain = db.command({"explain": {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": explain}

