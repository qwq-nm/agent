FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

COPY pyproject.toml ./
RUN python -c "import tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); print('\n'.join(data['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "setuptools>=75" \
    && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" dirsearch sqlmap
USER root
RUN python -m playwright install --with-deps chromium

COPY backend ./backend
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps --no-build-isolation .
RUN useradd --create-home --uid 10001 secagent \
    && mkdir -p /data \
    && chown -R secagent:secagent /app /data /ms-playwright

USER secagent
EXPOSE 8000
CMD ["uvicorn", "secagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
