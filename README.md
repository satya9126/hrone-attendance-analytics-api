# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, attendance regularization, and analytics.

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a local `.env` file using `.env.example` as a reference, and configure `MONGO_URI` and `MONGO_DB`.

## Run the application

From the project root, run:

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` to explore the API using Swagger UI.

## Configuration

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. Do not commit `.env` or database credentials.

## Main features

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance regularization with change history
- Monthly employee and department analytics
- Late-arrival leaderboard and department attendance trends
- MongoDB query explain endpoint
