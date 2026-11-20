FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QD_DATA_DIR=/data QD_SANDBOX_MODE=disabled
RUN apt-get update && apt-get install -y --no-install-recommends util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY quantdesk ./quantdesk
COPY web ./web
RUN useradd --system --uid 10001 --home /app app && mkdir -p /data /spool && chown app /data /spool
USER app
EXPOSE 8000
CMD ["python", "-m", "quantdesk.run_all"]
