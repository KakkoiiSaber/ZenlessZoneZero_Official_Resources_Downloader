# NAS Docker 下载器

GitHub Actions 继续生成三个 metafile；NAS 只读取 `wiki.json` 中已经选好的链接，
按 Wiki 目录下载图片和视频。NAS 主动向 GitHub 和媒体 CDN 发起请求，
不用开放端口、WebDAV 或提供 NAS 登录密码。仓库目前保持 public，不需要 GitHub token。

## 首次部署

NAS 需要支持 Linux 容器，并安装 Docker Compose（群晖对应 Container Manager 的“项目”）。
镜像在 NAS 本机构建，只安装 Python 和下载依赖，不包含项目 Python 源码。
仓库的 `src/` 在运行时只读挂载进容器，NAS 不用安装 Python。
个人 UID/GID、目录等只保存在本地 `.env`，已被 Git 忽略；GitHub 只保存通用模板。

1. 确认 NAS 的 `git --version` 可用，在选好的项目父目录 clone main：

   ```sh
   git clone --depth 1 --branch main https://github.com/KakkoiiSaber/ZenlessZoneZero_Official_Resources_Downloader.git
   cd ZenlessZoneZero_Official_Resources_Downloader
   ```

   目标目录须不存在或为空。已有手动上传目录的迁移见后文，不要直接覆盖。
   浅克隆仅获取当前版本，避免下载旧媒体历史；代码更新无需 SSH 密钥或 GitHub token。
2. 默认使用仓库内 `downloads/` 和 `state/`，只提交 `.gitkeep` 占位；实际内容由 Git 忽略。
   也可以在 `.env` 指向已有的 NAS 下载和状态目录，例如：

   ```text
   /volume1/Media/ZZZ
   /volume1/docker/zzz-downloader/state
   ```

3. 将 `.env.example` 复制为 `.env`，填写实际路径、运行用户的 UID/GID。
   可以使用现有普通 NAS 用户，不需要专门创建 WebDAV 账户。
   SSH 下 `id 用户名` 可以查询数字 UID 和 GID；通过 NAS 权限设置，
   给这个用户两个目录的读写权限。不要直接照抄示例中的 1000。

   ```dotenv
   NAS_UID=你的用户数字UID
   NAS_GID=你的用户数字GID
   DOWNLOAD_PATH=/volume1/Media/ZZZ
   STATE_PATH=/volume1/docker/zzz-downloader/state
   VERIFY_DAYS=7
   MANIFEST_URL=https://raw.githubusercontent.com/KakkoiiSaber/ZenlessZoneZero_Official_Resources_Downloader/main/metafiles/wiki.json
   ```

4. 在项目目录执行：

   ```sh
   docker compose build --pull
   # 先验证清单，显示资源数量，不下载媒体。
   docker compose run --rm downloader --dry-run
   # 首次执行：增量下载一轮，完成后退出并返回容器退出码。
   sudo /bin/sh ./run-downloader.sh
   ```

   群晖可以在 Container Manager → 项目 → 新增，选择含 `compose.yaml` 和 `.env`
   的目录后构建并启动；其他 NAS 使用对应的 Compose/项目功能。
   部分群晖版本的路径选择器不显示 `home/homes`；项目放在个人目录时，
   可以通过 NAS 终端在该目录执行 Compose，之后到 Container Manager 的容器列表查看状态和日志。
   如界面没有终端入口，验证与日志也可以通过 SSH 执行上面的命令。
   `compose.yaml` 和 `run-downloader.sh` 保持仓库原样，个人参数全部放 `.env`；
   不能只粘贴 YAML 而缺少 `src/` 和依赖文件。代码挂载是只读，容器不能修改仓库源码。
   项目文件夹保留仓库原名。Compose 默认项目名为 `zenlesszonezero_official_resources_downloader`，
   因 Docker 项目名只允许小写；如需自定义，只在本地 `.env` 设置 `COMPOSE_PROJECT_NAME`。

首次会下载清单中的全部资源，可能占用较多容量。默认串行下载，避免大量并发。
首次部署测试可在项目的 `command` 中加入重复的 `--resource-id 资源ID`，
只处理明确选定的少量资源；不存在的 ID 会报错，不会回退成下载全部。
配合 `--rate-limit 262144` 限速为每秒 256 KiB，方便在日志显示视频开始后手动停止、
重新启动，核对 `validated HTTP 206; continuing at ...` 续传日志中的非零进度。
测试和正式项目都使用 `restart: "no"`；每次完成后退出，由 NAS 任务计划决定下一次何时运行。
容器无需特权模式，不挂载 Docker socket，仅挂载只读源码和下载、状态目录；使用填写的普通用户权限，
容器根文件系统只读。下载目录最好专门用于本项目，避免混入手工保存的同名文件。

## 增量、续传和本地记录

本地状态保存在 `STATE_PATH/downloads.sqlite3`，包含资源 ID、版本指纹、归档路径、
大小、SHA-256、HTTP 校验值、完成状态和检查时间。不提交 GitHub，不增加第四份业务 metafile。
SQLite 和下载目录应位于 NAS 本机存储，不要通过 SMB/NFS 挂载状态数据库。

| 情况 | 行为 |
| --- | --- |
| 新资源或最终 URL、已知大小、ETag 变化 | 下载新版本，通过校验后替换本程序管理的文件 |
| 已完成且本地文件存在、大小一致 | 跳过，不请求媒体；到复查周期再检查 |
| 米游社签名 CDN 只有 `auth_key` 续期 | 不改变版本指纹，不重复下载；请求时仍使用完整链接 |
| 本地文件缺失或大小错误 | 重新下载；多目录资源优先用已有完整副本修复 |
| 只改目录或文件名 | 使用已有文件归档，完成后清理该资源的旧位置，不重新请求媒体 |
| 清单不再列出某项 | 保留本地文件，不自动删除 |
| 同名文件不是本程序管理的 | 报错并保留原文件，不覆盖 |

未完成数据放在下载目录的隐藏文件夹 `.zzz-partials/`。如果服务器提供强 ETag，
下一次会使用 `Range` 和 `If-Range` 从已落盘的位置续传，并核对 `206 Content-Range`、
ETag 和总大小。服务器忽略 Range 或文件版本改变时，从头写临时文件，避免错误拼接。
没有强 ETag 时安全地重新下载；不能保证所有 CDN 都支持续传。
下载完成后计算 SHA-256，再将临时文件原子改名为正式文件。
SHA-256 用于后续本地完整性复查，并不是上游提供的可信校验和。

默认每七天对已完成资源复查本地 SHA-256，并通过 HEAD 比较远端大小、ETag 或 Last-Modified。
HEAD 不支持时只检查本地完整性；远端缺少校验值时，无法保证发现同大小的内容替换。
复查会读取本地文件，大视频较多时可适当增大 `VERIFY_DAYS`。
`VERIFY_DAYS=0` 表示每轮复查，不是关闭复查。

过期视频链接不会盲目下载；日志提示刷新 GitHub 清单。下载器下一轮会重新读取清单，
下次任务使用新链接继续。NAS 不解析网页，也不会自行运行抓取程序。
GitHub Actions 的定时工作流运行在默认 main 分支；也可到 Actions →
`Update metadata - incremental source crawl`（`update_metadata.yml`）→ Run workflow，选择 main 手动刷新。
默认只扫描最新帖子，整页均为已知帖时停止；旧正文不按年龄批量刷新。
最终选用的视频需要续期时仍定点刷新来源帖。手动运行可勾选 `full_scan` 核对全部历史索引。
正式 NAS 配置读取 main 清单；dev 仅供后续开发和测试。

每个资源最多尝试三次；单个失败不阻塞其他资源，下次任务仍会重试。
清单抓取或校验失败时跳过该轮，不修改现有下载文件。
同一状态目录只允许一个下载器运行，避免同时写库或拼接同一临时文件。

## 日常操作

```sh
# 查看状态与日志
docker compose ps
docker compose logs --tail=100 downloader

# 停止；下载和状态保留
docker compose down

# 只执行一轮（先停后台实例，避免锁冲突）
docker compose up --no-build --abort-on-container-exit --exit-code-from downloader

# 日常代码更新：等当前任务结束后同步，无需构建镜像
git pull --ff-only origin main

# 仅 requirements-download.txt 或 Dockerfile 变化时重建运行环境
docker compose build --pull
```

日常只需 `git pull --ff-only origin main`；下一次任务启动新的 Python 进程时直接使用新代码。
不需要传文件、不需要为源码变化重新 build，也不需要立即运行 Compose。
`git pull` 不会覆盖被忽略的本地 `.env`。若修改了仓库跟踪文件导致无法快进，先检查本地改动，
不要用 `reset --hard` 抹掉配置；个人参数应迁回 `.env`。
依赖或 Dockerfile 改动时需要重新 build；Compose 配置变化会在下次任务时应用。
更改 `.env` 后，下次 `docker compose up` 会应用配置变化；单纯 restart 不应用配置变化。
不要删除状态目录，否则已有文件失去归属记录，程序会拒绝覆盖同名文件。
备份时先停止容器，再一起备份下载目录和状态目录；迁移时保持这两者配套。
不要让两个不同状态目录的实例共享同一个下载目录。

如果下载报权限错误，检查 `.env` 的 UID/GID 和 NAS 目录 ACL；
如果连接 GitHub 失败，检查 NAS 的 DNS、代理和网络能否访问 `raw.githubusercontent.com`。
容器日志只记录资源 ID、动作和错误类型，不打印带签名的完整链接。
应用日志格式为 `[yyyy-mm-dd hh:mm:ss][INFO] message`，不带毫秒。
启动后立即输出 `Downloader started`，随后报告本地记录准备、清单请求、响应接收和资源数量。
清单传输较慢时也会每隔约五秒报告读取进度。若只有 `Fetching download manifest` 而没有
`Manifest response received`，说明正在等待 HTTP 请求；若群晖界面为空，先用
`docker compose logs --tail=100 downloader` 核对终端日志，区分界面显示和程序等待。
文件大小自动使用 B、KB、MB、GB 或 TB（每级 1000），已知总大小时显示进度百分比，例如
`progress 125.00 MB / 500.00 MB (25.0%)`。续传百分比包含之前已经下载的部分，
成功接收并核对完整文件后输出 `100.0%`；总大小未知时显示 `total unknown`。
时间使用容器本地时区；当前镜像默认通常为 UTC。
终端查看时可用 `docker compose logs --no-log-prefix -f --tail=100 downloader`
隐藏 Compose 额外添加的容器名前缀。

## 从小样测试切换正式运行

1. 停止旧测试项目，保留宿主机下载目录与状态目录。`docker compose down` 仅移除
   该项目容器和网络；不要删除实际的挂载目录。
2. 正式 Compose 使用相同 UID/GID、相同下载及状态挂载路径，配置
   `restart: "no"`，去掉测试 `command` 和旧 `CHECK_INTERVAL`。
   不再传入 `--once`、`--resource-id`、`--rate-limit` 或 `--retries 1`；默认无资源筛选、
   不限速、失败最多重试三次。
3. 正式项目每次读取完整清单，跳过已经完成的小样资源，下载剩余资源，然后退出。
   不在容器内部等待，不配置何时启动；NAS 重启后也不会由 Docker 自动重新运行。
4. `data` 内媒体、`.zzz-partials` 和 `state/downloads.sqlite3` 都应保留。
   独立续传测试产生的 `data/.resume-test`、`state/resume-test` 可在测试容器退出后删除，
   它们与正式记录分开。旧测试项目文件夹可以暂留作回退，确认正式服务正常后再删除。
5. 旧测试配置如果仍读取 `/dev/metafiles/wiki.json`，将其改为 `/main/metafiles/wiki.json`。
   下次任务运行时 Compose 会应用环境变量变化；媒体目录和下载记录保持原样。

旧版将代码打进镜像、使用个人 `docker-compose.yml` 的部署，需要一次性迁移：
先停用任务计划并等当前任务完成，停止旧 Compose 项目；保留旧项目目录作备份，
在新目录 clone main，将原 UID/GID、下载和状态绝对路径填写到新目录的 `.env`，
首次 build 依赖镜像，并将任务计划指向新仓库的 `run-downloader.sh`。
原下载目录、SQLite 记录均不迁移或清空；测试成功后再启用任务计划。

## 群晖每日任务计划

先在新仓库目录构建一次镜像：`sudo docker compose --env-file .env -f compose.yaml build`。
计划任务只运行现有镜像，不每天构建或自动升级程序。

DSM 控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本：

- 名称：`ZZZ Downloader`。
- 运行用户：`root`，用于操作 Docker 服务；容器内部仍按 Compose 的普通 UID/GID 写入文件。
- 计划：每天一次，具体钟点由用户选择，按 DSM 时区执行。
- 任务设置：运行 Compose，保持前台等待，以便 DSM 记录完成状态和退出码。

脚本示例（项目路径替换为实际值，脚本会定位 Docker，并显式读取同目录 `.env`）：

```sh
/bin/sh '/实际项目路径/run-downloader.sh'
```

不要使用 `up -d` 或仅 `start` 作为计划任务：前者立即返回，后者不能直接反映本次下载结果。
这里使用同一个项目和服务容器，日志保留在 Container Manager 中；正常完成后容器显示停止是预期行为。
任务计划的“设置”可启用“保存输出结果”，再通过“操作 → 查看结果”查看本次输出。
创建后先选中任务手动“运行”一次，确认下载和退出状态正常，再启用每日计划。
同一下载记录目录具有进程锁；不要另建第二个下载项目同时运行。

## 不用 Docker 的本地验证

```sh
pip install -r requirements-download.txt
python -m src.download --manifest-file metafiles/wiki.json --dry-run
python -m src.download --download-dir ./nas-data/downloads --state-dir ./nas-data/state
python -m unittest discover -s tests -v
```

实现遵循 [HTTP Range / If-Range 语义](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.5)，
目录持久化使用 [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。
