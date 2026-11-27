#!/bin/sh
# 容器入口（可同时叠加三种模式）：
#   1) CRON：常驻，按 CRON 定时同步 strm
#   2) WEBHOOK_ENABLED=true：常驻 webhook HTTP 服务，收到 POST /refresh 立即同步
#   3) EMBY_SCAN_ENABLED=true：常驻独立 cron，按 EMBY_SCAN_CRON 定时扫描指定 Emby 媒体库
# 三种可并存：webhook 实时 + strm cron 兜底 + Emby 定向库扫描。
set -e

LOG=/out/run.log
mkdir -p /out

CRONTAB=/etc/crontabs/root
: > "$CRONTAB"

RUN_CMD="cd /app && /usr/local/bin/python /app/run_sync.py >> $LOG 2>&1"
SCAN_CMD="cd /app && /usr/local/bin/python /app/emby_scan.py >> $LOG 2>&1"

# ---- strm 同步 cron ----
if [ -n "$CRON" ]; then
    echo "[$CRON] $RUN_CMD" >> "$CRONTAB"
    echo "[entrypoint] strm 同步 cron：$CRON"
fi

# ---- Emby 定向媒体库扫描 cron（独立于 strm 同步）----
EMBY_SCAN_ENABLED="${EMBY_SCAN_ENABLED:-false}"
EMBY_SCAN_CRON="${EMBY_SCAN_CRON:-0 */3 * * *}"
if [ "$EMBY_SCAN_ENABLED" = "true" ]; then
    echo "[$EMBY_SCAN_CRON] $SCAN_CMD" >> "$CRONTAB"
    echo "[entrypoint] Emby 定向扫描 cron：$EMBY_SCAN_CRON"
fi

HAS_CRON="no"
[ -s "$CRONTAB" ] && HAS_CRON="yes"

WEBHOOK_ENABLED="${WEBHOOK_ENABLED:-false}"

if [ "$WEBHOOK_ENABLED" = "true" ]; then
    echo "[entrypoint] webhook 模式：监听端口 ${WEBHOOK_PORT:-5679}"
    /usr/local/bin/python /app/webhook_server.py >> "$LOG" 2>&1 &
    if [ "$HAS_CRON" = "yes" ]; then
        exec crond -f -l 2
    else
        wait
    fi
elif [ "$HAS_CRON" = "yes" ]; then
    exec crond -f -l 2
else
    echo "[entrypoint] 单次运行模式"
    exec /usr/local/bin/python /app/run_sync.py
fi
