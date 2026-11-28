#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 媒体库入库扫描（快速模式）+ 缺元数据自动补刮 + 扫描任务卡死自愈。

独立于 strm 同步 / webhook，由 entrypoint 里的独立 cron 每 3 小时调用。

【为什么要这么写 —— 踩过的坑】
坑 1：POST /emby/Items/{id}/Refresh（不带 Recursive）
      只刷新该条目自身元数据，不遍历目录，发现不了新文件。
坑 2：POST /emby/Library/Refresh?Path=...&Recursive=true
      会启动「全库扫描」，且对每个已有 strm 重新做完整元数据刷新（每集向 TMDB
      请求 5 种语言），实测 1.2 万个 strm 要跑十几小时。期间任务状态一直 Running，
      后续所有扫描请求都被当成「已有实例在跑」直接吞掉。
坑 3：扫描线程遇到损坏的 .nfo（System.Xml.XmlException @ NfoMetadata.BaseNfoSaver）
      会挂掉，但 State 仍停在 Running —— 从此永久卡死，新文件再也进不了库。
坑 4：只做 ValidationOnly 扫描会「有壳无皮」—— 新剧出现在库里，但没有海报、
      没有简介、没有 TMDB ID（因为 ValidationOnly 完全不请求外部元数据源）。

【最终方案：两阶段】
阶段 A 快速入库
  POST /emby/Items/{folderId}/Refresh
       ?Recursive=true&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly
  ValidationOnly 只做「文件是否存在的校验 + 发现新增文件」，不请求任何外部元数据源。
  实测 1.2 万文件的库 15 秒内完成。

阶段 B 补刮（本版新增，解决坑 4）
  扫完立刻回查库内「缺 Primary 海报」的 Series / Movie，逐个触发：
  POST /emby/Items/{itemId}/Refresh
       ?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh
       &ReplaceAllMetadata=false&ReplaceAllImages=false
  只对缺图的条目动手，补上后自然不再命中，是收敛的，不会每轮重刮全库。
  连续失败 3 次的条目进入冷却（7 天后再试），避免每轮空打 TMDB。

配置（环境变量）：
  EMBY_SCAN_BASE           Emby 内网地址，如 http://192.168.0.4:2345
  EMBY_SCAN_API_KEY        Emby API key
  EMBY_SCAN_LIBS           要扫描的库名，逗号分隔，如 "电视剧/每日更新,电影/每日更新"
  EMBY_SCAN_PATHS          可选兜底根路径，逗号分隔（库名解析不到时用）
  EMBY_SCAN_STATE_FILE     状态快照文件，默认 /out/.emby_scan_state.json
  EMBY_SCAN_MODE           validation（默认，推荐）| refresh（旧的全库刷新，慢）
  EMBY_BACKFILL_ENABLED    是否启用阶段 B 补刮，默认 1
  EMBY_BACKFILL_LIMIT      每轮最多补刮多少个条目，默认 30
  EMBY_BACKFILL_MAX_TRIES  单个条目最多尝试次数，默认 3
  EMBY_BACKFILL_COOLDOWN   超过次数后的冷却秒数，默认 7 天
  EMBY_BACKFILL_DELAY      扫描后等待秒数再补刮，默认 10
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

BACKFILL_ENABLED = os.environ.get("EMBY_BACKFILL_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
BACKFILL_LIMIT = int(os.environ.get("EMBY_BACKFILL_LIMIT", "30") or 30)
BACKFILL_MAX_TRIES = int(os.environ.get("EMBY_BACKFILL_MAX_TRIES", "3") or 3)
BACKFILL_COOLDOWN = int(os.environ.get("EMBY_BACKFILL_COOLDOWN", str(7 * 86400)) or 7 * 86400)
BACKFILL_DELAY = int(os.environ.get("EMBY_BACKFILL_DELAY", "10") or 10)

HEADERS = {"X-Emby-Token": KEY} if KEY else {}
SCAN_TASK_KEY = "RefreshLibrary"
ITEM_TYPES = ("Series", "Movie")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(patch):
    """合并写入，避免各阶段互相覆盖。"""
    state = load_state()
    state.update(patch)
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
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


def list_lib_items(session, lib_id, item_type, page=500, hard_cap=6000):
    """分页拉某库某类型下的全部条目（字段精简，够判定是否缺图即可）。"""
    items, start = [], 0
    while start < hard_cap:
        try:
            r = session.get(
                BASE + "/emby/Items",
                headers=HEADERS,
                params={
                    "ParentId": lib_id,
                    "Recursive": "true",
                    "IncludeItemTypes": item_type,
                    "Fields": "Path,ImageTags,ProviderIds,DateCreated",
                    "SortBy": "DateCreated",
                    "SortOrder": "Descending",
                    "Limit": page,
                    "StartIndex": start,
                },
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[emby-scan] 拉取库 {lib_id}/{item_type} 条目失败: {e}", flush=True)
            break
        batch = data.get("Items") or []
        items.extend(batch)
        start += len(batch)
        total = data.get("TotalRecordCount") or 0
        if not batch or start >= total:
            break
    return items


def needs_backfill(item):
    """缺主海报就算需要补刮（有 TMDB ID 但图没下下来的也属于这种）。"""
    return not (item.get("ImageTags") or {}).get("Primary")


def refresh_item(session, item_id):
    url = (
        f"{BASE}/emby/Items/{item_id}/Refresh"
        f"?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh"
        f"&ReplaceAllMetadata=false&ReplaceAllImages=false"
    )
    r = session.post(url, headers=HEADERS, timeout=180)
    return r.status_code


def backfill_missing(session, targets):
    """阶段 B：给缺海报的条目补刮元数据（海报 / 简介 / 外部 ID）。"""
    if not BACKFILL_ENABLED:
        print("[emby-scan] 补刮已关闭（EMBY_BACKFILL_ENABLED=0）", flush=True)
        return

    lib_ids = []
    for name, path, fid in targets:
        if fid:
            lib_ids.append((name, fid))
    if not lib_ids:
        print("[emby-scan] 补刮跳过：没有可用的库 ID", flush=True)
        return

    now = int(time.time())
    state = load_state()
    tried = state.get("backfill") or {}

    candidates = []
    for name, fid in lib_ids:
        for typ in ITEM_TYPES:
            for it in list_lib_items(session, fid, typ):
                if not needs_backfill(it):
                    continue
                iid = str(it.get("Id"))
                rec = tried.get(iid) or {}
                if int(rec.get("n", 0)) >= BACKFILL_MAX_TRIES and now - int(rec.get("ts", 0)) < BACKFILL_COOLDOWN:
                    continue
                candidates.append((iid, it.get("Name"), it.get("Path"), name))

    print(f"[emby-scan] 待补刮条目 {len(candidates)} 个（本轮上限 {BACKFILL_LIMIT}）", flush=True)
    if not candidates:
        return
    if BACKFILL_DELAY > 0:
        time.sleep(BACKFILL_DELAY)

    done = 0
    for iid, nm, path, lib in candidates[:BACKFILL_LIMIT]:
        try:
            code = refresh_item(session, iid)
        except Exception as e:
            code = f"ERR {e}"
        rec = tried.get(iid) or {}
        tried[iid] = {"n": int(rec.get("n", 0)) + 1, "ts": now, "name": nm}
        if code in (200, 204):
            done += 1
            print(f"[emby-scan] 补刮 {nm} ({iid}) -> {code}", flush=True)
        else:
            print(f"[emby-scan] 补刮失败 {nm} ({iid}) -> {code}", flush=True)
        time.sleep(1.5)

    # 只保留最近 2000 条记录，避免状态文件无限膨胀
    if len(tried) > 2000:
        ordered = sorted(tried.items(), key=lambda kv: int((kv[1] or {}).get("ts", 0)), reverse=True)[:2000]
        tried = dict(ordered)

    save_state({"backfill": tried, "backfill_ts": now})
    print(f"[emby-scan] 补刮触发完成：{done}/{min(len(candidates), BACKFILL_LIMIT)}", flush=True)


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

    # 3) 阶段 A：逐个触发快速入库
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

    print(f"[emby-scan] 入库扫描完成：{ok}/{len(targets)} 个库已触发（mode={MODE}，耗时 {time.time()-t0:.1f}s）", flush=True)

    # 4) 阶段 B：缺海报的条目补刮元数据
    try:
        backfill_missing(session, targets)
    except Exception as e:
        print(f"[emby-scan] 补刮阶段异常: {e}", flush=True)

    # 5) 记录一次任务快照，供下次卡死判定
    task = get_scan_task(session)
    if task:
        save_state({"state": task.get("State"), "progress": task.get("CurrentProgressPercentage"),
                    "ts": int(time.time())})


if __name__ == "__main__":
    main()
