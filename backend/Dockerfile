FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
WORKDIR /app

COPY pyproject.toml ./
RUN python -c "import tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); print('\n'.join(data['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir "setuptools>=75" \
    && pip install --no-cache-dir -r /tmp/requirements.txt
RUN pip install --no-cache-dir dirsearch sqlmap
# curl/wget so the sandbox_bash tool can construct HTTP requests with custom
# headers (e.g. an HTTP-header-maze CTF challenge).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*
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
