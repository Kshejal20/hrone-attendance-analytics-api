"""HROne Employee Attendance & Analytics API.

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (or local .env).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Existing shell environment variables take priority over .env values.
load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Helpers: dates, instants, response serialization, business calculations
# ---------------------------------------------------------------------------
def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM month")
    return start, end


def epoch_ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    # The model enforces strict integer and range validation before this call.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo naive dates are UTC.
    return int(value.timestamp() * 1000)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)  # type: ignore[union-attr]


def time_parts(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return time_parts(shift_end) <= time_parts(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = as_utc(punch_in).astimezone(IST)  # type: ignore[union-attr]
    day = local.date()
    if is_overnight(shift_start, shift_end) and (local.hour, local.minute) < time_parts(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_datetime(attendance_day: str, shift_start: str) -> datetime:
    day = parse_date(attendance_day)
    hour, minute = time_parts(shift_start)
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def shift_end_datetime(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    day = parse_date(attendance_day)
    hour, minute = time_parts(shift_end)
    end = datetime.combine(day, time(hour, minute), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_day: str) -> int:
    if punch_in is None:
        return 0
    start = shift_start_datetime(attendance_day, shift_start).astimezone(UTC)
    seconds = (as_utc(punch_in) - start).total_seconds()  # type: ignore[operator]
    if seconds <= 600:
        return 0
    return max(0, int(seconds // 60))


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))  # type: ignore[operator]
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(
    punch_out: Optional[datetime], shift_start: str, shift_end: str, attendance_day: str
) -> int:
    if punch_out is None:
        return 0
    end = shift_end_datetime(attendance_day, shift_start, shift_end).astimezone(UTC)
    seconds = (as_utc(punch_out) - end).total_seconds()  # type: ignore[operator]
    if seconds < 1800:
        return 0
    return max(0, int(seconds // 60))


def derived_values(
    status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict, attendance_day: str
) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, employee["shift_start"], attendance_day),
        "overtime_minutes": compute_overtime_minutes(
            punch_out, employee["shift_start"], employee["shift_end"], attendance_day
        ),
        "half_day": bool(work_hours is not None and work_hours < 4.50),
    }


def attendance_response(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_response(item) for item in (doc.get("history") or [])],
    }


def history_response(item: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field, pair in (item.get("changes") or {}).items():
        changes[field] = {
            "from": datetime_to_epoch_ms(pair.get("from")) if field in ("punch_in", "punch_out") else pair.get("from"),
            "to": datetime_to_epoch_ms(pair.get("to")) if field in ("punch_in", "punch_out") else pair.get("to"),
        }
    return {
        "at": datetime_to_epoch_ms(item["at"]),
        "by": item["by"],
        "reason": item["reason"],
        "changes": changes,
    }


def employee_response(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def mongo_half_up(expr: Any, places: int) -> dict[str, Any]:
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def month_log_match(start: date, end: date) -> dict[str, Any]:
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def weekday_expression(date_expr: Any) -> dict[str, Any]:
    # Mongo $dayOfWeek: Sunday=1, Monday=2, ..., Saturday=7.
    return {"$and": [{"$gte": [{"$dayOfWeek": date_expr}, 2]}, {"$lte": [{"$dayOfWeek": date_expr}, 6]}]}


def working_days_expression(joined_on_expr: Any, month_end: date) -> dict[str, Any]:
    month_start_string = None  # effective start is max(joined_on, month start) inside the pipeline.
    del month_start_string
    return {"$let": {
        "vars": {
            "start": {"$cond": [
                {"$gt": [joined_on_expr, {"$literal": month_end.isoformat()}]},
                {"$literal": month_end.isoformat()},
                joined_on_expr,
            ]}
        },
        "in": {"$size": {"$filter": {
            "input": {"$map": {
                "input": {"$range": [0, {"$max": [0, {"$dateDiff": {
                    "startDate": {"$dateFromString": {"dateString": "$$start"}},
                    "endDate": {"$dateFromString": {"dateString": {"$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": {"$literal": month_end.isoformat()}}},
                        "unit": "day", "amount": 1
                    }}}},
                    "unit": "day"
                }}]}]},
                "as": "offset",
                "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": "$$start"}}, "unit": "day", "amount": "$$offset"}}
            }},
            "as": "d",
            "cond": weekday_expression("$$d")
        }}}
    }}


# ---------------------------------------------------------------------------
# MongoDB aggregation pipeline builders
# ---------------------------------------------------------------------------
def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict[str, Any]]:
    effective_start = {"$cond": [{"$gt": ["$joined_on", start.isoformat()]}, "$joined_on", start.isoformat()]}
    working_days = {"$size": {"$filter": {
        "input": {"$map": {
            "input": {"$range": [0, {"$max": [0, {"$dateDiff": {
                "startDate": {"$dateFromString": {"dateString": effective_start}},
                "endDate": {"$dateFromString": {"dateString": {"$literal": (end + timedelta(days=1)).isoformat()}}},
                "unit": "day"
            }}]}]},
            "as": "i",
            "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": effective_start}}, "unit": "day", "amount": "$$i"}}
        }},
        "as": "d", "cond": weekday_expression("$$d")
    }}}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {**month_log_match(start, end), "$expr": {"$eq": ["$emp_code", "$$code"]}}},
                {"$addFields": {"_weekday": weekday_expression({"$dateFromString": {"dateString": "$date"}})}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, "$_weekday"]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
                }}
            ],
            "as": "monthly"
        }},
        {"$addFields": {"_m": {"$ifNull": [{"$arrayElemAt": ["$monthly", 0]}, {
            "present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0
        }]}, "working_days": working_days}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": start.strftime("%Y-%m")},
            "working_days": 1,
            "present_days": mongo_half_up("$_m.present_days", 2),
            "leave_days": "$_m.leave_days", "late_count": "$_m.late_count",
            "total_late_minutes": "$_m.total_late_minutes", "total_overtime_minutes": "$_m.total_overtime_minutes",
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, mongo_half_up({"$multiply": [{"$divide": ["$_m.present_days", "$working_days"]}, 100]}, 4), None]}
        }}
    ]


def department_summary_pipeline(start: date, end: date, department: Optional[str]) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {**month_log_match(start, end), "$expr": {"$eq": ["$emp_code", "$$code"]}}},
                {"$addFields": {"_weekday": weekday_expression({"$dateFromString": {"dateString": "$date"}})}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, "$_weekday"]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [{"$isNumber": "$work_hours"}, "$work_hours", 0]}},
                    "work_hours_count": {"$sum": {"$cond": [{"$isNumber": "$work_hours"}, 1, 0]}}
                }}
            ], "as": "stats"
        }},
        {"$addFields": {"_s": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {
            "present_days": 0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0,
            "on_duty_count": 0, "work_hours_sum": 0, "work_hours_count": 0
        }]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$_s.present_days"}, "late_count": {"$sum": "$_s.late_count"},
            "total_late_minutes": {"$sum": "$_s.total_late_minutes"}, "leave_count": {"$sum": "$_s.leave_count"},
            "on_duty_count": {"$sum": "$_s.on_duty_count"}, "work_hours_sum": {"$sum": "$_s.work_hours_sum"},
            "work_hours_count": {"$sum": "$_s.work_hours_count"}
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": mongo_half_up("$present_days", 2), "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_half_up({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2), None]}
        }},
        {"$sort": {"department": 1}}
    ]


def leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": month_log_match(start, end)},
        {"$group": {"_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$match": {"late_count": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # Rank cutoff includes every employee tied at the cutoff rank.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": 1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ])
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_exclusive_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    seed_day = datetime.combine(start - timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1, "_seed": {"$literal": False}}},
        # Add a sentinel before the range so densify also emits all dates when the range has no logs.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "day": {"$literal": seed_day}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}, "_seed": {"$literal": True}}}
        ]}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive_dt]}}},
        {"$match": {"day": {"$gte": start_dt, "$lt": end_exclusive_dt}}},
        {"$addFields": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            "date_string": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": weekday_expression("$day")}},
        {"$lookup": {"from": "employees", "let": {"d": "$date_string"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"}
        ], "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
            "present_count": mongo_half_up("$present_count", 2), "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": mongo_half_up("$moving_avg_7d", 4)}} ,
        {"$sort": {"date": 1}}
    ]


# ---------------------------------------------------------------------------
# Startup and basic CRUD endpoints
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """Create indexes idempotently; keep the process alive if Atlas is temporarily down."""
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_date_desc")
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_emp")
        db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_date_emp_punchin")
    except PyMongoError as exc:
        logger.warning("Could not create MongoDB indexes at startup; database may be unavailable: %s", exc)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = truncate_seconds(epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    attendance_day = attendance_date_for(ts, employee["shift_start"], employee["shift_end"]).isoformat()
    derived = derived_values(body.status, ts, None, employee, attendance_day)
    doc = {"emp_code": body.emp_code, "date": attendance_day, "status": body.status,
           "punch_in": ts, "punch_out": None, **derived, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance already exists for this employee and date")
    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from is not None:
        try: parse_date(date_from)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    if date_to is not None:
        try: parse_date(date_to)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    query: dict[str, Any] = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if status is not None: query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = truncate_seconds(epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance already punched out")
    punch_in_at = as_utc(record.get("punch_in"))
    elapsed = (ts - punch_in_at).total_seconds()  # type: ignore[operator]
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    derived = derived_values(record["status"], punch_in_at, ts, employee, record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # Another concurrent request may have completed the punch-out.
        latest = db.attendance_logs.find_one({"_id": record["_id"]})
        if latest and latest.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Attendance already punched out")
        raise HTTPException(status_code=409, detail="Attendance changed concurrently")
    return attendance_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn) -> dict[str, Any]:
    try: parse_date(date)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if original is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")
    status = body.status if "status" in supplied else original["status"]
    old_in = as_utc(original.get("punch_in"))
    old_out = as_utc(original.get("punch_out"))
    new_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied else old_in
    new_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied else old_out

    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="Do not supply punch times with ABSENT or LEAVE")
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        if attendance_date_for(new_in, employee["shift_start"], employee["shift_end"]).isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to this attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    new_derived = derived_values(status, new_in, new_out, employee, date)
    old_derived = {
        "work_hours": original.get("work_hours"), "late_minutes": int(original.get("late_minutes", 0) or 0),
        "overtime_minutes": int(original.get("overtime_minutes", 0) or 0), "half_day": bool(original.get("half_day", False)),
    }
    new_fields = {"status": status, "punch_in": new_in, "punch_out": new_out, **new_derived}
    old_fields = {"status": original["status"], "punch_in": old_in, "punch_out": old_out, **old_derived}
    changes: dict[str, Any] = {}
    for field, new_value in new_fields.items():
        old_value = old_fields[field]
        if field in ("punch_in", "punch_out"):
            old_cmp, new_cmp = as_utc(old_value), as_utc(new_value)
        else:
            old_cmp, new_cmp = old_value, new_value
        if old_cmp != new_cmp:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="Regularization does not change anything")

    history = original.get("history") or []
    now = datetime.now(UTC).replace(microsecond=0)
    entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: compare the old values and history, then append atomically.
    guard: dict[str, Any] = {"_id": original["_id"]}
    if "history" in original:
        guard["history"] = history
    else:
        guard["history"] = {"$in": [[], None]}
    for field in ("status", "punch_in", "punch_out"):
        guard[field] = original.get(field)
    update = {"$set": new_fields, "$push": {"history": entry}}
    result = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(status_code=409, detail="Attendance record changed concurrently; retry the correction")
    return attendance_response(result)


# ---------------------------------------------------------------------------
# Analytics endpoints - all data analytics are computed in MongoDB pipelines
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    start, end = parse_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    rows = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    if not rows:
        raise HTTPException(status_code=404, detail="Employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    start, end = parse_month(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(start, end, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    start, end = parse_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
) -> dict[str, Any]:
    try:
        start, end = parse_date(from_date), parse_date(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Department not found")
    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(department, start, end)))
    # The pipeline's sentinel is filtered out; sort is stable by date.
    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
def explain_aggregate(collection_name: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    result = db.command(
        "explain",
        {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    # Convert BSON types to JSON-compatible extended JSON object.
    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if status is not None: query["status"] = status
        if date_from is not None:
            try: parse_date(date_from)
            except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
            query.setdefault("date", {})["$gte"] = date_from
        if date_to is not None:
            try: parse_date(date_to)
            except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
            query.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        result = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, end = parse_month(month)
        result = explain_aggregate("employees", employee_monthly_pipeline(emp_code, start, end))
        collection = "employees"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        result = explain_aggregate("employees", department_summary_pipeline(start, end, department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        result = explain_aggregate("attendance_logs", leaderboard_pipeline(start, end, limit, department))
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try: start, end = parse_date(from_date), parse_date(to_date)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        result = explain_aggregate("attendance_logs", department_trend_pipeline(department, start, end))
        collection = "attendance_logs"
    if endpoint == "attendance_list":
        result = json.loads(json_util.dumps(result))
    return {"endpoint": endpoint, "collection": collection, "explain": result}
