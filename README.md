# strm-gen（小雅 Alist → Emby strm 生成器）

配置驱动地扫描小雅 Alist 目录、生成 `.strm` 写入 Emby 媒体库。**新增挂载目录只需改 docker-compose.yml 里 `MAPPINGS` 加一行并重跑**，不用改代码。

## 为什么是 Docker 版

老G速装里 Emby 的 `/media` 是**容器内的 loop 镜像**（宿主机没有对应目录），所以普通「写宿主机目录」的行不通。本镜像通过挂载 `docker.sock`，用 `tar | docker exec -i emby tar` 管道直接把 strm 解包进 Emby 容器，兼容这种架构。

## 快速开始

镜像托管在 **GitHub Container Registry（ghcr.io）**，用 GitHub 登录即可，公开包拉取免登录。

```bash
# 1. 拉取镜像（公开后无需 docker login）
docker pull ghcr.io/junboss1013/strm-gen:latest

# 2. 下载 compose
curl -O https://raw.githubusercontent.com/junboss1013/strm-gen-docker/main/docker-compose.yml

# 3. 编辑 docker-compose.yml 里的 ALIST_* / PUBLIC_PREFIX / EMBY_* / MAPPINGS

# 4. 跑一次（生成 strm 并同步进 Emby）
docker compose run --rm strm-gen
```

> 若拉取提示 `denied`：说明包还是私有。到 GitHub 仓库页面 → 右侧 **Packages** → 点 `strm-gen` → **Package settings** → **Change visibility** → **Public**。设为公开后即可免登录拉取。（一次性操作）

## 配置项（全部走环境变量）

| 变量 | 说明 | 默认 |
|---|---|---|
| `ALIST_BASE` | Alist 内网地址；host 网络下填 `http://127.0.0.1:5678` | `http://127.0.0.1:5678` |
| `ALIST_USERNAME` / `ALIST_PASSWORD` | Alist 账号（需能读目标目录） | guest / guest_Api789 |
| `PUBLIC_PREFIX` | strm 里写入的公网直链前缀（播放器实际访问） | 必填 |
| `EMBY_PUBLIC` / `EMBY_API_KEY` | Emby 公网地址与 API Key，用于触发刷新；留空则跳过 | 可选 |
| `EMBY_CONTAINER` | Emby 容器名 | `emby` |
| `AUTO_SCAN` | 生成后是否触发 Emby 刷新 | `true` |
| `CRON` | 定时表达式，如 `0 3 * * *`；设置后容器常驻 | 空（单次运行） |
| `WEBHOOK_ENABLED` | `true` 开启 webhook 实时刷新服务（常驻 HTTP） | `false` |
| `WEBHOOK_PORT` | webhook 监听端口（host 网络下即宿主机端口，勿与 5678 冲突） | `5679` |
| `WEBHOOK_TOKEN` | 调用鉴权 token，请求头 `X-Webhook-Token` 或 `?token=` | 空（不鉴权，不安全） |
| `WEBHOOK_DELAY` | 触发前延迟秒数，给离线下载/网盘索引留时间 | `0` |
| `MAPPINGS` | 目录映射 JSON 数组（见下） | 必填 |
| `CONFIG_FILE` | 可选：挂载 config.json 并用此变量指向，代替环境变量 | 空 |

## Webhook 实时刷新（配合教父插件）

容器常驻一个轻量 HTTP 服务，收到请求立即触发一次 strm 扫描 + 写 Emby + 刷新媒体库，
解决「转存到 115 后要等到 cron 点才刷进 Emby」的问题。可与 `CRON` 同时开启（webhook 实时 + cron 兜底）。

```bash
# 触发同步（带 token）
curl -X POST "http://<宿主机IP>:5679/refresh?path=青春共体" \
     -H "X-Webhook-Token: strmrefresh2026"
# 健康检查
curl "http://<宿主机IP>:5679/health"
```

- 收到请求后立即返回 `{"status":"accepted"}`，扫描在后台异步执行（带文件锁，防与 cron 并发冲突）。
- `<宿主机IP>`：插件容器若也是 host 网络可用 `127.0.0.1`；否则填宿主机局域网 IP。
- 教父转存插件/订阅插件转存成功后会自动 POST 这个地址（在插件配置里填 webhook 地址与 token）。
- 115 **离线下载**是异步任务，文件真正到位可能要几分钟；可把 `WEBHOOK_DELAY` 设为 30~60 秒。

## Emby 定时扫描 + 元数据补刮（v1.0.9）

`EMBY_SCAN_ENABLED=true` 时容器常驻一个独立 cron（默认每 3 小时），只处理 `EMBY_SCAN_LIBS` 指定的库，分三阶段：

| 阶段 | 做什么 | 耗时 |
|---|---|---|
| A 快速入库 | `Items/{folderId}/Refresh` + `ValidationOnly`，只校验文件、发现新增，**不请求任何外部元数据源** | 万级文件 < 1 秒 |
| B 剧/电影补刮 | 回查库内**缺主海报**的 Series/Movie，逐个 `Items/{id}/Refresh` + `FullRefresh` 补海报/简介/TMDB ID | 每个几秒～几十秒 |
| C 单集补刮 | 回查**完全没有 TMDB/IMDb ID** 的 Episode（标题仍是文件名的也算），按剧聚合后对命中的那几部剧做 `Recursive` 刷新，一次刷完整部剧 | 每部剧几秒～几分钟 |

三个阶段都是**收敛式**的：补上之后就不再命中，不会每轮重刮全库。

- 阶段 B 单条目连续失败 3 次冷却 7 天。
- 阶段 C 按**缺集数从少到多**排队，小剧先清完，避免一部千集大剧长期霸占配额；同一部剧连续 3 轮没进展（对比上一轮缺集数不降）就冷却 7 天，防止永远匹配不上 TMDB 的剧反复空转。
- 阶段 C 会跳过阶段 B 本轮刚触发过的剧，不重复排队。
- 判定「缺」的标准是**完全没有 TMDB/IMDb ID**（说明从没刮过）。有 ID 但简介为空**不算缺**——那是 TMDB 上这一集本身就没写简介，重刷一百遍也不会变，算进去只会白占配额。

> **前提**：Emby 库里 `Episode` 类型的抓取器必须是开着的，否则 `Recursive` 刷新也不会去查 TMDB。
> 检查方法：媒体库 → 高级 → 各类型选项 → 剧集，确认「元数据读取器」里有 `TheMovieDb`。

| 变量 | 说明 | 默认 |
|---|---|---|
| `EMBY_SCAN_ENABLED` / `EMBY_SCAN_CRON` | 是否开启定时扫描 / cron 表达式 | `false` / `0 */3 * * *` |
| `EMBY_SCAN_BASE` / `EMBY_SCAN_API_KEY` | Emby 内网地址 / API Key | 必填 |
| `EMBY_SCAN_LIBS` | 要管的库名（逗号分隔），会解析出所有挂载路径 | 必填 |
| `EMBY_SCAN_PATHS` | 库名解析失败时的兜底根路径 | 空 |
| `EMBY_SCAN_MODE` | `validation`（推荐）/ `refresh`（旧全库刷新，极慢） | `validation` |
| `EMBY_BACKFILL_ENABLED` | 是否开启阶段 B 补刮 | `true` |
| `EMBY_BACKFILL_LIMIT` | 每轮最多补刮多少个 | `30` |
| `EMBY_BACKFILL_MAX_TRIES` / `_COOLDOWN` | 单条目重试上限 / 冷却秒数 | `3` / `7 天` |
| `EMBY_BACKFILL_DELAY` | 扫完等几秒再补刮 | `10` |
| `EMBY_EPISODE_ENABLED` | 是否开启阶段 C 单集补刮 | `true` |
| `EMBY_EPISODE_LIMIT` | 每轮最多处理多少部剧 | `10` |
| `EMBY_EPISODE_MAX_TRIES` / `_COOLDOWN` | 单部剧无进展上限 / 冷却秒数 | `3` / `7 天` |
| `EMBY_EPISODE_DELAY` | 每部剧触发间隔秒数 | `5` |
| `EMBY_EPISODE_MAX_PER_SERIES` | 单剧缺集超过此数就跳过（防千集大剧占住刷新队列） | `500` |

首次补齐存量时可以临时把 `EMBY_EPISODE_LIMIT` 调大（例如 `60`），跑完再改回 10。

### 海报/简介还是不出来？先查「空壳 nfo」

Emby 的 `SaveLocalMetadata` 若曾开启，会把刮削结果写回媒体目录的 `tvshow.nfo` / `movie.nfo`。
如果那次扫描没刮到东西，就会写出一个**只有标题、没有 tmdbid/year 的空壳 nfo**。
之后再怎么刷新，Emby 都认为「本地已有元数据」，不再向 TMDB 搜索 —— 表现就是**有壳无皮、永远没海报**。

排查与清理（在 Emby 容器内执行）：

```bash
docker exec -it emby sh -c 'cd "/media/每日更新" && find . -name "*.nfo" -exec grep -LiE "tmdbid|imdbid|tvdbid" {} +'
```

列出来的就是空壳 nfo，备份后删除，再触发一次库扫描即可：

```bash
# 备份（示例：打包到 Emby 配置目录）
docker exec emby sh -c 'cd "/media/每日更新" && find . -name "*.nfo" -exec grep -LiE "tmdbid|imdbid|tvdbid" {} + > /tmp/e.txt && tar czf /config/nfo_backup.tgz -T /tmp/e.txt'
# 删除
docker exec emby sh -c 'cd "/media/每日更新" && find . -name "*.nfo" -exec grep -LiE "tmdbid|imdbid|tvdbid" {} + | while read f; do rm -f "$f"; done'
```

同时把媒体库的 `SaveLocalMetadata` 关掉，避免再次生成空壳：媒体库设置 → 元数据 → 取消「将元数据保存到媒体文件夹中」。

> 个别剧（目录名是纯外文/无年份，如 `再婚皇后 재혼황후`）TMDB 匹配不上时，可在该剧目录手写一个带 ID 的 `tvshow.nfo`：
> ```xml
> <tvshow><title>再婚皇后</title><year>2026</year><tmdbid>279246</tmdbid></tvshow>
> ```
> 再对该条目触发一次元数据刷新即可。

### MAPPINGS 格式

```json
[
  {"alist_path":"/🥝115网盘/115/影视库/电影","type":"movie","emby_path":"/media/115/电影"},
  {"alist_path":"/🥝115网盘/115/影视库/电视剧","type":"tv","emby_path":"/media/115/电视剧"}
]
```

- `alist_path`：Alist 里的源目录
- `type`：`movie`（生成 `片名 (年份).strm`）或 `tv`（生成 `剧名 - SxxExx - 标题.strm` + `Season N` 目录）
- `emby_path`：Emby 媒体库已扫描的目标路径（必须是 Emby 里真实存在的库路径）

**以后加目录**：在 `MAPPINGS` 里加一条 → 重跑即可。

## 两种运行模式

1. **单次**（默认）：`docker compose run --rm strm-gen`，跑完退出。适合手动或外部计划任务调用。
2. **cron 常驻**：取消 compose 里 `CRON` 注释，`docker compose up -d strm-gen`，容器按 cron 定时生成。日志在挂载目录的 `strmgen-out/run.log`。

## 前置条件

- 容器需挂载 `/var/run/docker.sock:/var/run/docker.sock`（用于写入 Emby 容器）。
- 用 `network_mode: host`，这样 `127.0.0.1:5678` 指向宿主机 Alist。
- 账号密码、API Key 明文写在 compose 里，注意文件权限（建议 `chmod 600 docker-compose.yml`），勿外传。

## 从源码构建

```bash
git clone https://github.com/junboss1013/strm-gen-docker.git
cd strm-gen-docker
docker build -t strm-gen:local .
```
