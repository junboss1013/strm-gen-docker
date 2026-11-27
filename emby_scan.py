#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 定向媒体库定时扫描：按库名触发指定媒体库路径的「扫描媒体库文件」。

独立于 strm 同步 / webhook，由 entrypoint 里的独立 cron 每 3 小时调用。

【修复说明】旧版本用 POST /emby/Items/{id}/Refresh，该接口只刷新已存在条目的
元数据，不会扫描目录、不会发现新文件，因此新转存的 strm 永远进不了库。
实测验证：真正能发现新文件的是 POST /emby/Library/Refresh?Path=<根路径>&Recursive=true
（等价于 Emby Web 上「扫描媒体库文件」），且必须带 Recursive=true 递归子目录。

- 先按 EMBY_SCAN_LIBS 里的库名，从 /Library/VirtualFolders 动态解析每个库的
  路径 Locations（库重建后 ItemId 会变，按库名+路径解析比写死 ID 稳）。
- 再对每个路径 POST /emby/Library/Refresh?Path=...&Recursive=true 触发真扫描。
- 路径取库配置里的真实根路径（如 /media/每日更新/电视剧），中文路径自动 URL 编码。
- 仅扫描指定库，不做全库刷新，避免触碰无关媒体库。

配置（环境变量）：
  EMBY_SCAN_BASE        Emby 内网地址，如 http://192.168.0.4:2345
  EMBY_SCAN_API_KEY     Emby API key
  EMBY_SCAN_LIBS        要扫描的库名，逗号分隔，如 "电视剧/每日更新,电影/每日更新"
  EMBY_SCAN_PATHS       可选兜底根路径，逗号分隔（库名解析不到时用），如
                        "/media/每日更新/电视剧,/media/每日更新/电影"
"""
import os
from urllib.parse import quote

import requests

BASE = os.environ.get("EMBY_SCAN_BASE", "").rstrip("/")
KEY = os.environ.get("EMBY_SCAN_API_KEY", "")
LIBS = [x.strip() for x in os.environ.get("EMBY_SCAN_LIBS", "").split(",") if x.strip()]
FALLBACK_PATHS = [x.strip() for x in os.environ.get("EMBY_SCAN_PATHS", "").split(",") if x.strip()]

HEADERS = {"X-Emby-Token": KEY} if KEY else {}


def resolve_paths(session):
    """按库名动态解析出待扫描的根路径，返回 [(显示名, path), ...]。"""
    folders = []
    try:
        r = session.get(BASE + "/emby/Library/VirtualFolders", headers=HEADERS, timeout=30)
        r.raise_for_status()
        folders = r.json()
    except Exception as e:
        print(f"[emby-scan] 读取媒体库列表失败: {e}", flush=True)

    targets = []
    for name in LIBS:
        hit = next((v for v in folders if v.get("Name") == name), None)
        if hit is None:
            hit = next((v for v in folders if name in v.get("Name", "")), None)
        locs = (hit or {}).get("Locations") or []
        if locs:
            # 一个库可能有多个 Locations，这里把每个根路径都纳入扫描
            for p in locs:
                targets.append((name, p))
                print(f"[emby-scan] 解析库 '{name}' -> 路径 {p}", flush=True)
        else:
            print(f"[emby-scan] 未找到库 '{name}' 的路径（将尝试兜底路径）", flush=True)

    for p in FALLBACK_PATHS:
        if not any(x[1] == p for x in targets):
            targets.append((f"兜底路径#{p}", p))
    return targets


def main():
    if not BASE or not KEY:
        print("[emby-scan] 未配置 EMBY_SCAN_BASE / EMBY_SCAN_API_KEY，跳过", flush=True)
        return
    session = requests.Session()
    targets = resolve_paths(session)
    if not targets:
        print("[emby-scan] 无待扫描路径，跳过", flush=True)
        return

    ok = 0
    for name, path in targets:
        # 关键修复：用 Library/Refresh 按路径扫描（能发现新文件），中文路径做 URL 编码
        url = f"{BASE}/emby/Library/Refresh"
        params = {"Path": path, "Recursive": "true"}
        try:
            r = session.post(url, headers=HEADERS, params=params, timeout=180)
            if r.status_code in (200, 204):
                ok += 1
                print(f"[emby-scan] 已触发扫描 '{name}' (path={path}) HTTP {r.status_code}", flush=True)
            else:
                print(f"[emby-scan] 扫描 '{name}' 返回 HTTP {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"[emby-scan] 扫描 '{name}' 失败: {e}", flush=True)
    print(f"[emby-scan] 完成：{ok}/{len(targets)} 个路径已触发扫描", flush=True)


if __name__ == "__main__":
    main()
