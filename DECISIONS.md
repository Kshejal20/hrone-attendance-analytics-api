# DECISIONS.md

1. **Indexes.** I enforce unique `employees.emp_code` and unique `(attendance_logs.emp_code, date)` so concurrent creates and punch-ins cannot create duplicates. Date/employee, employee/date, and status/date indexes support filtered, sorted attendance queries and month-based analytics. Department/join-date helps headcount lookups. I avoid indexing every field because each index increases write cost and storage.

2. **Punch-in race.** Both requests may reach the insert. MongoDB's unique compound index allows one insert to succeed; the other raises `DuplicateKeyError`, which the API converts to 409. A pre-insert existence check alone would not be safe.

3. **Ties.** Ranking is calculated with `$rank` before applying the rank cutoff. Employees tied at the cutoff share a rank and are all returned; `emp_code` gives deterministic order within a rank.

4. **Headcount.** Department summary starts from employees who joined by the period end, then looks up that employee's logs. Employees with no logs therefore remain in the aggregation and count toward headcount.

5. **100x data.** I would review query plans and index selectivity using production-like data, add load tests and monitoring, and consider pre-aggregated daily summaries for analytics while keeping raw attendance as the source of truth.
