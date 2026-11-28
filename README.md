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

## 剧集命名与集号识别（v1.1.0）

strm 文件名形如 `剧名 - S02E171 - 原始标题.strm`，Emby 靠 `SxxExx` 匹配季集。

集号解析顺序（先看视频文件名，解析不到再看它所在的目录名）：

| 写法 | 例子 |
|---|---|
| `SxxExx` | `权力的游戏 - S01E03.mkv` |
| `1x05` | `Show - 1x05 - x.mkv` |
| 第 N 季 第 M 集 | `第2季 第18集.mp4` |
| `Season 2 - 178` | `[ANi] 黑色五叶草 Season 2 - 178 [1080P].mp4` |
| `EP12` / `E12` | `某剧.EP12.1080p.mkv` |
| 结尾独立数字 | `[Group] Title - 12 [1080p].mkv` |

分辨率（1080/720/2160…）和年份（1900–2100）不会被误当集号。
实在解析不出来的，按出现顺序递增编号（`1、2、3…`），**不会**再把多集堆到同一集号上——
1.1.0 之前就是这个 bug：8 个文件全叫 `S02E01`，Emby 当成同一集的 8 个版本，界面上只显示 1 集。

日志里会有 `[WARN] … 未识别集号，按顺序记为 E1`，看到就说明该资源命名太乱，值得手工整理一下。

> 每集一个子文件夹的资源（`Season 02/剧名 - 178/xxx.mp4`）也能正确识别：
> 文件名没集号时会回退到用它所在文件夹的名字解析。

## 删除同步：115 上删了，媒体库跟着删（v1.1.0）

之前只做「新增」不做「删除」：115 网盘里删掉影视后，strm 还躺在 Emby 媒体目录里，
所以媒体库照旧能看到、点开却放不了。现在加了 manifest 对账。

**原理**

1. 每轮生成完，把「本工具生成过的 strm 完整清单」写到 `/out/.strm_manifest.json`
2. 下一轮生成完再对比：上一轮有、这一轮没有的 → 判定源已删除
3. 对这些路径：删 strm 文件 → **删对应 Emby 条目** → 目录空了就整目录删掉

> 只删 strm 不够。实测 `ValidationOnly` 扫描**不会**移除「文件已消失」的条目，
> 必须显式 `DELETE /Items/{id}`，否则媒体库里会一直留着一个点开就报错的空壳。

**安全阀（重要）**

| 情况 | 行为 |
|---|---|
| 首次运行（还没有 manifest） | 只建立基线，**一个文件都不删** |
| 某个映射本轮扫到 0 个 strm（Alist 掉线/未挂载） | 跳过该映射，不删 |
| 单轮删除比例超过 `PRUNE_MAX_PERCENT`（默认 50%） | 跳过并告警，避免挂载抖动误删整库 |
| 容器 `find` 检查失败（docker 报错） | 不删目录，只删清单里的单个文件 |

跳过删除的路径会**保留在 manifest 里**，下一轮继续尝试，不会被遗忘。

**只管自己生成的文件**：认领依据是 strm 内容里的 URL 前缀（一定是
`PUBLIC_PREFIX/d/<alist_path>/` 开头）。媒体库里其他工具、小雅自带的 strm
URL 不匹配，一条都不会被动。

升级到 1.1.x 时若没有清单，会先扫一遍磁盘把历史文件认领回来再对账，
所以「升级前就删掉的那批」也能一次性清干净。

| 变量 | 说明 | 默认 |
|---|---|---|
| `PRUNE_ENABLED` | 是否开启删除同步 | `true` |
| `PRUNE_MAX_PERCENT` | 单轮允许删除的最大比例（%），超了跳过 | `50` |
| `PRUNE_GUARD_MIN` | 比例保护的最小生效数量：删除数小于它时不受比例限制（避免 1 集的剧永远删不掉） | `5` |
| `PRUNE_DELETE_EMBY` | 同时删除 Emby 条目 | `true` |
| `PRUNE_INIT_SCAN` | 没有清单时扫磁盘、按 strm 里的 URL 前缀认领历史文件 | `true` |

要一次性大清理（比如换了一批片源），把 `PRUNE_MAX_PERCENT` 临时设成 `100`，跑完改回来。

## 用 MoviePilot 整理 115 存量：联动操作手册

MoviePilot v3 能直接读写 115（`type: u115`），可以把 `/影视库/电视剧` 下命名混乱的资源
原地重命名成 `剧名 (年份)/Season 01/剧名 - S01E01.mkv`。整理完 strm-gen 会自动跟随，
但**顺序错了会留下一堆僵尸条目**，所以按下面走。

先加一条目录配置（设定 → 目录），字段是关键：

| 字段 | 值 | 说明 |
|---|---|---|
| 源存储 / 源目录 | 115网盘 · `/影视库/电视剧` | 115 存储的根挂在 `/115` 这一层，路径别写成 `/115/影视库` |
| 目标存储 / 媒体库目录 | 115网盘 · `/影视库/电视剧` | 与源相同 = 原地整理，MAPPINGS 不用改 |
| 整理方式 | `move` | 115→115 是服务端移动，秒级；选 copy 会双倍占空间 |
| 覆盖模式 | `never` | 首次整理用最保守的 |
| 刮削 | 关 | 刮削会把 nfo/jpg 写进 115，而 Emby 读的是本地 strm 目录，看不到 |

然后 `文件管理 → 115网盘 → /影视库/电视剧 → 选中剧目 → 整理`，先看预览再提交。
**只选一部剧、别选整个电视剧目录**——整理范围越大，下面这个坑越深。

### 坑：改文件名 = strm 全量重建，会被安全阀拦住

整理会把 `The.Body.2026.S01E01.Slutty.God...mkv` 改成 `青春共体 - S01E01.mkv`，
于是所有 strm 的**目标路径全变了**。下一轮同步会认为「旧的全没了、全是新的」：

```
[PRUNE] /media/每日更新/电视剧 待删 68/88（77%）超过阈值 50%，本轮跳过
```

安全阀按设计拦住了，结果就是**新旧两套 strm 并存**（8 集的剧变 16 个文件，
60 集的剧变 122 个），Emby 里集数翻倍、点开是同一集。这不是 bug，是保护生效后
「旧的没删掉、新的又加进来」的必然结果。

正确做法（整理前做，一次到位）：

```bash
# 1) 停同步，避免整理过程中被扫到半成品
docker compose stop strm-gen

# 2) 备份并清空该映射的输出目录，同时删掉清单里这个 key
mv strmgen-out/media/每日更新/电视剧 /tmp/tv_backup_$(date +%s)
# 编辑 strmgen-out/.strm_manifest.json，删掉 "/media/每日更新/电视剧" 这一项

# 3) 删掉 Emby 里对应剧的 Series（会连空目录一起清），让它随重建重新生成
#    DELETE /emby/Items/{id}  —— 注意这个接口会连同磁盘文件一起删

# 4) 起同步，跑一轮全量重建
docker compose start strm-gen
```

实测一部 8 集的剧，整理 + 重建约 3 分钟：strm 从 `青春共体 (2026) - S01E01 -
青春共体 - S01E01.strm` 变成 `青春共体 (2026) - S01E01.strm`，Emby 重建出干净的
Series（8 集 + 海报 + TMDb/IMDb/TVDB 三件套）。

顺带一个收益：重建会顺手清掉历史上「同一路径多条重复条目」的僵尸——实测
《伟大的长征》从 3 个 Series / 123 条条目收敛成 1 个 / 62 集。

### 命名格式在 `/config/app.env`，不在数据库

改 `设定 → 重命名` 之类的地方没反应时，看 `/config/app.env` 的 `TV_RENAME_FORMAT`。
它的优先级高于 `app.py` 里的默认值、也不在 `systemconfig` 表里。干净的写法：

```
TV_RENAME_FORMAT='{{title}}{% if year %} ({{year}}){% endif %}/Season {{(season|string).zfill(2)}}/{{title}} - {{season_episode}}{{fileExt}}'
```

默认模板会带 `{tmdbid=291507}` 后缀和一串 `1080p.Netflix.WEB-DL.H264.Atmos DDP 5.1`，
Emby 会把整串当剧名显示。改完要重启 MoviePilot 才生效。

### v1.1.9：规范命名后 strm 文件名重复

用 MoviePilot 把源文件整理成 `青春共体 - S01E01.mkv` 之后，生成的 strm 变成了
`青春共体 (2026) - S01E01 - 青春共体 - S01E01.strm`——剧名和集号各出现两次。

原因是 `_tidy_ep_title()` 剥离剧名时用的是带年份的 `series`（`青春共体 (2026)`），
而文件名里的剧名通常不带年份（`青春共体 - S01E01`），`startswith` 匹配不上，
整段就被当成「单集标题」保留了下来。修法是比对前先把 series 的年份括号去掉，
再补一条「剥离后只剩 `SxxEyy` 就整段丢弃」的规则；顺带修了季集正则不认两位补零
（`E01` 只写 `1` 匹配不到）的问题。

### v1.1.8：兜底集号会吃掉真实第一集

有些资源里混着「整季合集」「花絮」这类文件名里根本没集号的视频（例如
`伟大的长征[全60集].The.Great.Long.March.S01.2026.2160p.mp4`）。这类文件解析不出集号时，
代码会按出现顺序兜底编号，而兜底计数器**从 E1 开始**——于是它直接撞上真正的第一集，
把 `S01E01.strm` 覆盖成那个合集文件的直链，用户点开第 1 集播的是整季合集。

修法是把它挪到第二遍：先让全剧所有能解析出集号的文件走完，拿到「已用最大集号」，
挂起的未识别文件再从它 +1 往后排。实测《伟大的长征》60 集 + 2 个合集文件 → E61/E62，
E01/E02 保持真实集不被覆盖。

### v1.1.7：这条链路真正跑通前修掉的 4 个坑

v1.1.0 只写了逻辑、没做端到端验证，实际上有四个缺陷会让它「看着在工作、其实没删」：

| 坑 | 现象 | 修法 |
|---|---|---|
| 只删 Emby 侧，不删 `/out` 里的副本 | 媒体库删干净了，下一轮同步又原样推回去，像「删了又自己长回来」 | 删除时同时清 `/out`，保证「本地内容 == 本轮有效集合」 |
| 季目录反查不到 Series | `剧名/Season 2/x.strm` 的父目录是季目录，而 Series 的 Path 是剧目录，映射查不到 → 分集条目永远删不掉 | 季目录先上提一层再匹配；电影改用 strm 文件路径本身精确命中 |
| 每轮 `rmtree` 清空 `/out` | 所有 strm 的 mtime 被刷新，Emby 把每个文件都当新文件重新探测远程直链（单集 1~10 秒），扫描长期跑不完 | 不再清空，内容相同就不重写，mtime 保持 |
| 孤儿清理绕过安全阀 | manifest 外的残留文件被单独删掉，但容器和 Emby 条目还在 → 媒体库里留个点开报错的空壳 | 孤儿并入删除对账，走同一套「本地 + 容器 + 条目」流程和同一个安全阀 |
| 先删目录、后删条目 | Emby 删除 Series 时会往剧目录回写 `tvshow.nfo`，排队中的扫描还会把条目重建一遍 → 留下空剧壳 | 改成「先删条目、再删目录」，并加延迟复核（隔 4 秒查两轮） |

另外移除了 `EMBY_NEW_SCAN_*`。它原本用 `POST /Library/Refresh?Path=xxx` 给新剧单独扫描，
但实测该接口**忽略 Path 参数、直接启动全库扫描**（几小时，期间还会因挂载抖动批量误删条目，
是真实事故的根源），而且它一旦有排队就直接 `return`，把正常的 `ValidationOnly` 快速入库**整个跳过**
——这就是「115 里有 8 集、媒体库一集都没有」的直接原因。
现已确认 `ValidationOnly` 本身就能为新目录建出 Series 与分集（5 万文件的库约 60 秒建壳、120 秒出集），
不需要额外扫描。

**覆盖率修正**：比例保护改成「数量 + 比例」双条件（`PRUNE_GUARD_MIN`，默认 5）。
原来只按比例，导致「只有 1 集的剧/单片电影」删除时 100% > 50% 被永久拦住。

**实测记录一**（隔离测试剧，造 2 集 → 入库 → 模拟源删除）：

```
[1] 造 2 个 strm                文件数 2
[2] ValidationOnly 扫描         64s 建 Series，120s 出 2 集
[4] 删 strm                     2/2
[5] 清空目录                    TDIR 已不存在
[6] 删 Emby 条目                 剧删除 1（级联清分集）
[7] 验证                        测试剧 Series=0；黑色五叶草仍 8 集未受影响
```

**实测记录二**（走真实入口 `gen_strm.py`，模拟「115 上删了一部剧」）：

```
[3] manifest[电视剧] 89 条，含待删项
[4] [PRUNE] 源已删除 1 个（1.1%），开始清理
    [PRUNE]   删除本地 strm 1/1
    [PRUNE]   删除 strm 文件 1/1
    [PRUNE]   清理空季 .../__prune4__/Season 1
    [PRUNE]   清理空目录 .../__prune4__
    [PRUNE]   删除 Emby 条目：分集/电影 1 个，空剧/空片 1 个
    [SYNC] 91 strm -> emby:/   [SYNC] 抽查 4 个文件均已就位
[5] 本地文件还在 False | 容器目录还在 False | Emby Series 剩余 0 | 黑色五叶草 集数 8
```

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
