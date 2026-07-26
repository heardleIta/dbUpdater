import logging
import os
import time

import requests
from ytmusicapi import YTMusic

from . import DATA_DIR
from . import events
from .auth import auth_headers
from . import api_endpoint

log = logging.getLogger(__name__)

ENDPOINT = api_endpoint()

# Il checkpoint vive nella cartella dati persistente, non nel CWD del processo:
# in container il CWD è effimero e a ogni ricreazione si ripartiva da zero.
CHECKPOINT_FILE = os.path.join(DATA_DIR, "checkpoint_lyrics_sent.txt")

# Istanza YTMusic condivisa. Viene inizializzata da `set_client()` con la stessa
# sessione Tor usata dallo scraping: prima veniva creata qui a livello di modulo
# senza proxy, quindi le richieste dei testi uscivano dall'IP reale mentre tutto
# il resto passava dall'exit italiano.
_yt: YTMusic | None = None


def set_client(yt: YTMusic) -> None:
    """Inietta il client YTMusic (con proxy Tor) creato da main.py."""
    global _yt
    _yt = yt


def _client() -> YTMusic:
    """
    Client YTMusic da usare per i testi. Se nessuno lo ha iniettato ricade su
    un'istanza senza proxy, ma lo segnala: significa che le richieste escono
    dall'IP reale della macchina.
    """
    global _yt
    if _yt is None:
        log.warning(
            "Client YTMusic non iniettato: i testi verranno scaricati SENZA "
            "passare da Tor (IP reale esposto)."
        )
        _yt = YTMusic()
    return _yt


def _get_songs_of_artist(youtube_artist_id):
    r = requests.post(
        f"{ENDPOINT}/heardle/artist/songs/filtered",
        json={
            "youtubeArtistId": youtube_artist_id,
            "songIdToExclude": [0],
            "filter": "    ",
            "limit": 20000,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["songs"]


def _fetch_lyrics(youtube_song_id):
    try:
        playlist = _client().get_watch_playlist(videoId=youtube_song_id, limit=1)
        browse_id = playlist.get("lyrics") if playlist else None
        if not browse_id:
            return None
        result = _client().get_lyrics(browseId=browse_id)
        return result.get("lyrics") if result else None
    except Exception:
        return None


def _insert_lyrics(youtube_song_id, title, lyrics):
    payload = {
        "songYoutubeId": youtube_song_id,
        "songTitle": title,
        "value": lyrics,
    }
    resp = requests.post(
        f"{ENDPOINT}/heardle/insert/lyrics", headers=auth_headers(), json=payload, timeout=15
    )
    if resp.status_code == 401:
        resp = requests.post(
            f"{ENDPOINT}/heardle/insert/lyrics",
            headers=auth_headers(force_refresh=True),
            json=payload,
            timeout=15,
        )
    return resp


def run(youtube_artist_id, artist_name=None):
    """
    Scarica e inserisce i testi mancanti per un artista, saltando le canzoni
    già processate secondo il checkpoint.
    """
    label = artist_name or youtube_artist_id

    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            processed = set(line.strip() for line in f)
    except FileNotFoundError:
        processed = set()

    try:
        all_songs = _get_songs_of_artist(youtube_artist_id)
        songs = [s for s in all_songs if s["songYoutubeId"] not in processed]
    except Exception as e:
        log.error("Errore critico testi per artist_id %s: %s", youtube_artist_id, e)
        events.emit("lyrics_error", artist=label, error=str(e)[:300])
        return

    if not songs:
        log.info("Testi: nessuna nuova canzone da processare per %s.", label)
        events.emit("lyrics_skip", artist=label)
        return

    log.info("Testi: inizio processing di %d canzoni per %s.", len(songs), label)
    events.emit("lyrics_start", artist=label, total=len(songs))

    ok = no_lyrics = errors = 0
    start = time.time()

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as checkpoint:
        for i, song in enumerate(songs, 1):
            youtube_id = song["songYoutubeId"]
            title = song["songTitle"]

            try:
                lyrics = _fetch_lyrics(youtube_id)
                if lyrics:
                    resp = _insert_lyrics(youtube_id, title, lyrics)
                    if resp.status_code == 200:
                        checkpoint.write(f"{youtube_id}\n")
                        checkpoint.flush()
                        ok += 1
                    else:
                        errors += 1
                else:
                    no_lyrics += 1
            except Exception:
                errors += 1

            elapsed = time.time() - start
            remaining_min = ((elapsed / i) * (len(songs) - i)) / 60

            # Un progresso ogni 10 canzoni (e all'ultima): prima era un print con
            # \r su singola riga, illeggibile una volta finito in un file di log.
            if i % 10 == 0 or i == len(songs):
                log.info(
                    "Testi %s: %d/%d | OK:%d | SenzaTesto:%d | Err:%d | stimati %.1f min",
                    label, i, len(songs), ok, no_lyrics, errors, remaining_min,
                )
                events.emit(
                    "lyrics_progress",
                    artist=label,
                    done=i,
                    total=len(songs),
                    ok=ok,
                    no_lyrics=no_lyrics,
                    errors=errors,
                    eta_min=round(remaining_min, 1),
                )

    log.info(
        "Testi %s completati. Inseriti: %d, Senza testo: %d, Errori: %d",
        label, ok, no_lyrics, errors,
    )
    events.emit("lyrics_end", artist=label, ok=ok, no_lyrics=no_lyrics, errors=errors)
