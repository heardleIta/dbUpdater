"""
Eventi strutturati per il monitoraggio dei run dal back office.

Lo scraping continua a loggare in chiaro su stdout come sempre; in più, quando
il processo è stato avviato dal servizio di controllo (env `UPDATER_EVENTS_FILE`
valorizzata), ogni tappa significativa viene scritta anche come riga JSON su
file. La UI legge quel file in append e ne ricava barra di avanzamento e
contatori senza dover interpretare il testo dei log, che resta libero di
cambiare.

Fuori da un run pilotato (es. `python -m dbUpdater.main` lanciato a mano) le
`emit()` sono no-op: lo script si comporta esattamente come prima.
"""

import json
import os
import threading
import time

_EVENTS_FILE = os.environ.get("UPDATER_EVENTS_FILE")
_lock = threading.Lock()


def enabled() -> bool:
    """True se il processo sta girando sotto il servizio di controllo."""
    return bool(_EVENTS_FILE)


def emit(event: str, **fields) -> None:
    """
    Accoda un evento al file JSONL del run. Non solleva mai: un problema di
    telemetria non deve far fallire uno scraping che sta andando bene.
    """
    if not _EVENTS_FILE:
        return
    record = {"ts": time.time(), "event": event, **fields}
    try:
        with _lock:
            with open(_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
    except Exception:
        pass
