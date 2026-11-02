"""Provider ``AutoFree``: failover automatico fra i provider keyless di g4f.

Perche' esiste
--------------
Il routing automatico nativo di g4f (``AnyProvider``) parte da liste statiche
di provider scritte in ``g4f/models.py``; in questa installazione quelle liste
puntano a provider che richiedono Chrome o un account e falliscono subito.
Il risultato e' che un client OpenAI con ``model="gpt-4o-mini"`` riceve
``Request execution failed``.

``AutoFree`` non sceglie nulla a mano e non ha una lista di "provider da
ignorare": l'ordine dei candidati arriva da ``health.py``, che misura i
provider in background e salva l'esito su disco (TTL 6h). Chi smette di
funzionare scende in fondo, chi torna a funzionare risale.

Il failover vero e proprio e' delegato a ``IterListProvider``, il composito
nativo di g4f:

* prova i provider nell'ordine dato e passa al successivo finche' non arriva
  il primo token (nessun contenuto parziale gia' inviato al client),
* registra successi/fallimenti in ``ProviderCircuitBreaker`` (cooldown 60s
  dopo 3 fallimenti consecutivi),
* rispetta ``--ignored-providers``.

Override manuali, se servono: ``G4F_AUTOFREE_PROVIDERS`` (lista fissa, in
ordine) e ``G4F_AUTOFREE_EXCLUDE`` (nomi da non usare mai).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional, Type

from g4f import debug
from g4f.Provider import AsyncGeneratorProvider, ProviderLoader, ProviderUtils
from g4f.providers.base_provider import get_async_provider_method
from g4f.providers.retry_provider import IterListProvider

# Una riga solo per le richieste con immagine: serve a vedere dal journal quale
# catena ha servito davvero e se il routing vision e' scattato. Il logger "g4f"
# sta a ERROR, quindi il livello va alzato qui o la riga sparisce.
logger = logging.getLogger("g4f.autofree")
logger.setLevel(logging.INFO)

# Un provider browser-based (turnstile) puo' impiegare 70s+ prima del primo
# token: oltre questa soglia la catena passa al successivo invece di far
# aspettare il client.
PROVIDER_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_PROVIDER_TIMEOUT", 15))
# Budget complessivo della catena: oltre questo tempo si restituisce l'errore
# invece di continuare a provare provider (il client ha comunque un suo timeout).
TOTAL_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_TOTAL_TIMEOUT", 75))
# Le richieste con immagine hanno un solo provider utile per volta (quello con
# la chiave) e l'upload pesa: budget separato, piu' generoso sul singolo hop.
VISION_PROVIDER_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_VISION_PROVIDER_TIMEOUT", 45))
VISION_TOTAL_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_VISION_TOTAL_TIMEOUT", 120))

# Alcuni provider "free" rispondono 200 OK con il messaggio di errore come
# contenuto (rate limit, quota, invito a registrarsi). Senza questo filtro il
# failover non scatta e il client riceve quel testo come risposta.
_POISON = (
    "请求过多",          # rate limit (zh-CN)
    "限流",
    "充值",
    "rate limit",
    "too many requests",
    "sign up",
    "iscriviti",        # "Sign up and repeat your request" (it)
    "registrati",
    "suscript",
    "insufficient quota",
    "quota exceeded",
    "no cake credits",
    "temporarily unavailable",
)


def _is_poison(text: str) -> bool:
    head = (text or "")[:200].lower()
    return any(marker in head for marker in _POISON)

try:
    import health as _health
except ImportError:  # nome del modulo non canonico: import esplicito
    from g4f_ext import health as _health  # type: ignore


_IMAGE_PART_TYPES = ("image_url", "input_image")


def _part_image(part) -> Optional[str]:
    """URL/data-URI di una parte immagine, se la parte ne contiene una."""
    if not isinstance(part, dict) or part.get("type") not in _IMAGE_PART_TYPES:
        return None
    url = part.get("image_url") or part.get("image")
    if isinstance(url, dict):
        url = url.get("url")
    return url if isinstance(url, str) and url else None


def _media_from_messages(messages) -> List[tuple]:
    """Immagini OpenAI-style -> ``media``, il formato che i provider capiscono.

    L'API inoltra i ``messages`` al provider senza convertirli: una parte
    ``image_url`` arriva intatta e i provider la trattano come un URL (Groq:
    ``invalid base64 url``, Gemini: ``Unsupported file URI type: data:...``).
    ``media`` invece e' il canale gestito da g4f: ``resolve_media`` nel client,
    ``render_messages`` nei template OpenAI-compatibili, ``inlineData`` in
    Gemini.
    """
    items: List[tuple] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            url = _part_image(part)
            if url:
                items.append((url, part.get("filename") or part.get("file_name")))
    return items


def _text_only(messages) -> list:
    """Copia dei ``messages`` senza le parti immagine (i testi restano)."""
    cleaned = []
    for message in messages or []:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list) or not any(_part_image(p) for p in content):
            cleaned.append(message)
            continue
        texts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        cleaned.append({**message, "content": "\n".join(texts) if texts else ""})
    return cleaned


def _has_image(messages, kwargs) -> bool:
    """Vero se la richiesta contiene un'immagine.

    Un provider text-only non riceve l'immagine e risponde lo stesso: senza
    questo controllo il client otterrebbe una risposta sicura su un'immagine
    che nessuno ha visto. Vanno contati tutti i modi in cui l'API accetta
    immagini, non solo ``image``: il client converte ``image``/``images`` in
    ``media`` prima di arrivare qui, quindi guardare solo ``image`` mandava le
    richieste con immagine sulla catena keyless text-only.
    """
    if kwargs.get("image") or kwargs.get("images") or kwargs.get("media"):
        return True
    return bool(_media_from_messages(messages))


def _pinned_names() -> List[str]:
    raw = os.environ.get("G4F_AUTOFREE_PROVIDERS", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _vision_pinned_names() -> List[str]:
    raw = os.environ.get("G4F_AUTOFREE_VISION_PROVIDERS", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _excluded() -> set:
    raw = os.environ.get("G4F_AUTOFREE_EXCLUDE", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _resolve(names: List[str]) -> List[tuple]:
    """Risolve i nomi in ``(nome, classe)``, saltando esclusioni e sconosciuti.

    Il nome serve a chiedere a health.py il modello giusto per quel provider: la
    classe da sola non basta, perche' il nome e' la chiave dello stato misurato.
    """
    resolved, missing, excluded = [], [], _excluded()
    seen = set()
    for name in names:
        if name in excluded:
            continue
        try:
            provider = ProviderLoader.from_name(name)
        except Exception:
            provider = None
        if provider is None:
            missing.append(name)
        elif getattr(provider, "working", True) and provider not in seen:
            seen.add(provider)
            resolved.append((name, provider))
    if missing:
        debug.log(f"AutoFree: provider sconosciuti ignorati: {', '.join(missing)}")
    return resolved


# Attributi che descrivono cosa sa fare il provider: senza copiarli sul wrapper
# la capability sparisce (era il motivo per cui /v1/models diceva sempre
# "vision: false" per AutoFree e nessun routing poteva funzionare).
_PROVIDER_ATTRS = (
    "label",
    "url",
    "working",
    "needs_auth",
    "default_model",
    "default_vision_model",
    "models",
    "vision_models",
    "image_models",
    "supports_stream",
    "supports_system_message",
    "supports_message_history",
    "supports_native_tools",
)


def _provider_api_key(provider: Type):
    """Chiave configurata per il provider (env ``<PROVIDER>_API_KEY`` o cookies)."""
    try:
        from g4f.tools.auth import AuthManager

        return AuthManager.load_api_key(provider)
    except Exception:
        return None


def _gate_key():
    """La nostra chiave (G4F_API_KEY): non e' una credenziale per l'upstream."""
    try:
        from g4f.config import AppConfig

        return getattr(AppConfig, "g4f_api_key", None)
    except Exception:
        return None


def _bounded(
    provider: Type,
    model_override: str = None,
    deadline: float = None,
    provider_timeout: float = PROVIDER_TIMEOUT,
) -> Type:
    """Sottoclasse che impone un tetto al tempo prima del primo token.

    IterListProvider passa al provider successivo solo se il precedente solleva:
    qui l'attesa eccessiva diventa un ``TimeoutError``, quindi il failover
    scatta anche quando un provider e' "vivo" ma lentissimo.
    """

    async def _generate(model, messages, stream=True, **kwargs):
        # Al provider non va la chiave del nostro gateway: il middleware
        # dell'API la mette in ``config.api_key`` e finirebbe come credenziale
        # upstream (401). Se il provider ne ha una configurata si usa quella.
        gate = _gate_key()
        if gate and kwargs.get("api_key") == gate:
            kwargs.pop("api_key", None)
        key = _provider_api_key(provider)
        if key:
            kwargs["api_key"] = key
        generator = get_async_provider_method(provider)(
            model=model_override or model,
            messages=messages,
            stream=stream,
            **kwargs,
        )
        loop = asyncio.get_event_loop()
        pending = []
        try:
            # Il timeout deve coprire il primo *contenuto*, non il primo chunk:
            # molti provider emettono prima un ProviderInfo e poi restano muti.
            while True:
                timeout = provider_timeout
                if deadline is not None:
                    timeout = min(timeout, deadline - loop.time())
                    if timeout <= 0:
                        raise RuntimeError(
                            f"{getattr(provider, '__name__', provider)}: budget catena esaurito"
                        )
                try:
                    chunk = await asyncio.wait_for(generator.__anext__(), timeout)
                except StopAsyncIteration:
                    # Provider che chiude senza aver prodotto nulla: senza
                    # questo, lo StopAsyncIteration uscirebbe dal generatore
                    # come "async generator raised StopAsyncIteration" e
                    # nasconderebbe la causa al client.
                    raise RuntimeError(
                        f"{getattr(provider, '__name__', provider)}: nessuna risposta"
                    )
                if isinstance(chunk, str) and chunk.strip():
                    if _is_poison(chunk):
                        raise RuntimeError(
                            f"{getattr(provider, '__name__', provider)}: risposta di "
                            "errore travestita da contenuto"
                        )
                    for buffered in pending:
                        yield buffered
                    yield chunk
                    break
                pending.append(chunk)
            async for chunk in generator:
                yield chunk
        finally:
            try:
                await generator.aclose()
            except Exception:
                pass

    attrs = {
        name: getattr(provider, name)
        for name in _PROVIDER_ATTRS
        if hasattr(provider, name)
    }
    attrs["create_async_generator"] = staticmethod(_generate)
    attrs["working"] = True
    attrs["needs_auth"] = getattr(provider, "needs_auth", False)
    return type(getattr(provider, "__name__", "BoundedProvider"), (AsyncGeneratorProvider,), attrs)


class AutoFree(AsyncGeneratorProvider):
    """Provider composito che sceglie da solo un backend keyless funzionante."""

    label = "AutoFree"
    url = "https://github.com/xtekky/gpt4free"
    working = True
    active_by_default = True
    needs_auth = False
    supports_stream = True
    supports_system_message = True
    supports_message_history = True
    default_model = "gpt-4"
    # L'elenco e' informativo (AutoFree accetta qualsiasi nome), ma serve a
    # /api/AutoFree/models: senza voci esplicite il mixin lo riduce a
    # [default_model] e la capability vision non comparirebbe.
    models: List[str] = ["gpt-4", "gpt-4o"]
    # AutoFree accetta immagini, ma solo instradandole su un provider che le
    # vede: dichiararlo qui e' come i client scoprono la capability.
    default_vision_model = "gpt-4o"
    vision_models = ["gpt-4o"]

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages,
        stream: bool = True,
        ignored: list = None,
        **kwargs,
    ):
        # Un'immagine non e' un dettaglio del prompt: un provider text-only la
        # scarta e risponde comunque, quindi la catena va ristretta a chi ha
        # dimostrato (probe) o dichiarato di vedere. Meglio un errore chiaro di
        # una risposta inventata.
        vision = _has_image(messages, kwargs)
        # Le immagini vanno spostate su ``media`` prima di scegliere il
        # provider: i messaggi OpenAI-style non vengono convertiti da nessuno e
        # i provider li rifiutano (400) invece di vedere l'immagine.
        media = _media_from_messages(messages)
        if media:
            existing = kwargs.get("media")
            if isinstance(existing, str):
                existing = [(existing, None)]
            merged = list(existing or [])
            seen = {item[0] for item in merged if isinstance(item, (list, tuple))}
            merged += [item for item in media if item[0] not in seen]
            kwargs["media"] = merged
            messages = _text_only(messages)
        if vision:
            names = _vision_pinned_names() or _health.ordered_names(vision=True)
            provider_timeout = VISION_PROVIDER_TIMEOUT
            total_timeout = VISION_TOTAL_TIMEOUT
        else:
            names = _pinned_names() or _health.ordered_names()
            provider_timeout = PROVIDER_TIMEOUT
            total_timeout = TOTAL_TIMEOUT
        if vision:
            logger.info(
                "AutoFree: richiesta con immagine (media=%d) -> catena: %s",
                len(kwargs.get("media") or []),
                ", ".join(names) or "vuota",
            )

        candidates = _resolve(names)
        if not candidates:
            if vision:
                raise RuntimeError(
                    "AutoFree: nessun provider con supporto vision disponibile ("
                    + (", ".join(names) or "catena vuota")
                    + "). Configura G4F_AUTOFREE_VISION_PROVIDERS o una chiave API "
                    "(es. GEMINI_API_KEY)."
                )
            raise RuntimeError(
                "AutoFree: nessun provider disponibile (" + ", ".join(names) + ")"
            )

        deadline = asyncio.get_event_loop().time() + total_timeout
        overrides = {}
        bounded = []
        for name, provider in candidates:
            override = (
                _health.preferred_vision_model(name, model)
                if vision
                else _health.preferred_model(name)
            )
            overrides[getattr(provider, "__name__", name)] = override
            bounded.append(_bounded(provider, override, deadline, provider_timeout))
        chain = IterListProvider(bounded, shuffle=False)

        started = time.time()
        async for chunk in chain.create_async_generator(
            model=model,
            messages=messages,
            stream=stream,
            ignored=ignored or [],
            **kwargs,
        ):
            yield chunk
        # Un successo reale e' il segnale piu' affidabile: aggiorna la classifica
        # senza aspettare il probe periodico. Con un'immagine vale anche come
        # prova che quel provider la vede davvero.
        used = getattr(chain.last_provider, "__name__", None)
        _health.record_success(
            used,
            time.time() - started,
            vision=vision,
            vision_model=overrides.get(used),
        )


def install() -> None:
    """Rende ``AutoFree`` risolvibile come qualunque provider di g4f."""
    if "AutoFree" in ProviderLoader.names:
        return
    ProviderLoader.names = list(ProviderLoader.names) + ["AutoFree"]
    # from_name() consulta prima questa cache: evita un import di modulo.
    ProviderLoader.loaded["AutoFree"] = AutoFree
    ProviderLoader.loaded["autofree"] = AutoFree
    # Le cache derivate vanno invalidate, altrimenti restano quelle di prima.
    ProviderLoader._lower_names = None
    ProviderLoader._providers_cache = None
    ProviderUtils.convert["AutoFree"] = AutoFree
    _health.load()
    _health.ensure_fresh()
    debug.log("AutoFree registrato; ordinamento dei provider da health.py")
