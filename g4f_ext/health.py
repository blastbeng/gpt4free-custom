"""Auto-diagnostica dei provider keyless, senza liste statiche.

Perche' esiste
--------------
Quali provider gratuiti rispondono cambia nel tempo: oggi un endpoint e' vivo,
domani risponde 402/403 o richiede un browser che qui non c'e'. Una lista di
"provider da ignorare" scritta a mano invecchia in giorni.

Qui i provider si misurano da soli, in background, con due domande distinte:

1. *sono vivi?* — una richiesta minima con il loro modello di default;
2. *tollerano un modello qualsiasi?* — la stessa richiesta usando
   ``G4F_AUTOFREE_PROBE_MODEL`` (default ``gpt-4o-mini``), cioe' quello che un
   client OpenAI generico manda davvero.

La seconda proprieta' e' quella che conta: un provider che risponde solo al
proprio catalogo va bene come riserva, non come prima scelta.

L'esito (ok, tolerant, latenza, timestamp) viene salvato in
``$G4F_CONFIG_DIR/autofree_health.json`` e riusato per ``G4F_AUTOFREE_TTL``
secondi (default 6h). Il probe gira in un thread separato: non blocca mai una
richiesta, che viene servita con l'ordine noto in quel momento. Un successo
reale registrato da ``record_success()`` promuove subito il provider.

Nessun provider e' escluso per definizione: chi torna a funzionare rientra da
solo al primo probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from g4f import debug
from g4f.Provider import ProviderLoader

STATE_PATH = Path(
    os.environ.get(
        "G4F_AUTOFREE_STATE",
        str(Path(os.environ.get("G4F_CONFIG_DIR", "/data/g4f")) / "autofree_health.json"),
    )
)
TTL = float(os.environ.get("G4F_AUTOFREE_TTL_SECONDS", 6 * 3600))
PROBE_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_PROBE_TIMEOUT", 20))
CONCURRENCY = max(1, int(os.environ.get("G4F_AUTOFREE_PROBE_CONCURRENCY", 3)))
MAX_CANDIDATES = int(os.environ.get("G4F_AUTOFREE_MAX_CANDIDATES", 24))
PROBE_MODEL = os.environ.get("G4F_AUTOFREE_PROBE_MODEL", "gpt-4o-mini")
# --- Vision ---------------------------------------------------------------
# Una richiesta con immagine data a un provider text-only produce una risposta
# sicura e sbagliata (l'immagine viene scartata): va quindi misurata come le
# altre proprieta', con un'immagine vera e una risposta vincolata.
PROBE_VISION_MODEL = os.environ.get("G4F_AUTOFREE_VISION_MODEL", "gemini-2.5-flash")
VISION_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_PROBE_VISION_TIMEOUT", 45))
# 16x16 magenta: il payload piu' piccolo che un modello multimodale accetta.
VISION_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAYElEQVR4nO3PQQ0AIBDAMMC/50MEj4"
    "ZkVbDtWbN+dnTAqwa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0C2IYAn6MoAmGAAAAAElFTkSuQmCC"
)
VISION_PROMPT = "What is the dominant colour of this image? Answer with one word."
# Risposte accettate: un modello che non vede l'immagine non puo' indovinare
# un colore cosi' specifico.
VISION_WORDS = ("magenta", "purple", "pink", "fuchsia", "violet", "viola", "rosa")
# Oltre questa latenza un provider vivo resta usabile ma passa dietro agli ignoti.
MAX_LATENCY = float(os.environ.get("G4F_AUTOFREE_MAX_LATENCY", 30))
# Finestra in cui un successo reale protegge un provider da un probe fallito.
REAL_GRACE = float(os.environ.get("G4F_AUTOFREE_REAL_GRACE", 3600))
# Quanti probe falliti consecutivi prima di declassare un provider che era vivo.
FAIL_STREAK_TO_DEMOTE = int(os.environ.get("G4F_AUTOFREE_FAIL_STREAK", 2))
# Solo un suggerimento di ordine finche' il primo probe non ha misurato nulla.
SEED = tuple(
    n.strip()
    for n in os.environ.get(
        "G4F_AUTOFREE_SEED", "Yqcloud,CohereForAI_C4AI_Command,HuggingSpace,Cloudflare"
    ).split(",")
    if n.strip()
)

# Provider che non fanno chat: cercarli qui evita di metterli in cima alla
# classifica solo perche' rispondono a una richiesta vuota.
_NON_CHAT_SUFFIXES = ("Search", "Tools", "Downloader", "MarkItDown", "Down")
_MEDIA_SUFFIXES = ("image", "images", "audio", "tts", "voice", "video")

_lock = threading.Lock()
_probing = False
_state: Dict[str, Any] = {"providers": {}, "updated": 0.0}


def load() -> None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # stato corrotto: si riparte da zero
        debug.log(f"AutoFree health: stato illeggibile ({exc!r})")
        return
    if isinstance(data, dict) and isinstance(data.get("providers"), dict):
        _state.clear()
        _state.update(data)


def _save() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as exc:
        debug.log(f"AutoFree health: salvataggio stato fallito ({exc!r})")


def _excluded() -> set:
    """Nomi esclusi a mano (``G4F_AUTOFREE_EXCLUDE``).

    Vale anche per il probe, non solo per la catena: un provider browser-based
    che non risponde piu' costa una tab e un renderer a ogni misura, e con 24
    candidati x 2 domande bastava a saturare Chromium (121 renderer, OOM loop
    del 2026-11-02). Chi e' escluso qui non viene nemmeno misurato.
    """
    raw = os.environ.get("G4F_AUTOFREE_EXCLUDE", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _is_non_chat(provider: type) -> bool:
    name = getattr(provider, "__name__", "")
    module = getattr(provider, "__module__", "").lower()
    if name.endswith(_NON_CHAT_SUFFIXES):
        return True
    if name.lower().endswith(_MEDIA_SUFFIXES):
        return True
    return any(f".{kind}." in module for kind in ("image", "audio", "video", "search"))


def _own_model(provider: type) -> str:
    model = getattr(provider, "default_model", None) or ""
    if model:
        return model
    for candidate in (getattr(provider, "models", None) or []):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _vision_key(provider: type) -> Optional[str]:
    """Chiave API del provider, se ne esiste una configurata."""
    try:
        from g4f.tools.auth import AuthManager

        return AuthManager.load_api_key(provider)
    except Exception:
        return None


def vision_model_map() -> Dict[str, str]:
    """Modello multimodale per provider: ``Provider=model`` separati da virgola.

    Serve per i provider che non dichiarano ``vision_models`` (le API
    OpenAI-compatibili espongono solo il catalogo, senza dire quali modelli
    accettano immagini) e per non dover indovinare: Groq, per esempio, ha un
    solo modello che vede, ``qwen/qwen3.8-27b``.
    """
    raw = os.environ.get("G4F_AUTOFREE_VISION_MODELS", "")
    mapping: Dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        name, model = item.split("=", 1)
        if name.strip() and model.strip():
            mapping[name.strip()] = model.strip()
    return mapping


def declared_vision_model(provider: type) -> Optional[str]:
    """Modello vision dichiarato dal provider (``default_vision_model`` o lista)."""
    model = getattr(provider, "default_vision_model", None)
    if isinstance(model, str) and model:
        return model
    for candidate in getattr(provider, "vision_models", None) or []:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def vision_capable(provider: type) -> bool:
    """Può vedere immagini: lo dichiara, oppure ha una chiave API configurata.

    La chiave conta quanto la dichiarazione: i provider OpenAI-compatibili
    (Gemini API, Groq, OpenRouter) non dichiarano ``vision_models`` finché il
    catalogo non è stato scaricato, ma con una chiave funzionano.
    """
    if vision_model_map().get(getattr(provider, "__name__", "")):
        return True
    return bool(declared_vision_model(provider) or _vision_key(provider))


def vision_candidates() -> List[str]:
    """Provider su cui ha senso provare la vision.

    Più stretto di ``candidates()``: qui interessano anche i provider senza
    ``default_model`` (catalogo caricato a runtime) se hanno una chiave, mentre
    un provider text-only non va nemmeno interrogato con un'immagine.
    """
    names: List[str] = []
    excluded = _excluded()
    for name in ProviderLoader.names:
        if name == "AutoFree" or name in excluded:
            continue
        try:
            provider = ProviderLoader.from_name(name)
        except Exception:
            continue
        if provider is None or not getattr(provider, "working", True):
            continue
        if _is_non_chat(provider) or not vision_capable(provider):
            continue
        if getattr(provider, "needs_auth", False) and not _vision_key(provider):
            continue
        names.append(name)
    return names


def candidates() -> List[str]:
    """Provider che ha senso misurare: chat, senza auth obbligatoria."""
    names: List[str] = []
    excluded = _excluded()
    for name in ProviderLoader.names:
        if name == "AutoFree" or name in excluded:
            continue
        try:
            provider = ProviderLoader.from_name(name)
        except Exception:
            continue
        if provider is None or not getattr(provider, "working", True):
            continue
        if getattr(provider, "needs_auth", False) or _is_non_chat(provider):
            continue
        if not _own_model(provider):
            continue  # nessun modello dichiarato: non e' un provider di chat
        names.append(name)
    seed_rank = {name: i for i, name in enumerate(SEED)}
    names.sort(key=lambda n: (seed_rank.get(n, len(SEED)), n))
    return names[:MAX_CANDIDATES]


async def _ask(provider: type, model: str) -> Optional[str]:
    """Una chat minima con la stessa strada usata dai client (AsyncClient)."""
    from g4f.client import AsyncClient

    response = await asyncio.wait_for(
        AsyncClient().chat.completions.create(
            model=model,
            provider=provider,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=16,
            stream=False,
        ),
        PROBE_TIMEOUT,
    )
    return (response.choices[0].message.content or "").strip()


async def _ask_ok(provider: type, model: str) -> bool:
    """Vero solo se il provider risponde con contenuto *utile*.

    Un 200 con "rate limit"/"sign up" come contenuto non e' un successo: senza
    questo controllo il probe promuoverebbe provider che non servono nulla.
    """
    content = await _ask(provider, model)
    if not content:
        return False
    head = content[:200].lower()
    poison = (
        "请求过多",
        "限流",
        "rate limit",
        "too many requests",
        "sign up",
        "iscriviti",
        "registrati",
        "suscript",
        "insufficient quota",
        "quota exceeded",
        "no cake credits",
        "temporarily unavailable",
    )
    return not any(marker in head for marker in poison)


async def _ask_vision(provider: type, model: str) -> Optional[str]:
    """Chat con un'immagine reale: serve un modello che la veda davvero."""
    from g4f.client import AsyncClient

    response = await asyncio.wait_for(
        AsyncClient().chat.completions.create(
            model=model,
            provider=provider,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": VISION_PNG}},
                    ],
                }
            ],
            # I modelli multimodali "thinking" consumano il budget in ragionamento
            # prima di scrivere: con 24 token la risposta e' vuota e il provider
            # sembrerebbe cieco.
            max_tokens=512,
            stream=False,
        ),
        VISION_TIMEOUT,
    )
    return (response.choices[0].message.content or "").strip()


async def _probe(provider: type) -> Dict[str, Any]:
    """Misura vita e tolleranza al modello generico di un provider.

    Chiamare direttamente ``provider.create_async_generator`` non basta: diversi
    provider emettono oggetti Response/chunk che solo il client normalizza, e un
    provider sano verrebbe marcato come guasto.
    """
    name = getattr(provider, "__name__", str(provider))
    own_model = _own_model(provider)
    started = time.time()
    tolerant = False
    alive = False
    used_model = own_model

    try:
        if await _ask_ok(provider, PROBE_MODEL):
            tolerant = alive = True
            used_model = PROBE_MODEL
    except Exception as exc:
        debug.log(f"AutoFree health: {name} non tollera {PROBE_MODEL}: {exc!r}")

    if not alive and own_model and own_model != PROBE_MODEL:
        try:
            if await _ask_ok(provider, own_model):
                alive = True
                used_model = own_model
        except Exception as exc:
            debug.log(f"AutoFree health: {name} non risponde: {exc!r}")

    if not alive:
        # Un rate limit transitorio non deve declassare un provider sano:
        # un secondo tentativo, un attimo dopo, prima di dichiararlo guasto.
        await asyncio.sleep(3)
        try:
            if await _ask_ok(provider, PROBE_MODEL):
                tolerant = alive = True
                used_model = PROBE_MODEL
        except Exception:
            pass

    vision = False
    vision_model = None
    vision_latency = None
    if vision_capable(provider):
        candidate = (
            vision_model_map().get(name)
            or declared_vision_model(provider)
            or PROBE_VISION_MODEL
        )
        started_vision = time.time()
        try:
            answer = await _ask_vision(provider, candidate)
            vision = any(word in (answer or "").lower() for word in VISION_WORDS)
            if vision:
                vision_model = candidate
            else:
                debug.log(
                    f"AutoFree health: {name} non vede l'immagine "
                    f"({(answer or '')[:80]!r})"
                )
        except Exception as exc:
            debug.log(f"AutoFree health: {name} vision fallita: {exc!r}")
        vision_latency = round(time.time() - started_vision, 1)

    return {
        "ok": alive,
        "tolerant": tolerant,
        "latency": round(time.time() - started, 1),
        "model": used_model,
        "vision": vision,
        "vision_model": vision_model,
        "vision_latency": vision_latency,
        "ts": time.time(),
    }


async def probe_all() -> Dict[str, Any]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: Dict[str, Any] = {}

    async def worker(name: str) -> None:
        async with semaphore:
            try:
                provider = ProviderLoader.from_name(name)
            except Exception:
                return
            results[name] = await _probe(provider)

    measured = list(dict.fromkeys(list(candidates()) + vision_candidates()))
    await asyncio.gather(*(worker(name) for name in measured))
    with _lock:
        for name, fresh in results.items():
            old = dict(_state["providers"].get(name) or {})
            streak = int(old.get("fail_streak") or 0)
            if fresh.get("ok"):
                fresh["fail_streak"] = 0
                _state["providers"][name] = fresh
                continue
            streak += 1
            # Un probe fallito non basta a declassare: i rate limit sono
            # transitori e il failover a runtime copre comunque i guasti veri.
            # Servono FAIL_STREAK_TO_DEMOTE fallimenti consecutivi.
            recent_success = old.get("ok") and (
                time.time() - float(old.get("ts") or 0) < REAL_GRACE
            )
            if recent_success and streak < FAIL_STREAK_TO_DEMOTE:
                merged = old
                merged["fail_streak"] = streak
                merged["probe_ts"] = fresh["ts"]
                _state["providers"][name] = merged
            else:
                fresh["fail_streak"] = streak
                _state["providers"][name] = fresh
        _state["updated"] = time.time()
    _save()
    ok = sum(1 for r in results.values() if r["ok"])
    tolerant = sum(1 for r in results.values() if r.get("tolerant"))
    seeing = [n for n, r in results.items() if r.get("vision")]
    debug.log(
        f"AutoFree health: probe completata, {ok}/{len(results)} vivi, "
        f"{tolerant} tolleranti, vision: {', '.join(seeing) or 'nessuno'}"
    )
    return results


def _run_probe() -> None:
    global _probing
    try:
        # Il thread di probe e' un contesto nuovo rispetto al boot del server:
        # senza questo, i provider che usano CDP non vedrebbero
        # G4F_BROWSER_HOST/PORT e risulterebbero falsamente guasti.
        try:
            from g4f.cookies import BrowserConfig

            BrowserConfig.load_from_env()
        except Exception:
            pass
        load()
        asyncio.run(probe_all())
    except Exception as exc:
        debug.log(f"AutoFree health: probe fallita ({exc!r})")
    finally:
        with _lock:
            _probing = False


def ensure_fresh(force: bool = False) -> None:
    """Avvia il probe in background se lo stato e' scaduto (mai bloccante)."""
    global _probing
    with _lock:
        stale = force or (time.time() - float(_state.get("updated") or 0)) > TTL
        if _probing or not stale:
            return
        _probing = True
    threading.Thread(target=_run_probe, daemon=True, name="autofree-health").start()


def record_success(
    name: Optional[str],
    latency: Optional[float] = None,
    vision: bool = False,
    vision_model: Optional[str] = None,
) -> None:
    """Un successo reale vale piu' di un probe: promuove subito il provider.

    Se il client ha mandato un modello arbitrario ed e' andata bene, il provider
    e' anche tollerante: e' l'informazione piu' preziosa per l'ordinamento.
    Con ``vision`` il successo riguarda una richiesta con immagine: il provider
    ha visto davvero il contenuto, quindi entra subito nella catena vision.
    """
    if not name:
        return
    with _lock:
        entry = dict(_state["providers"].get(name) or {})
        entry.update(
            {
                "ok": True,
                "tolerant": bool(entry.get("tolerant")) or True,
                "real": True,
                "fail_streak": 0,
                "latency": round(latency, 1)
                if latency
                else float(entry.get("latency") or 0.0),
                "ts": time.time(),
            }
        )
        if vision:
            entry["vision"] = True
            entry["vision_model"] = vision_model or entry.get("vision_model")
            if latency:
                entry["vision_latency"] = round(latency, 1)
        _state["providers"][name] = entry
    _save()


def preferred_model(name: str) -> Optional[str]:
    """Modello da usare con questo provider.

    ``None`` = passa quello chiesto dal client (provider tollerante).
    Altrimenti il modello su cui il provider ha risposto nel probe: cosi' anche
    i provider legati al proprio catalogo servono da riserva utile invece di
    fallire su ogni modello "straniero".
    """
    entry = (_state.get("providers") or {}).get(name) or {}
    if entry.get("tolerant"):
        return None
    return entry.get("model") or None


def preferred_vision_model(name: str, requested: Optional[str] = None) -> str:
    """Modello da passare al provider per una richiesta con immagine.

    Il nome chiesto dal client (``gpt-4o``) non e' un modello che un backend
    gratuito conosce: si usa quello misurato nel probe vision, altrimenti quello
    dichiarato dal provider, altrimenti ``G4F_AUTOFREE_VISION_MODEL``. Il nome
    del client si conserva solo se il provider lo espone tra i suoi vision.
    """
    entry = (_state.get("providers") or {}).get(name) or {}
    try:
        provider = ProviderLoader.from_name(name)
    except Exception:
        provider = None
    if requested and provider is not None:
        if requested in (getattr(provider, "vision_models", None) or []):
            return requested
    # La configurazione esplicita vince sulla misura: se un modello va in 429
    # (quota giornaliera) si cambia in ``.env`` senza aspettare il probe.
    return (
        vision_model_map().get(name)
        or entry.get("vision_model")
        or (declared_vision_model(provider) if provider is not None else None)
        or PROBE_VISION_MODEL
    )


def ordered_names(vision: bool = False) -> List[str]:
    """Candidati ordinati per convenienza reale, non per lista.

    * vivi: punteggio = latenza misurata (i non tolleranti ricevono il proprio
      modello, quindi sono comunque utili)
    * vivi ma lentissimi (turnstile): dopo gli ignoti, prima dei falliti
    * ignoti, poi falliti

    Con ``vision=True`` la lista contiene solo i provider che hanno dichiarato
    o misurato la capacita' di vedere immagini, ordinati per latenza vision.
    """
    ensure_fresh()
    probed = dict(_state.get("providers", {}))
    seed_rank = {name: i for i, name in enumerate(SEED)}
    names = vision_candidates() if vision else candidates()

    if vision:
        def vision_key(name: str):
            entry = probed.get(name) or {}
            rank = seed_rank.get(name, len(SEED))
            if entry.get("vision"):
                latency = float(entry.get("vision_latency") or entry.get("latency") or 999.0)
                return (0, latency, rank)
            # Mai misurato (probe in corso o provider appena comparso): vale un
            # tentativo, ma dopo chi ha già dimostrato di vedere.
            return (1, 0.0, rank) if not entry else (2, 0.0, rank)

        return sorted(names, key=vision_key)

    def key(name: str):
        entry = probed.get(name) or {}
        latency = float(entry.get("latency") or 999.0)
        rank = seed_rank.get(name, len(SEED))
        # La tolleranza non e' piu' decisiva: ai provider legati al proprio
        # catalogo passiamo il loro modello (preferred_model). Conta la latenza,
        # con un bonus a chi ha appena servito una richiesta vera: "sticky" sui
        # provider che funzionano davvero riduce la varianza dei tempi.
        score = latency
        if entry.get("real") and (time.time() - float(entry.get("ts") or 0)) < REAL_GRACE:
            score *= 0.5
        if entry.get("tolerant") or entry.get("ok"):
            # Anche un provider tollerante ma lentissimo (turnstile da 70s)
            # non deve stare davanti a uno piu' rapido.
            return (0, score, rank) if score <= MAX_LATENCY else (1, score, rank)
        if not entry:
            return (2, 0.0, rank)
        return (3, 0.0, rank)

    return sorted(names, key=key)
