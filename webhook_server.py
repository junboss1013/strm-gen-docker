#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm-gen webhook 服务：收到 HTTP 请求即触发一次 strm 同步（扫描+写 Emby+刷新）。

- 用 Python 标准库 http.server，无需额外依赖
- 监听 WEBHOOK_PORT（默认 5678，host 网络下与 Alist 5678 冲突时请改，如 5679）
- 需要 token 鉴权：请求头 X-Webhook-Token 或 ?token=xxx（与 WEBHOOK_TOKEN 一致）
- 触发路径：POST /refresh   （可选 ?path= 仅用于日志，不影响全量扫描）
- 健康检查：GET  /health
- 收到请求后异步触发 run_sync.py（子进程带 flock 锁，防与 cron 并发冲突），
  立即返回，不阻塞调用方。
"""
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WEBHOOK_PORT", "5679"))
TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
DELAY = int(os.environ.get("WEBHOOK_DELAY", "0"))  # 触发前延迟秒数，给离线下载/网盘索引留时间
VERSION = "1.0.6"  # 镜像版本标记，便于 /health 核验部署版本
RUN_CMD = [sys.executable, "/app/run_sync.py"]


def _trigger(tag: str = ""):
    def _run():
        if DELAY > 0:
            import time
            print(f"[webhook] 延迟 {DELAY}s 后触发同步（{tag}）", flush=True)
            time.sleep(DELAY)
        print(f"[webhook] 触发同步（{tag}）", flush=True)
        try:
            p = subprocess.run(RUN_CMD, capture_output=True, text=True, timeout=1800)
            out = (p.stdout or "")[-800:]
            err = (p.stderr or "")[-400:]
            print(f"[webhook] 同步退出码={p.returncode}\n{out}\n{err}", flush=True)
        except Exception as e:
            print(f"[webhook] 同步异常: {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        if not TOKEN:
            return True
        supplied = self.headers.get("X-Webhook-Token", "")
        if not supplied and "token=" in self.path:
            supplied = self.path.split("token=", 1)[1].split("&", 1)[0]
        return supplied == TOKEN

    def do_GET(self):
        if self.path.startswith("/health"):
            self._reply(200, '{"status":"ok","version":"%s"}' % VERSION)
        else:
            self._reply(404, '{"error":"not found"}')

    def do_POST(self):
        from urllib.parse import unquote
        tag = ""
        if "path=" in self.path:
            tag = unquote(self.path.split("path=", 1)[1].split("&", 1)[0])
        if self.path.startswith("/refresh"):
            if not self._auth_ok():
                self._reply(401, '{"error":"unauthorized"}')
                return
            _trigger(tag)
            self._reply(200, '{"status":"accepted","msg":"strm refresh triggered"}')
        else:
            self._reply(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        try:
            print("[webhook] " + (fmt % args), flush=True)
        except Exception:
            pass


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[webhook] 监听 0.0.0.0:{PORT}  token={'set' if TOKEN else 'EMPTY(不鉴权!)'}  delay={DELAY}s", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
