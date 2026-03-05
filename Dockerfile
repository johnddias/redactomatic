FROM python:3.12-slim

# System deps (PyMuPDF ships bundled binaries; no extra libs needed on slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Data directory for uploads and outputs
RUN mkdir -p /data

ENV UPLOAD_FOLDER=/data \
    MAX_CONTENT_MB=100 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "main:app"]
