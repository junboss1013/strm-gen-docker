# 小雅 Alist → Emby strm 生成器（配置驱动 / Docker 版）
# 构建： docker build -t strm-gen:local .
FROM python:3.11-slim

# 需要 docker CLI（通过 docker.sock 写入 Emby 容器）、tar、cron
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io tar cron \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir requests

WORKDIR /app
COPY gen_strm.py /app/gen_strm.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

VOLUME /out
ENV TZ=Asia/Shanghai \
    OUT_ROOT=/out \
    ALIST_BASE=http://127.0.0.1:5678 \
    ALIST_USERNAME=guest \
    ALIST_PASSWORD=guest_Api789 \
    EMBY_CONTAINER=emby \
    AUTO_SCAN=true

ENTRYPOINT ["/app/entrypoint.sh"]
