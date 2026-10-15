"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Query
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    # employees
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index("joined_on")

    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_utc(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def attendance_date_ist(ts: datetime, shift_start: str, shift_end: str) -> str:
    """Return the IST attendance date string (R1).
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    ts_ist = ts.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_minutes = ts_ist.hour * 60 + ts_ist.minute
        end_minutes = eh * 60 + em
        if punch_minutes < end_minutes:  # early-morning portion of overnight shift
            return (ts_ist.date() - timedelta(days=1)).isoformat()
    return ts_ist.date().isoformat()


def half_up_round(value: float, decimals: int) -> float:
    """Half-up rounding (not Python banker's rounding)."""
    from decimal import Decimal, ROUND_HALF_UP
    quantizer = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in_ist: datetime, shift_start: str) -> int:
    """R2: grace period is 10 min; late only if strictly > 10 min after shift_start."""
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    minutes = int(diff_seconds / 60)  # floor
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, half-up rounded to 2 decimals."""
    return half_up_round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """R3: overtime only when >= 30 min after shift_end. Handles overnight shifts."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base_date = datetime.fromisoformat(date_str)
    if is_overnight:
        # shift_end falls on the next calendar day
        base_date += timedelta(days=1)
    end = base_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    diff = int((punch_out - end).total_seconds() / 60)
    return diff if diff >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def serialize_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance doc to API response format (epoch ms timestamps)."""
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = dt_to_ms(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = dt_to_ms(doc["punch_out"])
    for entry in doc.get("history", []):
        entry["at"] = dt_to_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                if changes[field]["from"] is not None:
                    changes[field]["from"] = dt_to_ms(changes[field]["from"])
                if changes[field]["to"] is not None:
                    changes[field]["to"] = dt_to_ms(changes[field]["to"])
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health(response: Response):
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        response.status_code = 503
        return {"detail": "MongoDB unavailable"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size          # fixed: was page * page_size
    total = db.employees.count_documents(q)  # fixed: use filtered query
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)          # fixed: sort by emp_code asc
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        item["created_at"] = dt_to_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(422, "status must be PRESENT, WFH, or ON_DUTY")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")  # fixed: was crashing on None

    # R1: convert to UTC, truncate to whole seconds
    ts = ms_to_utc(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc).replace(microsecond=0)
    ts = ts.replace(microsecond=0)  # truncate sub-seconds

    # R1: determine IST attendance date (handles overnight shifts)
    d = attendance_date_ist(ts, emp["shift_start"], emp["shift_end"])

    # Compute late_minutes in IST context (R2)
    ts_ist = ts.astimezone(IST)
    late = compute_late_minutes(ts_ist, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique index handles race condition
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    # fixed: use DB-level sort + skip/limit (no loading entire collection)
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in page_docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# POST /attendance/punch-out
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_utc(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc).replace(microsecond=0)
    ts = ts.replace(microsecond=0)

    # Find the most recent open record where punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no open punch-in found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 if punched_at <= punch_in or > 24h after punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch-out must be within 24 hours of punch-in")

    wh = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: 409 if already punched out
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "already punched out")

    return serialize_attendance(result)


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date}  (regularize)
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status or record["status"]
    ABSENCE = {"ABSENT", "LEAVE"}
    PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

    # Validate: ABSENT/LEAVE cannot come with punch times
    if new_status in ABSENCE and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "punch_in/punch_out cannot be set for ABSENT or LEAVE")

    # Resolve new punch_in
    def resolve_dt(epoch_ms, existing):
        if epoch_ms is not None:
            return ms_to_utc(epoch_ms)
        return existing

    if new_status in ABSENCE:
        new_punch_in = None
        new_punch_out = None
    else:
        existing_pi = record.get("punch_in")
        if existing_pi and existing_pi.tzinfo is None:
            existing_pi = existing_pi.replace(tzinfo=timezone.utc)
        existing_po = record.get("punch_out")
        if existing_po and existing_po.tzinfo is None:
            existing_po = existing_po.replace(tzinfo=timezone.utc)

        new_punch_in = resolve_dt(body.punch_in, existing_pi)
        new_punch_out = resolve_dt(body.punch_out, existing_po)

        if new_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        pi_date = attendance_date_ist(new_punch_in, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, "punch_in must remain on the record's attendance date")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24h of punch_in")

    # Recompute derived fields
    if new_status in ABSENCE or new_punch_in is None:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        pi_ist = new_punch_in.astimezone(IST)
        new_late = compute_late_minutes(pi_ist, emp["shift_start"])
        if new_punch_out is not None:
            new_wh = compute_work_hours(new_punch_in, new_punch_out)
            new_ot = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build changes dict (only fields that actually changed)
    def ms_or_none(dt):
        return dt_to_ms(dt) if dt is not None else None

    old_pi = record.get("punch_in")
    if old_pi and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    old_po = record.get("punch_out")
    if old_po and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)

    changes = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_punch_in != old_pi:
        changes["punch_in"] = {"from": ms_or_none(old_pi), "to": ms_or_none(new_punch_in)}
    if new_punch_out != old_po:
        changes["punch_out"] = {"from": ms_or_none(old_po), "to": ms_or_none(new_punch_out)}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_wh != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if new_hd != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Convert punch ms back to datetime for storage in changes
    def ms_to_dt_or_none(ms_val):
        return ms_to_utc(ms_val) if ms_val is not None else None

    stored_changes = {}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            stored_changes[k] = {
                "from": ms_to_dt_or_none(v["from"]) if isinstance(v["from"], int) else v["from"],
                "to": ms_to_dt_or_none(v["to"]) if isinstance(v["to"], int) else v["to"],
            }
        else:
            stored_changes[k] = v
    history_entry["changes"] = stored_changes

    # Atomic update with optimistic concurrency on history length
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": len(record.get("history", []))}},
        {"$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_wh,
            "late_minutes": new_late,
            "overtime_minutes": new_ot,
            "half_day": new_hd,
        }, "$push": {"history": history_entry}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "concurrent edit detected, please retry")

    return serialize_attendance(result)


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
import calendar


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    year, mon = int(month[:4]), int(month[5:])
    month_start = f"{year:04d}-{mon:02d}-01"
    last_day = calendar.monthrange(year, mon)[1]
    month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

    joined = emp["joined_on"]
    effective_start = max(month_start, joined)

    # Count working days (Mon–Fri) from effective_start to month_end
    from datetime import date as date_cls
    def count_working_days(start_str, end_str):
        s = date_cls.fromisoformat(start_str)
        e = date_cls.fromisoformat(end_str)
        count = 0
        cur = s
        while cur <= e:
            if cur.weekday() < 5:  # Mon=0, Fri=4
                count += 1
            cur += timedelta(days=1)
        return count

    working_days = count_working_days(effective_start, month_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {
            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "is_weekday": {"$and": [
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
            ]},
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "half_day_val": {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_presence", "$is_weekday"]},
                "$half_day_val", 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {}

    present_days = half_up_round(r.get("present_days", 0), 2)
    attendance_pct = (
        half_up_round(present_days / working_days * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    year, mon = int(month[:4]), int(month[5:])
    last_day = calendar.monthrange(year, mon)[1]
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

    emp_match = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from employees (to capture zero-log employees)
        {"$match": {**emp_match, "joined_on": {"$lte": month_end}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        # Join attendance logs
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"dept_emps": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$in": ["$emp_code", "$$dept_emps"]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                    ]
                }}},
                {"$addFields": {
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "is_weekday": {"$let": {
                        "vars": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}},
                        "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]}
                    }},
                    "half_day_val": {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                }},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {
                "$cond": [{"$and": ["$$l.is_presence", "$$l.is_weekday"]}, "$$l.half_day_val", 0]
            }}}},
            "avg_work_hours_list": {"$filter": {
                "input": "$logs",
                "as": "l",
                "cond": {"$and": [
                    "$$l.is_presence",
                    {"$ne": ["$$l.work_hours", None]},
                    {"$gt": ["$$l.work_hours", None]},
                ]}
            }},
            "late_logs": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$gt": ["$$l.late_minutes", 0]}
            }},
            "leave_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "LEAVE"]}
            }}},
            "on_duty_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
            }}},
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": [{"$size": "$avg_work_hours_list"}, 0]},
                {"$avg": "$avg_work_hours_list.work_hours"},
                None,
            ]},
            "late_count": {"$size": "$late_logs"},
            "total_late_minutes": {"$sum": "$late_logs.late_minutes"},
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": ASCENDING}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": half_up_round(r.get("present_days", 0), 2),
            "avg_work_hours": half_up_round(r["avg_work_hours"], 2) if r.get("avg_work_hours") is not None else None,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    year, mon = int(month[:4]), int(month[5:])
    last_day = calendar.monthrange(year, mon)[1]
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"

    emp_match = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # Join employee info
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
        *([ {"$match": {"emp_info.department": department}} ] if department else []),
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp_info.name",
            "department": "$emp_info.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
from fastapi import Query as _Query
from fastapi import Query  # also available for explain_endpoint
from datetime import date as _date_cls
from collections import defaultdict


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = _Query(..., alias="from"),
    to: str = _Query(...),
):
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    d_from = _date_cls.fromisoformat(from_)
    d_to = _date_cls.fromisoformat(to)
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # 404 if no employees in this department
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    dept_emps = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
    all_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))

    # Fetch all logs for the department within the range
    logs_cursor = db.attendance_logs.find(
        {"date": {"$gte": from_, "$lte": to}, "emp_code": {"$in": dept_emps}},
        {"date": 1, "status": 1, "late_minutes": 1, "half_day": 1, "_id": 0},
    )
    daily = defaultdict(lambda: {"present_count": 0.0, "late_count": 0})
    for log in logs_cursor:
        d = log["date"]
        if log["status"] in ("PRESENT", "WFH", "ON_DUTY"):
            hd = log.get("half_day", False) or False
            daily[d]["present_count"] += 0.5 if hd else 1.0
        if log.get("late_minutes", 0) > 0:
            daily[d]["late_count"] += 1

    # Build day-by-day result with gap-filling (within 92 days, Python loop is fine)
    items = []
    cur = d_from
    non_null_rates: list = []
    while cur <= d_to:
        ds = cur.isoformat()
        is_working = cur.weekday() < 5
        headcount = sum(1 for e in all_employees if e["joined_on"] <= ds)
        pc = daily[ds]["present_count"]
        lc = daily[ds]["late_count"]

        if is_working and headcount > 0:
            rate = half_up_round(pc / headcount, 4)
        else:
            rate = None

        non_null_rates.append(rate)
        # 7-day trailing moving average of non-null rates (only within requested range)
        window = [r for r in non_null_rates[-7:] if r is not None]
        moving_avg = half_up_round(sum(window) / len(window), 4) if window else None

        items.append({
            "date": ds,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": pc,
            "late_count": lc,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}




# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        q = {}
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
        skip = (page - 1) * page_size
        raw_explain = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(raw_explain))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        year, mon = int(month[:4]), int(month[5:])
        last_day = calendar.monthrange(year, mon)[1]
        month_start = f"{year:04d}-{mon:02d}-01"
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        ]
        raw_explain = db.command(
            {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(raw_explain))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = int(month[:4]), int(month[5:])
        last_day = calendar.monthrange(year, mon)[1]
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"
        emp_match = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        raw_explain = db.command(
            {"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(raw_explain))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = int(month[:4]), int(month[5:])
        last_day = calendar.monthrange(year, mon)[1]
        month_start = f"{year:04d}-{mon:02d}-01"
        month_end = f"{year:04d}-{mon:02d}-{last_day:02d}"
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        ]
        raw_explain = db.command(
            {"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(raw_explain))}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required")
        dept_emps = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        raw_explain = db.attendance_logs.find(
            {"date": {"$gte": from_, "$lte": to}, "emp_code": {"$in": dept_emps}}
        ).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(raw_explain))}

