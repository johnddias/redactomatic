# Build stage: extract the git commit into a plain VERSION file so the
# final image can show a build identifier without shipping .git or a git
# binary (the version is a debugging aid for the UI footer, not runtime
# logic -- falls back to "dev" if the commit can't be determined).
FROM python:3.12-slim AS version
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# `--dirty` needs the actual tracked files present to diff against the
# index -- copying just .git leaves every tracked file looking "deleted",
# so it always reports dirty regardless of the real commit state.
COPY . .
RUN git describe --always --dirty > /VERSION 2>/dev/null || echo "dev" > /VERSION

FROM python:3.12-slim

# System deps (PyMuPDF ships bundled binaries; no extra libs needed on slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY --from=version /VERSION ./VERSION

# Data directory for uploads and outputs
RUN mkdir -p /data

ENV UPLOAD_FOLDER=/data \
    MAX_CONTENT_MB=100 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "main:app"]
