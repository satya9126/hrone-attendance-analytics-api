# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` and `date` to prevent duplicate records. The `(date, emp_code)` index supports attendance listing, while `(date, late_minutes)` helps the late leaderboard filter records by date and lateness. Additional employee indexes support department filtering and joining-date queries.

2. **Punch-in race.** Two simultaneous requests can both find no existing attendance record. The unique compound index ensures that only one insert succeeds for an employee and date. I catch `DuplicateKeyError` and return HTTP 409 Conflict for the other request.

3. **Ties.** The leaderboard uses standard competition ranking, such as 1, 2, 2, 4. It calculates ranks before applying the limit, so employees tied at the cutoff are included.

4. **Headcount.** The department summary starts with employees and uses `$lookup` to find attendance records. `$unwind` with `preserveNullAndEmptyArrays` keeps employees without attendance records in the headcount.

5. **Scaling.** If the dataset grew by 100 times, I would benchmark query performance, review index usage, and consider precomputed analytics to reduce repeated aggregation work.