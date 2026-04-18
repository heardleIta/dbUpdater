
import json
import logging
import os
import re
import socket
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from ytmusicapi import YTMusic

from . import ARTISTI_REVISIONATI
from .send import sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Proxy SOCKS5 verso il container tor-proxy. socks5h:// fa risolvere il DNS
# attraverso Tor, così anche le query per music.youtube.com escono dall'exit IT.
TOR_PROXY_URL = os.getenv("TOR_PROXY_URL", "socks5h://tor-proxy:9050")
_PROXIES = {"http": TOR_PROXY_URL, "https": TOR_PROXY_URL}

TOR_CONTROL_HOST = os.getenv("TOR_CONTROL_HOST", "tor-proxy")
TOR_CONTROL_PORT = int(os.getenv("TOR_CONTROL_PORT", "9051"))
TOR_CONTROL_PASSWORD = os.getenv("TOR_CONTROL_PASSWORD", "")


def _build_session() -> requests.Session:
    """Session con retry HTTP-level (429/5xx) + proxy SOCKS5 montato di default."""
    s = requests.Session()
    retry = Retry(
        total=5, connect=5, read=3, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.proxies.update(_PROXIES)
    return s


def wait_for_tor(timeout_s: int = 180) -> str:
    """Attende che Tor abbia un circuito IT funzionante. RuntimeError se fallisce."""
    deadline = time.monotonic() + timeout_s
    delay, last_err = 2.0, None
    while time.monotonic() < deadline:
        try:
            r = requests.get(
                "https://check.torproject.org/api/ip",
                proxies=_PROXIES, timeout=15,
            )
            data = r.json()
            if data.get("IsTor") is True and data.get("IP"):
                log.info("Tor pronto. Exit IP=%s (ExitNodes={it}, StrictNodes=1)", data["IP"])
                return data["IP"]
            last_err = f"not-tor: {data}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(delay)
        delay = min(delay * 1.7, 20.0)
    raise RuntimeError(f"Tor proxy non raggiungibile dopo {timeout_s}s: {last_err}")


def _tor_control(command: str) -> str:
    """Invia un singolo comando al ControlPort di Tor dopo autenticazione."""
    with socket.create_connection((TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=10) as s:
        f = s.makefile("rwb")
        f.write(f'AUTHENTICATE "{TOR_CONTROL_PASSWORD}"\r\n'.encode())
        f.flush()
        auth = f.readline().decode(errors="replace").strip()
        if not auth.startswith("250"):
            raise RuntimeError(f"Tor AUTHENTICATE fallita: {auth}")
        f.write(f"{command}\r\n".encode())
        f.flush()
        return f.readline().decode(errors="replace").strip()


def renew_circuit() -> None:
    """Forza la creazione di un nuovo circuito Tor (rate-limit del NEWNYM ~10s)."""
    resp = _tor_control("SIGNAL NEWNYM")
    if not resp.startswith("250"):
        raise RuntimeError(f"Tor NEWNYM fallita: {resp}")
    # Il segnale è accettato ma il circuito nuovo richiede qualche secondo per stabilirsi
    time.sleep(10)


# Provider GeoIP ordinati per allineamento con MaxMind (il DB che usa YouTube).
# ipinfo.io è partner MaxMind → il suo verdetto è quello più vicino a YouTube,
# quindi lo usiamo come "primario"; gli altri servono per cross-check di sanità.
_GEOIP_PROVIDERS = [
    ("https://ipinfo.io/json", "ip", "country"),
    ("https://ifconfig.co/json", "ip", "country_iso"),
    ("http://ip-api.com/json/?fields=status,countryCode,query", "query", "countryCode"),
]


def _lookup_ip_country(url: str, ip_key: str, cc_key: str) -> tuple[str, str] | None:
    """Singola query GeoIP. Ritorna (ip, country_upper) o None in caso di errore."""
    try:
        r = requests.get(url, proxies=_PROXIES, timeout=20,
                         headers={"User-Agent": "curl/8.0"})
        if r.status_code != 200 or not r.text.strip():
            return None
        data = r.json()
        ip = data.get(ip_key, "") or ""
        cc = (data.get(cc_key) or "").upper()
        if ip and cc:
            return ip, cc
    except Exception:
        pass
    return None


def _get_exit_geo() -> tuple[str, str, list[str]]:
    """
    Ritorna (ip, country_primario, countries_secondari).
    Il primario è ipinfo.io (MaxMind-aligned, vicino al verdetto di YouTube);
    i secondari servono per contestare eventualmente un falso IT. Raise
    RuntimeError se il primario non risponde: senza il suo verdetto non possiamo
    decidere in modo affidabile.
    """
    primary = _lookup_ip_country(*_GEOIP_PROVIDERS[0])
    if primary is None:
        raise RuntimeError("Provider GeoIP primario (ipinfo.io) non raggiungibile")
    ip, cc_primary = primary
    secondaries: list[str] = []
    for url, ip_key, cc_key in _GEOIP_PROVIDERS[1:]:
        r = _lookup_ip_country(url, ip_key, cc_key)
        if r:
            secondaries.append(r[1])
    return ip, cc_primary, secondaries


def _exclude_exit_ips(ips: set[str]) -> None:
    """Aggiorna a caldo ExcludeExitNodes così NEWNYM non ri-seleziona gli IP già scartati."""
    if not ips:
        return
    value = ",".join(sorted(ips))
    resp = _tor_control(f'SETCONF ExcludeExitNodes="{value}"')
    if not resp.startswith("250"):
        raise RuntimeError(f"Tor SETCONF ExcludeExitNodes fallita: {resp}")


def ensure_italian_exit(max_rotations: int = 20) -> str:
    """
    Il GeoIP interno di Tor (ExitNodes {it}) non coincide sempre con il GeoIP
    commerciale usato da YouTube: un nodo può essere classificato IT nel consensus
    Tor ma DE/FR/etc. secondo MaxMind. Inoltre anche tra provider commerciali
    ci sono divergenze (es. ip-api.com dice IT mentre MaxMind/ipinfo dice DE),
    e l'IP falsamente IT scatena LOGIN_REQUIRED a catena su YouTube. Accettiamo
    l'exit solo se il provider primario (ipinfo.io, MaxMind-aligned) lo classifica
    IT E nessun provider secondario contesta il verdetto.
    """
    blacklist: set[str] = set()
    for attempt in range(1, max_rotations + 1):
        try:
            ip, cc_primary, cc_secondaries = _get_exit_geo()
            log.info("Tentativo %d/%d: exit IP=%s country=%s (secondari=%s)",
                     attempt, max_rotations, ip, cc_primary,
                     ",".join(cc_secondaries) if cc_secondaries else "n/a")
        except Exception as e:
            log.warning("Lookup GeoIP fallito (tentativo %d): %s — ruoto circuito", attempt, e)
            renew_circuit()
            continue

        dissent = [c for c in cc_secondaries if c != "IT"]
        if cc_primary == "IT" and not dissent:
            return ip

        if ip:
            blacklist.add(ip)
            _exclude_exit_ips(blacklist)
        reason = f"primary={cc_primary}" if cc_primary != "IT" else f"secondari non-IT={dissent}"
        log.warning("Exit non IT confermato (%s). Blacklist=%d, rotazione...", reason, len(blacklist))
        renew_circuit()
    raise RuntimeError(f"Impossibile ottenere un exit IT confermato dopo {max_rotations} rotazioni")


def build_ytmusic() -> YTMusic:
    """YTMusic con Session retry + proxies SOCKS5h. location='IT' per mercato italiano."""
    return YTMusic(location="IT", requests_session=_build_session(), proxies=_PROXIES)


def _retry_ytmusic(fn, *args, attempts: int = 3, base_delay: float = 5.0, **kwargs):
    """
    Wrapper per le chiamate YTMusic. Gestisce errori di proxy/connessione
    (circuito Tor fallito, exit IT congestionato) con backoff esponenziale;
    l'ultima eccezione ri-emerge così il chiamante a monte può saltare
    l'artista/album e continuare senza abortire il run.
    """
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            log.warning("Chiamata YTMusic fallita (tentativo %d/%d): %s", i + 1, attempts, e)
            time.sleep(base_delay * (2 ** i))
    raise last


# yt viene inizializzato nel blocco if __name__ == "__main__" dopo wait_for_tor()
yt: YTMusic


def getAllArtists():
    """Recupera tutti gli artisti presenti nel database dell'applicazione."""
    response = requests.get("https://be.heardleitalia.com/api/heardle/artist/all")
    response = response.json()
    if response.get("data") is None:
        log.error("Errore nella richiesta degli artisti: %s", response.get("errorMessage", response))
        return []
    return response["data"]["artists"]


def getAllAlbumOfArtistInDB(artistId):
    """Recupera dal DB tutti gli album già salvati per un dato artista (per ID YouTube)."""
    response = requests.get(f"https://be.heardleitalia.com/api/heardle/album?youtubeArtistId={artistId}")
    data = response.json()
    if data.get("data") is None:
        if data.get("errorMessage") != "Artist not found":
            log.error("Errore album per artista %s: %s", artistId, data.get("errorMessage", "Unknown error"))
        return []
    return data["data"]["albums"]


def getAllAlbumArtistsInYouTube(artistIdYouTube):
    """
    Recupera da YouTube Music la lista di album di un artista.
    Gestisce due casi restituiti dall'API:
      - album con params+browseId → richiede una chiamata aggiuntiva get_artist_albums
      - album già presenti nei risultati diretti
    Normalizza inoltre la chiave 'audioPlaylistId' → 'playlistId' per uniformità.
    """
    artista_obj = _retry_ytmusic(yt.get_artist, channelId=artistIdYouTube)
    albums = artista_obj.get("albums")
    listaAlbumArtista = []
    if albums is not None:
        params = albums.get("params")
        browseID = albums.get("browseId")
        if params and browseID:
            # L'artista ha molti album: serve una chiamata dedicata per ottenerli tutti
            listaAlbumArtista = _retry_ytmusic(yt.get_artist_albums, channelId=browseID, params=params)
        else:
            # Gli album sono già inclusi nella risposta principale
            listaAlbumArtista = albums.get("results", [])
    else:
        log.warning("Nessun album trovato per l'artista %s", artista_obj.get("name", artistIdYouTube))

    # Normalizza la chiave: alcuni album usano 'audioPlaylistId' invece di 'playlistId'
    for item in listaAlbumArtista:
        if "playlistId" not in item and "audioPlaylistId" in item:
            item["playlistId"] = item["audioPlaylistId"]
    return listaAlbumArtista


def filtraFeaturing(featuring, idArtista):
    """
    Rimuove dalla lista dei featuring l'artista principale e le voci senza ID.
    Restituisce [] se la lista risultante è vuota.
    """
    featuring = [a for a in featuring if a["id"] != idArtista and a["id"] is not None]
    return featuring


def getAllArtistsInSongs(listArtists, author):
    """
    Costruisce la lista degli artisti di una canzone nel formato atteso dal DB.
    Aggiunge l'artista principale (author) se non è già presente tra gli artisti della traccia.
    """
    newKeys = []
    for artist in listArtists:
        newKeys.append({
            "name": artist["name"],
            "youtubeAuthorId": artist["id"],
        })
    # Assicura che l'artista principale dell'album sia sempre incluso
    if not any(author["youtubeAuthorId"] == artist["youtubeAuthorId"] for artist in newKeys):
        newKeys.append({
            "name": author["name"],
            "youtubeAuthorId": author["youtubeAuthorId"],
        })
    return newKeys


def getThumbnail(thumbnails):
    """Restituisce l'URL della thumbnail con la risoluzione più alta tra quelle disponibili."""
    if not thumbnails:
        return None
    max_width = 0
    max_height = 0
    url = ""
    for thumbnail in thumbnails:
        if thumbnail["width"] > max_width and thumbnail["height"] > max_height:
            max_width = thumbnail["width"]
            max_height = thumbnail["height"]
            url = thumbnail["url"]
    return url


def isSongPlayable(videoId: str) -> bool:
    """
    Verifica la riproducibilità di una traccia tramite get_song.
    Salta le tracce con status UNPLAYABLE/LOGIN_REQUIRED o playableInEmbed=False.
    """
    try:
        playability = _retry_ytmusic(yt.get_song, videoId).get("playabilityStatus", {})
        status = playability.get("status")
        if status in ("UNPLAYABLE", "LOGIN_REQUIRED"):
            log.warning("  Canzone %s non riproducibile (status=%s), saltata", videoId, status)
            return False
        if playability.get("playableInEmbed") is False:
            log.warning("  Canzone %s non incorporabile (playableInEmbed=False), saltata", videoId)
            return False
    except Exception as e:
        log.debug("get_song fallito per %s: %s", videoId, e)
    return True


def getSongsOfAlbum(albumBrowseId, artistName, artistaChannelId, idAlbum):
    """
    Recupera le tracce di un album da YouTube Music e le formatta per il DB.
    Esclude tracce prive di videoId e tracce live, remix o remastered.
    """
    album_obj = _retry_ytmusic(yt.get_album, browseId=albumBrowseId)

    newSongs = []
    for song in album_obj["tracks"]:
        # Salta tracce non riproducibili (es. video non disponibili)
        if song["videoId"] is None:
            continue
        # Salta versioni alternative che non vogliamo indicizzare
        title_lower = song["title"].lower()
        if re.search(r"\blive\b|\bremix\b|\bremastered\b", title_lower):
            continue

        if not isSongPlayable(song["videoId"]):
            continue

        newSong = {
            "title": song["title"],
            "duration": song["duration_seconds"],
            "youtubeSongId": song["videoId"],
            "youtubeViews": 0,
            # Se l'anno non è disponibile, usa 9999 come valore sentinella
            "releaseDate": album_obj.get("year", 9999) if album_obj.get("year", "") != "" else 9999,
            "artists": getAllArtistsInSongs(
                song["artists"],
                author={"name": artistName, "youtubeAuthorId": artistaChannelId},
            ),
            "album": {
                "youtubeAlbumId": idAlbum,
                "thumbnail": getThumbnail(album_obj["thumbnails"]),
                "title": album_obj["title"],
                "releaseDate": album_obj.get("year", 9999) if album_obj.get("year", "") != "" else 9999,
                "author": {
                    "name": artistName,
                    "youtubeAuthorId": artistaChannelId,
                },
            },
        }
        newSongs.append(newSong)
    return newSongs


def writeJSON(obj, filename):
    """Salva l'oggetto come file JSON nella cartella ArtistiRevisionati, pronto per l'invio."""
    os.makedirs(ARTISTI_REVISIONATI, exist_ok=True)
    path = os.path.join(ARTISTI_REVISIONATI, filename)
    try:
        with open(path, "w", encoding="utf-8") as outfile:
            json.dump(obj, outfile, ensure_ascii=False, indent=4)
    except Exception as e:
        log.error("Errore scrittura su %s: %s", filename, e)


if __name__ == "__main__":
    # 1) Tor deve essere raggiungibile con un circuito qualsiasi
    # 2) ...ma l'exit deve essere realmente IT secondo il GeoIP commerciale,
    #    non solo secondo il consensus Tor (ruotiamo finché coincidono)
    wait_for_tor()
    ensure_italian_exit()
    yt = build_ytmusic()

    # Carica gli artisti dal DB e unisce quelli nuovi definiti localmente in newArtists.json
    allArtists = getAllArtists()
    newArtists_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newArtists.json")
    newArtists = json.load(open(newArtists_path, "r", encoding="utf-8"))
    newArtistIds = {a["youtubeArtistId"] for a in newArtists}
    allArtists = newArtists + [a for a in allArtists if a["youtubeArtistId"] not in newArtistIds]

    log.info("Avvio elaborazione: %d artisti totali (%d nuovi)", len(allArtists), len(newArtists))

    for index, artist in enumerate(allArtists):
        allSongs = []
        log.info("[%d/%d] Artista: %s", index + 1, len(allArtists), artist["name"])
        try:
            # Recupera gli album dell'artista sia da YouTube che dal DB
            allAlbumYoutube = getAllAlbumArtistsInYouTube(artist["youtubeArtistId"])
            allAlbumDB = getAllAlbumOfArtistInDB(artist["youtubeArtistId"])

            if len(allAlbumYoutube) == 0:
                log.warning("Artista %s: nessun album trovato su YouTube", artist["name"])
                continue

            # Trova gli album presenti su YouTube ma non ancora nel DB (da aggiungere)
            albumDBIds = {album["youtubeAlbumId"] for album in allAlbumDB}
            filteredYTAlbums = [
                a for a in allAlbumYoutube
                if a.get("playlistId") is not None and a.get("playlistId") not in albumDBIds
            ]

            log.info(
                "Artista %s: %d album su YouTube, %d già nel DB, %d nuovi",
                artist["name"], len(allAlbumYoutube), len(allAlbumDB), len(filteredYTAlbums),
            )
            if filteredYTAlbums:
                for albumYTFiltered in filteredYTAlbums:
                    songs = getSongsOfAlbum(
                        albumYTFiltered["browseId"],
                        artistName=artist["name"],
                        artistaChannelId=artist["youtubeArtistId"],
                        idAlbum=albumYTFiltered["playlistId"],
                    )
                    log.info(
                        "  Album '%s' (%s): %d canzoni trovate",
                        albumYTFiltered.get("title", "?"), albumYTFiltered["playlistId"], len(songs),
                    )
                    allSongs += songs
                if allSongs:
                    writeJSON(allSongs, f"{artist['name']}.json")
                    log.info("Artista %s: %d canzoni totali salvate nel JSON", artist["name"], len(allSongs))
                else:
                    log.warning("Artista %s: album trovati ma nessuna canzone valida (tutte saltate?)", artist["name"])
            else:
                log.info("Artista %s: nessun album nuovo, skip", artist["name"])

        except Exception as e:
            log.error("Errore per l'artista %s (id: %s): %s", artist["name"], artist["youtubeArtistId"], e)

    log.info("Elaborazione completata. Avvio invio al backend...")

    sender()
