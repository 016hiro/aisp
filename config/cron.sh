#!/usr/bin/env bash
# A-ISP 定时任务执行脚本
# 用法: cron.sh <morning|close|fetch-us|fetch-commodities|fetch-cn>

set -euo pipefail

PROJECT_DIR="/Users/lijunjie/github/aisp"
UV="/Users/lijunjie/.local/bin/uv"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "${LOG_DIR}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COMMAND="${1:-}"

if [[ -z "$COMMAND" ]]; then
    echo "Usage: $0 <morning|close|fetch-us|fetch-commodities|fetch-cn>"
    exit 1
fi

LOG_FILE="${LOG_DIR}/${COMMAND}_${TIMESTAMP}.log"

cd "${PROJECT_DIR}"

echo "[$(date)] Starting aisp ${COMMAND}..." | tee -a "${LOG_FILE}"

case "$COMMAND" in
    morning)
        ${UV} run aisp --log-file "${LOG_FILE}" run-morning 2>&1 | tee -a "${LOG_FILE}"
        ;;
    close)
        ${UV} run aisp --log-file "${LOG_FILE}" run-close 2>&1 | tee -a "${LOG_FILE}"
        ;;
    fetch-us)
        ${UV} run aisp --log-file "${LOG_FILE}" fetch-us 2>&1 | tee -a "${LOG_FILE}"
        ;;
    fetch-commodities)
        ${UV} run aisp --log-file "${LOG_FILE}" fetch-commodities 2>&1 | tee -a "${LOG_FILE}"
        ;;
    fetch-cn)
        ${UV} run aisp --log-file "${LOG_FILE}" fetch-cn 2>&1 | tee -a "${LOG_FILE}"
        ;;
    *)
        echo "Unknown command: ${COMMAND}" | tee -a "${LOG_FILE}"
        echo "Usage: $0 <morning|close|fetch-us|fetch-commodities|fetch-cn>" | tee -a "${LOG_FILE}"
        exit 1
        ;;
esac

EXIT_CODE=$?
echo "[$(date)] Finished aisp ${COMMAND} (exit code: ${EXIT_CODE})" | tee -a "${LOG_FILE}"

# 清理 30 天前的日志
find "${LOG_DIR}" -name "*.log" -mtime +30 -delete 2>/dev/null || true

exit ${EXIT_CODE}
