# REVIEW.md

| # | Where | What is wrong | How to notice | How it is fixed |
|---|---|---|---|---|
| 1 | `/health` | It returns success without checking MongoDB. | Stop Atlas or use an invalid URI; it still reports `ok`. | Ping MongoDB and return 503 on a driver error. |
| 2 | `EmployeeIn` | It accepts malformed employee codes, emails, dates, and shift times. | Submit `emp_code: "x"`, an invalid date, or `25:90`. | Add schema constraints and parse calendar dates. |
| 3 | `EmployeeIn` | Equal shift start/end is accepted even though it is invalid for this contract. | Submit `09:30` for both fields. | Reject equal shift times. |
| 4 | `create_employee` | A check-then-insert duplicate check is race-prone. | Send simultaneous POSTs with the same `emp_code`. | Enforce uniqueness with a MongoDB unique index and handle `DuplicateKeyError` as 409. |
| 5 | `create_employee` | `created_at` is a naive local datetime rather than a UTC instant returned in epoch milliseconds. | Inspect the stored BSON value and response type. | Store UTC and serialize to epoch milliseconds. |
| 6 | `list_employees` | `skip = page * page_size` skips the first page. | Request page 1 with a small page size. | Use `(page - 1) * page_size`. |
| 7 | `list_employees` | `total` counts all employees instead of the filtered set. | Filter by a department and compare total with matching rows. | Call `count_documents` with the same filter. |
| 8 | `list_employees` | Page and page size have no bounds. | Request page 0 or page size 1000. | Validate page >= 1 and page_size 1..100. |
| 9 | `compute_late_minutes` | It uses the timestamp's existing clock/time zone and does not construct the shift start from the attendance date. | Test a UTC timestamp or an overnight shift. | Convert to IST and compare with the correct shift-start instant. |
| 10 | `compute_overtime` | It ignores overnight shifts and the 30-minute minimum. | Punch out 20 minutes after shift end or after midnight. | Calculate the correct shift-end instant and apply the 30-minute threshold. |
| 11 | `compute_work_hours` | Python `round` uses ties-to-even and floating-point arithmetic. | Test a duration whose hours round exactly at a half-cent boundary. | Use decimal half-up rounding. |
| 12 | `punch_in` | Unknown employees cause a key error instead of 404. | Punch in with a non-existent employee code. | Check the employee and return 404. |
| 13 | `punch_in` | It uses machine-local time and accepts timestamp values without strict epoch-millisecond validation. | Test seconds, floats, strings, or a non-IST machine. | Validate integer milliseconds, truncate to seconds, store UTC, and derive the attendance date in IST. |
| 14 | `punch_in` | The duplicate check is not concurrency-safe. | Send concurrent requests for the same employee/day. | Rely on the unique `(emp_code, date)` index and return 409 on duplicate-key errors. |
| 15 | `list_attendance` | It reads every matching record into Python and sorts/slices in memory. | Profile a large attendance collection. | Filter, sort, skip, limit, and count in MongoDB. |
| 16 | `list_attendance` | Equal-date rows have no `emp_code` tie-breaker, and the response leaks MongoDB `_id` as `id`. | Inspect same-date results and response fields. | Sort by date descending then employee code ascending; omit `_id` and `id`. |
| 17 | Entire file | Punch-out, regularization, analytics, and explain endpoints are missing. | Compare registered routes with `openapi.yaml`. | Implement each contract endpoint and test status codes and aggregation results. |
| 18 | Startup | No indexes are created for natural keys or list/analytics queries. | Inspect `getIndexes()` and explain plans. | Create idempotent unique and query-supporting indexes at startup. |

## Reviewed and retained

The use of `emp_code` as the employee identifier is correct; it is the contract's business key and must not be replaced with MongoDB `_id`. Keeping calendar dates such as `date` and `joined_on` as `YYYY-MM-DD` strings is also intentional. Instants are stored as BSON dates and converted at the API boundary.
