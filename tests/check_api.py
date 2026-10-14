"""
End-to-end check against a REAL MongoDB and a running server.

    docker run -d -p 27017:27017 mongo:7
    MONGO_DB=attendance_check uvicorn app.main:app --port 8000        # terminal 1
    MONGO_DB=attendance_check python tests/check_api.py                # terminal 2

WARNING: it wipes the database named in MONGO_DB. Never point it at real data.

It seeds the awkward cases from the problem statement (ties, no-log employees, weekend records,
mid-month and future joiners, an overnight shift, missing weekdays, legacy documents, a log for an
unknown employee), computes every analytics answer with a plain-Python reference implementation of
R1-R10, and compares that with what the API returns. Then it checks races and explain plans.
"""
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from pymongo import MongoClient

BASE = os.getenv("API", "http://127.0.0.1:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.environ["MONGO_DB"]]
UTC, IST = timezone.utc, timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
failures = 0


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, got, want):
    global failures
    if got != want:
        failures += 1
        print(f"FAIL {name}\n  got:  {got}\n  want: {want}")


def hu(x, places):
    return None if x is None else float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))


# --------------------------------------------------------------------------- seed
def seed():
    rng = random.Random(7)
    db.employees.delete_many({})          # delete, not drop: keeps the indexes the app created at startup
    db.attendance_logs.delete_many({})
    emps = []
    for i in range(1, 25):
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"E{i}", "email": f"e{i}@x.io",
                     "department": ["Eng", "Sales", "Support"][i % 3],
                     "shift_start": "22:00" if i == 5 else "09:30", "shift_end": "06:00" if i == 5 else "18:30",
                     "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, tzinfo=UTC)})
    emps[1]["joined_on"] = "2026-07-15"     # mid-month joiner
    emps[2]["joined_on"] = "2026-08-03"     # joins after July: not in July headcount
    emps.append({**emps[0], "emp_code": "EMP0099", "department": "Ghost", "joined_on": "2026-01-01"})  # no logs at all
    for code, dept in (("EMP0030", "Sales"), ("EMP0031", "Sales"), ("EMP0032", "Eng")):   # three-way tie at rank 1
        emps.append({**emps[0], "emp_code": code, "name": code, "department": dept})
    db.employees.insert_many([dict(e) for e in emps])

    logs = []
    day = date(2026, 6, 20)
    while day <= date(2026, 7, 31):
        for e in emps[:20]:
            if day.isoformat() < e["joined_on"] or rng.random() < 0.15:   # missing days
                continue
            if day.weekday() >= 5 and rng.random() < 0.85:                # few weekend records
                continue
            status = rng.choice(["PRESENT"] * 6 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status,
                   "punch_in": None, "punch_out": None, "work_hours": None,
                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
            if status in PRESENCE:
                start = datetime.combine(day, datetime.strptime(e["shift_start"], "%H:%M").time(), IST)
                pin = start + timedelta(minutes=rng.choice([-5, 0, 5, 12, 25, 60]))
                hours = rng.choice([3.5, 8, 9, 9.5])
                doc.update(punch_in=pin, punch_out=pin + timedelta(hours=hours), work_hours=hours,
                           late_minutes=rng.choice([0, 0, 0, 12, 25, 60]), overtime_minutes=rng.choice([0, 0, 35]),
                           half_day=hours < 4.5)
            if rng.random() < 0.05:                                       # legacy shape
                doc.pop("history"); doc.pop("half_day")
            logs.append(doc)
        day += timedelta(days=1)
    # Forced tie on the leaderboard: three employees with an identical, top July total (a Saturday record).
    for code in ("EMP0030", "EMP0031", "EMP0032"):
        logs.append({"emp_code": code, "date": "2026-07-04", "status": "PRESENT", "late_minutes": 2000,
                     "punch_in": datetime(2026, 7, 4, 6, tzinfo=UTC), "punch_out": None, "work_hours": None,
                     "overtime_minutes": 0, "half_day": False, "history": []})
    logs.append({"emp_code": "EMP7777", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 5000})
    db.attendance_logs.insert_many(logs)
    return emps


# --------------------------------------------------------------- reference model
def presence_value(r):
    return (0.5 if r.get("half_day") else 1) if r.get("status") in PRESENCE else 0


def is_weekday(d):
    return date.fromisoformat(d).weekday() < 5


def month_days(month):
    first = date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def ref_monthly(emp, month, logs):
    first, last = month_days(month)
    mine = [r for r in logs if r["emp_code"] == emp["emp_code"] and first <= r["date"] <= last]
    d, wd = date.fromisoformat(max(first, emp["joined_on"])), 0
    while d.isoformat() <= last:
        wd += d.weekday() < 5
        d += timedelta(days=1)
    present = sum(presence_value(r) for r in mine if is_weekday(r["date"]))
    late = [r for r in mine if (r.get("late_minutes") or 0) > 0]
    return {"emp_code": emp["emp_code"], "month": month, "working_days": wd, "present_days": hu(present, 2),
            "leave_days": sum(r["status"] == "LEAVE" for r in mine), "late_count": len(late),
            "total_late_minutes": sum(r["late_minutes"] for r in late),
            "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in mine),
            "attendance_pct": hu(Decimal(str(present)) * 100 / wd, 2) if wd else None}


def ref_summary(month, emps, logs, department=None):
    first, last = month_days(month)
    out = {}
    for e in emps:
        if e["joined_on"] > last or (department is not None and e["department"] != department):
            continue
        s = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present_days": 0,
                                             "wh": [], "late_count": 0, "total_late_minutes": 0,
                                             "leave_count": 0, "on_duty_count": 0})
        s["headcount"] += 1
        for r in logs:
            if r["emp_code"] != e["emp_code"] or not first <= r["date"] <= last:
                continue
            if is_weekday(r["date"]):
                s["present_days"] += presence_value(r)
            if r["status"] in PRESENCE and isinstance(r.get("work_hours"), (int, float)):
                s["wh"].append(Decimal(str(r["work_hours"])))
            if (r.get("late_minutes") or 0) > 0:
                s["late_count"] += 1
                s["total_late_minutes"] += r["late_minutes"]
            s["leave_count"] += r["status"] == "LEAVE"
            s["on_duty_count"] += r["status"] == "ON_DUTY"
    items = []
    for dept in sorted(out):
        s = out[dept]
        wh = s.pop("wh")
        s["present_days"] = hu(s["present_days"], 2)
        s["avg_work_hours"] = hu(sum(wh) / len(wh), 2) if wh else None
        items.append(s)
    return {"month": month, "items": items}


def ref_leaderboard(month, limit, emps, logs, department=None):
    first, last = month_days(month)
    by_code = {e["emp_code"]: e for e in emps}
    totals = {}
    for r in logs:
        e = by_code.get(r["emp_code"])
        if e and first <= r["date"] <= last and (r.get("late_minutes") or 0) > 0 \
                and (department is None or e["department"] == department):
            t = totals.setdefault(r["emp_code"], [0, 0])
            t[0] += r["late_minutes"]
            t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for i, (code, (total, n)) in enumerate(rows):
        rank = i + 1 if i == 0 or rows[i - 1][1][0] != total else items[-1]["rank"]
        if rank > limit:
            break
        items.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"],
                      "department": by_code[code]["department"], "total_late_minutes": total, "late_count": n})
    return {"month": month, "items": items}


def ref_trend(dept, start, end, emps, logs):
    members = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    rows, d = [], date.fromisoformat(start)
    while d.isoformat() <= end:
        ds = d.isoformat()
        day_logs = [r for r in logs if r["emp_code"] in codes and r["date"] == ds]
        hc = sum(e["joined_on"] <= ds for e in members)
        present = sum(presence_value(r) for r in day_logs)
        working = d.weekday() < 5
        rate = hu(Decimal(str(present)) / hc, 4) if working and hc else None
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": hu(present, 2),
                     "late_count": sum((r.get("late_minutes") or 0) > 0 for r in day_logs),
                     "attendance_rate": rate})
        d += timedelta(days=1)
    for i, row in enumerate(rows):
        window = [r["attendance_rate"] for r in rows[max(0, i - 6):i + 1] if r["attendance_rate"] is not None]
        row["moving_avg_7d"] = hu(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None
    return {"department": dept, "items": rows}


# --------------------------------------------------------------------------- run
def main():
    emps = seed()
    logs = list(db.attendance_logs.find({}, {"_id": 0}))
    status, _ = call("GET", "/health")
    check("health", status, 200)

    for e in emps:
        _, got = call("GET", f"/analytics/employees/{e['emp_code']}/monthly?month=2026-07")
        check(f"monthly {e['emp_code']}", got, ref_monthly(e, "2026-07", logs))
    for dept in (None, "Eng", "Ghost", "Nope"):
        q = "" if dept is None else f"&department={dept}"
        _, got = call("GET", f"/analytics/departments/summary?month=2026-07{q}")
        check(f"summary {dept}", got, ref_summary("2026-07", emps, logs, dept))
    for limit in (1, 2, 3, 5, 50):
        for dept in (None, "Sales"):
            q = "" if dept is None else f"&department={dept}"
            _, got = call("GET", f"/analytics/leaderboard/late?month=2026-07&limit={limit}{q}")
            check(f"leaderboard {limit} {dept}", got, ref_leaderboard("2026-07", limit, emps, logs, dept))
    for dept, start, end in (("Eng", "2026-06-25", "2026-07-31"), ("Support", "2026-07-01", "2026-07-10"),
                             ("Ghost", "2026-07-01", "2026-07-07")):
        _, got = call("GET", f"/analytics/departments/{dept}/trend?from={start}&to={end}")
        check(f"trend {dept}", got, ref_trend(dept, start, end, emps, logs))
    check("trend 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0], 404)
    check("trend 93 days", call("GET", "/analytics/departments/Eng/trend?from=2026-05-01&to=2026-08-01")[0], 422)

    # Races: exactly one winner each time.
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-03"})
    pin = int(datetime(2026, 8, 3, 9, 30, tzinfo=IST).timestamp() * 1000)
    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda _: call("POST", "/attendance/punch-in",
                                             {"emp_code": "EMP0001", "punched_at": pin})[0], range(20)))
        check("punch-in race", sorted(codes), [201] + [409] * 19)
        codes = list(pool.map(lambda _: call("POST", "/attendance/punch-out",
                                             {"emp_code": "EMP0001", "punched_at": pin + 8 * 3600_000})[0], range(20)))
        check("punch-out race", sorted(codes), [200] + [409] * 19)
        outs = [pin + (8 * 3600 + 60 * i) * 1000 for i in range(1, 11)]
        codes = list(pool.map(lambda t: call("PATCH", "/attendance/EMP0001/2026-08-03",
                                             {"punch_out": t, "reason": "race test", "regularized_by": "qa"})[0], outs))
    _, rec = call("GET", "/attendance?emp_code=EMP0001&date_from=2026-08-03&date_to=2026-08-03")
    check("patch race keeps every successful edit", len(rec["items"][0]["history"]), codes.count(200))

    # Explain: index scan, no collection scan.
    for endpoint, q in (("attendance_list", ""), ("attendance_list", "status=LEAVE"),
                        ("attendance_list", "emp_code=EMP0004&date_from=2026-07-01"),
                        ("employee_monthly", "emp_code=EMP0004&month=2026-07"),
                        ("department_summary", "month=2026-07"), ("department_summary", "month=2026-07&department=Eng"),
                        ("late_leaderboard", "month=2026-07"), ("late_leaderboard", "month=2026-07&department=Eng"),
                        ("department_trend", "department=Eng&from=2026-07-01&to=2026-07-31")):
        status, body = call("GET", f"/admin/explain/{endpoint}?{q}")
        plan = json.dumps(body)
        check(f"explain {endpoint} {q}", (status, "IXSCAN" in plan, "COLLSCAN" in plan), (200, True, False))
    check("explain missing param", call("GET", "/admin/explain/employee_monthly?month=2026-07")[0], 422)

    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} check(s) failed")


if __name__ == "__main__":
    main()
