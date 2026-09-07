#!/usr/bin/env bash
# Tiny load generator for lab-app. Drives the HPA without installing anything.
#
#   kubectl -n lab-dev port-forward svc/lab-app 8080:80 &
#   ./scripts/load.sh http://localhost:8080 180 4
#
set -euo pipefail

URL="${1:-http://localhost:8080}"
SECONDS_TOTAL="${2:-120}"
CONCURRENCY="${3:-4}"
BURN="${4:-20}"

echo "Driving $URL for ${SECONDS_TOTAL}s with ${CONCURRENCY} workers"
echo "Watch it with: kubectl get hpa,pods -w"

end=$(( $(date +%s) + SECONDS_TOTAL ))
pids=()

for _ in $(seq 1 "$CONCURRENCY"); do
  (
    while [ "$(date +%s)" -lt "$end" ]; do
      curl -fsS -o /dev/null "${URL}/burn?seconds=${BURN}" || true
      curl -fsS -o /dev/null "${URL}/info" || true
      sleep 1
    done
  ) &
  pids+=("$!")
done

wait "${pids[@]}"
echo "Done. Scale-down starts after the HPA stabilization window."
