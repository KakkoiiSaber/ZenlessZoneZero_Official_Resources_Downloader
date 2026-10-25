# NAS Docker 下载器

GitHub Actions 继续生成三个 metafile；NAS 只读取 `wiki.json` 中已经选好的链接，
按 Wiki 目录下载图片和视频。NAS 主动向 GitHub 和媒体 CDN 发起请求，
不用开放端口、WebDAV 或提供 NAS 登录密码。仓库目前保持 public，不需要 GitHub token。

## 首次部署

NAS 需要支持 Linux 容器，并安装 Docker Compose（群晖对应 Container Manager 的“项目”）。
镜像在 NAS 本机构建，没有预先发布到镜像仓库；Python 和依赖安装在镜像里，NAS 不用安装 Python。

1. 把 **dev 分支**的代码放到 NAS，例如 `/volume1/docker/zzz-downloader/app`。
   可以在 GitHub 切换到 dev 后下载 ZIP、解压上传；不需要下载历史媒体。
   实际构建只用到 `Dockerfile`、`requirements-download.txt` 和 `src/download.py`，
   部署还需要 `compose.yaml` 和 `.env.example`。
2. 建立下载目录和状态目录，例如：

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
   CHECK_INTERVAL=3600
   VERIFY_DAYS=7
   MANIFEST_URL=https://raw.githubusercontent.com/KakkoiiSaber/zzz_scraper/dev/metafiles/wiki.json
   ```

4. 在项目目录执行：

   ```sh
   docker compose build --pull
   # 先验证清单，显示资源数量，不下载媒体。
   docker compose run --rm downloader --dry-run
   # 启动：立即下载一轮，此后每小时检查。
   docker compose up -d
   docker compose logs -f --tail=100 downloader
   ```

   群晖可以在 Container Manager → 项目 → 新增，选择含 `compose.yaml` 和 `.env`
   的目录后构建并启动；其他 NAS 使用对应的 Compose/项目功能。
   如界面没有终端入口，验证与日志也可以通过 SSH 执行上面的命令。
   Compose 的 `build: .` 必须能访问同目录的代码，不能只粘贴 YAML 而缺少源文件。

首次会下载清单中的全部资源，可能占用较多容量。默认串行下载，避免大量并发。
容器无需特权模式，不挂载 Docker socket，仅挂载这两个目录；使用填写的普通用户权限，
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
使用新链接继续。NAS 不解析网页，也不会自行运行抓取程序。
尚在 dev 分支时，GitHub Actions 的定时工作流不会仅因文件存在于 dev 就自动运行；
请到 Actions → `Update metadata - incremental source crawl`（`update_metadata.yml`）→ Run workflow，选择 dev 刷新。
默认只扫描最新帖子，整页均为已知帖时停止；旧正文不按年龄批量刷新。
最终选用的视频需要续期时仍定点刷新来源帖。手动运行可勾选 `full_scan` 核对全部历史索引。
正式合入默认分支后，再将 `MANIFEST_URL` 调整到对应分支，使用每日定时更新。

每个资源最多尝试三次；单个失败不阻塞其他资源，下轮仍会重试。
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
docker compose run --rm downloader --once

# 更新应用代码后重新构建、启动；.env、下载与状态目录保留
docker compose build --pull
docker compose up -d
```

更新代码需要重新下载 dev 的代码或执行 `git pull`，但不要覆盖自己的 `.env`。
更改 `.env` 后执行 `docker compose up -d` 重新创建服务，单纯 restart 不应用配置变化。
不要删除状态目录，否则已有文件失去归属记录，程序会拒绝覆盖同名文件。
备份时先停止容器，再一起备份下载目录和状态目录；迁移时保持这两者配套。
不要让两个不同状态目录的实例共享同一个下载目录。

如果下载报权限错误，检查 `.env` 的 UID/GID 和 NAS 目录 ACL；
如果连接 GitHub 失败，检查 NAS 的 DNS、代理和网络能否访问 `raw.githubusercontent.com`。
容器日志只记录资源 ID、动作和错误类型，不打印带签名的完整链接。

## 不用 Docker 的本地验证

```sh
pip install -r requirements-download.txt
python -m src.download --manifest-file metafiles/wiki.json --dry-run
python -m src.download --download-dir ./nas-data/downloads --state-dir ./nas-data/state --once
python -m unittest discover -s tests -v
```

实现遵循 [HTTP Range / If-Range 语义](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.5)，
目录持久化使用 [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。
