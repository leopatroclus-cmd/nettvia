FROM python:3.11-slim

WORKDIR /app

# Install backend dependencies first (better layer caching).
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code: backend/ holds the `app` package; frontend/ is served at / (single-origin).
COPY backend ./backend
COPY frontend ./frontend

# SQLite lives on a mounted volume in prod so data persists across deploys/restarts.
ENV DB_PATH=/data/netting.db

WORKDIR /app/backend
EXPOSE 8000

# PORT is injected by the host (Railway/Render/Fly); defaults to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
