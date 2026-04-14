
import logging
import threading
from queue import Empty, Queue

import requests
from ytmusicapi import YTMusic

log = logging.getLogger(__name__)

ENDPOINT = "https://be.heardleitalia.com/api"


def _refresh_token() -> str | None:
    try:
        response = requests.get(f"{ENDPOINT}/refresh")
        if response.status_code == 200:
            return response.json()["data"]
        log.error("Errore refresh token per testi: HTTP %d", response.status_code)
    except Exception as e:
        log.error("Impossibile ottenere token per testi: %s", e)
    return None


def _get_lyrics_browse_id(yt: YTMusic, video_id: str) -> str | None:
    try:
        playlist = yt.get_watch_playlist(videoId=video_id, limit=1)
        if playlist and playlist.get("lyrics"):
            return playlist["lyrics"]
    except Exception as e:
        log.debug("browseId testi non disponibile per %s: %s", video_id, e)
    return None


def _fetch_lyrics(yt: YTMusic, browse_id: str) -> str | None:
    try:
        result = yt.get_lyrics(browseId=browse_id)
        if result and result.get("lyrics"):
            return result["lyrics"]
    except Exception as e:
        log.debug("Testo non recuperabile per browseId %s: %s", browse_id, e)
    return None


def _worker(queue: Queue, key: str, counter: dict, lock: threading.Lock) -> None:
    """
    Worker thread con la propria istanza YTMusic per evitare problemi di thread safety.
    Recupera e invia i testi per ogni canzone in coda.
    """
    yt_local = YTMusic(language="it", location="IT")

    while True:
        try:
            song = queue.get_nowait()
        except Empty:
            break

        video_id = song["youtubeSongId"]
        title = song["title"]

        browse_id = _get_lyrics_browse_id(yt_local, video_id)
        if browse_id is None:
            with lock:
                counter["not_found"] += 1
            continue

        lyrics_text = _fetch_lyrics(yt_local, browse_id)
        if not lyrics_text:
            with lock:
                counter["not_found"] += 1
            continue

        try:
            response = requests.post(
                f"{ENDPOINT}/heardle/insert/lyrics",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "x-api-key": "key",
                },
                json={
                    "songYoutubeId": video_id,
                    "songTitle": title,
                    "value": lyrics_text,
                },
            )
            with lock:
                if response.ok:
                    counter["sent"] += 1
                else:
                    log.warning("Backend testi: HTTP %d per '%s'", response.status_code, video_id)
                    counter["errors"] += 1
        except Exception as e:
            log.warning("Errore invio testo per '%s': %s", video_id, e)
            with lock:
                counter["errors"] += 1


def process_lyrics(songs: list[dict], artist_name: str, num_threads: int = 2) -> None:
    """
    Recupera e invia i testi per le canzoni di un artista usando num_threads worker.
    Ogni worker ha la propria istanza YTMusic — nessun problema di thread safety.
    """
    key = _refresh_token()
    if key is None:
        log.error("Artista %s: pipeline testi annullata, token non disponibile.", artist_name)
        return

    queue: Queue = Queue()
    for song in songs:
        queue.put(song)

    counter = {"sent": 0, "not_found": 0, "errors": 0}
    lock = threading.Lock()

    workers = [
        threading.Thread(target=_worker, args=(queue, key, counter, lock))
        for _ in range(min(num_threads, len(songs)))
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    log.info(
        "Artista %s — testi: %d inviati, %d non trovati, %d errori",
        artist_name, counter["sent"], counter["not_found"], counter["errors"],
    )
