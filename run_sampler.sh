#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${DIR}/sampler.pid"
LOG_FILE="${DIR}/data/sampler.log"

mkdir -p "${DIR}/data"

action="${1:-start}"

is_running() {
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid="$(cat "${PID_FILE}")"
        if kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

case "${action}" in
    start)
        if is_running; then
            echo "Sampler already running (PID: $(cat "${PID_FILE}"))."
            exit 0
        fi
        echo "Starting liq-tape sampler..."
        nohup python3 "${DIR}/sampler.py" >> "${LOG_FILE}" 2>&1 &
        echo $! > "${PID_FILE}"
        echo "Started (PID: $!). Logging to ${LOG_FILE}"
        ;;
    stop)
        if ! is_running; then
            echo "Sampler is not running."
            rm -f "${PID_FILE}"
            exit 0
        fi
        pid="$(cat "${PID_FILE}")"
        echo "Stopping sampler (PID: ${pid})..."
        kill "${pid}" 2>/dev/null || true
        for _ in {1..20}; do
            if ! kill -0 "${pid}" 2>/dev/null; then
                break
            fi
            sleep 0.2
        done
        rm -f "${PID_FILE}"
        echo "Stopped."
        ;;
    status)
        if is_running; then
            echo "Sampler is RUNNING (PID: $(cat "${PID_FILE}"))."
        else
            echo "Sampler is NOT running."
            rm -f "${PID_FILE}" 2>/dev/null || true
        fi
        ;;
    log)
        touch "${LOG_FILE}"
        tail -f "${LOG_FILE}"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|log}"
        exit 1
        ;;
esac
