FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    aria2 \
    ffmpeg \
    mediainfo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTE: no VOLUME instruction -- some hosts (e.g. Railway) reject
# Dockerfiles that use it. Downloaded files are temporary (deleted right
# after upload), so persistent storage isn't needed for DOWNLOAD_DIR.

CMD ["python", "main.py"]
