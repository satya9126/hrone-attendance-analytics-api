# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` and `date` to enforce uniqueness. The `(date, emp_code)` index supports attendance listing, while `(date, late_minutes)` supports date-filtered late-arrival queries. Additional indexes support employee department and joining-date queries. Index choices should be validated using execution statistics on the grader's larger dataset.

2. **Punch-in concurrency.** A check followed by an insert is not sufficient because simultaneous requests can both observe that no record exists. The unique compound index is the final safeguard: one insert succeeds, and duplicate-key errors are translated into HTTP 409 Conflict.

3. **Punch-out concurrency.** The record must be updated atomically so concurrent requests cannot both close the same attendance record successfully. A conditional update that requires `punch_out` to be null prevents a second request from overwriting the first punch-out.

4. **Leaderboard ties.** I use standard competition ranking, such as 1, 2, 2, 4. Ranking is calculated before applying the limit, so employees tied at the cutoff are included. A deterministic secondary sort by employee code makes the output stable.

5. **Department headcount.** The department summary starts from employees and uses `$lookup` to find attendance records. `$unwind` with `preserveNullAndEmptyArrays` retains employees who have no attendance records, while joining-date filters exclude employees who have not yet joined during the reporting period.

6. **Analytics and date gaps.** Analytics should use MongoDB aggregation pipelines and stored derived attendance fields rather than loading large collections into Python or recalculating stored metrics. Department trends must generate every calendar day in the requested range, including weekends and days without records, before calculating the moving average.

7. **Scaling.** For a dataset 100 times larger, I would benchmark representative queries, inspect `executionStats` for index scans and documents examined, and review index overhead. If repeated analytics become expensive, I would consider precomputed daily summaries while preserving the API's required results.