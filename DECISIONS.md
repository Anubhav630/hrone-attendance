# Implementation decisions

**Indexes.** A unique `employees.emp_code` index protects employee identity. A unique `(emp_code, date)` attendance index enforces one daily record and resolves punch-in races. Department/join-date and date/status indexes support department summaries, date-filtered lists, and employee-month lookups. I would reject a standalone `status` index unless `executionStats` showed a required query benefits enough to justify its write cost.

**Punch-in race.** Concurrent requests can both reach MongoDB, but the unique compound index allows only one insert. That request returns 201; the duplicate-key request is translated to 409. Punch-out uses a conditional update that only matches an open record, so only one request can close it. Regularization must append history and update fields atomically to avoid losing audit entries.

**Ties.** The leaderboard uses MongoDB `$rank`, not array position. Tied employees share a rank; the next rank is skipped. Rows at the cutoff rank are included, then ordered deterministically by employee code.

**Headcount.** Department summary starts from employees joined by the period end and looks up attendance, so employees with no logs still count. Work-hour averages are calculated over qualifying attendance records, not averages of employee averages.

**100x scale.** I would benchmark with representative data and `executionStats`, remove redundant indexes, and consider pre-aggregated monthly summaries if freshness requirements permit.
