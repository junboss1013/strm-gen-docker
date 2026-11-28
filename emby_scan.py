#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 媒体库入库扫描（快速模式）+ 扫描任务卡死自愈。

独立于 strm 同步 / webhook，由 entrypoint 里的独立 cron 每 3 小时调用。

【为什么要这么写 —— 踩过的三个坑】
坑 1：POST /emby/Items/{id}/Refresh（不带 Recursive）
      只刷新该条目自身元数据，不遍历目录，发现不了新文件。
坑 2：POST /emby/Library/Refresh?Path=...&Recursive=true
      会启动「全库扫描」，且对每个已有 strm 重新做完整元数据刷新（每集向 TMDB
      请求 5 种语言），实测 1.2 万个 strm 要跑十几小时。期间任务状态一直 Running，
      后续所有扫描请求都被当成「已有实例在跑」直接吞掉。
坑 3：扫描线程遇到损坏的 .nfo（System.Xml.XmlException @ NfoMetadata.BaseNfoSaver）
      会挂掉，但 State 仍停在 Running —— 从此永久卡死，新文件再也进不了库。

【最终方案】
对「库的根文件夹条目」执行：
  POST /emby/Items/{folderId}/Refresh
       ?Recursive=true&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly
ValidationOnly 只做「文件是否存在的校验 + 发现新增文件」，不请求任何外部元数据源。
实测 1.2 万文件的库 15 秒内完成，《再婚皇后》等新剧立刻出现在搜索结果里。
元数据（海报/简介）可后续用 Default 模式对单部剧单独刷新，或由 Emby 自身异步补全。

配置（环境变量）：
  EMBY_SCAN_BASE        Emby 内网地址，如 http://192.168.0.4:2345
  EMBY_SCAN_API_KEY     Emby API key
  EMBY_SCAN_LIBS        要扫描的库名，逗号分隔，如 "电视剧/每日更新,电影/每日更新"
  EMBY_SCAN_PATHS       可选兜底根路径，逗号分隔（库名解析不到时用）
  EMBY_SCAN_STATE_FILE  状态快照文件，默认 /out/.emby_scan_state.json
  EMBY_SCAN_MODE        validation（默认，推荐）| refresh（旧的全库刷新，慢）
"""
import json
import os
import time
from urllib.parse import quote

import requests

BASE = os.environ.get("EMBY_SCAN_BASE", "").rstrip("/")
KEY = os.environ.get("EMBY_SCAN_API_KEY", "")
LIBS = [x.strip() for x in os.environ.get("EMBY_SCAN_LIBS", "").split(",") if x.strip()]
FALLBACK_PATHS = [x.strip() for x in os.environ.get("EMBY_SCAN_PATHS", "").split(",") if x.strip()]
STATE_FILE = os.environ.get("EMBY_SCAN_STATE_FILE", "/out/.emby_scan_state.json")
MODE = os.environ.get("EMBY_SCAN_MODE", "validation").strip().lower()

HEADERS = {"X-Emby-Token": KEY} if KEY else {}
SCAN_TASK_KEY = "RefreshLibrary"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        print(f"[emby-scan] 状态快照写入失败: {e}", flush=True)


def get_scan_task(session):
    try:
        r = session.get(BASE + "/emby/ScheduledTasks", headers=HEADERS, timeout=30)
        r.raise_for_status()
        for t in r.json():
            if t.get("Key") == SCAN_TASK_KEY:
                return t
    except Exception as e:
        print(f"[emby-scan] 读取计划任务失败: {e}", flush=True)
    return None


def heal_stuck_scan(session):
    """全库扫描任务卡死自愈（validation 模式不会用到这个任务，但历史残留要清掉）。"""
    task = get_scan_task(session)
    if not task:
        return
    state = task.get("State")
    prog = task.get("CurrentProgressPercentage")
    prev = load_state()
    print(
        f"[emby-scan] 全库扫描任务 state={state} progress={prog} "
        f"(上次 {prev.get('state')}/{prev.get('progress')})",
        flush=True,
    )
    if state == "Running" and prev.get("state") == "Running":
        same = (
            prog is not None
            and prev.get("progress") is not None
            and abs(float(prog) - float(prev["progress"])) < 0.01
        )
        if same:
            tid = task.get("Id")
            print(f"[emby-scan] 检测到全库扫描卡死（进度两次相同 {prog}），停止任务 {tid} 复位", flush=True)
            try:
                session.delete(BASE + f"/emby/ScheduledTasks/Running/{tid}", headers=HEADERS, timeout=30)
                time.sleep(3)
            except Exception as e:
                print(f"[emby-scan] 停止任务失败: {e}", flush=True)
    save_state({"state": state, "progress": prog, "ts": int(time.time())})


def folder_id_of(session, path):
    """按磁盘路径反查该目录在 Emby 里的 Folder 条目 ID。"""
    try:
        r = session.get(
            BASE + "/emby/Items",
            headers=HEADERS,
            params={"Path": path, "Recursive": "false", "Fields": "Path"},
            timeout=60,
        )
        r.raise_for_status()
        items = r.json().get("Items") or []
        if items:
            return items[0].get("Id")
    except Exception as e:
        print(f"[emby-scan] 反查目录条目失败 '{path}': {e}", flush=True)
    return None


def resolve_targets(session):
    """按库名解析出待扫描的库根目录，返回 [(显示名, path, folder_id|None), ...]。"""
    folders = []
    try:
        r = session.get(BASE + "/emby/Library/VirtualFolders", headers=HEADERS, timeout=30)
        r.raise_for_status()
        folders = r.json()
    except Exception as e:
        print(f"[emby-scan] 读取媒体库列表失败: {e}", flush=True)

    targets, seen = [], set()
    for name in LIBS:
        hit = next((v for v in folders if v.get("Name") == name), None)
        if hit is None:
            hit = next((v for v in folders if name in v.get("Name", "")), None)
        for p in ((hit or {}).get("Locations") or []):
            if p in seen:
                continue
            seen.add(p)
            targets.append((name, p, folder_id_of(session, p)))
    for p in FALLBACK_PATHS:
        if p in seen:
            continue
        seen.add(p)
        targets.append((f"兜底路径#{p}", p, folder_id_of(session, p)))
    return targets


def scan_validation(session, folder_id):
    """快速入库：只校验文件、发现新增，不请求外部元数据源。"""
    url = (
        f"{BASE}/emby/Items/{folder_id}/Refresh"
        f"?Recursive=true&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly"
        f"&ReplaceAllMetadata=false&ReplaceAllImages=false"
    )
    return session.post(url, headers=HEADERS, timeout=300)


def scan_refresh(session, path):
    """回退方案：按路径触发全库刷新（慢，仅在拿不到 folder id 时使用）。"""
    return session.post(
        f"{BASE}/emby/Library/Refresh",
        headers=HEADERS,
        params={"Path": path, "Recursive": "true"},
        timeout=300,
    )


def main():
    if not BASE or not KEY:
        print("[emby-scan] 未配置 EMBY_SCAN_BASE / EMBY_SCAN_API_KEY，跳过", flush=True)
        return
    session = requests.Session()

    # 1) 清理历史残留的卡死全库扫描任务
    heal_stuck_scan(session)

    # 2) 解析库根目录及其 Folder 条目
    targets = resolve_targets(session)
    if not targets:
        print("[emby-scan] 无待扫描路径，跳过", flush=True)
        return
    for name, path, fid in targets:
        print(f"[emby-scan] 目标库 '{name}' path={path} folder_id={fid}", flush=True)

    # 3) 逐个触发
    ok = 0
    t0 = time.time()
    for name, path, fid in targets:
        try:
            if MODE == "refresh":
                r = scan_refresh(session, path)
            elif fid:
                r = scan_validation(session, fid)
            else:
                print(f"[emby-scan] '{name}' 未取到 folder id，回退全库刷新（较慢）", flush=True)
                r = scan_refresh(session, path)
            if r.status_code in (200, 204):
                ok += 1
                print(f"[emby-scan] 已触发 '{name}' ({path}) HTTP {r.status_code}", flush=True)
            else:
                print(f"[emby-scan] '{name}' 返回 HTTP {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"[emby-scan] '{name}' 失败: {e}", flush=True)

    print(f"[emby-scan] 完成：{ok}/{len(targets)} 个库已触发（mode={MODE}，耗时 {time.time()-t0:.1f}s）", flush=True)

    # 4) 记录一次任务快照，供下次卡死判定
    task = get_scan_task(session)
    if task:
        save_state({"state": task.get("State"), "progress": task.get("CurrentProgressPercentage"),
                    "ts": int(time.time())})


if __name__ == "__main__":
    main()
