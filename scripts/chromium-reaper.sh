#!/usr/bin/env bash
# Reaper del browser di gpt4free.
#
# Perche' esiste
# --------------
# Chromium qui e' un servizio di supporto: se si satura, g4f resta bloccato
# dentro una richiesta CDP e non chiude piu' le tab. A quel punto il memcg
# comincia a uccidere i renderer uno per uno e Chrome li ricrea: e' un
# livelock, non un guasto singolo (misurato il 2026-11-02: 121 renderer,
# 110 OOM-kill in 28 minuti, API a timeout, load 18). Serve un attuatore
# esterno, perche' nessuno dei due container puo' accorgersene da solo: la
# healthcheck del browser va in timeout e Docker non riavvia un unhealthy.
#
# Cosa guarda
# -----------
# Solo il cgroup del container (memory.current contro memory.max, e il
# contatore oom_kill di memory.events). Mai `ps aux | grep chromium`: su questa
# macchina contava i chromium degli altri stack.
#
# Cosa fa
# -------
# Riavvia la *unit* systemd, non il singolo container: il compose gira con
# `--abort-on-container-exit`, quindi fermare chromium farebbe comunque
# terminare tutto lo stack, solo senza il controllo di systemd. Meglio farlo
# in modo esplicito, rate-limited e registrato nel journal.
#
# Uso: come unit `gpt4free-chromium-reaper.timer` (ogni 5 minuti).
#   scripts/chromium-reaper.sh --dry-run   # misura e basta, non riavvia
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${REAPER_CONTAINER:-gpt4free-chromium}"
UNIT="${REAPER_UNIT:-docker-compose@gpt4free}"
STATE="${REAPER_STATE:-/run/gpt4free-chromium-reaper.state}"
# Soglie: 90% del tetto, oppure 3 renderer uccisi fra due giri (5 minuti).
MEM_PCT="${REAPER_MEM_PCT:-90}"
OOM_DELTA="${REAPER_OOM_DELTA:-3}"
# Un riavvio ogni 20 minuti al massimo: un loop di riavvii sarebbe peggio
# dell'OOM, e dopo il riavvio il probe deve poter rimisurare i provider.
MIN_INTERVAL="${REAPER_MIN_INTERVAL:-1200}"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s %s\n' "$(date -Is)" "$*"; logger -t gpt4free-chromium-reaper "$*" 2>/dev/null || true; }

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
    say "$CONTAINER assente: niente da fare"
    exit 0
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != true ]; then
    say "$CONTAINER non in esecuzione: niente da fare"
    exit 0
fi

ID="$(docker inspect -f '{{.Id}}' "$CONTAINER")"
CG="$(find /sys/fs/cgroup -maxdepth 4 -name "docker-${ID}.scope" -print -quit 2>/dev/null)"
if [ -z "$CG" ] || [ ! -r "$CG/memory.current" ]; then
    say "cgroup di $CONTAINER non leggibile: esco"
    exit 1
fi

CUR="$(cat "$CG/memory.current")"
MAX="$(cat "$CG/memory.max")"
if [ "$MAX" = "max" ]; then
    say "memory.max illimitato: nessuna soglia da valutare"
    exit 0
fi
KILL="$(awk '/^oom_kill /{print $2}' "$CG/memory.events")"
PCT=$(( CUR * 100 / MAX ))

# Stato precedente: contatore kill e ultimo riavvio. Se il file manca o il
# container e' stato ricreato (kill < salvato) si riparte da zero.
PREV_KILL=0
LAST_RESTART=0
if [ -r "$STATE" ]; then
    PREV_KILL="$(awk '/^kill /{print $2}' "$STATE" 2>/dev/null || echo 0)"
    LAST_RESTART="$(awk '/^restart /{print $2}' "$STATE" 2>/dev/null || echo 0)"
fi
case "$PREV_KILL" in ''|*[!0-9]*) PREV_KILL=0 ;; esac
case "$LAST_RESTART" in ''|*[!0-9]*) LAST_RESTART=0 ;; esac
[ "$KILL" -lt "$PREV_KILL" ] && PREV_KILL=0
DELTA=$(( KILL - PREV_KILL ))
NOW="$(date +%s)"

say "$CONTAINER: ${PCT}% (${CUR}/${MAX}) oom_kill=${KILL} (+${DELTA} dall'ultimo giro)"

write_state() {
    [ "$DRY" = 1 ] && return 0
    printf 'kill %s\nrestart %s\n' "$1" "$2" > "$STATE"
}

TRIGGER=""
[ "$PCT" -ge "$MEM_PCT" ] && TRIGGER="memoria al ${PCT}% (soglia ${MEM_PCT}%)"
if [ -z "$TRIGGER" ] && [ "$DELTA" -ge "$OOM_DELTA" ]; then
    TRIGGER="+${DELTA} oom_kill in questo intervallo (soglia ${OOM_DELTA})"
fi

if [ -z "$TRIGGER" ]; then
    write_state "$KILL" "$LAST_RESTART"
    exit 0
fi

SINCE=$(( NOW - LAST_RESTART ))
if [ "$LAST_RESTART" -gt 0 ] && [ "$SINCE" -lt "$MIN_INTERVAL" ]; then
    say "trigger ($TRIGGER) ma ultimo riavvio ${SINCE}s fa (< ${MIN_INTERVAL}s): aspetto"
    write_state "$KILL" "$LAST_RESTART"
    exit 0
fi

if [ "$DRY" = 1 ]; then
    say "DRY-RUN: avrei riavviato $UNIT perche': $TRIGGER"
    exit 0
fi

say "riavvio $UNIT perche': $TRIGGER"
if systemctl restart "$UNIT"; then
    write_state "$KILL" "$NOW"
    say "riavvio completato"
else
    # Non aggiorno il contatore kill cosi' il prossimo giro ritenta (il
    # rate-limit resta valido perche' last_restart non cambia).
    say "ERRORE: riavvio di $UNIT fallito"
    exit 1
fi
