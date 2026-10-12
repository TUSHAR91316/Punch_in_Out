# DECISIONS.md

1. **Indexes.**
   - `employees`: unique `{"emp_code": 1}` for fast lookup and insertion integrity; compound `{"department": 1, "joined_on": 1}` backing department filtering and historical headcount lookups.
   - `attendance_logs`: unique compound `{"emp_code": 1, "date": 1}` guaranteeing one record per day; `{"date": -1, "emp_code": 1}` backing paginated listing; `{"emp_code": 1, "punch_in": -1}` for punch-out resolution; `{"date": 1, "late_minutes": 1}` and `{"emp_code": 1, "date": 1, "status": 1}` backing monthly and leaderboard aggregations.
   - *Rejected*: indexing `{"history.at": 1}` because the audit log is strictly appended, never queried independently.

2. **Punch-in race.**
   Both requests fetch the employee document (read-only) and resolve the attendance date. Both issue `insert_one`. WiredTiger resolves the race at the index level: the first write locks the unique key `(emp_code, date)` and succeeds (HTTP 201). The second fails with `DuplicateKeyError`, caught by our exception handler to return HTTP 409 safely without race conditions.

3. **Ties.**
   The leaderboard uses standard competition ranking via `$rank` in `$setWindowFields` (e.g. 1, 2, 2, 4). The `limit` filter is applied *after* ranking (`$match: {"rank": {"$lte": limit}}`). If two employees tie at the cutoff rank, both are returned, resulting in more than `limit` items, ordered secondary by `emp_code` ascending.

4. **Headcount.**
   The aggregation roots on `employees` filtered by `joined_on <= month_end`, grouping by department to establish baseline headcount first. It then `$lookup` joins `attendance_logs`. Employees without logs still contribute to the headcount counter, while contributing zeroes to attendance metrics.

5. **One thing you would change for 100x data.**
   Introduce daily attendance rollups using MongoDB Change Streams or a scheduled aggregation into a materialized `department_daily_rollups` collection. Aggregating over 10M raw attendance documents on every request becomes I/O heavy; pre-aggregated daily summaries turn monthly metrics and trends into sub-millisecond point lookups.
