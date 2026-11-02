#!/usr/bin/env bash
# Self-test end-to-end dell'API OpenAI-compatibile di gpt4free.
#
# Verifica che il servizio non sia solo "up" ma che completi davvero una chat
# tramite provider keyless. Non riavvia nulla: un guasto upstream di un provider
# non deve mai far restartare lo stack. Da usare a mano o da cron.
#
#   scripts/selftest.sh                # modelli candidati di default
#   scripts/selftest.sh gpt-4o-mini ... # modelli specifici
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

KEY="$(sed -n 's/^G4F_API_KEY=//p' .env 2>/dev/null | head -1)"
PORT="$(sed -n 's/^G4F_PORT=//p' .env 2>/dev/null | head -1)"
PORT="${PORT:-1337}"
BASE="http://127.0.0.1:${PORT}"
[ -n "$KEY" ] || { echo "FAIL: G4F_API_KEY assente in .env (esegui scripts/bootstrap.sh)"; exit 1; }

ok=0
fail=0

echo "== /v1/models =="
models_json="$(curl -fsS -m 15 -H "Authorization: Bearer $KEY" "$BASE/v1/models")" || {
    echo "FAIL: /v1/models non raggiungibile su $BASE"
    exit 1
}
model_count="$(printf '%s' "$models_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("data", [])))')"
echo "modelli esposti: $model_count"

models=("$@")
if [ ${#models[@]} -eq 0 ]; then
    # Candidati keyless storici di g4f; il primo che risponde viene stampato.
    models=(gpt-4o-mini gpt-4o "openai/gpt-oss-120b" llama-3.3-70b)
fi

for m in "${models[@]}"; do
    body="$(python3 - "$m" <<'PY'
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
    "max_tokens": 16,
    "stream": False,
}))
PY
)"
    start=$(date +%s)
    resp="$(curl -sS -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
        -d "$body" "$BASE/v1/chat/completions" 2>&1)"
    elapsed=$(( $(date +%s) - start ))
    if printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    c = d["choices"][0]["message"]["content"]
except Exception:
    sys.exit(1)
sys.exit(0 if c else 1)
' 2>/dev/null; then
        echo "PASS  $m  (${elapsed}s)"
        ok=$((ok + 1))
    else
        echo "FAIL  $m  (${elapsed}s) -> $(printf '%s' "$resp" | head -c 300)"
        fail=$((fail + 1))
    fi
done

echo "== risultato: $ok PASS / $fail FAIL =="
[ "$ok" -gt 0 ]
