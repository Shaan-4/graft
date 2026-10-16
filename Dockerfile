FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GRAFT_REPOS=/data/repos GRAFT_SEED_DEMO=1
WORKDIR /srv

COPY pyproject.toml ./
COPY graft ./graft
COPY server ./server
RUN pip install --no-cache-dir ".[server]"

RUN useradd --create-home app && mkdir -p /data/repos && chown -R app /data
USER app

# Several workers are safe: ref updates use an O_EXCL lock file and object
# writes are atomic renames, so correctness doesn't depend on one process.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2}"]
