# TransDub AI — optional Docker runtime (Windows native remains the primary path).
# Build: docker build -t transdub-ai .
# Run:   docker run --rm -p 8765:8765 -v transdub-data:/app/data --env-file .env transdub-ai
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# FFmpeg is required for probe/extract/mux; git for version info only.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY run.py ./run.py

RUN pip install --upgrade pip && pip install -e .[inference]

EXPOSE 8765
VOLUME ["/app/data"]
CMD ["python", "run.py"]
