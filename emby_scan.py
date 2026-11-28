#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 媒体库入库扫描（快速模式）+ 缺元数据自动补刮（剧/电影 + 单集）+ 扫描任务卡死自愈。

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
坑 5：把 Episode 抓取器清空能提速，但代价是每集标题退化成文件名（带网盘广告），
      简介/剧照全空。返回去对全库做 Recursive 刷新又会回到坑 2 的老路。

【最终方案：三阶段】
阶段 A 快速入库
  POST /emby/Items/{folderId}/Refresh
       ?Recursive=true&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly
  ValidationOnly 只做「文件是否存在的校验 + 发现新增文件」，不请求任何外部元数据源。
  实测 1.2 万文件的库 15 秒内完成。

阶段 B 补剧/电影元数据（解决坑 4）
  扫完回查库内「缺 Primary 海报」的 Series / Movie，逐个触发
  POST /emby/Items/{itemId}/Refresh
       ?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh
       &ReplaceAllMetadata=false&ReplaceAllImages=false
  只对缺图的条目动手，补上后自然不再命中，是收敛的，不会每轮重刮全库。

阶段 C 补单集元数据（解决坑 5）
  回查库内「缺 ProviderIds / 标题还是文件名 / 简介为空」的 Episode，按剧聚合，
  只对命中的那几部剧做 Recursive 刷新（一次刷完整部剧的集）：
  POST /emby/Items/{seriesId}/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh
  每轮限量（默认 10 部），按缺集数从少到多排队 —— 小剧先清完，大剧不会长期霸占配额；
  同一部剧连续 3 轮没进展就冷却 7 天，避免永远匹配不上 TMDB 的剧反复空转。

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
  EMBY_EPISODE_ENABLED     是否启用阶段 C 单集补刮，默认 1
  EMBY_EPISODE_LIMIT       每轮最多处理多少部剧，默认 10
  EMBY_EPISODE_MAX_TRIES   单部剧连续无进展几次后冷却，默认 3
  EMBY_EPISODE_COOLDOWN    冷却秒数，默认 7 天
  EMBY_EPISODE_DELAY       每部剧触发间隔秒数，默认 5
  EMBY_EPISODE_SETTLE      多久之后结算「上一轮是否有效」，默认 1800 秒
  EMBY_EPISODE_MAX_PER_SERIES  单剧缺集超过此数就跳过，默认 200
"""
import json
import os
import time

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

EPISODE_ENABLED = os.environ.get("EMBY_EPISODE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
EPISODE_LIMIT = int(os.environ.get("EMBY_EPISODE_LIMIT", "10") or 10)
EPISODE_MAX_TRIES = int(os.environ.get("EMBY_EPISODE_MAX_TRIES", "3") or 3)
EPISODE_COOLDOWN = int(os.environ.get("EMBY_EPISODE_COOLDOWN", str(7 * 86400)) or 7 * 86400)
EPISODE_DELAY = int(os.environ.get("EMBY_EPISODE_DELAY", "5") or 5)
EPISODE_SETTLE = int(os.environ.get("EMBY_EPISODE_SETTLE", "1800") or 1800)
# 单部剧缺集太多时跳过：一次 Recursive 刷新会长时间霸占 Emby 的刷新队列，
# 把后面的新文件入库一起卡住。正常剧集不会超过这个数。
EPISODE_MAX_PER_SERIES = int(os.environ.get("EMBY_EPISODE_MAX_PER_SERIES", "200") or 200)

HEADERS = {"X-Emby-Token": KEY} if KEY else {}
SCAN_TASK_KEY = "RefreshLibrary"
ITEM_TYPES = ("Series", "Movie")

# 集标题里出现这些字样，说明是拿文件名当标题（没刮到 TMDB）
AD_MARKERS = ("更多高清", "请访问", "www.", "http", "关注公众号", "扫描二维码")


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


def trim(mapping, keep=2000):
    """状态文件只保留最近 N 条，防止无限膨胀。"""
    if len(mapping) <= keep:
        return mapping
    ordered = sorted(mapping.items(), key=lambda kv: int((kv[1] or {}).get("ts", 0)), reverse=True)
    return dict(ordered[:keep])


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


def list_items(session, lib_id, item_type, fields, page=1000, hard_cap=300000, sort=False):
    """分页拉某库某类型下的全部条目。"""
    items, start = [], 0
    while start < hard_cap:
        params = {
            "ParentId": lib_id,
            "Recursive": "true",
            "IncludeItemTypes": item_type,
            "Fields": fields,
            "Limit": page,
            "StartIndex": start,
        }
        if sort:
            params.update({"SortBy": "DateCreated", "SortOrder": "Descending"})
        try:
            r = session.get(BASE + "/emby/Items", headers=HEADERS, params=params, timeout=180)
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


def refresh_item(session, item_id, recursive=False):
    url = (
        f"{BASE}/emby/Items/{item_id}/Refresh"
        f"?Recursive={'true' if recursive else 'false'}"
        f"&MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh"
        f"&ReplaceAllMetadata=false&ReplaceAllImages=false"
    )
    r = session.post(url, headers=HEADERS, timeout=300)
    return r.status_code


def backfill_missing(session, targets):
    """阶段 B：给缺海报的条目补刮元数据（海报 / 简介 / 外部 ID）。返回本轮已触发的 ID 集合。"""
    if not BACKFILL_ENABLED:
        print("[emby-scan] 补刮已关闭（EMBY_BACKFILL_ENABLED=0）", flush=True)
        return set()

    lib_ids = [(n, f) for n, p, f in targets if f]
    if not lib_ids:
        print("[emby-scan] 补刮跳过：没有可用的库 ID", flush=True)
        return set()

    now = int(time.time())
    state = load_state()
    tried = state.get("backfill") or {}

    candidates = []
    for name, fid in lib_ids:
        for typ in ITEM_TYPES:
            for it in list_items(session, fid, typ, "Path,ImageTags,ProviderIds,DateCreated", page=500, sort=True):
                if not needs_backfill(it):
                    continue
                iid = str(it.get("Id"))
                rec = tried.get(iid) or {}
                if int(rec.get("n", 0)) >= BACKFILL_MAX_TRIES and now - int(rec.get("ts", 0)) < BACKFILL_COOLDOWN:
                    continue
                candidates.append((iid, it.get("Name"), it.get("Path"), name))

    print(f"[emby-scan] 待补刮条目 {len(candidates)} 个（本轮上限 {BACKFILL_LIMIT}）", flush=True)
    if not candidates:
        return set()
    if BACKFILL_DELAY > 0:
        time.sleep(BACKFILL_DELAY)

    done, fired = 0, set()
    for iid, nm, path, lib in candidates[:BACKFILL_LIMIT]:
        try:
            code = refresh_item(session, iid)
        except Exception as e:
            code = f"ERR {e}"
        rec = tried.get(iid) or {}
        tried[iid] = {"n": int(rec.get("n", 0)) + 1, "ts": now, "name": nm}
        if code in (200, 204):
            done += 1
            fired.add(iid)
            print(f"[emby-scan] 补刮 {nm} ({iid}) -> {code}", flush=True)
        else:
            print(f"[emby-scan] 补刮失败 {nm} ({iid}) -> {code}", flush=True)
        time.sleep(1.5)

    save_state({"backfill": trim(tried), "backfill_ts": now})
    print(f"[emby-scan] 补刮触发完成：{done}/{min(len(candidates), BACKFILL_LIMIT)}", flush=True)
    return fired


def episode_missing_meta(ep):
    """判断单集是否缺元数据：没外部 ID / 标题还是文件名 / 简介为空。"""
    if not (ep.get("ProviderIds") or {}):
        return True
    nm = ep.get("Name") or ""
    if any(m in nm for m in AD_MARKERS):
        return True
    return not (ep.get("Overview") or "").strip()


def episode_backfill(session, targets, skip_series=None):
    """阶段 C：按剧补单集元数据（一次 Recursive 刷完整部剧的集）。"""
    if not EPISODE_ENABLED:
        print("[emby-scan] 单集补刮已关闭（EMBY_EPISODE_ENABLED=0）", flush=True)
        return

    lib_ids = [(n, f) for n, p, f in targets if f]
    if not lib_ids:
        print("[emby-scan] 单集补刮跳过：没有可用的库 ID", flush=True)
        return

    skip_series = skip_series or set()
    now = int(time.time())

    stat = {}   # seriesId -> {"name":..., "miss": 缺集数}
    total_ep = 0
    for name, fid in lib_ids:
        eps = list_items(session, fid, "Episode", "SeriesId,SeriesName,ProviderIds,Name,Overview")
        total_ep += len(eps)
        for ep in eps:
            if not episode_missing_meta(ep):
                continue
            sid = str(ep.get("SeriesId") or "")
            if not sid:
                continue
            rec = stat.setdefault(sid, {"name": ep.get("SeriesName") or sid, "miss": 0})
            rec["miss"] += 1

    miss_total = sum(v["miss"] for v in stat.values())
    print(
        f"[emby-scan] 单集检查：{total_ep} 集，{miss_total} 集缺元数据，涉及 {len(stat)} 部剧",
        flush=True,
    )
    if not stat:
        return

    state = load_state()
    tried = state.get("episode") or {}

    cands = []
    skipped_big = []
    for sid, rec in stat.items():
        if sid in skip_series:
            continue
        if rec["miss"] > EPISODE_MAX_PER_SERIES:
            skipped_big.append((rec["name"], rec["miss"]))
            continue
        prev = tried.get(sid) or {}
        n = int(prev.get("n", 0))
        last_ts = int(prev.get("ts", 0))
        last_miss = prev.get("miss")
        # 距上次触发足够久才结算：有进展就清零，没进展就累加失败次数
        if prev and last_miss is not None and now - last_ts > EPISODE_SETTLE:
            n = 0 if rec["miss"] < int(last_miss) else n + 1
        if n >= EPISODE_MAX_TRIES and now - last_ts < EPISODE_COOLDOWN:
            continue
        cands.append((rec["miss"], sid, rec["name"], n))

    cands.sort()
    if skipped_big:
        big = ", ".join(f"{n}({m}集)" for n, m in sorted(skipped_big, key=lambda x: -x[1])[:5])
        print(
            f"[emby-scan] 跳过 {len(skipped_big)} 部剧：单剧缺集超过 {EPISODE_MAX_PER_SERIES} 集，"
            f"避免长时间占用刷新队列 —— {big}",
            flush=True,
        )
    print(f"[emby-scan] 待补单集的剧 {len(cands)} 部（本轮上限 {EPISODE_LIMIT}）", flush=True)
    if not cands:
        return

    done = 0
    for miss, sid, nm, n in cands[:EPISODE_LIMIT]:
        try:
            code = refresh_item(session, sid, recursive=True)
        except Exception as e:
            code = f"ERR {e}"
        tried[sid] = {"n": n, "ts": now, "miss": miss, "name": nm}
        if code in (200, 204):
            done += 1
            print(f"[emby-scan] 补单集 {nm} ({sid}) 缺 {miss} 集 -> {code}", flush=True)
        else:
            print(f"[emby-scan] 补单集失败 {nm} ({sid}) -> {code}", flush=True)
        time.sleep(EPISODE_DELAY)

    save_state({"episode": trim(tried), "episode_ts": now})
    print(f"[emby-scan] 单集补刮触发完成：{done}/{min(len(cands), EPISODE_LIMIT)} 部剧", flush=True)


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
    fired = set()
    try:
        fired = backfill_missing(session, targets)
    except Exception as e:
        print(f"[emby-scan] 补刮阶段异常: {e}", flush=True)

    # 5) 阶段 C：按剧补单集元数据（跳过 B 刚触发过的剧，避免重复排队）
    try:
        episode_backfill(session, targets, skip_series=fired)
    except Exception as e:
        print(f"[emby-scan] 单集补刮阶段异常: {e}", flush=True)

    # 6) 记录一次任务快照，供下次卡死判定
    task = get_scan_task(session)
    if task:
        save_state({"state": task.get("State"), "progress": task.get("CurrentProgressPercentage"),
                    "ts": int(time.time())})


if __name__ == "__main__":
    main()
