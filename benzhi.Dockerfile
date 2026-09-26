FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY cranefleet ./cranefleet
COPY web ./web

RUN python -m compileall -q cranefleet

EXPOSE 8080

CMD ["python", "-m", "cranefleet", "--host", "0.0.0.0", "--port", "8080", "--data-dir", "/app/data", "--web-root", "/app/web"]
