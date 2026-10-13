# DECISIONS.md

1. **Indexes.**
   Unique `emp_code` on `employees`; compound `(joined_on, department)` and `(department, joined_on)` for department queries and headcount. On `attendance_logs`: unique `(emp_code, date)` enforcing one daily record; `(date, emp_code)` for paginated listing; `(emp_code, punch_in)` for punch-out; `(date, late_minutes)` for leaderboard. Rejected indexing `history.at` because audit logs are purely appended, never queried directly.

2. **Punch-in race.**
   Both requests compute the date and attempt `insert_one`. WiredTiger resolves concurrency at the unique `(emp_code, date)` index: the first write locks the key and returns 201; the second raises `DuplicateKeyError`, caught to return HTTP 409 without races.

3. **Ties.**
   The leaderboard assigns standard competition rank via `$rank` in `$setWindowFields` (1, 2, 2, 4). The `limit` applies post-ranking (`rank <= limit`). All employees tied at or below the cutoff are returned, even if exceeding `limit` rows, sorted secondary by `emp_code` ascending.

4. **Headcount.**
   The aggregation roots on `employees` filtered by `joined_on <= month_end`, grouping by department to establish baseline headcount before `$lookup` joining `attendance_logs`. Employees without logs still increment headcount while contributing zeroes to attendance metrics.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily attendance into a materialized `department_daily_rollups` collection via Change Streams. At 100x data, scanning raw logs degrades latency; pre-computed daily rollups convert monthly analytics and trends into sub-millisecond point lookups.
