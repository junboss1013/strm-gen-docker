# 小雅 Alist → Emby strm 生成器（配置驱动 / Docker 版）
FROM python:3.11-alpine

# 换国内 apk 源加速
RUN sed -i 's#https://dl-cdn.alpinelinux.org/alpine#https://mirrors.tuna.tsinghua.edu.cn/alpine#g' /etc/apk/repositories \
    && apk add --no-cache docker-cli tar tzdata \
    && mkdir -p /etc/crontabs

RUN pip3 install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple requests

WORKDIR /app
COPY gen_strm.py /app/gen_strm.py
COPY run_sync.py /app/run_sync.py
COPY webhook_server.py /app/webhook_server.py
COPY emby_scan.py /app/emby_scan.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

VOLUME /out
ENV TZ=Asia/Shanghai \
    OUT_ROOT=/out \
    ALIST_BASE=http://127.0.0.1:5678 \
    EMBY_CONTAINER=emby \
    AUTO_SCAN=true

ENTRYPOINT ["/app/entrypoint.sh"]
