FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests ytmusicapi

COPY . /app/dbUpdater/

RUN touch /app/dbUpdater/__init__.py && \
    mkdir -p /app/artistiRevisionati /app/ArtistiRevisionati /app/ArtistiSongSent

CMD ["python", "-m", "dbUpdater.main"]
