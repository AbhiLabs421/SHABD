#!/usr/bin/env bash
# Full end-to-end smoke test. Useful as a deployment gate.
#
#     bash scripts/smoke.sh                       # uses :8765 on localhost
#     SHABD_URL=https://my.shabd bash scripts/smoke.sh
#
# Exit code is non-zero on any failure.

set -euo pipefail

BASE="${SHABD_URL:-http://127.0.0.1:8765}"

step() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

step "Probing $BASE"

step "1. /healthz"
curl -fsS "$BASE/healthz" >/dev/null && ok "live" || fail "/healthz failed"

step "2. /readyz"
curl -fsS "$BASE/readyz" >/dev/null && ok "ready" || fail "/readyz failed"

step "3. /manifest"
N=$(curl -fsS "$BASE/manifest" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["spells"]))')
ok "$N spells advertised"

step "4. /metrics (JSON)"
curl -fsS "$BASE/metrics" | python3 -c 'import json,sys;json.load(sys.stdin)' && ok "JSON parses"

step "5. /metrics?format=prom"
curl -fsS "$BASE/metrics?format=prom" | grep -q '^shabd_info' && ok "Prometheus format ok"

step "6. /grimoire/verify"
RES=$(curl -fsS "$BASE/grimoire/verify")
echo "$RES" | python3 -c 'import json,sys;d=json.load(sys.stdin);assert d["ok"], d' && ok "chain intact"

step "7. /openapi.json"
curl -fsS "$BASE/openapi.json" | python3 -c 'import json,sys;json.load(sys.stdin)' && ok "OpenAPI parses"

step "8. /dashboard"
curl -fsS "$BASE/dashboard" -o /dev/null && ok "served"

printf "\n\033[1;32mAll smoke checks passed.\033[0m\n"
