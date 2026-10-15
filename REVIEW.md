# REVIEW.md

Here is the list of bugs and issues I found when going through the starter code in `app/main.py`.

### Summary table of bugs

| # | Where (function / line) | What is wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `list_employees` (L93) | Pagination skip formula was `page * page_size`. For page 1, it skipped the first 20 records right away. | Calling `GET /employees?page=1` returned an empty list even with data in the db. | Changed skip calculation to `(page - 1) * page_size`. |
| 2 | `list_employees` (L94) | `count_documents({})` had an empty filter, so it counted all employees in the collection instead of just the filtered department. | Filtering by department still showed the total count of the entire company. | Passed the filter dict `q` into `count_documents(q)`. |
| 3 | `list_employees` (L95) | Missing sort order. The API spec asks for `emp_code` ascending. | Returned order was whatever Mongo returned by default (arbitrary). | Added `.sort("emp_code", 1)` to the query. |
| 4 | `create_employee` (L82) | `datetime.now()` without timezone used local machine time, and `created_at` returned a datetime object instead of epoch ms. | Saw the response JSON had a string/datetime instead of integer milliseconds. | Used `datetime.now(tz=timezone.utc)` and converted to integer epoch ms (`int(ts.timestamp() * 1000)`). |
| 5 | `create_employee` (L107) | Checked for duplicate `emp_code` using `find_one` followed by `insert_one`. If two requests hit at the exact same millisecond, both pass the check. | Classic race condition issue under concurrent calls. | Put a unique index on `emp_code` and caught `DuplicateKeyError` to return 409 conflict. |
| 6 | `punch_in` (L101) | Didn't check if employee exists before accessing `emp["shift_start"]`. If `emp` was None, server threw an unhandled 500 error. | Tested punch-in with a fake employee code and got an internal server error instead of 404. | Added an explicit `if not emp: raise HTTPException(status_code=404, detail="Employee not found")`. |
| 7 | `punch_in` (L102) | `datetime.fromtimestamp(ms / 1000)` without timezone converted to local system timezone instead of UTC, and kept microsecond fractions. | Punches on servers in different timezones would shift unexpectedly. | Converted explicitly with `tz=timezone.utc` and rounded/truncated microseconds to whole seconds. |
| 8 | `punch_in` (L103) | Extracted date using `ts.date()` in UTC instead of IST, and didn't consider overnight shifts crossing midnight. | A punch-in late evening IST (like 11:30 PM IST) was being assigned to the wrong date, and night shifts after midnight failed. | Built a helper function to convert UTC to IST first and handle overnight shift date attribution rules. |
| 9 | `punch_in` (L132) | Checked if already punched in using `find_one` before `insert_one`. Again prone to race conditions if two punch requests arrive at once. | Two simultaneous requests could create duplicate punch logs for the same day. | Handled via a unique compound index on `(emp_code, date)` and catching `DuplicateKeyError` -> 409. |
| 10 | `compute_late_minutes` (L58) | Shift start time comparison constructed a datetime in local/UTC without matching IST timezone. | Employees starting on time showed up as hours late or negative late minutes depending on UTC offset. | Did the calculation cleanly in IST timezone so the hours and minutes match the employee's shift. |
| 11 | `compute_work_hours` (L64) | Used Python's standard `round(val, 2)` which uses banker's rounding (round half to even). | Numbers like 8.445 rounded to 8.44 instead of 8.45 as required by the business rule. | Used `decimal.Decimal` with `ROUND_HALF_UP`. |
| 12 | `compute_overtime` (L69) | Overtime counted any extra minutes (> 0) instead of the required >= 30 min minimum. Also broke if shift ended past midnight. | Working 10 minutes over gave overtime, which violates R3. | Added check `if ot_minutes >= 30` and handled overnight shift end date calculation. |
| 13 | `list_attendance` (L144) | Fetched all records from Mongo into a Python list in memory and sorted/paginated in Python. | On a large collection (like 100k rows) this would blow up memory and make responses super slow. | Moved sort, skip, and limit directly into the MongoDB query cursor. |
| 14 | `health` (L72) | Health check returned static `{"status": "ok"}` without checking if MongoDB was actually alive. | If Mongo service was stopped, `/health` still reported 200 OK. | Added `client.admin.command("ping")` and return 503 Service Unavailable if it fails. |


### What I checked and kept as-is (Not Defects)

- `IST = timezone(timedelta(hours=5, minutes=30))` is correct.
- Using FastAPI and Pydantic schemas for request validation.
- `load_dotenv()` placed at startup before database connections are initialized.


### My thought process & learning notes

Since I am relatively early in my backend journey, I approached this systematically:

1. **What I found directly on my own:**
   Going through the file line by line with the spec (`openapi.yaml` and `DATA_MODEL.md`), the logic bugs stood out pretty quickly when testing endpoints locally:
   - The pagination off-by-one error (page 1 returning nothing).
   - The health endpoint returning 200 even when Mongo wasn't running.
   - Missing 404 check when an employee doesn't exist.
   - Doing pagination and sorting in Python memory instead of letting MongoDB do the work.

2. **Advanced topics where I did research & used AI to learn:**
   There were a few tricky areas where my initial intuition wasn't enough, so I read the docs and used AI (ChatGPT) as a tutor to explain the concepts:
   - **Race conditions with check-then-insert:** I initially thought `find_one` was enough to prevent duplicates. After researching concurrency issues, I learned why that fails under simultaneous requests. I asked AI to explain how MongoDB unique indexes handle this atomically and how to catch `DuplicateKeyError`.
   - **Python Rounding vs Math Rounding:** I did not know Python's `round()` uses Banker's rounding (round-to-even) by default. Once I learned why `round(8.445, 2)` gave `8.44`, I looked up how to do half-up rounding. I used AI to verify the exact syntax using `decimal.Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)`.
   - **Overnight shifts across midnight in IST:** Mapping punch times to the correct shift date when employees work past midnight was tricky to think through. I mapped out the cases on paper (e.g., punch in at 00:30 for a 22:00-06:00 shift belongs to yesterday) and used AI to help double-check my date calculation logic.
   - **MongoDB aggregation pipelines:** For the analytics endpoints and leaderboard ties, I learned about `$setWindowFields` and `$rank` rather than trying to pull everything into memory. AI helped me understand the aggregation pipeline structure so I could write and test it properly.