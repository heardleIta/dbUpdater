"""
Lettura del catalogo artisti già presente nel backend.

Serve al back office per segnalare i duplicati quando si accodano artisti nuovi.
Gli artisti si inseriscono a mano (nome + channel id): la ricerca su YouTube
Music è stata rimossa, quindi questo modulo non ha più bisogno né di YTMusic né
del proxy Tor.
"""

import logging

import requests

from .. import api_endpoint

log = logging.getLogger(__name__)

ENDPOINT = api_endpoint()


def db_artists() -> list[dict]:
    """
    Gli artisti già presenti nel database dell'applicazione, per segnalare i
    duplicati prima di accodarli.
    """
    try:
        r = requests.get(f"{ENDPOINT}/heardle/artist/all", timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("Impossibile leggere gli artisti dal backend: %s", e)
        raise

    if not data.get("data"):
        raise RuntimeError(data.get("errorMessage") or "Risposta inattesa dal backend")
    return data["data"]["artists"]
