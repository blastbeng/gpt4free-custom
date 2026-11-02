"""Registrazione automatica del provider di failover ``AutoFree``.

Il file viene importato da Python all'avvio grazie a ``PYTHONPATH=/ext``
(meccanismo standard ``sitecustomize``): nessuna patch all'immagine upstream,
solo un modulo montato read-only nel container.

Se qualcosa va storto la registrazione fallisce in modo rumoroso ma non
interrompe il processo: l'API continua a funzionare con i provider normali.
"""

import os
import sys

# sitecustomize gira durante l'init di `site`, prima che sys.path[0] contenga
# la working directory: senza questo, quando g4f e' eseguito come sorgente
# (`python3 -m g4f` da /app) l'import fallisce con ModuleNotFoundError.
_APP_DIR = os.environ.get("G4F_APP_DIR", "/app")
if os.path.isdir(_APP_DIR) and _APP_DIR not in sys.path:
    sys.path.insert(1, _APP_DIR)

if os.environ.get("G4F_AUTOFREE", "1").lower() not in ("0", "false", "no"):
    try:
        import autofree_g4f

        autofree_g4f.install()
    except Exception as exc:  # pragma: no cover - solo diagnostica
        print(f"[autofree] registrazione fallita: {exc!r}", flush=True)
