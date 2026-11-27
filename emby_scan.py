#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 定向媒体库定时扫描：按库名触发指定媒体库的「扫描媒体库文件」。

独立于 strm 同步 / webhook，由 entrypoint 里的独立 cron 每 3 小时调用。

- 先按 EMBY_SCAN_LIBS 里的库名，从 /Library/VirtualFolders 动态解析 ItemId
  （库重建后 ID 会变，动态解析比写死 ID 稳）；解析不到可用 EMBY_SCAN_ITEM_IDS 兜底。
- 再对每个 ItemId POST /Items/{id}/Refresh?Recursive=true，轻量刷新（不强制重刮元数据）。
- 仅扫描指定库，不做全库刷新，避免触碰无关媒体库。

配置（环境变量）：
  EMBY_SCAN_BASE        Emby 内网地址，如 http://192.168.0.4:2345
  EMBY_SCAN_API_KEY     Emby API key
  EMBY_SCAN_LIBS        要扫描的库名，逗号分隔，如 "电视剧/每日更新,电影/每日更新"
  EMBY_SCAN_ITEM_IDS    可选兜底 ItemId，逗号分隔（库名解析不到时用）
"""
import os

import requests

BASE = os.environ.get("EMBY_SCAN_BASE", "").rstrip("/")
KEY = os.environ.get("EMBY_SCAN_API_KEY", "")
LIBS = [x.strip() for x in os.environ.get("EMBY_SCAN_LIBS", "").split(",") if x.strip()]
FALLBACK_IDS = [x.strip() for x in os.environ.get("EMBY_SCAN_ITEM_IDS", "").split(",") if x.strip()]

HEADERS = {"X-Emby-Token": KEY} if KEY else {}


def resolve_ids(session):
    """按库名动态解析 ItemId，返回 [(显示名, itemId), ...]。"""
    folders = []
    try:
        r = session.get(BASE + "/emby/Library/VirtualFolders", headers=HEADERS, timeout=20)
        r.raise_for_status()
        folders = r.json()
    except Exception as e:
        print(f"[emby-scan] 读取媒体库列表失败: {e}", flush=True)

    targets = []
    for name in LIBS:
        hit = next((v for v in folders if v.get("Name") == name), None)
        if hit is None:
            hit = next((v for v in folders if name in v.get("Name", "")), None)
        if hit and hit.get("ItemId"):
            iid = hit["ItemId"]
            targets.append((name, iid))
            print(f"[emby-scan] 解析库 '{name}' -> ItemId {iid}", flush=True)
        else:
            print(f"[emby-scan] 未找到库 '{name}'（将尝试兜底 ID）", flush=True)

    for iid in FALLBACK_IDS:
        if not any(str(x[1]) == iid for x in targets):
            targets.append((f"ItemId#{iid}", iid))
    return targets


def main():
    if not BASE or not KEY:
        print("[emby-scan] 未配置 EMBY_SCAN_BASE / EMBY_SCAN_API_KEY，跳过", flush=True)
        return
    session = requests.Session()
    targets = resolve_ids(session)
    if not targets:
        print("[emby-scan] 无待扫描媒体库，跳过", flush=True)
        return

    params = {
        "Recursive": "true",
        "ImageRefreshMode": "Default",
        "MetadataRefreshMode": "Default",
        "ReplaceAllMetadata": "false",
        "ReplaceAllImages": "false",
    }
    ok = 0
    for name, iid in targets:
        try:
            r = session.post(
                f"{BASE}/emby/Items/{iid}/Refresh",
                headers=HEADERS, params=params, timeout=60,
            )
            if r.status_code in (200, 204):
                ok += 1
                print(f"[emby-scan] 已触发扫描 '{name}' (id={iid}) HTTP {r.status_code}", flush=True)
            else:
                print(f"[emby-scan] 扫描 '{name}' 返回 HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"[emby-scan] 扫描 '{name}' 失败: {e}", flush=True)
    print(f"[emby-scan] 完成：{ok}/{len(targets)} 个媒体库已触发扫描", flush=True)


if __name__ == "__main__":
    main()
