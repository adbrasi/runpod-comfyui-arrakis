#!/usr/bin/env bash
set -euo pipefail

TCMALLOC="$(ldconfig -p | grep -Po 'libtcmalloc.so.\d' | head -n 1 || true)"
if [ -n "${TCMALLOC}" ]; then
  export LD_PRELOAD="${TCMALLOC}"
fi

comfy-manager-set-mode offline || echo "arrakis-lb - Could not set ComfyUI-Manager network_mode" >&2

: "${COMFY_LOG_LEVEL:=INFO}"
: "${LB_SERVER_HOST:=0.0.0.0}"
: "${PORT:=8000}"
: "${PORT_HEALTH:=${PORT}}"

echo "arrakis-lb: Starting ComfyUI"
python -u /comfyui/main.py --disable-auto-launch --disable-metadata --verbose "${COMFY_LOG_LEVEL}" --log-stdout &
COMFY_PID=$!

echo "arrakis-lb: Starting load-balancer API on port ${PORT}"
python -u /lb_server.py --host "${LB_SERVER_HOST}" --port "${PORT}" &
API_PID=$!

HEALTH_PID=""
if [ "${PORT_HEALTH}" != "${PORT}" ]; then
  echo "arrakis-lb: Starting health API on port ${PORT_HEALTH}"
  python -u /lb_server.py --host "${LB_SERVER_HOST}" --port "${PORT_HEALTH}" &
  HEALTH_PID=$!
fi

cleanup() {
  kill "${API_PID}" "${COMFY_PID}" ${HEALTH_PID:+${HEALTH_PID}} >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

wait -n "${COMFY_PID}" "${API_PID}" ${HEALTH_PID:+${HEALTH_PID}}
