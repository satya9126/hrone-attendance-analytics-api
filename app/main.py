"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Literal
from datetime import date, datetime
from bson import Timestamp, ObjectId
import base64
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator
from pymongo import MongoClient
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager
from pymongo.errors import DuplicateKeyError
from pymongo import MongoClient, ReturnDocument
from pydantic import BaseModel, field_validator

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))


db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation at startup. Keep the unique natural keys.
    db.employees.create_index([("emp_code", 1)], unique=True)
    # Department listing and date-based department headcount calculations.
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="employee_department_code_idx",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="employee_department_joined_idx",
    )

    db.employees.create_index(
        [("joined_on", 1)],
        name="employee_joined_on_idx",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    # Supports attendance list date ordering and month-range scans.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="attendance_date_emp_code_idx",
    )
    # Helps late leaderboard's month/date filter plus late-only condition.
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1)],
        name="attendance_date_late_minutes_idx",
    )
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str
) -> int:
    h, m = map(int, shift_start.split(":"))
    shift_date = datetime.fromisoformat(attendance_date).date()

    start = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        h,
        m,
        tzinfo=IST
    )

    punch_in_ist = punch_in.astimezone(IST)

    minutes_late = int(
        (punch_in_ist - start).total_seconds() // 60
    )

    return minutes_late if minutes_late > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))




def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    attendance_date = datetime.fromisoformat(date_str).date()
    punch_out_ist = punch_out.astimezone(IST)

    # An end time <= start time means the shift crosses midnight.
    overnight = (end_h, end_m) <= (start_h, start_m)

    end_date = attendance_date
    if overnight:
        end_date += timedelta(days=1)

    shift_end_time = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_h,
        end_m,
        tzinfo=IST
    )

    overtime_seconds = (
        punch_out_ist - shift_end_time
    ).total_seconds()

    if overtime_seconds < 30 * 60:
        return 0

    return int(overtime_seconds // 60)


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

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%H:%M")
            if parsed.strftime("%H:%M") != value:
                raise ValueError
        except ValueError:
            raise ValueError("Time must be a valid HH:MM value")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != value:
                raise ValueError
        except ValueError:
            raise ValueError("Date must be a valid YYYY-MM-DD value")
        return value

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError(
                "punched_at must be an integer epoch-millisecond value"
            )

        if not 100_000_000_000 <= value <= 4_102_444_800_000:
            raise ValueError(
                "punched_at is outside the allowed epoch-millisecond range"
            )

        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        # Reject strings, floats, and booleans instead of coercing them.
        if type(value) is not int:
            raise ValueError("punched_at must be an integer epoch-millisecond value")

        if not 100_000_000_000 <= value <= 4_102_444_800_000:
            raise ValueError("punched_at is outside the allowed epoch-millisecond range")

        return value

class RegularizeIn(BaseModel):
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None

    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_punch_timestamp(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError("Timestamp must be integer epoch milliseconds")

        if not 100_000_000_000 <= value <= 4_102_444_800_000:
            raise ValueError("Timestamp is outside the allowed range")

        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value):
        if not 5 <= len(value) <= 200:
            raise ValueError("reason must contain 5 to 200 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, value):
        if not 1 <= len(value) <= 50:
            raise ValueError("regularized_by must contain 1 to 50 characters")
        return value

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop("_id", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):

    if page < 1:
        raise HTTPException(400, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(400, "page_size must be between 1 and 100")

    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = int(
                item["created_at"].replace(tzinfo=timezone.utc).timestamp() * 1000
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)
    local_ts = ts.astimezone(IST)
    attendance_date = local_ts.date()

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    if shift_end <= shift_start and local_ts.strftime("%H:%M") < shift_end:
        attendance_date -= timedelta(days=1)

    d = attendance_date.isoformat()

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            d
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )


    response = doc.copy()
    response["punch_in"] = int(
        doc["punch_in"].timestamp() * 1000
    )
    response.pop("_id", None)
    return response



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date formats
    for field, value in [
        ("date_from", date_from),
        ("date_to", date_to),
    ]:
        if value is not None:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                if parsed.strftime("%Y-%m-%d") != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} must be a valid YYYY-MM-DD date",
                )

    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status is not None:
        if status not in {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }:
            raise HTTPException(
                status_code=422,
                detail="Invalid status",
            )
        q["status"] = status

    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    for doc in docs:
        for field in ("punch_in", "punch_out"):
            value = doc.get(field)
            if isinstance(value, datetime):
                doc[field] = int(
                    value.replace(tzinfo=timezone.utc).timestamp() * 1000
                )

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        )
    else:
        ts = datetime.now(timezone.utc)

    # Truncate to whole seconds, as required by the contract.
    ts = ts.replace(microsecond=0)

    # Find the most recent record that has already been punched in.

    # Find the most recent punch-in for this employee,
    # including an open record that may be after the supplied timestamp.

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "date": {
                "$gte": (
                    ts.astimezone(IST).date() - timedelta(days=1)
                ).isoformat(),
                "$lte": ts.astimezone(IST).date().isoformat()
            },
            "punch_in": {"$lte": ts}
        },
        sort=[("punch_in", -1)]
    )


    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = record["punch_in"].replace(
        tzinfo=timezone.utc
    )

    elapsed_seconds = (ts - punch_in_time).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            422,
            "Punch-out must be after punch-in and within 24 hours"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"]
    )
    half_day = work_hours < 4.50

    # Atomic update: only one simultaneous request can close this record.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(409, "Already punched out")

    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if isinstance(value, datetime):
            updated[field] = int(
                value.replace(tzinfo=timezone.utc).timestamp() * 1000
            )

    return updated

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):
    # 1. Check whether the employee exists.
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "Employee not found")

    # 2. Find the attendance record by its natural key.
    query = {"emp_code": emp_code, "date": date}
    record = db.attendance_logs.find_one(query)

    if not record:
        raise HTTPException(404, "Attendance record not found")

    # 3. Convert the supplied epoch milliseconds into UTC datetimes.
    new_status = body.status if body.status is not None else record["status"]


    new_punch_in = record.get("punch_in")
    if new_punch_in is not None:
        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
        else:
            new_punch_in = new_punch_in.astimezone(timezone.utc)

    new_punch_out = record.get("punch_out")
    if new_punch_out is not None:
        if new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
        else:
            new_punch_out = new_punch_out.astimezone(timezone.utc)


    if body.punch_in is not None:
        new_punch_in = datetime.fromtimestamp(
            body.punch_in / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    if body.punch_out is not None:
        new_punch_out = datetime.fromtimestamp(
            body.punch_out / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    # 4. ABSENT and LEAVE must not have punch times.
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "ABSENT and LEAVE cannot have punch times"
            )

        new_punch_in = None
        new_punch_out = None

    # 5. Presence statuses require a punch-in.
    elif new_punch_in is None:
        raise HTTPException(
            422, "A presence status requires punch_in"
        )

    # 6. The punch-in must belong to the record's attendance date.
    if new_punch_in is not None:
        local_date = new_punch_in.astimezone(IST).date()
        shift_end = emp["shift_end"]
        shift_start = emp["shift_start"]

        if (
            shift_end <= shift_start
            and new_punch_in.astimezone(IST).strftime("%H:%M") < shift_end
        ):
            local_date -= timedelta(days=1)

        if local_date.isoformat() != date:
            raise HTTPException(
                422, "punch_in does not belong to this attendance date"
            )

    # 7. A punch-out must follow punch-in and be within 24 hours.
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (new_punch_out - new_punch_in).total_seconds()

        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                422, "punch_out must be after punch_in and within 24 hours"
            )

    # 8. Recompute all derived fields from the final values.
    if new_punch_in is None:
        new_late_minutes = 0
    else:
        new_late_minutes = compute_late_minutes(
            new_punch_in, emp["shift_start"], date
        )

    if new_punch_in is not None and new_punch_out is not None:
        new_work_hours = compute_work_hours(
            new_punch_in, new_punch_out
        )
        new_overtime_minutes = compute_overtime(
            new_punch_out,
            emp["shift_start"],
            emp["shift_end"],
            date
        )
        new_half_day = new_work_hours < 4.50
    else:
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    # 9. Record only fields whose values actually changed.
    changes = {}

    def epoch_ms(value):
        if value is None:
            return None
        return int(
            value.replace(tzinfo=timezone.utc).timestamp() * 1000
        )

    old_values = {
        "status": record["status"],
        "punch_in": epoch_ms(record.get("punch_in")),
        "punch_out": epoch_ms(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": epoch_ms(new_punch_in),
        "punch_out": epoch_ms(new_punch_out),
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    for field, new_value in new_values.items():
        if old_values[field] != new_value:
            changes[field] = {
                "from": old_values[field],
                "to": new_value
            }

    if not changes:
        raise HTTPException(422, "Request does not change the record")

    # 10. Append one audit-history entry.
    now = datetime.now(timezone.utc).replace(microsecond=0)

    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Compare-and-update prevents concurrent edits from losing history.
    updated = db.attendance_logs.find_one_and_update(
        {
            **query,
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history", [])
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            409, "Record changed concurrently; retry the correction"
        )

    # 11. Convert stored datetimes to epoch milliseconds for the response.
    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        updated[field] = epoch_ms(updated.get(field))

    for entry in updated.get("history", []):
        entry["at"] = epoch_ms(entry["at"])

    return updated
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    )
):
    # 1. Check that the employee exists.
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "Employee not found")

    # 2. Calculate the month boundaries.
    month_start = datetime.strptime(month, "%Y-%m").date()
    if month_start.month == 12:
        next_month = month_start.replace(
            year=month_start.year + 1, month=1
        )
    else:
        next_month = month_start.replace(
            month=month_start.month + 1
        )

    month_end = next_month - timedelta(days=1)

    # 3. Count weekdays from joining date through month end.
    joined_on = datetime.strptime(
        emp["joined_on"], "%Y-%m-%d"
    ).date()

    working_days = 0

    if joined_on <= month_end:
        first_day = max(joined_on, month_start)
        current_day = first_day

        while current_day <= month_end:
            if current_day.weekday() < 5:
                working_days += 1

            current_day += timedelta(days=1)

    # 4. Aggregate attendance metrics in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat()
                }
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$day_of_week",
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # 5. If there are no attendance records, use zero totals.
    metrics = results[0] if results else {}

    present_days = Decimal(
        str(metrics.get("present_days", 0))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 6. Calculate attendance percentage using half-up rounding.
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (present_days * Decimal("100") / Decimal(working_days))
            .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None
):
    # Calculate month boundaries.
    month_start = datetime.strptime(month, "%Y-%m").date()

    if month_start.month == 12:
        next_month = month_start.replace(
            year=month_start.year + 1, month=1
        )
    else:
        next_month = month_start.replace(
            month=month_start.month + 1
        )

    month_end = next_month - timedelta(days=1)

    # Include only employees who joined by the end of the month.
    employee_match = {
        "joined_on": {"$lte": month_end.isoformat()}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},

        # Attach this month's attendance records to each employee.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            month_start.isoformat()
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            "$date",
                                            next_month.isoformat()
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },

        # Calculate metrics for each department.
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$logs.day_of_week",
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$logs.late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]}
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid_hours"}, 0]},
                                {"$avg": "$$valid_hours"},
                                None
                            ]
                        }
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    # Round average work hours to 2 decimal places, half-up.
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = float(
                Decimal(str(item["avg_work_hours"])).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    # Calculate the selected month's date range.
    month_start = datetime.strptime(month, "%Y-%m").date()

    if month_start.month == 12:
        next_month = month_start.replace(
            year=month_start.year + 1, month=1
        )
    else:
        next_month = month_start.replace(
            month=month_start.month + 1
        )

    start_date = month_start.isoformat()
    end_date = next_month.isoformat()

    # Aggregate late attendance records by employee.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_date,
                    "$lt": end_date,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # If requested, rank only employees in that department.
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4.
    items = []
    previous_total = None
    current_rank = 0

    for position, employee in enumerate(results, start=1):
        total = employee["total_late_minutes"]

        if total != previous_total:
            current_rank = position

        employee["rank"] = current_rank
        previous_total = total

        # Apply the limit AFTER calculating the rank.
        if current_rank <= limit:
            items.append(employee)
        else:
            break

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # 1. Validate the supplied dates strictly.
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()

        if start.isoformat() != from_date or end.isoformat() != to_date:
            raise ValueError("Dates must use YYYY-MM-DD format")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="'from' and 'to' must be valid dates in YYYY-MM-DD format",
        )

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    # Maximum range: 92 calendar dates, inclusive.
    if (end - start).days >= 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # 2. Ensure the department exists.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department}' not found",
        )

    start_dt = datetime.combine(start, datetime.min.time())
    end_exclusive = datetime.combine(
        end + timedelta(days=1),
        datetime.min.time(),
    )

    # 3. Generate every date, including the requested final date.
    # $densify uses an exclusive upper bound, so use midnight
    # of the day AFTER the requested end date.
    pipeline = [
        {
            "$documents": [
                {"date": start_dt}
            ]
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                }
            }
        },

        # 4. Find the department's attendance records for each date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$day"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.late_count", 0]},
                        0,
                    ]
                },
            }
        },

        # 5. Count department employees who have joined by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$headcount_result.count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
                "weekday": {"$dayOfWeek": "$date"},
            }
        },

        # MongoDB dayOfWeek: Sunday=1, Monday=2, ..., Saturday=7.
        {
            "$set": {
                "is_working_day": {
                    "$in": ["$weekday", [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        # 6. Calculate a rolling average over the current date
        # and the six preceding calendar dates. Null rates are ignored.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
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

    # $documents is a database-level aggregation stage.
    items = list(db.aggregate(pipeline))

    # 7. Round rates to four decimal places using half-up rounding.
    def round_rate(value):
        if value is None:
            return None
        return float(
            Decimal(str(value)).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    for item in items:
        item["attendance_rate"] = round_rate(
            item["attendance_rate"]
        )
        item["moving_avg_7d"] = round_rate(
            item["moving_avg_7d"]
        )

    return {
        "department": department,
        "items": items,
    }

def make_json_safe(value):
    if isinstance(value, dict):
        return {
            str(key): make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]

    if isinstance(value, Timestamp):
        return {
            "time": value.time,
            "increment": value.inc,
        }

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, bytes):
        return {
            "binary_base64": base64.b64encode(value).decode("ascii")
        }

    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    from datetime import date, timedelta

    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed)}",
        )

    def validate_date(value, field):
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{field} must be a valid YYYY-MM-DD date",
            )

    def month_bounds(value):
        if value is None:
            raise HTTPException(status_code=422, detail="month is required for this endpoint")
        try:
            parsed = datetime.strptime(value, "%Y-%m").date()
            if parsed.strftime("%Y-%m") != value:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
        first = parsed.replace(day=1)
        next_month = (
            first.replace(year=first.year + 1, month=1)
            if first.month == 12
            else first.replace(month=first.month + 1)
        )
        return first, next_month

    collection = "attendance_logs"

    if endpoint == "attendance_list":
        start = validate_date(date_from, "date_from")
        end = validate_date(date_to, "date_to")
        if start and end and start > end:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
                raise HTTPException(status_code=422, detail="Invalid status")
            query["status"] = status
        date_filter = {}
        if start:
            date_filter["$gte"] = start.isoformat()
        if end:
            date_filter["$lte"] = end.isoformat()
        if date_filter:
            query["date"] = date_filter

        explain = db.command(
            "explain",
            {
                "find": collection,
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")
        month_start, next_month = month_bounds(month)
        if not db.employees.find_one({"emp_code": emp_code}):
            raise HTTPException(status_code=404, detail="Employee not found")

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {
                "$gte": month_start.isoformat(), "$lt": next_month.isoformat()
            }}},
            {"$addFields": {"day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                ]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
    elif endpoint == "department_summary":
        month_start, next_month = month_bounds(month)
        month_end = next_month - timedelta(days=1)
        employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
        if department is not None:
            employee_match["department"] = department
        collection = "employees"
        pipeline = [
            {"$match": employee_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$employee_code"]},
                        {"$gte": ["$date", month_start.isoformat()]},
                        {"$lt": ["$date", next_month.isoformat()]},
                    ]}}},
                    {"$addFields": {"day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
                ],
                "as": "logs",
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [{"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": ["$logs.day_of_week", [2, 3, 4, 5, 6]]},
                ]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                "work_hours_values": {"$push": {"$cond": [{"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$logs.work_hours", None]},
                ]}, "$logs.work_hours", None]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {"$let": {
                    "vars": {"valid_hours": {"$filter": {
                        "input": "$work_hours_values", "as": "hours",
                        "cond": {"$ne": ["$$hours", None]},
                    }}},
                    "in": {"$cond": [
                        {"$gt": [{"$size": "$$valid_hours"}, 0]},
                        {"$avg": "$$valid_hours"},
                        None,
                    ]},
                }},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }},
            {"$sort": {"department": 1}},
        ]
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        month_start, next_month = month_bounds(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees", "localField": "_id",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0, "emp_code": "$_id", "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1, "late_count": 1,
            }},
        ])
        # The real route computes standard-competition ranks in Python after
        # fetching all sorted employees, so do not insert a MongoDB $limit.
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )



    else:  # department_trend
        if not department:
            raise HTTPException(
                status_code=422,
                detail="department is required",
            )

        start = validate_date(from_date, "from")
        end = validate_date(to_date, "to")

        if start is None or end is None:
            raise HTTPException(
                status_code=422,
                detail="from and to are required",
            )

        if end < start or (end - start).days >= 92:
            raise HTTPException(
                status_code=422,
                detail="Invalid trend date range; maximum is 92 days",
            )

        if db.employees.count_documents(
            {"department": department}
        ) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"Department '{department}' not found",
            )

        collection = "attendance_logs"

        # Explain the indexed date-range query used by the trend.
        explain = db.command(
            "explain",
            {
                "find": collection,
                "filter": {
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": end.isoformat(),
                    }
                },
                "sort": {
                    "date": -1,
                    "emp_code": 1,
                },
            },
            verbosity="executionStats",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": make_json_safe(explain),
    }
