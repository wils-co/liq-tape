#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${DIR}/board.pid"
LOG_FILE="${DIR}/data/board.log"
BOARD_HOST="127.0.0.1"
BOARD_PORT="8791"

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

port_answer() {
    (exec 3<>"/dev/tcp/${BOARD_HOST}/${BOARD_PORT}") 2>/dev/null
}

start_it() {
    # Refuse if the port is already answering: a stale PID file is cheap to
    # miss, but starting a second server is not — the first one wins the bind
    # and the second one dies on its first listen, leaving two half-states.
    if port_answer; then
        echo "Port ${BOARD_PORT} is already answering. A board (or something
else) holds it — stop the existing process first."
        exit 1
    fi
    rm -f "${PID_FILE}"
    echo "Starting liq-tape board..."
    nohup python3 "${DIR}/server.py" >> "${LOG_FILE}" 2>&1 &
    echo $! > "${PID_FILE}"
    echo "Started (PID: $!). Logging to ${LOG_FILE}"
}

case "${action}" in
    start)
        if is_running; then
            echo "Board already running (PID: $(cat "${PID_FILE}"))."
            exit 0
        fi
        start_it
        ;;
    restart)
        if is_running; then
            pid="$(cat "${PID_FILE}")"
            echo "Stopping board (PID: ${pid})..."
            kill "${pid}" 2>/dev/null || true
            for _ in {1..20}; do
                if ! kill -0 "${pid}" 2>/dev/null; then
                    break
                fi
                sleep 0.2
            done
            rm -f "${PID_FILE}"
            echo "Stopped."
        elif port_answer; then
            echo "Port ${BOARD_PORT} is answering but no board is tracked here.
Refusing to restart over it — resolve the foreign process first."
            exit 1
        fi
        start_it
        ;;
    stop)
        if ! is_running; then
            echo "Board is not running."
            rm -f "${PID_FILE}"
            exit 0
        fi
        pid="$(cat "${PID_FILE}")"
        echo "Stopping board (PID: ${pid})..."
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
            echo "Board is RUNNING (PID: $(cat "${PID_FILE}"))."
        else
            echo "Board is NOT running."
            rm -f "${PID_FILE}" 2>/dev/null || true
        fi
        ;;
    log)
        touch "${LOG_FILE}"
        tail -f "${LOG_FILE}"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart|log}"
        exit 1
        ;;
esac
