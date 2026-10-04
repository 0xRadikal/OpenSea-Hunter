#!/usr/bin/env bash
# Start (or stop) a local Foundry anvil fork of Robinhood Chain, PINNED to a block before the AERENT
# sale (block 79226000: totalSupply 50/1000) so fork tests are reproducible after sell-out.
# NOTE: the fork is tracked by PID file only. Never `pkill anvil` — other daemons (e.g. Dovecot's
# "anvil" process on mail servers) share that name.
#   ./fork.sh        start (restarts our own previous fork if running)
#   ./fork.sh stop   stop our fork
set -euo pipefail
PIDFILE="${TMPDIR:-/tmp}/opensea-hunter-anvil.pid"
PORT="${FORK_PORT:-8547}"

stop() {
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    # kill only if that PID really is a Foundry anvil fork on our port
    if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "--fork-url.*--port $PORT"; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    fi
    rm -f "$PIDFILE"
  fi
}

[ "${1:-}" = "stop" ] && { stop; echo "fork stopped"; exit 0; }

stop
BLOCK="${FORK_BLOCK:-79226000}"
RPC="${FORK_RPC:-https://robinhood.drpc.org}"
command -v anvil >/dev/null || { echo "Foundry 'anvil' not found on PATH" >&2; exit 1; }
setsid anvil --fork-url "$RPC" --fork-block-number "$BLOCK" --port "$PORT" --chain-id 4663 --silent \
  >/dev/null 2>&1 </dev/null &
echo $! > "$PIDFILE"
for _ in $(seq 1 60); do
  curl -s -m 2 -X POST -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "http://127.0.0.1:$PORT" | grep -q 0x1237 \
    && { echo "fork ready @ block $BLOCK (pid $(cat "$PIDFILE"))"; exit 0; }
  sleep 0.5
done
echo "fork failed to start" >&2; stop; exit 1
