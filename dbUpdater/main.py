
import argparse
import json
import logging
import os
import re
import socket
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from ytmusicapi import YTMusic

from . import ARTISTI_REVISIONATI
from . import events
from . import insert_lyrics
from .insert_lyrics import run as insert_lyrics_for_artist
from .send import sender
from . import api_endpoint

ENDPOINT = api_endpoint()

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
    """
    Attende che Tor abbia un circuito IT funzionante. RuntimeError se fallisce.

    Ogni tentativo viene registrato: l'attesa può durare minuti e senza traccia
    il pannello di controllo resterebbe muto, indistinguibile da un run bloccato.
    """
    log.info("Attesa del proxy Tor su %s (fino a %ds)...", TOR_PROXY_URL, timeout_s)
    events.emit("tor_wait", proxy=TOR_PROXY_URL, timeout_s=timeout_s)

    deadline = time.monotonic() + timeout_s
    delay, last_err = 2.0, None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            r = requests.get(
                "https://check.torproject.org/api/ip",
                proxies=_PROXIES, timeout=15,
            )
            data = r.json()
            if data.get("IsTor") is True and data.get("IP"):
                log.info("Tor pronto. Exit IP=%s (ExitNodes={it}, StrictNodes=1)", data["IP"])
                events.emit("tor_ready", ip=data["IP"])
                return data["IP"]
            last_err = f"not-tor: {data}"
        except Exception as e:
            last_err = repr(e)
        log.warning(
            "Tor non ancora pronto (tentativo %d): %s — riprovo tra %.0fs",
            attempt, last_err, delay,
        )
        events.emit("tor_retry", attempt=attempt, error=str(last_err)[:200])
        time.sleep(delay)
        delay = min(delay * 1.7, 20.0)

    msg = f"Tor proxy non raggiungibile dopo {timeout_s}s: {last_err}"
    log.error(msg)
    events.emit("tor_failed", error=str(last_err)[:200])
    raise RuntimeError(msg)


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


def _get_exit_geo() -> tuple[str, str | None, list[str]]:
    """
    Ritorna (ip, country_primario_o_None, countries_secondari).

    Il primario è ipinfo.io (MaxMind-aligned, vicino al verdetto di YouTube); i
    secondari servono per contestare eventualmente un falso IT. Il primario può
    non dare verdetto (`None`): da quando ipinfo.io rifiuta il traffico Tor con
    403 è di fatto la norma, e pretenderlo bloccava ogni run. La decisione su
    cosa fare senza di lui sta in `_exit_verdict`.

    Raise RuntimeError solo se *nessun* provider risponde: lì davvero non
    sappiamo dove siamo usciti.
    """
    primary = _lookup_ip_country(*_GEOIP_PROVIDERS[0])
    ip = primary[0] if primary else ""
    cc_primary = primary[1] if primary else None

    secondaries: list[str] = []
    for url, ip_key, cc_key in _GEOIP_PROVIDERS[1:]:
        r = _lookup_ip_country(url, ip_key, cc_key)
        if r:
            secondaries.append(r[1])
            ip = ip or r[0]

    if cc_primary is None and not secondaries:
        raise RuntimeError("Nessun provider GeoIP raggiungibile")
    return ip, cc_primary, secondaries


def _exit_verdict(cc_primary: str | None, cc_secondaries: list[str]) -> tuple[bool, str]:
    """
    Decide se l'exit corrente è accettabile, restituendo (ok, motivo).

    Regola invariata: si accetta solo col verdetto IT del primario (ipinfo.io,
    allineato a MaxMind, che è il GeoIP guardato da YouTube) e nessun secondario
    che lo contesti. Un falso IT scatena LOGIN_REQUIRED a catena, quindi non si
    concede il beneficio del dubbio.

    Se il primario non risponde — capita: rifiuta il traffico da certi exit con
    403 — l'exit viene *rifiutato*, non accettato per ripiego. La differenza
    rispetto a prima è che il rifiuto porta a mettere l'IP in blacklist e a
    ruotare davvero: prima il lookup fallito veniva trattato come errore e
    l'IP restava selezionabile, così NEWNYM poteva riproporre in eterno lo
    stesso exit bloccato ed esaurire tutte le rotazioni su di lui.
    """
    dissent = [c for c in cc_secondaries if c != "IT"]

    if cc_primary is None:
        return False, "primario senza verdetto (403 o non raggiungibile)"
    if cc_primary != "IT":
        return False, f"primario={cc_primary}"
    if dissent:
        return False, f"secondari non-IT={dissent}"
    return True, "primario IT, nessuna contestazione"


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
    e l'IP falsamente IT scatena LOGIN_REQUIRED a catena su YouTube. La regola
    di accettazione è in `_exit_verdict`.
    """
    blacklist: set[str] = set()
    for attempt in range(1, max_rotations + 1):
        try:
            ip, cc_primary, cc_secondaries = _get_exit_geo()
            log.info("Tentativo %d/%d: exit IP=%s country=%s (secondari=%s)",
                     attempt, max_rotations, ip, cc_primary or "n/d",
                     ",".join(cc_secondaries) if cc_secondaries else "n/a")
        except Exception as e:
            log.warning("Lookup GeoIP fallito (tentativo %d): %s — ruoto circuito", attempt, e)
            renew_circuit()
            continue

        ok, reason = _exit_verdict(cc_primary, cc_secondaries)
        if ok:
            log.info("Exit accettato: %s (%s)", ip, reason)
            events.emit("exit_accepted", ip=ip, reason=reason)
            return ip

        if ip:
            blacklist.add(ip)
            _exclude_exit_ips(blacklist)
        log.warning("Exit non IT confermato (%s). Blacklist=%d, rotazione...", reason, len(blacklist))
        events.emit("exit_rejected", attempt=attempt, total=max_rotations, ip=ip, reason=reason)
        renew_circuit()
    raise RuntimeError(f"Impossibile ottenere un exit IT confermato dopo {max_rotations} rotazioni")


# Stato per il check periodico dell'exit (vedi maintain_italian_exit).
# I circuiti Tor possono essere ruotati spontaneamente (MaxCircuitDirtiness
# di default 10 min) e portarci su un exit non-IT a metà run: ri-verifichiamo
# regolarmente per intercettare il cambio prima che YouTube restituisca errori.
_LAST_EXIT_CHECK = 0.0
_EXIT_CHECK_INTERVAL_S = 300

# Rotazione forzata dopo N album processati: con troppe chiamate dallo stesso
# exit YouTube inizia a flaggare l'IP e restituisce UNPLAYABLE a raffica anche
# su contenuti validi. Ruotare proattivamente "resetta" la reputazione dell'IP.
_ALBUMS_SINCE_ROTATION = 0
_ROTATE_EVERY_ALBUMS = 30


def force_rotate_italian_exit() -> None:
    """
    Forza la rotazione del circuito Tor anche se l'exit corrente è già IT, poi
    ri-verifica il consenso GeoIP sul nuovo exit. Da chiamare periodicamente
    (ogni _ROTATE_EVERY_ALBUMS album) per evitare il rate-limit di YouTube.
    """
    global _LAST_EXIT_CHECK, _ALBUMS_SINCE_ROTATION
    log.info("Rotazione Tor forzata dopo %d album", _ALBUMS_SINCE_ROTATION)
    renew_circuit()
    ensure_italian_exit()
    _LAST_EXIT_CHECK = time.monotonic()
    _ALBUMS_SINCE_ROTATION = 0


def maintain_italian_exit() -> None:
    """
    Re-verifica periodica dell'exit Tor. Saltata se l'ultima verifica è entro
    _EXIT_CHECK_INTERVAL_S. Se il consenso GeoIP non è più IT, rientra in
    ensure_italian_exit per ruotare fino a ritrovare un exit italiano.
    """
    global _LAST_EXIT_CHECK
    now = time.monotonic()
    if (now - _LAST_EXIT_CHECK) < _EXIT_CHECK_INTERVAL_S:
        return
    try:
        ip, cc_primary, cc_secondaries = _get_exit_geo()
    except Exception as e:
        log.warning("Re-check exit fallito: %s — forzo nuova procedura", e)
        ensure_italian_exit()
        _LAST_EXIT_CHECK = time.monotonic()
        return

    ok, reason = _exit_verdict(cc_primary, cc_secondaries)
    if ok:
        log.info("Re-check exit: IP=%s ancora accettabile (%s)", ip, reason)
    else:
        log.warning("Re-check exit: IP=%s non più accettabile (%s) — rotazione", ip, reason)
        ensure_italian_exit()
    _LAST_EXIT_CHECK = time.monotonic()


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
    response = requests.get(f"{ENDPOINT}/heardle/artist/all")
    response = response.json()
    if response.get("data") is None:
        log.error("Errore nella richiesta degli artisti: %s", response.get("errorMessage", response))
        return []
    return response["data"]["artists"]


def getAllAlbumOfArtistInDB(artistId):
    """Recupera dal DB tutti gli album già salvati per un dato artista (per ID YouTube)."""
    response = requests.get(f"{ENDPOINT}/heardle/album?youtubeArtistId={artistId}")
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


# Contatore cumulativo delle tracce scartate da isSongPlayable, aggregato per
# motivo (status YouTube o "NOT_EMBEDDABLE"). Usato per il riepilogo finale.
_UNPLAYABLE_STATS: dict[str, int] = {}


def isSongPlayable(videoId: str) -> bool:
    """
    Verifica la riproducibilità di una traccia tramite get_song.
    Salta le tracce con playableInEmbed=False.
    """
    try:
        playability = _retry_ytmusic(yt.get_song, videoId).get("playabilityStatus", {})
        if playability.get("playableInEmbed") is False:
            log.warning("  Canzone %s non incorporabile (playableInEmbed=False), saltata", videoId)
            _UNPLAYABLE_STATS["NOT_EMBEDDABLE"] = _UNPLAYABLE_STATS.get("NOT_EMBEDDABLE", 0) + 1
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


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="dbUpdater",
        description="Scraping YouTube Music degli artisti Heardle e invio al backend.",
    )
    parser.add_argument(
        "--only-queued", action="store_true",
        help="Elabora solo gli artisti nuovi in coda, senza ripassare quelli già nel DB.",
    )
    parser.add_argument(
        "--artist", action="append", metavar="YT_ID", default=None,
        help="Elabora solo l'artista con questo id YouTube (ripetibile).",
    )
    parser.add_argument(
        "--skip-lyrics", action="store_true",
        help="Salta lo scraping dei testi.",
    )
    parser.add_argument(
        "--lyrics-only", action="store_true",
        help="Solo testi: nessuno scraping di album e canzoni.",
    )
    parser.add_argument(
        "--no-send", action="store_true",
        help="Non inviare al backend a fine run: i JSON restano in ArtistiRevisionati "
             "in attesa di approvazione dal back office.",
    )
    parser.add_argument(
        "--queue-file", default=None, metavar="PATH",
        help="File JSON con gli artisti nuovi (default: newArtists.json del progetto).",
    )
    return parser.parse_args(argv)


def _load_artists(args):
    """
    Costruisce la lista di artisti da elaborare secondo le opzioni scelte.
    Gli artisti in coda precedono sempre quelli già presenti nel DB.
    """
    queue_path = args.queue_file or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newArtists.json"
    )
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            newArtists = json.load(f)
    except FileNotFoundError:
        log.warning("Coda artisti non trovata in %s: nessun artista nuovo.", queue_path)
        newArtists = []

    if args.only_queued:
        dbArtists = []
    else:
        dbArtists = getAllArtists()

    newArtistIds = {a["youtubeArtistId"] for a in newArtists}
    allArtists = newArtists + [a for a in dbArtists if a["youtubeArtistId"] not in newArtistIds]

    if args.artist:
        wanted = set(args.artist)
        allArtists = [a for a in allArtists if a["youtubeArtistId"] in wanted]
        known = {a["youtubeArtistId"] for a in allArtists}
        missing = wanted - known
        if missing:
            # Un id passato a mano può non essere in coda: lo elaboriamo comunque,
            # ma il nome va cercato nel database, altrimenti log e pannello
            # mostrerebbero il channel id al posto dell'artista.
            names = {}
            try:
                names = {a["youtubeArtistId"]: a["name"] for a in getAllArtists()}
            except Exception as e:
                log.warning("Nomi artisti non recuperabili dal backend: %s", e)
            allArtists += [
                {"name": names.get(i, i), "youtubeArtistId": i} for i in missing
            ]

    return allArtists, len(newArtists)


def main(argv=None):
    global yt, _LAST_EXIT_CHECK, _ALBUMS_SINCE_ROTATION

    args = _parse_args(argv)

    # 1) Tor deve essere raggiungibile con un circuito qualsiasi
    # 2) ...ma l'exit deve essere realmente IT secondo il GeoIP commerciale,
    #    non solo secondo il consensus Tor (ruotiamo finché coincidono)
    wait_for_tor()
    exit_ip = ensure_italian_exit()
    _LAST_EXIT_CHECK = time.monotonic()
    yt = build_ytmusic()
    # I testi devono uscire dallo stesso exit italiano dello scraping.
    insert_lyrics.set_client(yt)

    allArtists, queuedCount = _load_artists(args)

    log.info("Avvio elaborazione: %d artisti totali (%d nuovi)", len(allArtists), queuedCount)
    events.emit(
        "run_start",
        total_artists=len(allArtists),
        queued_artists=queuedCount,
        exit_ip=exit_ip,
        mode="lyrics" if args.lyrics_only else ("scrape" if args.skip_lyrics else "full"),
    )

    for index, artist in enumerate(allArtists):
        maintain_italian_exit()
        allSongs = []
        log.info("[%d/%d] Artista: %s", index + 1, len(allArtists), artist["name"])
        events.emit(
            "artist_start",
            index=index + 1,
            total=len(allArtists),
            name=artist["name"],
            youtube_artist_id=artist["youtubeArtistId"],
        )
        if not args.lyrics_only:
            try:
                # Recupera gli album dell'artista sia da YouTube che dal DB
                allAlbumYoutube = getAllAlbumArtistsInYouTube(artist["youtubeArtistId"])
                allAlbumDB = getAllAlbumOfArtistInDB(artist["youtubeArtistId"])

                if len(allAlbumYoutube) == 0:
                    log.warning("Artista %s: nessun album trovato su YouTube", artist["name"])
                    events.emit("artist_skipped", name=artist["name"], reason="no_albums_youtube")
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
                events.emit(
                    "artist_albums",
                    name=artist["name"],
                    youtube=len(allAlbumYoutube),
                    in_db=len(allAlbumDB),
                    new=len(filteredYTAlbums),
                )
                if filteredYTAlbums:
                    for albumYTFiltered in filteredYTAlbums:
                        if _ALBUMS_SINCE_ROTATION >= _ROTATE_EVERY_ALBUMS:
                            force_rotate_italian_exit()
                        songs = getSongsOfAlbum(
                            albumYTFiltered["browseId"],
                            artistName=artist["name"],
                            artistaChannelId=artist["youtubeArtistId"],
                            idAlbum=albumYTFiltered["playlistId"],
                        )
                        _ALBUMS_SINCE_ROTATION += 1
                        log.info(
                            "  Album '%s' (%s): %d canzoni trovate",
                            albumYTFiltered.get("title", "?"), albumYTFiltered["playlistId"], len(songs),
                        )
                        events.emit(
                            "album_done",
                            name=artist["name"],
                            album=albumYTFiltered.get("title", "?"),
                            playlist_id=albumYTFiltered["playlistId"],
                            songs=len(songs),
                        )
                        allSongs += songs
                    if allSongs:
                        writeJSON(allSongs, f"{artist['name']}.json")
                        log.info("Artista %s: %d canzoni totali salvate nel JSON", artist["name"], len(allSongs))
                        events.emit("artist_saved", name=artist["name"], songs=len(allSongs))
                    else:
                        log.warning("Artista %s: album trovati ma nessuna canzone valida (tutte saltate?)", artist["name"])
                        events.emit("artist_skipped", name=artist["name"], reason="no_valid_songs")
                else:
                    log.info("Artista %s: nessun album nuovo, skip", artist["name"])
                    events.emit("artist_skipped", name=artist["name"], reason="no_new_albums")

            except Exception as e:
                log.error("Errore per l'artista %s (id: %s): %s", artist["name"], artist["youtubeArtistId"], e)
                events.emit("artist_error", name=artist["name"], error=str(e)[:300])

        # I testi erano stati disattivati commentando questa chiamata: ora la
        # scelta e un'opzione (--skip-lyrics / --lyrics-only), esposta anche dal
        # pannello, invece di stare nel codice.
        if not args.skip_lyrics:
            log.info("Avvio scraping testi per %s...", artist["name"])
            insert_lyrics_for_artist(artist["youtubeArtistId"], artist_name=artist["name"])

    log.info("Elaborazione completata.")

    total_unplayable = sum(_UNPLAYABLE_STATS.values())
    if total_unplayable:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(_UNPLAYABLE_STATS.items()))
        log.info("Riepilogo canzoni non playable: %d totali (%s)", total_unplayable, breakdown)
    else:
        log.info("Riepilogo canzoni non playable: 0")
    events.emit("unplayable_summary", total=total_unplayable, breakdown=dict(_UNPLAYABLE_STATS))

    if args.no_send:
        log.info("Invio saltato (--no-send): i JSON restano in attesa di approvazione.")
        events.emit("run_end", sent=False)
    else:
        log.info("Avvio invio al backend...")
        sender()
        events.emit("run_end", sent=True)


if __name__ == "__main__":
    sys.exit(main())
