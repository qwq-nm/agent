FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN pip install --no-cache-dir .
RUN pip install --no-cache-dir dirsearch
USER root
RUN python -m playwright install --with-deps chromium
RUN useradd --create-home --uid 10001 secagent \
    && mkdir -p /data \
    && chown -R secagent:secagent /app /data /ms-playwright

USER secagent
EXPOSE 8000
CMD ["uvicorn", "secagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
