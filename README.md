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

1. `gpt4free`: `curl /v1/models` con chiave (30s, 3 retry, start_period 120s).
2. `chromium`: `bash` su `/dev/tcp/127.0.0.1/9222` (l'immagine non ha curl).
3. `scripts/selftest.sh`: chat reale su piu' modelli, log only, non riavvia
   nulla (i compose attivi sono gia' controllati dal cron di sistema).

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

Knobs utili in `.env`: `G4F_AUTOFREE_PROVIDERS` (blocca una catena fissa),
`G4F_AUTOFREE_EXCLUDE` (nomi mai usati), `G4F_AUTOFREE_TTL_SECONDS`,
`G4F_AUTOFREE_PROVIDER_TIMEOUT`, `G4F_AUTOFREE_PROBE_MODEL`.
Il file di stato si puo' cancellare in qualsiasi momento: viene ricostruito.

## Aggiornamenti

`/usr/local/bin/manage-docker` forza il check dei digest e riavvia la unit solo
se un'immagine cambia. Per bloccare una versione: `G4F_IMAGE` / `CHROMIUM_IMAGE`
in `.env` (tag o digest) e restart della unit.
