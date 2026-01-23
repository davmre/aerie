FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY collector/ ./collector/

# Default database path (mounted as volume)
ENV AERIE_DB_PATH=/app/data/tweets.db

# Run with gunicorn
# Single worker (-w 1) because the classification background worker uses
# module-level globals that don't share across gunicorn's forked workers
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--timeout", "120", "collector.server:create_app()"]
