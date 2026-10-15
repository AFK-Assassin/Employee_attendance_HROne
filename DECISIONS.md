# DECISIONS.md

Here are my answers to the 5 design questions, explaining my thought process, what I implemented, and what I learned along the way.

---

### 1. Indexes. Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

Here are the indexes I created in `create_indexes()` on app startup:

**On `employees` collection:**
1. `emp_code` (unique = True)
   - *Why:* Guarantees that employee codes can never be duplicated in the database. Also makes individual lookups like `db.employees.find_one({"emp_code": ...})` an index scan (O(1)) instead of scanning the whole collection.
2. `[("department", ASCENDING), ("emp_code", ASCENDING)]`
   - *Why:* In `GET /employees`, users filter by department and the spec requires sorting by `emp_code ASC`. Putting both in a compound index allows MongoDB to filter and return pre-sorted results without needing an in-memory sort stage.
3. `joined_on`
   - *Why:* Used when filtering employees by join date (for example in department summary analytics where we only count employees who joined on or before the month).

**On `attendance_logs` collection:**
1. `[("emp_code", ASCENDING), ("date", ASCENDING)]` (unique = True)
   - *Why:* Business rule R1 requires that an employee can have at most one attendance log per date. Making this a unique compound index enforces this guarantee at the database level and protects against simultaneous duplicate punch-ins.
2. `[("date", DESCENDING), ("emp_code", ASCENDING)]`
   - *Why:* Directly matches the default sorting requirement of `GET /attendance` (most recent date first, then by employee code). This avoids sorting in Python memory or slow collscans.

**What I considered and rejected:**
- I initially considered adding an index on `[("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]` or indexing `status` by itself.
- Why I rejected it: `status` only has a couple of distinct values (PRESENT, HALF_DAY, ABSENT), which means very low cardinality. The existing unique index on `(emp_code, date)` already narrows down any query to at most one record. Adding `status` as a third field would just increase write latency and index storage without helping our analytics queries (which mostly aggregate across date ranges or departments).

---

### 2. Punch-in race. Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

Here is the exact step-by-step of what happens if two identical punch-in requests hit the server at the exact same moment:

1. Both incoming HTTP requests enter the `punch_in` route concurrently.
2. Both fetch the employee document using `db.employees.find_one({"emp_code": ...})` to check shift timings. Both find the employee successfully.
3. Both convert the punch timestamp to IST and determine the shift date (e.g., `"2026-03-02"`).
4. Both compute late minutes and prepare an attendance log document with `emp_code` and `date`.
5. Both call `db.attendance_logs.insert_one(doc)`.
6. At the database layer, MongoDB tries to insert the documents into the `attendance_logs` collection. Because of the unique compound index on `(emp_code, date)`, MongoDB's internal locking allows only the first insert to complete.
7. The first request succeeds and returns `201 Created` with the punch-in record.
8. The second request immediately fails inside MongoDB with a `pymongo.errors.DuplicateKeyError` (error code 11000).
9. In my code, I wrapped `insert_one` in a `try...except DuplicateKeyError:` block. When this error is caught, the code raises an `HTTPException(status_code=409, detail="Attendance log already exists for this date")`.
10. The second request receives a `409 Conflict`.

*Note on my learning process:*
When I first started, I thought I could just write:
`if db.attendance_logs.find_one({"emp_code": ..., "date": ...}): raise 409`
Then I learned about "Time of Check to Time of Use" (TOCTOU) race conditions. If two requests run `find_one` at the same instant, both will see nothing in the db, both pass the check, and both attempt to insert. I used AI to understand how production systems handle concurrency without heavy locking, and learned that relying on database unique constraints is the standard pattern.

---

### 3. Ties. Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

**What it returns:**
Both tied employees are included in the response, even if the total number of returned rows exceeds `limit`.

For example, if `limit = 10` and:
- Rank 9: Employee A (45 late minutes)
- Rank 10: Employee B (40 late minutes)
- Rank 10: Employee C (40 late minutes)
- Rank 12: Employee D (35 late minutes)

My endpoint returns all 11 employees (up to Employee C) because both Employee B and C share Rank 10, and both satisfy `rank <= limit`.

**Why:**
The problem statement and business rules specify standard competition ranking (often called "1224 ranking"). In competition ranking, tied items share the same rank, and the next rank is skipped. Furthermore, the requirement states that anyone whose rank is `<= limit` must be returned. Cutting off one of the tied employees arbitrarily just to force the list length to equal `limit` would be unfair and violate the ranking spec.

*How I implemented it & AI assistance:*
Doing tie-aware ranking on large datasets in Python memory is slow and prone to bugs with slicing. While reading about MongoDB capabilities, I learned about window functions introduced in Mongo 5.0+. Because I had never written a `$setWindowFields` pipeline before, I used AI to explain the syntax of `$setWindowFields` with the `$rank` operator vs `$denseRank`. Once I understood the difference (`$rank` skips ranks after ties, which is what 1224 ranking requires), I built the aggregation pipeline:
1. `$match` on date range and `late_minutes > 0`.
2. `$group` by employee to calculate total late minutes.
3. `$sort` by `late_minutes DESC, emp_code ASC`.
4. `$setWindowFields` to assign competition `$rank`.
5. `$match` where `rank <= limit`.

---

### 4. Headcount. How does your department summary make sure employees with zero logs are counted?

The most common trap in attendance reporting is starting the aggregation from `attendance_logs`. If an employee was absent the whole month, on long leave, or simply never punched in, they will have zero records in `attendance_logs`. If you query `attendance_logs`, that employee is invisible and your department headcount will be wrong.

**How I solved it:**
1. I started the query from the **`employees`** collection, not `attendance_logs`.
2. I filter `employees` by `department` and make sure their `joined_on` date is on or before the last day of the requested month. This gives the true total headcount of people employed in that department during that month.
3. Then I use a `$lookup` stage (equivalent to a SQL LEFT JOIN) to pull in any matching attendance logs from `attendance_logs` where `emp_code` matches and the log's `date` falls within the target month.
4. If an employee has zero punches in that month, the `$lookup` simply returns an empty array `[]`.
5. When computing department totals, that employee is still counted in the total headcount (`total_employees`), but contributes 0 to present days or late days.

This guarantees that even if nobody in a department showed up for the entire month, the department summary still correctly reports the active employee headcount.

---

### 5. One thing you would change if this had to serve 100x the data.

Right now, in the department trend endpoint (`/analytics/departments/{department}/trend`), the logic fetches the logs within the date range (up to 92 days) and computes the day-by-day attendance counts, gap-filling for missing dates, and the 7-day rolling average in Python memory.

For the current scope and small departments this works fine. But if data grew by 100x (say 50,000 employees per department and millions of attendance records):
- Fetching raw punch records across a whole department into the FastAPI application memory would cause high CPU and RAM usage, and create high latency.

**What I would change:**
I would introduce **daily pre-aggregated attendance rollups**:
- Either via a daily scheduled background job (or a MongoDB scheduled trigger) running at midnight, or updating a `daily_department_stats` collection incrementally whenever a punch occurs.
- The `daily_department_stats` collection would store pre-calculated numbers: `{ department, date, headcount, present_count, late_count }`.
- When a user calls `/analytics/departments/{department}/trend`, instead of scanning raw logs for thousands of workers across 90 days, the query only needs to read ~90 pre-calculated daily summary documents. Computing a 7-day rolling average over 90 numbers is instantaneous.

*Concepts I explored with AI:*
I also researched whether MongoDB could do this on-the-fly using pipeline operators like `$densify` (to fill missing dates) and `$movingAverage`. AI explained how these operators work in MongoDB 5.0+. While powerful, for 100x data scale, pre-aggregating daily rollups is the more practical architectural decision because it turns an expensive multi-record calculation into an O(N_days) lookup.
