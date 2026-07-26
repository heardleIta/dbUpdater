"""
Autenticazione verso il backend Heardle.

Storicamente il token per gli endpoint `/insert/*` veniva preso da
`GET /api/refresh`, che era pubblico: chiunque poteva ottenere un JWT admin e
scrivere nel database. Ora `/refresh` richiede a sua volta un token valido,
quindi l'updater si autentica con credenziali proprie su `/api/authenticate`.

Il token viene tenuto in cache: un run lungo farebbe altrimenti una login per
ogni canzone. La cache scade in anticipo rispetto al token (che dura 2h) così da
non usarne mai uno sul punto di scadere a metà richiesta.
"""

import logging
import os
import threading
import time

import requests
from . import api_endpoint

log = logging.getLogger(__name__)

ENDPOINT = api_endpoint()
ADMIN_USERNAME = os.environ.get("HEARDLE_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("HEARDLE_ADMIN_PASSWORD", "")

# Il JWT del backend dura 2h: rinnoviamo con abbondante margine.
_TOKEN_TTL_S = 90 * 60

_lock = threading.Lock()
_cached_token: str | None = None
_cached_at: float = 0.0


class AuthError(RuntimeError):
    """Credenziali mancanti o rifiutate dal backend."""


def get_token(force_refresh: bool = False) -> str:
    """
    Restituisce un JWT admin valido, autenticandosi solo quando serve.

    :param force_refresh: ignora la cache (da usare dopo un 401).
    :raises AuthError: se mancano le credenziali o il backend le rifiuta.
    """
    global _cached_token, _cached_at

    with _lock:
        fresh = _cached_token and (time.time() - _cached_at) < _TOKEN_TTL_S
        if fresh and not force_refresh:
            return _cached_token

        if not ADMIN_USERNAME or not ADMIN_PASSWORD:
            raise AuthError(
                "Credenziali backend mancanti: valorizzare HEARDLE_ADMIN_USERNAME "
                "e HEARDLE_ADMIN_PASSWORD nell'ambiente dell'updater."
            )

        try:
            r = requests.post(
                f"{ENDPOINT}/authenticate",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                timeout=20,
            )
        except Exception as e:
            raise AuthError(f"Backend non raggiungibile per l'autenticazione: {e}") from e

        if r.status_code != 200:
            raise AuthError(f"Autenticazione rifiutata dal backend (HTTP {r.status_code})")

        token = (r.json() or {}).get("data")
        if not token:
            raise AuthError("Il backend non ha restituito alcun token")

        _cached_token = token
        _cached_at = time.time()
        log.info("Token backend ottenuto (valido ~2h, rinnovo ogni %d min)", _TOKEN_TTL_S // 60)
        return token


def auth_headers(force_refresh: bool = False) -> dict:
    """Header pronti per le chiamate autenticate agli endpoint `/insert/*`."""
    return {
        "Authorization": f"Bearer {get_token(force_refresh)}",
        "Content-Type": "application/json",
    }
