# gpt4free su Raspberry Pi 5

API OpenAI-compatibile (upstream: https://github.com/xtekky/gpt4free) esposta
solo in LAN su `http://blastpi5:1337/v1`, con provider scelti automaticamente.

## Architettura

| componente | immagine | ruolo |
| --- | --- | --- |
| `gpt4free` | `hlohaus789/g4f:latest-slim` (arm64) | API OpenAI-compatibile, 253 modelli |
| `chromium` | `chromedp/headless-shell` (arm64) | browser CDP per i provider che richiedono JS/turnstile |

`g4f_ext/` e' montata read-only in `/ext` e registrata via `PYTHONPATH` +
`sitecustomize.py`: aggiunge il provider **AutoFree** senza patchare l'immagine.
AutoFree delega a `IterListProvider` (composito nativo di g4f) e prova in ordine
`Yqcloud -> LLM7 -> HuggingSpace -> Cloudflare`, con circuit breaker (3
fallimenti = 60s di cooldown) e rispetto di `--ignored-providers`.

Chromium e' raggiunto via `G4F_BROWSER_HOST=172.31.0.10` (rete `browser` con
subnet fissa): **deve essere un IP**, perche' il server HTTP DevTools di Chrome
risponde 500 a un header `Host` che non sia un IP o `localhost`.

## Avvio

```sh
sudo systemctl enable --now docker-compose@gpt4free
systemctl status docker-compose@gpt4free
docker ps --filter name=gpt4free
```

Al primo avvio `scripts/bootstrap.sh` (drop-in `10-bootstrap.conf`) crea
`data/` e `.env` con una `G4F_API_KEY` casuale: nessun passo manuale.

## Uso

```sh
KEY=$(sed -n 's/^G4F_API_KEY=//p' /opt/docker/compose/gpt4free/.env)
curl -s http://blastpi5:1337/v1/models -H "Authorization: Bearer $KEY"
curl -s http://blastpi5:1337/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ciao"}]}'
```

Qualsiasi client OpenAI: `base_url=http://blastpi5:1337/v1`, `api_key=$KEY`.
Il nome del modello e' ininfluente finche' risponde il primo provider della
catena (Yqcloud accetta nomi arbitrari).

## Healthcheck

1. `gpt4free`: `curl /v1/providers` con chiave (30s, 3 retry, start_period
   120s). Non `/v1/models`: a cache fredda interroga gli upstream e puo'
   impiegare 9-20s, segnando unhealthy un container che risponde.
2. `chromium`: `bash` su `/dev/tcp/127.0.0.1/9222` con una vera GET
   `/json/version` (l'immagine non ha curl): la sola connessione TCP non basta,
   un Chromium saturo accetta il socket e poi non risponde.
3. `scripts/selftest.sh`: chat reale su piu' modelli, log only, non riavvia
   nulla (i compose attivi sono gia' controllati dal cron di sistema).

## Chromium: perche' e' limitato e cosa lo tiene in riga

Chromium qui e' un servizio di supporto (turnstile/JS per Cloudflare e simili),
ma e' anche l'unico componente che puo' far cadere tutto lo stack: se si satura,
g4f resta bloccato dentro una richiesta CDP e non chiude piu' le tab; ogni tab
resta un renderer, e il memcg comincia a ucciderli mentre Chrome li ricrea.

Misura del 2026-11-02, prima della correzione: 1024MiB/1GiB, swap saturo,
**121 renderer**, **110 OOM-kill in 28 minuti**, CPU 180%, `/v1/providers` in
timeout a 20s, load 18. Portare `mem_limit` da 640m a 1g non aveva risolto
nulla: le kill erano aumentate, perche' su Pi5 lo swap e' zram (RAM reale), non
disco. Aggiungere memoria non e' la leva.

Due difese, entrambe necessarie:

1. **Flag al browser** (`command:` del servizio chromium): `--disable-gpu`
   (il `run.sh` dell'immagine forza `--use-angle=swiftshader`: il processo GPU
   veniva ucciso e i renderer restavano in loop su
   `ContextResult::kTransientFailure`), `--renderer-process-limit=4` (Chrome
   riusa i renderer invece di crearne uno per tab), `--js-flags=`
   `--max-old-space-size=128`, `--disable-dev-shm-usage`.
2. **Reaper** (`scripts/chromium-reaper.sh`, timer
   `gpt4free-chromium-reaper.timer`, ogni 5 minuti): legge solo il cgroup del
   container (`memory.current` contro `memory.max`, `oom_kill` in
   `memory.events`) e, oltre il 90% o con 3 renderer uccisi fra due giri,
   riavvia `docker-compose@gpt4free`. Riavvia la **unit** e non il container
   perche' il compose gira con `--abort-on-container-exit`: fermare chromium
   farebbe comunque cadere lo stack, ma senza il controllo di systemd. Rate
   limit 20 minuti, stato in `/run/gpt4free-chromium-reaper.state`; soglie
   `REAPER_MEM_PCT`, `REAPER_OOM_DELTA`, `REAPER_MIN_INTERVAL`.

La latenza di un reaper che sbaglia e' un riavvio di troppo: `--dry-run` misura
e stampa la decisione senza toccare nulla.

Mai `ps aux | grep chromium`: su questa macchina conta i chromium degli altri
stack. Il cgroup si trova con `docker inspect -f '{{.Id}}'` e
`find /sys/fs/cgroup -name "docker-<id>.scope"`.

Installazione (una volta, come root):

```sh
sudo cp systemd/gpt4free-chromium-reaper.service \
        systemd/gpt4free-chromium-reaper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpt4free-chromium-reaper.timer
```

Dopo la correzione, stessa macchina: 245-350MiB stabili, 1 renderer, CPU ~0%,
`oom_kill` fermo a 0, `/v1/providers` in 34ms.

## Stato dei provider: si misura da solo

Nessuna lista statica di "provider da ignorare": invecchierebbe in giorni.

`g4f_ext/health.py` misura i provider in background (thread separato, mai nel
percorso della richiesta) con due domande:

1. **sono vivi?** richiesta minima con il loro modello di default;
2. **tollerano un modello qualsiasi?** stessa richiesta con
   `G4F_AUTOFREE_PROBE_MODEL` (`gpt-4o-mini`), cioe' quello che manda un client
   OpenAI generico. Un provider che risponde solo al proprio catalogo va bene
   come riserva, non come prima scelta.

L'esito finisce in `data/g4f/autofree_health.json` (TTL 6h) e ordina la catena:
tolleranti veloci -> vivi veloci -> ignoti -> falliti. Un successo reale
registrato a runtime promuove subito il provider; un probe fallito da solo non
lo declassa (servono 2 fallimenti consecutivi, e c'e' un retry dopo 3s per i
rate limit transitori). Ogni provider ha un tetto di `G4F_AUTOFREE_PROVIDER_TIMEOUT`
(25s) sul primo token: se lo supera la catena passa al successivo.

Misura del 2026-11 su questa rete: `Yqcloud` (1-2s, tollerante), `Cloudflare`
(14-18s, tollerante, turnstile via Chromium), `CohereForAI_C4AI_Command` (~1s,
solo catalogo), `HuggingSpace` (~7s, solo catalogo), `Perplexity` (risponde ma
con "Sign up and repeat your request"). Tutti gli altri falliscono per cookie,
API key, browser non-headless o crediti: restano nel catalogo e vengono
riprovati al probe successivo, senza bisogno di toccare la configurazione.

Due difese che non dipendono dal probe:

* **errori travestiti da contenuto**: alcuni provider gratuiti rispondono
  `200 OK` con "rate limit"/"Iscriviti e ripeti la richiesta" come testo. Il
  wrapper li riconosce e li tratta come guasti, quindi il failover prosegue
  invece di consegnare quel testo al client;
* **modello per provider**: ai provider legati al proprio catalogo viene passato
  il loro modello (quello su cui hanno risposto nel probe), cosi' anche loro
  servono da riserva utile. Al client si presenta il nome modello richiesto.

Knobs utili in `.env`: `G4F_AUTOFREE_PROVIDERS` (blocca una catena fissa),
`G4F_AUTOFREE_EXCLUDE` (nomi mai usati, **ne' dalla catena ne' dal probe**:
un tentativo su un provider browser-based costa una tab, cioe' un renderer),
`G4F_AUTOFREE_TTL_SECONDS`,
`G4F_AUTOFREE_PROVIDER_TIMEOUT` (primo contenuto, default 15s),
`G4F_AUTOFREE_TOTAL_TIMEOUT` (budget catena, default 75s),
`G4F_AUTOFREE_PROBE_MODEL`.
Il file di stato si puo' cancellare in qualsiasi momento: viene ricostruito.

## Vision (immagini)

Una richiesta con immagine (`content` a lista con `image_url`, oppure campo
`image`) **non** usa la catena keyless: i provider gratuiti che rispondono qui
sono text-only e scarterebbero l'immagine, producendo una risposta sicura e
sbagliata. AutoFree rileva l'immagine e instrada solo su provider che vedono
davvero, misurati come tutto il resto (`health.py`, stessa TTL e stesso file di
stato, con `vision: true/false` e il modello usato).

Se non c'e' nessun provider con vision disponibile la risposta e' un errore
esplicito (`AutoFree: nessun provider con supporto vision disponibile`), mai una
risposta inventata.

Configurazione in `.env`:

| variabile | ruolo |
| --- | --- |
| `GEMINI_API_KEY` | Google AI Studio, provider `GeminiPro` (consigliata) |
| `GROQ_API_KEY` | Groq (free tier) |
| `OPENROUTER_API_KEY` | OpenRouter (`:free`) |
| `G4F_AUTOFREE_VISION_PROVIDERS` | catena fissa per le immagini, in ordine |
| `G4F_AUTOFREE_VISION_MODELS` | modello multimodale per provider, `Provider=model` (vince sulla misura) |
| `G4F_AUTOFREE_VISION_MODEL` | fallback quando il provider non ne dichiara uno |
| `G4F_AUTOFREE_VISION_PROVIDER_TIMEOUT` | primo contenuto per provider (45s) |
| `G4F_AUTOFREE_VISION_TOTAL_TIMEOUT` | budget della catena vision (120s) |

Il nome modello chiesto dal client (`gpt-4o`) viene sostituito con quello
multimodale del provider: `gemini-3.5-flash-lite` per `GeminiPro`,
`qwen/qwen3.8-27b` per `Groq` (unico modello Groq che accetta immagini sul free
tier; gli altri rispondono `400 content must be a string`). La chiave del nostro
gateway non viene mai inoltrata all'upstream: il wrapper usa
`<PROVIDER>_API_KEY` presa dall'ambiente.

La mappa esplicita vince sulla misura, cosi' un modello finito in quota (429) si
cambia da `.env` senza aspettare il probe: `gemini-2.5-flash` e' andato in 429
sulla chiave attuale, `gemini-3.5-flash-lite` risponde.

Misura del 2026-11: `Groq` 0.4-1.3s, `GeminiPro` 1.0-2.6s, entrambi rispondono
correttamente al colore di un'immagine di test. Catena in `.env`:
`G4F_AUTOFREE_VISION_PROVIDERS=Groq,GeminiPro`; il failover e' verificato in
entrambe le direzioni (se il primo risponde 429/503 la richiesta passa al
secondo senza errori per il client).

Nota: il flag `vision` di `/v1/models` e' sempre `false` per un bug upstream
(`isinstance(model, VisionModel)` applicato agli id stringa). La capability vera
si legge su `/api/AutoFree/models`, dove `gpt-4o` risulta `vision: true`.

Verifica rapida:

```sh
IMG=$(base64 -w0 /percorso/immagine.png)
curl -s http://blastpi5:1337/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":[
      {\"type\":\"text\",\"text\":\"Che colore e'?\"},
      {\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$IMG\"}}]}]}"
```

## Aggiornamenti

`/usr/local/bin/manage-docker` forza il check dei digest e riavvia la unit solo
se un'immagine cambia. Per bloccare una versione: `G4F_IMAGE` / `CHROMIUM_IMAGE`
in `.env` (tag o digest) e restart della unit.
