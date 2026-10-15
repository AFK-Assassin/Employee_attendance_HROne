
# DECISIONS.md

Here are my answers to the 6 design questions, explaining my thought process, what I implemented, and what I learned along the way.

---

### 1. Indexes. Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

Here are the indexes I created in `create_indexes()` on app startup.

**On the `employees` collection:**

1. **`emp_code` (unique = True)**
   - **Why:** Guarantees that employee codes cannot be duplicated in the database. It also makes individual lookups like `db.employees.find_one({"emp_code": ...})` efficient by using an index instead of scanning the entire collection.

2. **`[("department", ASCENDING), ("emp_code", ASCENDING)]`**
   - **Why:** In `GET /employees`, users can filter by department, and the specification requires sorting by `emp_code ASC`. This compound index can help MongoDB filter employees by department and return results in the required order without an additional in-memory sort.

3. **`joined_on`**
   - **Why:** Supports queries that filter employees by their joining date, such as department summary analytics that count employees who joined on or before the end of the requested month.

**On the `attendance_logs` collection:**

1. **`[("emp_code", ASCENDING), ("date", ASCENDING)]` (unique = True)**
   - **Why:** Business rule R1 requires that an employee can have at most one attendance log per date. The unique compound index enforces this rule at the database level and protects against duplicate records caused by concurrent punch-in requests.

2. **`[("date", DESCENDING), ("emp_code", ASCENDING)]`**
   - **Why:** Matches the default sorting requirement of `GET /attendance`, which returns the most recent date first and then sorts by employee code. This index can help MongoDB avoid an in-memory sort for compatible queries.

**What I considered and rejected:**

- I initially considered adding an index on `[("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]` or creating an index on `status` alone.
- **Why I rejected it:** The `status` field has only a few possible values, such as `PRESENT`, `HALF_DAY`, and `ABSENT`, giving it low cardinality. The existing unique index on `(emp_code, date)` already efficiently identifies an employee's attendance record for a specific date. Adding `status` as a third field would increase index storage and write overhead without providing enough benefit for the current query patterns.

---

### 2. Punch-in race. Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

Here is what happens when two identical punch-in requests reach the server concurrently:

1. Both incoming HTTP requests enter the `punch_in` route.
2. Both fetch the employee document using `db.employees.find_one({"emp_code": ...})` to check the employee's shift timings. Both find the employee successfully.
3. Both convert the punch timestamp to IST and determine the attendance date, for example, `"2026-03-02"`.
4. Both calculate late minutes and prepare an attendance log document containing `emp_code` and `date`.
5. Both attempt to insert their documents using `db.attendance_logs.insert_one(doc)`.
6. At the database level, MongoDB enforces the unique compound index on `(emp_code, date)`. Only one of the two inserts can succeed for that employee and date.
7. The first request succeeds and returns `201 Created` with the punch-in record.
8. The second request fails with `pymongo.errors.DuplicateKeyError`, typically with error code `11000`.
9. In my code, I wrapped `insert_one` in a `try...except DuplicateKeyError` block. When the exception is caught, the code raises an `HTTPException` with status code `409` and the detail `"Attendance log already exists for this date"`.
10. The second request receives a `409 Conflict` response.

**Note on my learning process:**

Initially, I thought I could prevent duplicate attendance records by checking whether a record already existed:

`if db.attendance_logs.find_one({"emp_code": ..., "date": ...}): raise 409`

However, I learned about **Time of Check to Time of Use (TOCTOU) race conditions**. If two requests perform the check at nearly the same time, both might find no existing record and then attempt to insert one.

I used AI assistance and documentation to understand how production systems handle concurrency without relying on application-level locking. I learned that enforcing uniqueness at the database level is a reliable way to protect data integrity under concurrent requests.

---

### 3. Ties. Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

**What it returns:**

Both tied employees are included in the response, even if the total number of returned employees exceeds `limit`.

For example, if `limit = 10` and the leaderboard contains:

- Rank 9: Employee A — 45 late minutes
- Rank 10: Employee B — 40 late minutes
- Rank 10: Employee C — 40 late minutes
- Rank 12: Employee D — 35 late minutes

The endpoint returns 11 employees, including both Employee B and Employee C, because both have a rank less than or equal to 10. Employee D is excluded.

**Why:**

The business rules specify **standard competition ranking**, commonly represented by the sequence `1, 2, 2, 4`.

Employees with equal scores share the same rank, and the next rank is skipped. The requirement also states that anyone whose rank is `<= limit` must be returned.

Excluding one tied employee simply to force the response to contain exactly `limit` rows would violate this requirement.

**How I implemented it and what I learned:**

Calculating tie-aware rankings in Python memory can become inefficient and error-prone as the dataset grows. While exploring MongoDB aggregation capabilities, I learned about `$setWindowFields` and the `$rank` and `$denseRank` operators.

I used AI assistance to understand their syntax and differences. The `$rank` operator supports standard competition ranking, while `$denseRank` does not skip rank numbers after ties.

My aggregation pipeline follows these steps:

1. `$match` on the date range and `late_minutes > 0`.
2. `$group` by employee to calculate total late minutes.
3. `$sort` by `late_minutes DESC, emp_code ASC`.
4. `$setWindowFields` to assign competition ranks.
5. `$match` where `rank <= limit`.

This approach implements the required tie-handling behavior through MongoDB aggregation.

---

### 4. Headcount. How does your department summary make sure employees with zero logs are counted?

A common problem in attendance reporting is starting the aggregation from the `attendance_logs` collection. Employees who never punched in during a month would have no attendance records and could therefore be omitted from the report.

**How I solved it:**

1. I started the aggregation from the `employees` collection rather than `attendance_logs`.
2. I filtered employees by department and ensured their `joined_on` date was on or before the last day of the requested month. This establishes the eligible employee headcount for that month, assuming the dataset does not require accounting for termination dates.
3. I used a `$lookup` stage, equivalent to a SQL `LEFT JOIN`, to find matching attendance logs where `emp_code` matches and the log's `date` falls within the requested month.
4. If an employee has no attendance logs during that month, the `$lookup` returns an empty array, `[]`.
5. The employee is still included in the total headcount, but contributes zero to the attendance counts derived from their logs.

This approach ensures that employees with zero attendance logs are not automatically excluded from department headcount calculations.

---

### 5. One thing you would change if this had to serve 100x the data.

Currently, in the department trend endpoint (`/analytics/departments/{department}/trend`), the application fetches attendance logs within the requested date range, up to 92 days, and calculates daily attendance counts, fills missing dates, and computes the seven-day rolling average in Python memory.

For the current scope and smaller departments, this approach is manageable. However, if the dataset grew by 100 times, for example, to 50,000 employees per department and millions of attendance records, fetching and processing raw attendance logs in the FastAPI application could increase CPU usage, memory consumption, and response latency.

**What I would change:**

I would introduce **daily pre-aggregated attendance rollups**.

These could be maintained through a scheduled background job or updated incrementally when attendance records are created. A separate `daily_department_stats` collection could store pre-calculated values such as:

`{ department, date, headcount, present_count, late_count }`

When a user calls `/analytics/departments/{department}/trend`, the application would retrieve approximately 90 daily summary documents instead of processing all the underlying attendance records. It could then calculate the seven-day rolling average over those daily values.

The implementation would need to handle corrections, duplicate events, and changes to employee headcount so that the summaries remain accurate.

**Concepts I explored with AI:**

I also explored MongoDB aggregation operators such as `$densify` and `$setWindowFields` for filling date gaps and calculating rolling statistics. MongoDB provides these capabilities in supported versions, although the exact availability of individual operators depends on the server version.

While these operators can simplify on-demand analytics, I believe pre-aggregated daily rollups would be a practical architectural improvement at a substantially larger scale because they reduce the amount of raw data processed for each trend request.

---

### 6. API Testing. How did you test your endpoints?

I used **Swagger UI** to interact with and test the FastAPI endpoints during development.

- Opened Swagger UI at `/docs` to explore the available endpoints and their request and response schemas.
- Sent API requests directly from the browser to check endpoint behavior.
- Inspected HTTP status codes and response bodies to understand the results of successful requests and errors.
- Used the interactive interface to provide request parameters and JSON payloads where required.
- Used the feedback from these tests to identify and troubleshoot API implementation issues during development.

Swagger UI made it convenient to explore and test the API without having to manually construct every HTTP request in a separate tool.

**What I learned:**

I learned how API documentation can also serve as an interactive testing interface. It helped me understand the relationship between request schemas, input validation, HTTP status codes, and API responses.

Swagger UI was useful during development because it allowed me to test endpoints directly and inspect their behavior as I built the application.
