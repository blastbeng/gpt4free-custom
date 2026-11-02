#!/usr/bin/env bash
# Verifica post-boot dello stack gpt4free: attende che systemd e i container
# siano pronti, poi esegue il selftest e scrive l'esito in un log.
#
# Nasce come test una-tantum del ciclo di avvio completo (docker -> opt.mount ->
# bootstrap -> healthcheck). Per disattivarlo:
#   sudo systemctl disable --now gpt4free-bootcheck.service
#   sudo rm /etc/systemd/system/gpt4free-bootcheck.service
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
LOG="$DIR/data/boot-check.log"
mkdir -p "$DIR/data"

say() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; logger -t gpt4free-bootcheck "$*"; }

say "=== boot check: uptime $(uptime -p 2>/dev/null || echo n/a) ==="
say "unit: active=$(systemctl is-active docker-compose@gpt4free) enabled=$(systemctl is-enabled docker-compose@gpt4free)"
say "bootstrap: .env presente=$([ -s .env ] && echo si || echo NO) chiave=$([ -s .env ] && grep -q '^G4F_API_KEY=.\+' .env && echo si || echo NO)"

start=$(date +%s)
for _ in $(seq 1 60); do
    g="$(docker inspect -f '{{.State.Health.Status}}' gpt4free 2>/dev/null || echo absent)"
    c="$(docker inspect -f '{{.State.Health.Status}}' gpt4free-chromium 2>/dev/null || echo absent)"
    [ "$g" = healthy ] && [ "$c" = healthy ] && break
    sleep 5
done
say "container: gpt4free=$g chromium=$c dopo $(( $(date +%s) - start ))s"

if [ "$g" != healthy ]; then
    say "ESITO: FAIL (gpt4free non healthy)"
    docker logs --tail 20 gpt4free 2>&1 | tee -a "$LOG" >/dev/null
    exit 1
fi

KEY="$(sed -n 's/^G4F_API_KEY=//p' .env | head -1)"
PORT="$(sed -n 's/^G4F_PORT=//p' .env | head -1)"
PORT="${PORT:-1337}"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Authorization: Bearer $KEY" "http://127.0.0.1:${PORT}/v1/models")"
say "GET /v1/models: $code"
code_bad="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:${PORT}/v1/models")"
say "GET /v1/models senza chiave: $code_bad (atteso 401)"

out="$(./scripts/selftest.sh gpt-4o-mini gpt-4o 2>&1)"
printf '%s\n' "$out" | tee -a "$LOG" >/dev/null
if printf '%s' "$out" | grep -q '0 FAIL' && printf '%s' "$out" | grep -q 'PASS'; then
    say "ESITO: OK"
    exit 0
fi
say "ESITO: FAIL (selftest)"
exit 1
