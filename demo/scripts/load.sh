#!/usr/bin/env sh
#
# Load generator for demo-app. Drives the HPA, the volume, or both.
#
# POSIX sh and curl only, so it also runs unchanged inside a curlimages/curl
# pod - which is the more useful place to run it, because then the Service
# load-balances across every replica instead of a port-forward pinning you to
# one pod.
#
# Usage:
#   ./load.sh [url] [mode] [seconds] [concurrency] [burn_seconds] [write_mb]
#
# Examples:
#   # against a port-forward
#   ./load.sh http://localhost:8080 cpu 240 4
#
#   # hammer the volume instead
#   ./load.sh http://localhost:8080 storage 120 4 20 32
#
#   # from inside the cluster
#   kubectl -n demo-dev run load --rm -i --image=curlimages/curl --restart=Never \
#     -- sh -c "$(cat load.sh)" -- http://demo-app both 240 6

set -u

URL="${1:-http://localhost:8080}"
MODE="${2:-cpu}"          # cpu | storage | both
SECONDS_TOTAL="${3:-120}"
CONCURRENCY="${4:-4}"
BURN_SECONDS="${5:-20}"
WRITE_MB="${6:-16}"

echo "Driving $URL in '$MODE' mode for ${SECONDS_TOTAL}s with $CONCURRENCY workers..."
case "$MODE" in
  cpu)     echo "Watch it with: kubectl get hpa,pods -w" ;;
  storage) echo "Watch it with: kubectl exec <pod> -- df -h /data" ;;
  both)    echo "Watch it with: kubectl get hpa,pods -w" ;;
  *)       echo "Unknown mode '$MODE' (expected cpu, storage or both)" >&2; exit 2 ;;
esac

DEADLINE=$(( $(date +%s) + SECONDS_TOTAL ))

worker() {
  w="$1"
  n=0
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    n=$(( n + 1 ))
    case "$MODE" in
      cpu)
        curl -s -o /dev/null --max-time 15 "$URL/burn?seconds=$BURN_SECONDS"
        ;;
      storage)
        curl -s -o /dev/null --max-time 60 "$URL/write?mb=$WRITE_MB&name=load/w$w-$n.bin&fsync=1"
        ;;
      both)
        if [ $(( n % 2 )) -eq 1 ]; then
          curl -s -o /dev/null --max-time 15 "$URL/burn?seconds=$BURN_SECONDS"
        else
          curl -s -o /dev/null --max-time 60 "$URL/write?mb=$WRITE_MB&name=load/w$w-$n.bin&fsync=1"
        fi
        ;;
    esac
    # Failures are expected and often the point - a full volume returns 507, a
    # pod mid-rollout refuses the connection - so errors are not checked here.
    curl -s -o /dev/null --max-time 10 "$URL/info"
    sleep 1
  done
}

i=1
while [ "$i" -le "$CONCURRENCY" ]; do
  worker "$i" &
  i=$(( i + 1 ))
done
wait

echo "Done."
if [ "$MODE" != "cpu" ]; then
  echo "The volume now has a load/ directory on it. Check and clean up:"
  echo "  curl \"$URL/df\""
  echo "  curl \"$URL/rm?name=load\""
fi
if [ "$MODE" != "storage" ]; then
  echo "Scale-down starts after the HPA stabilization window (60s by default)."
fi
