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
| `MAPPINGS` | 目录映射 JSON 数组（见下） | 必填 |
| `CONFIG_FILE` | 可选：挂载 config.json 并用此变量指向，代替环境变量 | 空 |

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
