# Starter review

The following defects were identified in the starter implementation by inspecting the code and comparing it with the API contract. The symptom column describes a reproducing test; not every listed case has been independently rerun against the final implementation.

| # | Where (function / area) | What is wrong | How to notice it (test / symptom) | Fix applied or required |
|---|---|---|---|---|
| 1 | `health()` | Reports success without checking MongoDB availability. | Stop MongoDB and call `/health`; a healthy response is misleading. | Ping MongoDB and return 503 when the database cannot be reached. |
| 2 | Mongo client / startup | Required indexes are not created. | Inspect collection indexes after startup; only `_id` is present. | Create idempotent unique and query-supporting indexes at startup. |
| 3 | `create_employee()` | Check-then-insert is race-prone and duplicate-key errors are not translated. | Send simultaneous requests with the same `emp_code`. | Enforce uniqueness in MongoDB and translate `DuplicateKeyError` to 409. |
| 4 | `EmployeeIn` | Employee code, email, date, and shift constraints are insufficient; equal shift endpoints are not rejected. | Submit malformed values or identical shift start/end. | Add contract validation and reject invalid shift definitions. |
| 5 | `create_employee()` | Uses a naive local timestamp and does not serialize the creation instant as epoch milliseconds. | Compare the stored timestamp and API representation. | Use timezone-aware UTC and the documented timestamp serialization. |
| 6 | `list_employees()` | Page offset is off by one; filtered totals and deterministic sorting are incorrect or missing. | Request page 1 and compare totals with a department filter. | Use `(page - 1) * page_size`, count the filtered query, and sort by `emp_code`. |
| 7 | `compute_late_minutes()` | Does not resolve overnight shift start correctly and mishandles the strict 10-minute grace boundary. | Test a 09:30 shift at 09:40:00 and 09:40:01, plus an overnight shift. | Resolve the correct IST shift start, truncate instants to seconds, apply the strict threshold, then floor minutes. |
| 8 | `compute_work_hours()` | Python's built-in `round()` uses ties-to-even, not required half-up rounding. | Test a duration at a half-cent rounding boundary. | Use decimal half-up rounding for the required two-decimal result. |
| 9 | `compute_overtime()` | Does not consistently apply the 30-minute minimum or resolve overnight shift end. | Test 29:59 and 30:00 beyond shift end, including an overnight shift. | Resolve the correct shift end and count floored minutes only when the threshold is met. |
| 10 | `punch_in()` | Uses local time for omitted timestamps, treats zero as omitted, and derives attendance date without the IST/overnight rules. | Submit zero, invalid timestamp types, an unknown employee, or an overnight punch-in. | Validate epoch milliseconds, distinguish omitted values from zero, check employee existence, and apply R1. |
| 11 | `punch_in()` | Check-then-insert allows duplicate daily records under races; status validation is too broad. | Send simultaneous punch-ins or submit an unsupported status. | Use the unique `(emp_code, date)` index and allow only `PRESENT`, `WFH`, or `ON_DUTY`. |
| 12 | `list_attendance()` | Fetches matching records for Python-side sorting/pagination and exposes an internal MongoDB identifier. | Inspect behavior on a large collection and the returned field set. | Filter, sort, count, and paginate in MongoDB; return only documented fields. |
| 13 | `list_attendance()` | Date ranges and pagination bounds are not adequately validated. | Supply malformed dates, `date_from > date_to`, or page size above 100. | Validate dates/range and constrain page and page size. |

## Inspected and retained (not defects)

- Employee identity is `emp_code`; the internal MongoDB `_id` should not be exposed as the business identity.
- Defaulting punch-in status to `PRESENT` is correct, provided only `PRESENT`, `WFH`, and `ON_DUTY` are accepted as presence statuses.
- `punch_out` and `work_hours` being `null` immediately after punch-in is correct; they remain unset until punch-out.
- Missing `history` should be treated as an empty array, and missing `half_day` as `false`, for legacy documents.
