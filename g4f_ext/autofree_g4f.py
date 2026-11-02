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
import os
import time
from typing import List, Type

from g4f import debug
from g4f.Provider import AsyncGeneratorProvider, ProviderLoader, ProviderUtils
from g4f.providers.base_provider import get_async_provider_method
from g4f.providers.retry_provider import IterListProvider

# Un provider browser-based (turnstile) puo' impiegare 70s+ prima del primo
# token: oltre questa soglia la catena passa al successivo invece di far
# aspettare il client.
PROVIDER_TIMEOUT = float(os.environ.get("G4F_AUTOFREE_PROVIDER_TIMEOUT", 25))

try:
    import health as _health
except ImportError:  # nome del modulo non canonico: import esplicito
    from g4f_ext import health as _health  # type: ignore


def _pinned_names() -> List[str]:
    raw = os.environ.get("G4F_AUTOFREE_PROVIDERS", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _excluded() -> set:
    raw = os.environ.get("G4F_AUTOFREE_EXCLUDE", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _resolve(names: List[str]) -> List[Type]:
    resolved, missing, excluded = [], [], _excluded()
    for name in names:
        if name in excluded:
            continue
        try:
            provider = ProviderLoader.from_name(name)
        except Exception:
            provider = None
        if provider is None:
            missing.append(name)
        elif getattr(provider, "working", True) and provider not in resolved:
            resolved.append(provider)
    if missing:
        debug.log(f"AutoFree: provider sconosciuti ignorati: {', '.join(missing)}")
    return resolved


def _bounded(provider: Type) -> Type:
    """Sottoclasse che impone un tetto al tempo prima del primo token.

    IterListProvider passa al provider successivo solo se il precedente solleva:
    qui l'attesa eccessiva diventa un ``TimeoutError``, quindi il failover
    scatta anche quando un provider e' "vivo" ma lentissimo.
    """

    async def _generate(model, messages, stream=True, **kwargs):
        generator = get_async_provider_method(provider)(
            model=model, messages=messages, stream=stream, **kwargs
        )
        try:
            first = await asyncio.wait_for(generator.__anext__(), PROVIDER_TIMEOUT)
            yield first
            async for chunk in generator:
                yield chunk
        finally:
            try:
                await generator.aclose()
            except Exception:
                pass

    return type(
        getattr(provider, "__name__", "BoundedProvider"),
        (AsyncGeneratorProvider,),
        {
            "create_async_generator": staticmethod(_generate),
            "label": getattr(provider, "label", None),
            "url": getattr(provider, "url", None),
            "working": True,
            "needs_auth": getattr(provider, "needs_auth", False),
        },
    )


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
    models: List[str] = []

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages,
        stream: bool = True,
        ignored: list = None,
        **kwargs,
    ):
        names = _pinned_names() or _health.ordered_names()
        candidates = _resolve(names)
        if not candidates:
            raise RuntimeError(
                "AutoFree: nessun provider disponibile ("
                + ", ".join(names)
                + ")"
            )
        chain = IterListProvider([_bounded(p) for p in candidates], shuffle=False)
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
        # senza aspettare il probe periodico.
        _health.record_success(
            getattr(chain.last_provider, "__name__", None), time.time() - started
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
