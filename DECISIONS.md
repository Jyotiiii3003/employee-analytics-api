# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees and unique `(emp_code, date)` on attendance_logs: they enforce identity and serve punch-in and monthly lookups. `(date desc, emp_code)` serves the default list sort and the date-range scans in the leaderboard and trend. `(status, date, emp_code)` serves status filters, `(emp_code, punch_in)` the punch-out lookup, `(department, ...)` and `joined_on` the employee list and headcount. I rejected copying `department` onto each log: it saves a lookup but duplicates data that can change.
2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.
3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and apply `limit` after ranking, so both tied employees are returned, even if that exceeds `limit`, ordered by `emp_code`.
4. **Headcount.** The summary starts from employees, uses `$lookup` and `$unwind` with `preserveNullAndEmptyArrays`, so employees with no logs still reach `$group`; headcount is the size of the `emp_code` set.
5. **At 100x data** I would keep pre-aggregated monthly totals, updated on punch and correction, and use keyset pagination instead of `skip`.
