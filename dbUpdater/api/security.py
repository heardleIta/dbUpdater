"""
Autenticazione del servizio di controllo.

Non esiste un login separato: il back office fa login sul backend Heardle e usa
quel JWT anche qui. Validiamo la stessa firma HS256 condividendo `JWT_SECRET`,
e accettiamo solo i token admin (`sub == "admin"`), mai quelli partecipante
emessi per le sfide.

Nota sulla chiave: jjwt 0.11 (`signWith(SignatureAlgorithm, String)`) tratta il
segreto come **base64** e lo decodifica prima di firmare. Per verificare la firma
dobbiamo fare lo stesso; se il segreto non è base64 valido ricadiamo sui byte
grezzi, così la configurazione locale continua a funzionare.
"""

import base64
import binascii
import logging
import os

import jwt
from fastapi import Header, HTTPException, status

log = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "")
AUTH_DISABLED = os.environ.get("UPDATER_AUTH_DISABLED", "").lower() in ("1", "true", "yes")


def _candidate_keys() -> list[bytes]:
    keys: list[bytes] = []
    try:
        decoded = base64.b64decode(JWT_SECRET, validate=True)
        if decoded:
            keys.append(decoded)
    except (binascii.Error, ValueError):
        pass
    keys.append(JWT_SECRET.encode("utf-8"))
    return keys


def _decode(token: str) -> dict:
    last_error: Exception | None = None
    for key in _candidate_keys():
        try:
            return jwt.decode(token, key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            # La scadenza non dipende da quale derivazione della chiave usiamo:
            # inutile ritentare con l'altra.
            raise
        except jwt.PyJWTError as e:
            last_error = e
    raise last_error or jwt.InvalidTokenError("Token non valido")


def require_admin(authorization: str | None = Header(default=None)) -> str:
    """
    Dependency FastAPI: pretende un JWT admin valido nell'header Authorization.
    Restituisce il subject del token.
    """
    if AUTH_DISABLED:
        return "admin"

    if not JWT_SECRET:
        log.error("JWT_SECRET non configurato: nessuna richiesta può essere autenticata.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Servizio non configurato correttamente",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token mancante"
        )

    try:
        claims = _decode(authorization[7:])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token scaduto")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token non valido")

    if claims.get("sub") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token non autorizzato"
        )
    return "admin"
