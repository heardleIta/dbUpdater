import logging
import time
import requests
from ytmusicapi import YTMusic

# Configurazione minima del logger
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

ENDPOINT = "https://be.heardleitalia.com/api"
CHECKPOINT_FILE = "checkpoint_lyrics_sent.txt"

yt = YTMusic()

def _get_token():
    r = requests.get(f"{ENDPOINT}/refresh", timeout=15)
    r.raise_for_status()
    return r.json()["data"]

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
        playlist = yt.get_watch_playlist(videoId=youtube_song_id, limit=1)
        browse_id = playlist.get("lyrics") if playlist else None
        if not browse_id:
            return None
        result = yt.get_lyrics(browseId=browse_id)
        return result.get("lyrics") if result else None
    except Exception:
        return None

def _insert_lyrics(youtube_song_id, title, lyrics, key):
    return requests.post(
        f"{ENDPOINT}/heardle/insert/lyrics",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "x-api-key": "key",
        },
        json={
            "songYoutubeId": youtube_song_id,
            "songTitle": title,
            "value": lyrics,
        },
        timeout=15,
    )

def run(youtube_artist_id):
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            processed = set(line.strip() for line in f)
    except FileNotFoundError:
        processed = set()

    try:
        all_songs = _get_songs_of_artist(youtube_artist_id)
        songs = [s for s in all_songs if s["songYoutubeId"] not in processed]
    except Exception as e:
        print(f"Errore critico artist_id {youtube_artist_id}: {e}")
        return

    if not songs:
        print(f"Nessuna nuova canzone da processare per {youtube_artist_id}.")
        return

    print(f"Inizio processing: {len(songs)} canzoni.")

    ok = no_lyrics = errors = 0
    start = time.time()

    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as checkpoint:
        for i, song in enumerate(songs, 1):
            youtube_id = song["songYoutubeId"]
            title = song["songTitle"]
            
            try:
                lyrics = _fetch_lyrics(youtube_id)
                if lyrics:
                    key = _get_token()
                    resp = _insert_lyrics(youtube_id, title, lyrics, key)
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

            # Calcolo tempo rimanente
            elapsed = time.time() - start
            done = i
            avg_time = elapsed / done
            remaining = (avg_time * (len(songs) - done)) / 60
            
            # Print minimale su singola riga
            status = f"\rProgress: {done}/{len(songs)} | OK:{ok} | NoLyrics:{no_lyrics} | Err:{errors} | Fine stimata: {remaining:.1f}m"
            print(status, end="", flush=True)

    print(f"\nCompletato! Inseriti: {ok}, Senza testo: {no_lyrics}, Errori: {errors}")