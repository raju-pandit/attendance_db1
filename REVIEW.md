# REVIEW.md

Defects found in the starter `app/main.py`. Times below are IST; "fixed" refers to the function of the same purpose in the new file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | Module setup / startup | No indexes are created. There is no unique index on `employees.emp_code` or on `attendance_logs (emp_code, date)`, and nothing backs the list/analytics queries. | Fire 20 identical punch-ins in parallel: several 201s and duplicate records. `explain` on `GET /attendance` shows `COLLSCAN`. | `ensure_indexes()` runs in the `lifespan` handler, idempotently, and creates both unique indexes plus the query indexes. |
| 2 | `MongoClient(...)` | Created without `tz_aware=True`, so PyMongo returns naive datetimes and every conversion has to guess the zone. | Reading a seeded `punch_in` and calling `.timestamp()` on it uses the server's local zone: off by 5h30 on an IST machine. | `tz_aware=True`; all instants are aware UTC internally and epoch ms at the boundary. |
| 3 | `compute_late_minutes` | Grace check is done on *floored* minutes (`minutes > 10`), so anything from 10:00 to 10:59 after shift start is not late. R2 says strictly more than 10:00. | Shift 09:30, punch 09:40:30 → starter 0, expected 10. | Compare seconds: late when `seconds > 600`, then `seconds // 60`. |
| 4 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour, minute)`, i.e. on the punch's own calendar day, in whatever zone the datetime happens to be in. Wrong for overnight shifts and for any non-IST datetime. | Shift 22:00-06:00, punch 00:40: starter compares with 22:00 the same day → 0. Expected 160 (shift started the previous evening). | `shift_bounds(date, emp)` builds the shift start in IST on the attendance date (R1). |
| 5 | `compute_work_hours` | Uses `round()`, which is banker's/float rounding, not half-up (R4). | 09:00:00 → 11:07:30 = 2.125 h: starter 2.12, expected 2.13. | Integer seconds → `Decimal` → `quantize(ROUND_HALF_UP)`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3). | Shift end 18:30, punch-out 18:45 → starter 15, expected 0. | Return minutes only when `>= 30`. |
| 7 | `compute_overtime` | Shift end is put on the record's date even for overnight shifts, so it lies before the punch-in. | 22:00-06:00, out 06:40 next day → starter 1480 min, expected 40. | `shift_bounds` moves the end to the next day when `shift_end <= shift_start`. |
| 8 | `compute_overtime` | Subtracts an aware datetime (`tzinfo=IST`) from the naive `punch_out` produced by `fromtimestamp`. | `TypeError: can't subtract offset-naive and offset-aware datetimes` → 500. | Everything is aware. |
| 9 | `/health` | Always returns 200, even when MongoDB is down. | Stop MongoDB, call `/health` → 200. | `ping`; 503 on failure. |
| 10 | `POST /employees` | Check-then-insert with no unique index: two concurrent creates both succeed. | Parallel identical creates → two 201s, two documents. | Plain insert; the unique index decides; `DuplicateKeyError` → 409. |
| 11 | `POST /employees` | No validation: any `emp_code`, email, `HH:MM`, impossible `joined_on`, and `shift_start == shift_end` all accepted. | `{"shift_start": "9.30"}` is stored, later punch-ins crash with a 500. | Pydantic patterns from the contract, a real-date check, and a model validator for equal shift times. |
| 12 | `POST /employees` | `created_at = datetime.now()` is naive server-local time (stored as if UTC) and is returned as an ISO string, not epoch ms. | On an IST server the stored instant is 5h30 ahead; response has `"created_at": "2026-..."`. | `datetime.now(UTC)`, serialized with `to_ms`. |
| 13 | `GET /employees` | `skip = page * page_size`: page 1 skips the first page. | 3 employees, `page=1` → empty list. | `(page - 1) * page_size`. |
| 14 | `GET /employees` | `total` counts the whole collection, ignoring `department` (R10). | `?department=Sales` with 2 sales staff out of 6 → total 6. | `count_documents(q)`. |
| 15 | `GET /employees` | No sort (contract: by `emp_code`), and no bounds on `page`/`page_size`. | Order changes between calls; `page=0` → negative skip → 500; `page_size=0` → `limit(0)` returns everything. | `.sort("emp_code")`, `Query(ge=1)`, `page_size` 1-100. |
| 16 | `POST /attendance/punch-in` | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises. | `{"emp_code": "EMP9999"}` → 500 instead of 404. | `get_employee_or_404`. |
| 17 | `POST /attendance/punch-in` | `datetime.fromtimestamp(ms/1000)` uses the server's zone, so `date` is the server's calendar date, not IST, and the overnight rule is missing. | On a UTC server, a 02:00 IST punch gets yesterday's date; a 00:40 night-shift punch gets a new record. | `attendance_date()` implements R1 in IST. |
| 18 | `POST /attendance/punch-in` | Check-then-insert race (and no unique index). | 20 parallel identical punch-ins → several 201s. | Insert only; unique `(emp_code, date)`; `DuplicateKeyError` → 409. |
| 19 | `POST /attendance/punch-in` | `punched_at: int` is lax: seconds, floats and numeric strings are accepted; milliseconds are not truncated (R1). | `1783312500` (seconds) creates a 1970 record; `"1783312500000"` is accepted. | Strict int, 1e11 to 4102444800000; truncated to whole seconds. |
| 20 | `POST /attendance/punch-in` | `status` is any string. | `"status": "ABSENT"` or `"foo"` creates a record. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 21 | `punch-in` / `GET /attendance` | Responses expose `_id` as `id` and return datetimes as ISO strings; legacy records missing `half_day`/`history` come back without them. | Response contains `"id"`, `"punch_in": "2026-07-06T..."`. | `serialize_record()`: no id, instants in ms, defaults for missing fields. |
| 22 | `GET /attendance` | Loads the whole collection into memory and sorts/paginates in Python. | Slow and memory-hungry at 100k records; `explain` shows `COLLSCAN`. | Mongo `find().sort().skip().limit()` on an index, `count_documents` for `total`. |
| 23 | `GET /attendance` | Sort has no `emp_code` tiebreak; no validation of dates, `status`, `page`, `page_size`; `date_from > date_to` silently returns nothing. | Same-date rows in random order; `date_to=2026-7-1` compares wrongly as a string. | Sort `date` desc, `emp_code` asc; date pattern + real-date check; 422 when `date_from > date_to`. |

## Looked at and decided were not defects
- `load_dotenv()` without `override=True`: real environment variables already win, as the contract requires.
- Storing `date` / `joined_on` as `YYYY-MM-DD` strings and comparing them as strings: correct, since ISO dates sort lexicographically.
- The `IST` constant as a fixed +05:30 offset: India has no DST, so this is exact.
