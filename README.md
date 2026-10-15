# Employee Attendance & Analytics API

A robust RESTful backend service built with **FastAPI** and **MongoDB** for managing employee profiles, tracking attendance events (punch-in, punch-out, regularizations), and serving real-time analytics aggregations.

---

## Features

- **Employee Management:** Create and list employees with department filtering and pagination.
- **Attendance Tracking:**
  - Punch-in and punch-out workflows with automatic calculations for late minutes, overtime, and work hours.
  - Concurrency handling with database-level unique constraints to eliminate punch-in race conditions.
  - Support for overnight shifts crossing midnight in IST.
  - Audit logging for manual corrections (`history` array tracking previous states).
- **Analytics & Reporting:**
  - Monthly employee attendance summaries.
  - Department summary metrics taking into account zero-log employees.
  - Late-comers leaderboard using standard competition ranking (1224 ranking) via MongoDB `$setWindowFields`.
  - 7-day moving average attendance trends with gap-filling across calendar dates.
- **Query Optimization:**
  - Strategic index coverage (`emp_code`, compound department/date indexes) ensuring all queries execute via `IXSCAN` without `COLLSCAN`.
  - Dedicated `/admin/explain/{endpoint}` route to inspect query execution plans.

---

## Tech Stack

- **Python 3.10+**
- **FastAPI** — High-performance web framework for APIs
- **MongoDB 6.0+ / 7.0** & **PyMongo** — Database and aggregation engine
- **Pydantic** — Request and response data validation
- **Uvicorn** — ASGI web server

---

## Project Structure

```text
.
├── app/
│   └── main.py          # Complete application logic (API routes, models, business rules)
├── DECISIONS.md         # Architecture, indexing rationale, and design questions
├── REVIEW.md            # Defect log from starter codebase and learning reflections
├── requirements.txt     # Python dependencies
├── openapi.yaml         # Complete OpenAPI 3.0 API specification
├── DATA_MODEL.md        # MongoDB schema and document structure reference
└── README.md            # Project overview and setup instructions
```

---

## Getting Started

### 1. Prerequisites
- Python 3.10 or higher
- MongoDB running locally on port `27017` or via MongoDB Atlas

### 2. Environment Setup
Clone the repository and set up a virtual environment:

```bash
# Create and activate virtual environment
python -m venv .venv

# On Windows (PowerShell):
.\.venv\Scripts\activate.ps1

# On Linux/macOS:
source .venv/bin/activate
```

Install the dependencies:
```bash
pip install -r requirements.txt
```

### 3. Environment Variables
Create a `.env` file in the project root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Initial Data (Optional)
To populate sample employees and attendance logs:
```bash
python sample_seed.py
```

### 5. Run the Application
Start the development server with Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```

The API will be accessible at:
- **API Base:** `http://localhost:8000`
- **Interactive Swagger Documentation:** `http://localhost:8000/docs`
- **ReDoc Documentation:** `http://localhost:8000/redoc`
- **Health Check:** `http://localhost:8000/health`

---

## Submission Notes

- **[REVIEW.md](REVIEW.md):** Documents all 14 defects found and resolved from the initial starter kit, along with learning notes.
- **[DECISIONS.md](DECISIONS.md):** Covers database index selection, race condition handling, ranking tie behaviors, headcount preservation, and scale considerations.
