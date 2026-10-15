# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne Employee Attendance & Analytics API. Requires Python 3.11+ and MongoDB 6.0+ (the grading environment uses MongoDB 7.0).

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "hrone_test"
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for interactive Swagger documentation. Use a dedicated test database: `sample_seed.py` deletes and reloads the `employees` and `attendance_logs` collections in the database named by `MONGO_DB`. Environment variables override values in `.env`.

## Implementation overview

- `app/main.py` contains the API, validation, attendance calculations, indexes, MongoDB queries, and aggregation pipelines.
- MongoDB stores attendance instants as BSON UTC datetimes; API instants are integer epoch milliseconds. Attendance dates and shift times use the assignment's IST calendar rules.
- Unique indexes enforce employee-code uniqueness and one attendance record per employee/date. Other indexes support the documented list and analytics query patterns.
- Analytics calculations run in MongoDB aggregation pipelines. The late leaderboard uses `$setWindowFields`/`$rank`; the department trend creates daily rows and calculates its moving average in the pipeline.
- `/admin/explain/{endpoint}` exposes query-plan diagnostics for the supported endpoint variants.

## Verification status

Manual local smoke tests confirmed `/health`, employee listing, successful punch-out, duplicate punch-out returning 409, regularization history, the four analytics endpoints returning 200, and `attendance_list` explain selecting `ix_attendance_date_code` with `IXSCAN` on sample data.

Not yet fully verified: explain plans for the other four variants; concurrent request races; all timestamp and attendance-rule boundaries; tie-at-limit leaderboard behavior; and performance at the assignment's 100k-record scale. The sample dataset is not sufficient to establish large-data performance. Do not interpret the smoke tests as a full test-suite pass.

## Submission hygiene

Do not commit `.env`, credentials, `.venv`, `__pycache__`, local data dumps, or a Dockerfile. Keep the repository public through the requested review period.
