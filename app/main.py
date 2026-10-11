"""
Employee Attendance & Analytics API
FastAPI + MongoDB service compliant with OpenAPI 2.0 contract and business rules R1-R10.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation across collections
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("late_minutes", 1)])
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Models & Enums
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: PresenceStatus = PresenceStatus.PRESENT


# --------------------------------------------------------------------------- #
# Calculation & Serialization Helpers
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    # R1: Truncate to whole seconds before calculation
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def resolve_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end belongs
    to the shift that started on the previous day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, attendance_date: str, shift_start: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start.
    late_minutes = floor(diff since shift_start), not since grace period ended.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)

    diff_sec = (dt_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 & R8: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    d_hours = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(d_hours)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    """
    dt_ist = punch_out_utc.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    is_overnight = (eh, em) <= (sh, sm)
    target_date = date(y, m, d) + (timedelta(days=1) if is_overnight else timedelta(days=0))
    end_dt = datetime(target_date.year, target_date.month, target_date.day, eh, em, 0, tzinfo=IST)

    diff_sec = (dt_ist - end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    return work_hours < 4.50 if work_hours is not None else False


def format_history_entry(entry: dict) -> dict:
    formatted_changes = {}
    for k, v in entry.get("changes", {}).items():
        from_v = v.get("from")
        to_v = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_v, datetime):
                from_v = to_epoch_ms(from_v)
            if isinstance(to_v, datetime):
                to_v = to_epoch_ms(to_v)
        formatted_changes[k] = {"from": from_v, "to": to_v}

    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": formatted_changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


def format_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Endpoints (Part A - Remediated)
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=200)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with code '{body.emp_code}' already exists",
        )

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp()) * 1000
        ts = from_epoch_ms(now_ms)

    att_date = resolve_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Attendance record already exists for {body.emp_code} on {att_date}",
        )

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[Status] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be on or before date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_query: Dict[str, str] = {}
        if date_from:
            date_query["$gte"] = date_from
        if date_to:
            date_query["$lte"] = date_to
        q["date"] = date_query
    if status_filter:
        q["status"] = status_filter.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Part B Endpoints (To be implemented)
# --------------------------------------------------------------------------- #
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
