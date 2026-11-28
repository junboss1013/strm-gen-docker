#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置驱动的 strm 生成器（Docker 版）

- 扫描 Alist 目录生成 .strm（电影 / 电视剧命名规则）
- 经 docker.sock 用 tar 管道写入 Emby 容器媒体目录
  （兼容飞牛/老G速装里 /media 为容器内层叠 loop 镜像、宿主机无对应目录的情况）
- 可选触发 Emby 媒体库刷新
- 源文件删除后自动清理对应 strm 与 Emby 条目（manifest 对账）

新增挂载目录：只改 compose 里的 MAPPINGS（加一条映射），重启/重跑即可。
也可挂载 config.json 并用 CONFIG_FILE 指向它来配置。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

OUT_ROOT = os.environ.get("OUT_ROOT", "/out")
MANIFEST_FILE = os.environ.get("STRM_MANIFEST_FILE", "/out/.strm_manifest.json")
SCAN_STATE_FILE = "/out/.emby_scan_state.json"
VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".iso", ".rmvb", ".vob", ".mpg", ".mpeg",
}

# ---- 删除同步（源文件消失 → 删 strm + 删 Emby 条目）----
PRUNE_ENABLED = os.environ.get("PRUNE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
PRUNE_MAX_PERCENT = float(os.environ.get("PRUNE_MAX_PERCENT", "50") or 50)
# 比例保护只对「大批量」生效：1~4 个文件的删除直接放行，
# 否则只有 1 集的剧、单片电影会因为 100% > 阈值而永远删不掉。
PRUNE_GUARD_MIN = int(os.environ.get("PRUNE_GUARD_MIN", "5") or 5)
PRUNE_DELETE_EMBY = os.environ.get("PRUNE_DELETE_EMBY", "1").strip().lower() not in ("0", "false", "no", "off")
# 没有 manifest 时，是否扫磁盘按 URL 前缀认领历史 strm（升级后首次清理旧残留要靠它）
PRUNE_INIT_SCAN = os.environ.get("PRUNE_INIT_SCAN", "1").strip().lower() not in ("0", "false", "no", "off")

# 【已废弃】新剧「定向扫描」队列（EMBY_NEW_SCAN_*）
# 早期版本用 POST /Library/Refresh?Path=xxx 给新剧单独扫描，实测该接口会忽略 Path 参数、
# 直接启动**全库扫描**（几小时），扫描期间还会因挂载抖动批量删除已有条目（真实事故）。
# 现已确认 POST /Items/{libFolderId}/Refresh?MetadataRefreshMode=ValidationOnly
# 本身就能为新目录建出 Series 与分集（5 万文件库约 60s 建壳、120s 出集），无需额外扫描。
# 因此该机制整体移除；旧版本遗留的 .scan_pending.json 会被自动忽略。

TOKEN = None
ALIST = ""

# 分辨率标记，避免被误当成集号
_RES_MARKERS = {"480", "576", "720", "1080", "2160", "4320", "540", "360"}

# 网盘广告尾巴
_AD_PATTERNS = [
    r"【[^】]{0,60}(?:更多|访问|关注|公众号|资源|网址|www|http)[^】]{0,60}】",
    r"\[[^\]]{0,60}(?:更多|访问|关注|公众号|www\.|https?://)[^\]]{0,60}\]",
    r"https?://\S+",
    r"www\.[^\s\]\)]+",
    r"(?:更多高清|高清资源|请访问|关注公众号|扫描二维码)[^\s\]\)]*",
]


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and (v is None or str(v).strip() == ""):
        print(f"[FATAL] 缺少必需环境变量 {name}", flush=True)
        sys.exit(2)
    return v


def load_config():
    cfg = {
        "alist_base": _env("ALIST_BASE", "http://127.0.0.1:5678"),
        "alist_username": _env("ALIST_USERNAME", "guest"),
        "alist_password": _env("ALIST_PASSWORD", "guest_Api789"),
        "public_prefix": _env("PUBLIC_PREFIX", ""),
        "emby_public": _env("EMBY_PUBLIC", ""),
        "emby_api_key": _env("EMBY_API_KEY", ""),
        "emby_container": _env("EMBY_CONTAINER", "emby"),
        "auto_scan": _env("AUTO_SCAN", "true").lower() in ("1", "true", "yes", "on"),
        # 增量合并模式：写入前不清空目标目录，只覆盖/新增 strm，保留目录里已有的其他文件。
        # 用于把新 strm 写进已有大量历史 strm 的媒体库（如 /media/每日更新/*）而不破坏旧数据。
        "merge_mode": _env("MERGE_MODE", "false").lower() in ("1", "true", "yes", "on"),
    }
    # 可选：挂载 config.json 覆盖上面的环境变量
    cfg_file = os.environ.get("CONFIG_FILE", "")
    if cfg_file and os.path.exists(cfg_file):
        with open(cfg_file, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # mappings：环境变量 MAPPINGS 优先，其次 config.json
    mappings = None
    if os.environ.get("MAPPINGS"):
        try:
            mappings = json.loads(os.environ["MAPPINGS"])
        except Exception as e:
            print(f"[FATAL] MAPPINGS 不是合法 JSON: {e}", flush=True)
            sys.exit(2)
    elif cfg.get("mappings"):
        mappings = cfg["mappings"]
    if not isinstance(mappings, list) or not mappings:
        print("[FATAL] 未配置 MAPPINGS（映射列表为空）", flush=True)
        sys.exit(2)
    cfg["mappings"] = mappings
    return cfg


def login(cfg):
    global TOKEN, ALIST
    ALIST = cfg["alist_base"].rstrip("/")
    r = requests.post(
        ALIST + "/api/auth/login",
        json={"username": cfg["alist_username"], "password": cfg["alist_password"]},
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Alist 登录失败: {data}")
    TOKEN = data["data"]["token"]


def list_dir(path):
    items, page = [], 1
    while True:
        headers = {"Content-Type": "application/json"}
        if TOKEN:
            headers["Authorization"] = TOKEN
        r = requests.post(
            ALIST + "/api/fs/list",
            headers=headers,
            json={"path": path, "page": page, "per_page": 1000, "refresh": False},
            timeout=60,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 200:
            print(f"  [WARN] list {path} code={d.get('code')} msg={d.get('message')}", flush=True)
            break
        data = d.get("data") or {}
        items.extend(data.get("content") or [])
        total = data.get("total") or 0
        if page * 1000 >= total:
            break
        page += 1
    return items


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def parse_se(text):
    """从文件名/目录名解析 (季, 集)，解析不到返回 (None, None)。

    支持的写法（按优先级）：
      S02E178 / S02.E178 / S02EP178 / S02 E178
      第2季 第178集 / 第178集 / 第178话
      Season 2 - 178 / Season 2 Episode 178
      EP178 / E178 / Ep.178
      剧名 - 178 / 剧名.178（结尾独立数字，排除年份与分辨率）
    """
    if not text:
        return None, None
    s = os.path.splitext(text)[0]
    # 纯季目录名（Season 02 / S02 / 第2季）：只表达季，不表达集
    m = re.fullmatch(r"(?i)\s*(?:season|s)\s*(\d{1,2})\s*", s)
    if m:
        return int(m.group(1)), None
    m = re.fullmatch(r"\s*第\s*(\d{1,2})\s*季\s*", s)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?i)(?<![a-z])s(\d{1,2})\s*[.\-_ ]?\s*e[p]?\s*[.\-_ ]?(\d{1,4})(?!\d)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 1x05 这种写法（排除 1920x1080 之类的分辨率）
    m = re.search(r"(?<![a-z\d])(\d{1,2})[xX](\d{1,4})(?!\d)", s)
    if m:
        _se, _ep = int(m.group(1)), int(m.group(2))
        if _ep <= 999 and not (1900 <= _ep <= 2100) and str(_ep) not in _RES_MARKERS:
            return _se, _ep
    m = re.search(r"第\s*(\d{1,2})\s*季.*?第\s*(\d{1,4})\s*[集话回]", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"第\s*(\d{1,4})\s*[集话回]", s)
    if m:
        return None, int(m.group(1))
    m = re.search(r"(?i)season\s*(\d{1,2})\s*(?:[-–—_.: ]+|episode\s*)\s*(\d{1,4})(?!\d)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?i)(?<![a-z])(?:episode|ep)\s*[.\-_ ]?\s*(\d{1,4})(?!\d)", s)
    if m:
        n = int(m.group(1))
        if 0 < n <= 9999:
            return None, n
    m = re.search(r"(?i)(?<![a-z])e\s*[.\-_ ]?(\d{1,4})(?!\d)", s)
    if m:
        n = int(m.group(1))
        if 0 < n <= 9999:
            return None, n
    for m in re.finditer(r"(?:[-–—._ ]|\b)\s*(\d{1,4})\s*(?=[\[\]()\s]|$)", s):
        n = int(m.group(1))
        if n <= 0 or n > 999:
            continue
        if 1900 <= n <= 2100 or str(n) in _RES_MARKERS:
            continue
        return None, n
    return None, None


def gen_se(name, fallback=1):
    """兼容旧调用：返回 (季, 集)。"""
    s, e = parse_se(name)
    return (s or 1), (e or fallback)


def walk(path, rel=""):
    for it in list_dir(path):
        name = it.get("name", "")
        full = path.rstrip("/") + "/" + name
        if it.get("is_dir"):
            yield from walk(full, os.path.join(rel, name) if rel else name)
        elif is_video(name):
            yield (rel, name, full)


def direct_url(public_prefix, alist_full_path):
    return public_prefix.rstrip("/") + "/d" + urllib.parse.quote(alist_full_path, safe="/")


def clean(s):
    return re.sub(r"\s+", " ", s).strip()


def strip_ads(s):
    out = s
    for pat in _AD_PATTERNS:
        out = re.sub(pat, "", out, flags=re.I)
    out = clean(out)
    # 去完广告后可能只剩分隔符
    out = re.sub(r"^[\s\-_.]+|[\s\-_.]+$", "", out)
    return out


def safe_filename(name, limit=220):
    """截断到不超过 limit 字节（UTF-8），保证不超 ext4 的 255 字节限制。"""
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, ext = os.path.splitext(name)
    budget = limit - len(ext.encode("utf-8"))
    out = []
    used = 0
    for ch in stem:
        n = len(ch.encode("utf-8"))
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out) + ext


def _tidy_ep_title(title, series, season=None, ep=None):
    """单集标题：去广告、去与文件名重复的信息。"""
    t = strip_ads(title)
    if not t:
        return ""
    if t == series:
        return ""
    # 标题里常重复剧名
    if series and t.startswith(series):
        t = t[len(series):].strip(" -_.·")
    # 「Season 2 - 178」「S02E178」这类已由文件名本身表达
    if season is not None and ep is not None:
        for pat in (
            r"(?i)^season\s*\d{1,2}\s*[-–—_.:]?\s*%d\b" % ep,
            r"(?i)^s\d{1,2}\s*[.\-_ ]?e?p?\s*%d\b" % ep,
            r"^第\s*%d\s*[集话回]" % ep,
        ):
            t2 = re.sub(pat, "", t).strip(" -_.·")
            if t2 != t:
                t = t2
                break
    return t


def _emit_strm(sp, url):
    """写 strm 文件；内容未变则跳过写入。

    【关键】保住原 mtime。Emby 按 (mtime, size) 判断文件是否变化，每次同步都
    覆盖全部 strm 会让 mtime 全刷新，Emby 每次把上万个 strm 当新文件重新探测
    远程直链（单个 1~10 秒），扫描数小时且极易卡死。
    """
    if os.path.exists(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                if f.read() == url:
                    return
        except Exception:
            pass
    with open(sp, "w", encoding="utf-8") as f:
        f.write(url)


def process(cfg, mapping, root):
    """生成某个映射的 strm。返回 (数量, 容器内绝对路径列表)。"""
    src, dtype, target = mapping["alist_path"], mapping["type"], mapping["emby_path"]
    public_prefix = cfg["public_prefix"]
    if not public_prefix:
        print("[FATAL] 未配置 PUBLIC_PREFIX（strm 直链前缀）", flush=True)
        sys.exit(2)
    out = os.path.join(root, target.strip("/"))
    os.makedirs(out, exist_ok=True)
    cnt = 0
    written = []
    ep_counter = {}   # (series, season) -> 下一个可用集号（解析失败时兜底）
    fallback_n = 0
    pending_fb = []   # 未识别出集号的文件：(series, season, name, full)，等全剧解析完再分配号

    for rel, name, full in walk(src):
        if dtype == "movie":
            folder = rel.split(os.sep)[0] if os.sep in rel else clean(os.path.splitext(name)[0])
            md = os.path.join(out, folder)
            os.makedirs(md, exist_ok=True)
            sp = os.path.join(md, safe_filename(clean(os.path.splitext(name)[0]) + ".strm"))
        else:
            parts = [p for p in rel.split(os.sep) if p] if rel else []
            series = parts[0] if parts else "未知剧集"
            sdir = parts[1] if len(parts) >= 2 and re.match(
                r"(?i)season|第.{1,3}季|^s\d", parts[1]
            ) else None

            # 集号：先看文件名；文件名里没有再看它所在目录名（每集一个文件夹的资源很常见）
            f_season, f_ep = parse_se(name)
            if f_ep is None and parts and parts[-1] != sdir:
                p_season, p_ep = parse_se(parts[-1])
                if p_ep is not None:
                    f_season, f_ep = p_season, p_ep
            season = f_season or 1
            if sdir:
                sm = re.search(r"(\d+)", sdir)
                if sm:
                    season = int(sm.group(1))

            if f_ep is None:
                # 未识别集号：先挂起。绝不能立刻从 E1 开始编号 —— 那会跟真实的
                # 第一集撞名，把 E1/E2 的 strm 覆盖成花絮/整季合集文件。
                # 等本剧所有文件的集号都解析完，再从「最大集号 + 1」往后分配。
                pending_fb.append((series, season, name, full))
                continue
            else:
                ep = f_ep
                key = (series, season)
                ep_counter[key] = max(ep_counter.get(key, 1), ep + 1)

            ed = os.path.join(out, series, f"Season {season}")
            os.makedirs(ed, exist_ok=True)
            ep_title = _tidy_ep_title(os.path.splitext(name)[0], series, season, ep)
            base = f"{series} - S{season:02d}E{ep:02d}"
            if ep_title:
                base += f" - {ep_title}"
            sp = os.path.join(ed, safe_filename(base + ".strm"))

        url = direct_url(public_prefix, full)
        _emit_strm(sp, url)
        cnt += 1
        written.append(sp)

    # 第二遍：给挂起的未识别文件分配号，从该剧已用最大集号往后排
    for series, season, name, full in pending_fb:
        key = (series, season)
        ep = ep_counter.get(key, 1)
        ep_counter[key] = ep + 1
        fallback_n += 1
        print(f"  [WARN] {series}/{name} 未识别集号，按顺序记为 E{ep}", flush=True)
        ed = os.path.join(out, series, f"Season {season}")
        os.makedirs(ed, exist_ok=True)
        ep_title = _tidy_ep_title(os.path.splitext(name)[0], series, season, ep)
        base = f"{series} - S{season:02d}E{ep:02d}"
        if ep_title:
            base += f" - {ep_title}"
        sp = os.path.join(ed, safe_filename(base + ".strm"))
        _emit_strm(sp, direct_url(public_prefix, full))
        cnt += 1
        written.append(sp)

    if fallback_n:
        print(f"  [WARN] {mapping['alist_path']} 有 {fallback_n} 个文件没解析出集号，已按顺序编号", flush=True)
    # 转成 Emby 容器内绝对路径
    container_paths = []
    for p in written:
        relp = os.path.relpath(p, root)
        container_paths.append("/" + relp.replace(os.sep, "/"))
    return cnt, container_paths


def load_manifest():
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            d = json.load(f)
        paths = d.get("paths") or {}
        return paths if isinstance(paths, dict) else {}
    except Exception:
        return {}


def save_manifest(paths):
    try:
        os.makedirs(os.path.dirname(MANIFEST_FILE) or ".", exist_ok=True)
        tmp = MANIFEST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "paths": paths}, f, ensure_ascii=False)
        os.replace(tmp, MANIFEST_FILE)
    except Exception as e:
        print(f"  [WARN] 写 manifest 失败: {e}", flush=True)


def _local_orphans(key, cur, old_paths):
    """OUT_ROOT 里属于该映射、但既不在本轮结果、也不在上一轮清单里的 strm。

    来源：manifest 丢失后的残留、手工拷进来的副本。它们留在本地就会被 tar
    推回媒体库，表现成「115 删了、媒体库删了、下一轮又自己长回来」。
    交给 prune_removed 统一处理，才能保证本地/容器/Emby 条目三层一起删。
    """
    root = os.path.join(OUT_ROOT, key.lstrip("/"))
    if not os.path.isdir(root):
        return []
    known = set(cur) | set(old_paths or [])
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            if not f.lower().endswith(".strm"):
                continue
            fp = os.path.join(dirpath, f)
            rel = os.path.relpath(fp, OUT_ROOT).replace(os.sep, "/")
            cp = "/" + rel
            if cp not in known:
                out.append(cp)
    return out


def _docker_rm_files(container, paths, batch=200):
    """在 Emby 容器里批量删除文件（NUL 分隔，兼容空格与中文）。"""
    if not paths:
        return 0
    ok = 0
    for i in range(0, len(paths), batch):
        chunk = paths[i:i + batch]
        payload = b"\0".join(p.encode("utf-8") for p in chunk) + b"\0"
        r = subprocess.run(
            ["docker", "exec", "-i", container, "xargs", "-0", "rm", "-f", "--"],
            input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if r.returncode == 0:
            ok += len(chunk)
            continue
        # 回退：逐条删除
        for p in chunk:
            rc = subprocess.run(
                ["docker", "exec", container, "rm", "-f", "--", p],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
            ok += 1 if rc == 0 else 0
    return ok


def _docker_dir_has_strm(container, dir_path):
    """目录里是否还有 strm。

    返回 True=还有，False=确认没有，None=检查失败。
    三态很重要：容器不可用 / docker 报错时绝不能当成「空了」去删目录，
    否则一次命令失败就可能连整部剧的目录和 Emby 条目一起删掉。
    """
    try:
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             'find "$1" -maxdepth 4 -name "*.strm" -print -quit', "_", dir_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
        )
    except Exception:
        return None
    if r.returncode == 0:
        return bool((r.stdout or "").strip())
    err = (r.stderr or "").lower()
    if "no such file" in err or "not found" in err:
        return False
    print(f"[PRUNE]   目录检查失败（不删除）{dir_path}: {(r.stderr or '').strip()[:120]}", flush=True)
    return None


def _docker_rm_tree(container, dir_path):
    subprocess.run(
        ["docker", "exec", container, "rm", "-rf", "--", dir_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _emby_all_series_map(base, h):
    """拉库里所有 Series/Movie 的 Path -> {Id, Type} 映射。

    不能用 GET /Items?Path=xxx 反查：实测该参数对「文件已不存在的条目」返回 0 条，
    而我们要删的恰好都是这类条目。改为全量拉一遍本地比对，几千条几秒就完。
    """
    out = {}
    for typ in ("Series", "Movie"):
        start = 0
        while True:
            params = {"Recursive": "true", "IncludeItemTypes": typ,
                      "Fields": "Path", "Limit": 1000, "StartIndex": start}
            try:
                r = requests.get(base + "/Items", headers=h, params=params, timeout=180)
                d = r.json()
            except Exception as e:
                print(f"[PRUNE]   拉取 {typ} 列表失败: {e}", flush=True)
                break
            items = d.get("Items") or []
            for it in items:
                p = (it.get("Path") or "").rstrip("/")
                if p:
                    out[p] = {"Id": it.get("Id"), "Type": typ}
            start += len(items)
            if not items or start >= (d.get("TotalRecordCount") or 0):
                break
    return out


def _emby_episode_list(base, h, series_id):
    eps, start = [], 0
    while True:
        params = {"Fields": "Path", "Limit": 1000, "StartIndex": start}
        try:
            r = requests.get(base + f"/Shows/{series_id}/Episodes", headers=h, params=params, timeout=180)
            d = r.json()
        except Exception:
            break
        items = d.get("Items") or []
        eps.extend(items)
        start += len(items)
        if not items or start >= (d.get("TotalRecordCount") or 0):
            break
    return eps


def _emby_delete_ids(base, h, ids):
    """删除条目。DeleteFiles=false 只是尽力而为——实测这个版本的 Emby 会连文件一起删，
    所以调用方必须保证「该条目对应的文件确实已经不需要了」。"""
    ok = 0
    for i in ids:
        try:
            r = requests.delete(base + f"/Items/{i}", headers=h,
                                params={"DeleteFiles": "false"}, timeout=60)
            ok += 1 if r.status_code in (200, 204) else 0
        except Exception:
            pass
    return ok


def _is_season_dir(name):
    """Season 2 / 第 2 季 / S02 这类季目录名。"""
    return bool(name) and bool(re.match(r"(?i)^(?:season\s*\d{1,2}|第.{1,3}季|s\d{1,2})$", name))


def _emby_remove_for_paths(cfg, removed_paths, empty_dirs):
    """把「文件已被删掉」的条目从 Emby 里移除。

    只删 Episode / Movie 级条目；Series 只有在其目录确认已空时才删
    （删 Series 会级联删文件，目录还留东西时绝对不能碰）。
    """
    base = (cfg.get("emby_public") or "").rstrip("/")
    key = cfg.get("emby_api_key") or ""
    if not base or not key:
        return 0, 0
    h = {"X-Emby-Token": key}
    smap = _emby_all_series_map(base, h)
    if not smap:
        return 0, 0

    removed = set(removed_paths)
    # 电影：Emby 里 Movie 条目的 Path 就是 strm 文件本身 → 直接精确命中。
    # 剧集：条目的 Path 是剧目录，而我们要删的是 剧目录/Season N/xxx.strm，
    #       必须把季目录上提一层才找得到 Series（否则「只删几集」永远删不掉条目）。
    movie_ids, by_series = [], {}
    for p in removed_paths:
        hit = smap.get(p)
        if hit and hit.get("Type") == "Movie":
            movie_ids.append(hit["Id"])
            continue
        d = os.path.dirname(p)
        if _is_season_dir(os.path.basename(d)):
            d = os.path.dirname(d)
        by_series.setdefault(d.rstrip("/"), []).append(p)

    del_eps = _emby_delete_ids(base, h, movie_ids) if movie_ids else 0
    for d, paths in by_series.items():
        hit = smap.get(d)
        if not hit or hit.get("Type") != "Series":
            continue
        eps = _emby_episode_list(base, h, hit["Id"])
        # 只删「路径精确命中被删清单」的条目，避免误伤同剧其他集
        drop = [e.get("Id") for e in eps if (e.get("Path") or "") in removed]
        if drop:
            del_eps += _emby_delete_ids(base, h, drop)

    del_series = 0
    for d in (empty_dirs or []):
        hit = smap.get(d.rstrip("/"))
        if not hit:
            continue
        # 二次确认：目录里真的没有 strm 了才删整条
        if _docker_dir_has_strm(cfg["emby_container"], d) is not False:
            continue
        del_series += _emby_delete_ids(base, h, [hit["Id"]])
    return del_eps, del_series


def _emby_lookup(base, h, paths, workers=8):
    """按 Path 反查 Emby 条目 ID（保留兼容，Path 查询对已删文件无效）。"""
    def one(p):
        try:
            r = requests.get(base + "/Items", headers=h,
                             params={"Path": p, "Recursive": "false", "Fields": "Path"},
                             timeout=30)
            items = (r.json() or {}).get("Items") or []
            return [it.get("Id") for it in items if it.get("Id")]
        except Exception:
            return []
    ids = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, paths):
            ids.extend(res)
    return ids


def _emby_delete_items(base, h, ids, workers=4):
    def one(i):
        try:
            r = requests.delete(base + f"/Items/{i}", headers=h, timeout=30)
            return r.status_code in (200, 204)
        except Exception:
            return False
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, ids):
            ok += 1 if res else 0
    return ok


def managed_prefixes(cfg):
    """本工具生成的 strm 内容一定以这些 URL 前缀开头。

    用「URL 前缀」而不是 manifest 来认领文件，好处是首次运行 / manifest 丢失时
    也能准确认出哪些是本工具写的，绝不会碰媒体库里其他工具生成的历史 strm。
    """
    base = (cfg.get("public_prefix") or "").rstrip("/") + "/d"
    out = []
    for m in cfg.get("mappings", []):
        ap = str(m.get("alist_path", "")).rstrip("/")
        if not ap:
            continue
        out.append(base + urllib.parse.quote(ap, safe="/") + "/")
    return out


def _grep_managed(container, target, prefixes):
    """在目标目录里找出 URL 指向本工具源的 strm，返回容器内绝对路径列表。"""
    if not prefixes:
        return []
    args = " ".join("-e '%s'" % p.replace("'", "'\\''") for p in prefixes)
    script = (
        f'find "{target}" -name "*.strm" -print0 2>/dev/null '
        f'| xargs -0 grep -l -F {args} 2>/dev/null'
    )
    try:
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=900,
        )
    except Exception as e:
        print(f"[PRUNE]   扫描历史 strm 失败: {e}", flush=True)
        return []
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def _settle_deletions(cfg, dirs, rounds=2, wait=4):
    """删完条目后复核目录，清掉 Emby 回写/重建留下的空壳。

    Emby 删除 Series 的过程中会往剧目录写 tvshow.nfo，排队中的扫描也可能把条目重建；
    只删一次目录就会留下「目录存在但没有 strm」的空壳，媒体库里表现为点开报错。
    这里隔几秒再核一轮，幂等。
    """
    if not dirs:
        return
    container = cfg["emby_container"]
    for _ in range(rounds):
        time.sleep(wait)
        alive = [d for d in dirs if _docker_dir_has_strm(container, d) is not False]
        if not alive:
            return
        if PRUNE_DELETE_EMBY:
            try:
                _emby_remove_for_paths(cfg, [], alive)
            except Exception:
                pass
        for d in alive:
            if _docker_dir_has_strm(container, d) is False:
                _docker_rm_tree(container, d)


def prune_removed(cfg, current):
    """对账：上一轮有、本轮没有的 strm → 删除文件 + 删除 Emby 条目 + 清空目录。

    返回 (已删除数量, 本轮跳过删除的路径)，跳过的路径要合并回 manifest，
    否则下一轮就没有基线了，这些失效文件会被永久遗忘。

    安全阀：
      - 首次运行没有 manifest 时只记录，不删任何东西
      - 某映射本轮 0 个 strm（Alist 异常）→ 跳过该映射
      - 单轮删除比例超过 PRUNE_MAX_PERCENT → 跳过并告警，避免挂载抖动误删整库
    """
    if not PRUNE_ENABLED:
        print("[PRUNE] 已关闭（PRUNE_ENABLED=0）", flush=True)
        return 0, {}
    container = cfg["emby_container"]
    old = load_manifest()
    if not old:
        # 没有基线：扫一遍目标目录，按 strm 内容里的 URL 前缀认领本工具写过的文件。
        # 这是「115 删了但媒体库没删」能一次性清干净的关键——只靠 manifest 的话，
        # 升级前的历史 strm 永远不在基线里，就永远删不掉。
        if not PRUNE_INIT_SCAN:
            print("[PRUNE] 首次运行，仅建立基线，不执行删除", flush=True)
            return 0, {}
        prefixes = managed_prefixes(cfg)
        for m in cfg.get("mappings", []):
            tgt = "/" + str(m.get("emby_path", "")).strip("/")
            if not tgt.strip("/"):
                continue
            found = _grep_managed(container, tgt, prefixes)
            if found:
                old[tgt] = found
                print(f"[PRUNE] 首次对账：{tgt} 认出 {len(found)} 个历史 strm 属本工具", flush=True)
        if not old:
            print("[PRUNE] 首次运行，未发现历史 strm，仅建立基线", flush=True)
            return 0, {}

    total_del = 0
    skipped = {}
    for key, old_paths in old.items():
        cur = set(current.get(key) or [])
        if not cur:
            print(f"[PRUNE] {key} 本轮 0 个 strm，判定为源异常，跳过删除", flush=True)
            skipped[key] = list(old_paths or [])
            continue
        removed = [p for p in (old_paths or []) if p not in cur]
        for p in _local_orphans(key, cur, old_paths):
            if p not in removed:
                removed.append(p)
        if not removed:
            continue
        pct = 100.0 * len(removed) / max(len(old_paths or []), 1)
        if len(removed) >= PRUNE_GUARD_MIN and pct > PRUNE_MAX_PERCENT:
            print(f"[PRUNE] {key} 待删 {len(removed)}/{len(old_paths)}（{pct:.0f}%）"
                  f" 超过阈值 {PRUNE_MAX_PERCENT:.0f}%，本轮跳过。"
                  f"确认无误可调大 PRUNE_MAX_PERCENT 或置 100", flush=True)
            skipped[key] = removed
            continue

        print(f"[PRUNE] {key} 源已删除 {len(removed)} 个（{pct:.1f}%），开始清理", flush=True)

        # 先删本地 /out 里的副本。本工具的同步方式是「把整个 /out tar 进 Emby」，
        # 本地残留一个文件就等于下一轮又把它推回媒体库 —— 这正是过去「删了又长回来」的原因。
        loc = 0
        for p in removed:
            lp = os.path.join(OUT_ROOT, p.lstrip("/"))
            try:
                if os.path.isfile(lp):
                    os.remove(lp)
                    loc += 1
            except Exception:
                pass
        if loc:
            print(f"[PRUNE]   删除本地 strm {loc}/{len(removed)}", flush=True)

        n = _docker_rm_files(container, removed)
        total_del += n
        print(f"[PRUNE]   删除 strm 文件 {n}/{len(removed)}", flush=True)

        # 清理整部剧/整部电影目录：目录里已无 strm 就整目录删掉，避免留下空壳
        protected = {"/" + str(m.get("emby_path", "")).strip("/") for m in cfg.get("mappings", [])}

        # 先逐季检查（整季被删时 Season 会变空季，需要单独清）
        season_dirs = set()
        for p in removed:
            d = os.path.dirname(p)
            if _is_season_dir(os.path.basename(d)):
                season_dirs.add(d)
        for d in sorted(season_dirs):
            if _docker_dir_has_strm(container, d) is False:
                print(f"[PRUNE]   清理空季 {d}", flush=True)
                _docker_rm_tree(container, d)

        tops = set()
        for p in removed:
            d = os.path.dirname(p)
            # 剧集是 剧名/Season N/xxx.strm → 上提到剧名目录；电影是 片名/xxx.strm → 就是片名目录
            if _is_season_dir(os.path.basename(d)):
                d = os.path.dirname(d)
            tops.add(d)
        empty_tops = []
        for d in sorted(tops):
            if not d or d.count("/") < 2 or d in protected:
                continue
            if _docker_dir_has_strm(container, d) is False:
                empty_tops.append(d)

        # 先删 Emby 条目（ValidationOnly 扫描不会移除已消失文件的条目，必须显式删）
        if PRUNE_DELETE_EMBY:
            try:
                de, ds = _emby_remove_for_paths(cfg, removed, empty_tops)
                if de or ds:
                    print(f"[PRUNE]   删除 Emby 条目：分集/电影 {de} 个，空剧/空片 {ds} 个", flush=True)
                else:
                    print("[PRUNE]   Emby 里没有需要删除的条目", flush=True)
            except Exception as e:
                print(f"[PRUNE]   删除 Emby 条目失败（忽略）: {e}", flush=True)

        # 删目录必须排在「删条目」之后：Emby 删 Series 时会往剧目录回写 tvshow.nfo，
        # 甚至让排队中的扫描把刚删掉的条目重建一遍。先删目录就会留下一个「空剧壳」。
        for d in empty_tops:
            if _docker_dir_has_strm(container, d) is False:
                print(f"[PRUNE]   清理空目录 {d}", flush=True)
                _docker_rm_tree(container, d)
        _settle_deletions(cfg, empty_tops)
    return total_del, skipped


def sync_to_emby(cfg, root):
    """tar 管道经 docker exec 解包进 Emby 容器根目录，写入 /media/...。

    两种模式：
    - 覆盖模式（默认）：解包前清理容器内由本工具管理的目标目录（MAPPINGS 的 emby_path），
      避免历史遗留的「目录位置是文件」脏数据导致整批 strm 同步失败。适用于本工具独占的目录。
    - 合并模式（MERGE_MODE=true）：不清空目标目录，直接 tar 解包覆盖/新增 strm，
      保留目录里已有的其他文件。适用于写入已有大量历史 strm 的共享媒体库（如 /media/每日更新/*）。
      仅对「本批次 strm 将要落地的确切路径」做脏数据检查：若某路径被误写成文件则单独删除。
    """
    container = cfg["emby_container"]
    merge = cfg.get("merge_mode", False)

    # 收集所有 emby_path 的顶层管理目录（如 /media/115/电影、/media/每日更新/电视剧）
    targets = set()
    for m in cfg.get("mappings", []):
        p = str(m.get("emby_path", "")).strip()
        if p.startswith("/"):
            targets.add("/" + p.strip("/"))

    if not merge:
        # 覆盖模式：先删干净（含被误写成文件的同名项），tar 会重建
        for p in sorted(targets):
            subprocess.run(
                ["docker", "exec", container, "rm", "-rf", p],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    else:
        # 合并模式：清空 OUT_ROOT 中本批次 strm 将要落到、但在容器里被误写成文件的路径。
        # 只处理 OUT_ROOT 里真实出现的目录/文件，绝不触碰目标目录里其他历史 strm。
        print("  [MERGE] 增量合并模式：保留目标目录已有文件，仅覆盖/新增本批 strm", flush=True)
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                rel_dir = os.path.relpath(os.path.join(dirpath, d), root)
                # rel_dir 形如 media/115/电影/某剧；对应容器内 /media/...
                container_path = "/" + rel_dir.replace(os.sep, "/")
                # 若该位置在容器里是普通文件（非目录），删掉它以便 tar 建目录
                chk = subprocess.run(
                    ["docker", "exec", container, "sh", "-c",
                     f'[ -f "{container_path}" ] && echo FILE || echo OK'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
                if (chk.stdout or "").strip() == "FILE":
                    print(f"  [MERGE] 清理脏文件: {container_path}", flush=True)
                    subprocess.run(
                        ["docker", "exec", container, "rm", "-f", container_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )

    tar = subprocess.Popen(
        ["tar", "-cf", "-", "-C", root, "."],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    dx = subprocess.run(
        ["docker", "exec", "-i", container, "tar", "-xf", "-", "-C", "/"],
        stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    tar.wait()
    if dx.returncode != 0:
        print("  [WARN] tar 同步警告:", dx.stderr.decode("utf-8", "replace")[:300], flush=True)
        return False
    return True


def _load_scan_state():
    try:
        with open(SCAN_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_scan_state(d):
    try:
        os.makedirs(os.path.dirname(SCAN_STATE_FILE) or ".", exist_ok=True)
        with open(SCAN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def trigger_scan(cfg):
    """触发 Emby 入库扫描（快速模式），带卡死自愈。

    【为什么不用 /Library/Refresh】
    实测 /Library/Refresh 会启动全库扫描并对每个已有 strm 完整重新刮削
    （每集向 TMDB 请求 5 种语言），1.2 万文件要十几小时；期间任务一直 Running，
    后续扫描请求全被吞掉；遇到损坏 .nfo 还会让扫描线程挂掉、状态永久卡死。

    【正确做法】
    对每个写库目标目录反查 Emby 的 Folder 条目，然后：
      POST /Items/{folderId}/Refresh?Recursive=true
           &MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly
    只校验文件、发现新增，不请求外部元数据源 —— 实测 15 秒完成，新文件立即入库。
    注意：该模式不会移除「文件已消失」的条目，删除同步由 prune_removed() 显式处理。
    """
    if not cfg["emby_public"] or not cfg["emby_api_key"]:
        return False
    base = cfg["emby_public"].rstrip("/")
    h = {"X-Emby-Token": cfg["emby_api_key"]}

    def _folder_id(path):
        try:
            r = requests.get(base + "/Items", headers=h,
                             params={"Path": path, "Recursive": "false", "Fields": "Path"}, timeout=60)
            items = r.json().get("Items") or []
            return items[0].get("Id") if items else None
        except Exception:
            return None

    # 自愈：清理历史残留的卡死全库任务
    try:
        tasks = requests.get(base + "/ScheduledTasks", headers=h, timeout=20).json()
        task = next((t for t in tasks if t.get("Key") == "RefreshLibrary"), None)
        if task and task.get("State") == "Running":
            prog = task.get("CurrentProgressPercentage")
            prev = _load_scan_state()
            same = (prog is not None and prev.get("progress") is not None
                    and abs(float(prog) - float(prev["progress"])) < 0.01)
            if same:
                print(f"[SCAN] 全库扫描卡死（进度停在 {prog}），先停止复位", flush=True)
                requests.delete(base + f"/ScheduledTasks/Running/{task.get('Id')}", headers=h, timeout=20)
                time.sleep(3)
            else:
                print(f"[SCAN] 全库扫描进行中（{prog}%），不影响本次快速入库", flush=True)
    except Exception as e:
        print(f"[SCAN] 自愈检查失败（忽略）: {e}", flush=True)

    # 快速入库：对每个目标媒体目录做 ValidationOnly 刷新。
    # 实测这一种就够了——它能发现新增目录并建出 Series/分集；不要再用 Library/Refresh?Path=
    # 给新剧「单独扫描」，那个接口会忽略 Path 启动全库扫描，是历史事故的根源。
    done = 0
    for m in cfg.get("mappings", []):
        p = str(m.get("emby_path", "")).strip()
        if not p.startswith("/"):
            continue
        path = "/" + p.strip("/")
        fid = _folder_id(path)
        if not fid:
            # 解析不到库 folder 就跳过。绝不用 /Library/Refresh?Path= 兜底：
            # 该接口会忽略 Path 直接启动全库扫描，几小时跑不完，还会批量删条目。
            print(f"[SCAN] {path} 找不到对应库 folder，跳过（不影响入库）", flush=True)
            continue
        try:
            url = (base + f"/Items/{fid}/Refresh?Recursive=true"
                   "&MetadataRefreshMode=ValidationOnly&ImageRefreshMode=ValidationOnly"
                   "&ReplaceAllMetadata=false&ReplaceAllImages=false")
            r = requests.post(url, headers=h, timeout=300)
            print(f"[SCAN] {path} (folder_id={fid}) -> HTTP {r.status_code}", flush=True)
            if r.status_code in (200, 204):
                done += 1
        except Exception as e:
            print(f"[SCAN] {path} 触发失败: {e}", flush=True)
    return done > 0


def _verify_sync(cfg, current):
    """tar 之后抽查几个文件是否真的落到容器里。

    tar 返回 0 不代表写对了位置（比如目标被误建成同名普通文件）。
    逐个校验上万个文件太慢，抽样足以发现「整批没进去」这类故障。
    """
    container = cfg["emby_container"]
    samples = []
    for paths in (current or {}).values():
        ps = list(paths or [])
        if ps:
            samples.extend([ps[0], ps[-1]])
    samples = list(dict.fromkeys(samples))[:6]
    if not samples:
        return
    cmd = " ; ".join('[ -f "%s" ] && echo OK || echo MISS' % s.replace('"', '\\"')
                     for s in samples)
    try:
        r = subprocess.run(["docker", "exec", container, "sh", "-c", cmd],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=120)
        miss = (r.stdout or "").count("MISS")
    except Exception as e:
        print(f"[SYNC] 抽查失败（忽略）: {e}", flush=True)
        return
    if miss:
        print(f"[SYNC] 校验未通过：抽查 {len(samples)} 个文件，{miss} 个不在容器内", flush=True)
    else:
        print(f"[SYNC] 抽查 {len(samples)} 个文件均已就位", flush=True)


def main():
    cfg = load_config()
    login(cfg)
    # 注意：不再清空 OUT_ROOT。
    # 以前每轮 rmtree 重建，导致所有 strm 的 mtime 全部刷新，Emby 会把每一个文件都当成
    # 新文件重新探测远程直链（单集 1~10 秒），扫描长期跑不完；更关键的是「源已删除」的
    # 文件先被删掉、又被 tar 原样推回媒体库，删除同步等于没做。
    # 现在改为「增量覆盖 + 显式对账」：内容相同的文件不动（mtime 保持），
    # 失效文件由 prune_removed() 同时从 OUT_ROOT 和 Emby 两侧删除。
    os.makedirs(OUT_ROOT, exist_ok=True)

    total = 0
    current = {}
    for m in cfg["mappings"]:
        n, paths = process(cfg, m, OUT_ROOT)
        total += n
        current["/" + str(m["emby_path"]).strip("/")] = paths
        print(f"[OK] {m['alist_path']} -> {m['emby_path']} : {n} strm", flush=True)

    # 源文件已删除 → 先清本地 strm，再清容器文件/空目录，最后删 Emby 条目。
    # 必须排在 sync 之前：本地不清干净，tar 会把刚删掉的文件又推回媒体库。
    try:
        d, skipped = prune_removed(cfg, current)
        if d:
            print(f"[PRUNE] 共清理 {d} 个失效 strm", flush=True)
        # 本轮跳过的失效路径要留在 manifest 里，否则下轮丢基线、永久遗忘
        for k, ps in (skipped or {}).items():
            merged = list(dict.fromkeys(list(current.get(k) or []) + list(ps)))
            current[k] = merged
    except Exception as e:
        print(f"[PRUNE] 清理失败（不影响入库）: {e}", flush=True)

    if sync_to_emby(cfg, OUT_ROOT):
        print(f"[SYNC] {total} strm -> {cfg['emby_container']}:/", flush=True)
        _verify_sync(cfg, current)

    save_manifest(current)

    if cfg["auto_scan"]:
        try:
            if trigger_scan(cfg):
                print("[SCAN] Emby 媒体库刷新已触发", flush=True)
        except Exception as e:
            print(f"[SCAN] 跳过/失败: {e}", flush=True)
    print(f"[DONE] 共 {total} 个 strm", flush=True)


if __name__ == "__main__":
    main()
