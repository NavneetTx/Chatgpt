# ---------------------------------------------------------------------------
# House of Colour stylist portal — container image for Render.
#
# The DuckDB database is built during the image build from the CSVs in ./data,
# not at runtime. Render's filesystem is ephemeral, so baking it in means every
# restart comes back with the same clean seeded data and the first visitor
# after a cold start doesn't pay for the rebuild.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so this layer caches across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Seed data.duckdb. DISABLE_SCHEDULER stops importing app.py from spinning up
# the APScheduler thread just to run a build step.
RUN DISABLE_SCHEDULER=1 python -c "from app import build_db; build_db()"

# Render injects PORT; 8000 is the local default.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
