#!/bin/sh
# 容器入口（三种模式）：
#   1) 默认：生成一次后退出（手动 docker compose run / 外部 cron）
#   2) 设置 CRON：常驻，crond 定时执行
#   3) 设置 WEBHOOK_ENABLED=true：常驻一个 webhook HTTP 服务，收到 POST /refresh 立即同步
#      （可与 CRON 同时开启：webhook 实时 + cron 兜底）
set -e

LOG=/out/run.log
mkdir -p /out

RUN_CMD="cd /app && /usr/local/bin/python /app/run_sync.py >> $LOG 2>&1"

if [ -n "$CRON" ]; then
    echo "[$CRON] $RUN_CMD" > /etc/crontabs/root
    echo "[entrypoint] cron 模式：$CRON（日志 $LOG）"
fi

WEBHOOK_ENABLED="${WEBHOOK_ENABLED:-false}"

if [ "$WEBHOOK_ENABLED" = "true" ]; then
    echo "[entrypoint] webhook 模式：监听端口 ${WEBHOOK_PORT:-5679}"
    # 后台常驻 webhook 服务；有 CRON 时再启 crond；两者并存
    /usr/local/bin/python /app/webhook_server.py >> "$LOG" 2>&1 &
    if [ -n "$CRON" ]; then
        exec crond -f -l 2
    else
        # 无 cron：仅 webhook 常驻，等待子进程
        wait
    fi
elif [ -n "$CRON" ]; then
    exec crond -f -l 2
else
    echo "[entrypoint] 单次运行模式"
    exec /usr/local/bin/python /app/run_sync.py
fi
