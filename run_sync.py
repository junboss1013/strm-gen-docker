#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带文件锁的统一同步入口：cron 与 webhook 都走这里，避免并发互相踩踏。"""
import fcntl
import os
import sys

LOCK_PATH = "/tmp/strm-gen.lock"


def main():
    fp = open(LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[run_sync] 已有同步在执行，本次跳过", flush=True)
            return
        try:
            import gen_strm
            gen_strm.main()
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)
    finally:
        fp.close()


if __name__ == "__main__":
    main()
