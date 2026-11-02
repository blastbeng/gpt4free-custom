#!/usr/bin/env bash
# Bootstrap idempotente dello stack gpt4free.
#
# Gira come ExecStartPre di docker-compose@gpt4free.service, quindi non serve
# nessun passo manuale: al primo avvio crea le directory persistenti e genera
# la API key OpenAI-compatibile in .env (0600, mai committata). Agli avvii
# successivi non tocca nulla: la chiave esistente resta valida.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Directory montate nel container: l'immagine gira come uid/gid 1000 (g4f),
# lo stesso dell'utente blast, quindi non serve alcun chown.
mkdir -p data/g4f/cookies data/g4f/cache data/generated_media data/har_and_cookies
chmod 700 data/g4f

umask 077
if [ ! -s .env ]; then
    cat > .env <<EOF
# Generato da scripts/bootstrap.sh - NON committare (gitignored).
# Chiave richiesta da ogni client OpenAI-compatibile (header: Authorization: Bearer ...).
G4F_API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
# Porta host esposta sull'IP del Pi (http://blastpi5:1337/v1).
G4F_PORT=1337
# Provider composito registrato da g4f_ext/ (montato in /ext): sceglie da solo
# il backend keyless che risponde, con failover e circuit breaker di g4f.
# Nessun nome di provider/modello scritto a mano; per forzare un singolo
# provider basta mettere qui il suo nome (es. G4F_PROVIDER=Yqcloud).
# NB: una variabile G4F_* impostata a stringa vuota NON vale come "unset":
# g4f la passa al provider come "" e la richiesta fallisce (Provider not found).
G4F_PROVIDER=AutoFree
TZ=Europe/Rome
EOF
fi

# .env preesistente senza chiave: la aggiungiamo senza toccare il resto.
if ! grep -qE '^G4F_API_KEY=.+' .env; then
    printf 'G4F_API_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env
fi

chmod 600 .env
