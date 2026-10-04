#!/usr/bin/env bash
# Offline unit tests + anvil-fork tests. Needs Foundry's `anvil` on PATH. Nothing is sent to mainnet:
# fork tests use a throwaway wallet funded on the fork, and the OpenSea API is mocked.
set -uo pipefail
cd "$(dirname "$0")"
fail=0
echo "== unit"; python3 test_aerent_unit.py | tail -1 | grep -q "FAILS 0" || fail=1
python3 test_validate.py | tail -1 | grep -q "FAILS 0" || fail=1
run() { ./fork.sh >/dev/null && timeout 120 python3 "$@" 2>&1 | grep -E "RESULT|SCENARIO" ; }
echo "== fork";      run test_aerent_fork.py      | tee /dev/stderr | grep -q "holds=1" || fail=1
echo "== late flip"; run test_aerent_lateflip.py team | tee /dev/stderr | grep -q "holds=1" || fail=1
run test_aerent_lateflip.py 409 | tee /dev/stderr | grep -q "holds=1" || fail=1
echo "== safety";    run test_aerent_scen.py public | tee /dev/stderr | grep -q "txs sent=0" || fail=1
run test_aerent_scen.py badsig | tee /dev/stderr | grep -q "txs sent=1" || fail=1
./fork.sh stop >/dev/null
[ $fail -eq 0 ] && echo "ALL PASS" || { echo "SOME TESTS FAILED"; exit 1; }
