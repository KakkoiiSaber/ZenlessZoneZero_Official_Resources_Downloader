# zzz_scraper

抓取绝区零官网、米游社官方资讯和 Wiki，生成三个 JSON metafile。
抓取程序负责元数据；独立 Docker 下载器根据 Wiki 清单在 NAS 上增量下载媒体。

## NAS 下载

使用 `Dockerfile` 和 `compose.yaml` 部署。容器每次读取 public 仓库的 `wiki.json`，
支持增量下载、服务器支持时的断点续传、Wiki 目录归档和本地 SQLite 下载记录。
无需向公网开放 NAS，也不需要 NAS 账户密码或 GitHub token。
下载器完成一轮增量下载后退出；每天何时运行由 NAS 任务计划控制，无内部定时循环或自动重启。
日志格式为 `[yyyy-mm-dd hh:mm:ss][INFO] message`。
部署步骤、权限设置、测试转正式及恢复见 [NAS Docker 部署文档](docs/NAS_DOCKER.md)。

## 输出

| 文件 | 内容 |
| --- | --- |
| `metafiles/official.json` | 官网新闻与独立视频频道、正文原图、视频直链、Minas 分享链接和公开提取码 |
| `metafiles/miyoushe.json` | 米游社帖子、正文原图、已选定的最高画质视频、Minas 分享链接和公开提取码 |
| `metafiles/wiki.json` | Wiki 分类与分组目录、原始排列顺序、关联帖子、视频匹配结果、最终选定的资源直链 |

三个文件使用相同 `snapshot_id`。所有抓取与校验成功后，才一起替换上一批 metafile。
任何详情失败都会保存进度并返回非零退出码，默认不会发布不完整清单。
旧 CSV、分离的 Minas 表和多层 catalog 不再参与流程。

## 使用

需要 Python 3.11 或以上。

```powershell
pip install -r requirements.txt
# 第一次完整重建；中断后执行相同命令即可续跑。
python -m src.scrape.collect --fresh
# 之后的增量更新。
python -m src.scrape.collect
# 手动核对全部历史索引；仍复用未变化的正文。
python -m src.scrape.collect --full-scan
# 若要定期复查旧正文，显式启用年龄刷新。
python -m src.scrape.collect --full-scan --refresh-days 7
# 将已有清单离线迁移到精简格式；不发起任何网络请求。
python -m src.scrape.collect --compact-only
# 仅重新核对同名官网视频并匹配现有清单，不抓取索引或正文。
python -m src.scrape.collect --resolve-only
# 单元测试：全部离线，下载测试使用本地 HTTP 服务。
python -m unittest discover -s tests -v
```

Windows 默认使用系统 curl 执行元数据请求；其他系统默认使用 requests。
可通过 `--transport curl` 或 `--transport requests` 显式选择。

## 增量与恢复

日常按官网两个频道、米游社各自的新到旧顺序扫描列表。每页完整检查，遇到整页均为已知帖时停止翻页，
避免旧置顶帖出现在第一条导致提前停止；翻页期间发现的所有新帖都会处理。
以前保存的历史帖子继续保留，不因这次只扫描前几页而丢失。
首次或 `--fresh` 没有历史基线时扫描到底；`--full-scan` 可手动核对历史补发、重新排序的帖子。
这种快速边界策略不保证发现插入边界之后的回填帖子，需用手动全索引检查覆盖。
米游社列表接口提供完整结构化正文、图片 ID 映射与视频清晰度，因此直接解析这些数据，
仅在结构化正文确实不完整时请求详情接口。每条记录保存
`index_signature`、`checked_at` 和 `parser_version`，未变化的帖子不重复请求详情或解析正文。
新增、索引发生变化、之前失败、解析版本变化的记录会重新抓取。
默认 `--refresh-days 0`，跳过未变化的历史正文，不再每七天批量重抓旧帖。
本次看到的列表签名变化、失败记录或解析版本升级仍会触发处理。
Wiki 总目录通过一次请求检查，只有索引变化或失败的条目重新请求详情；资源选择在本地重新计算。
选完资源后，只对 Wiki 最终使用且即将过期的视频来源帖定点续期（提前一天），
未选用的米游社视频即使过期也不请求。签名轮换不被误判为正文修改。
如需覆盖未在列表字段中反映的历史修改，可显式设置 `--refresh-days 7`。

`.cache/scrape/` 只保存本地响应缓存和中断进度，不是第四份业务 metafile，也不提交到 Git。
一次抓取中断后重跑，会复用已成功获取的响应和记录，只补做未完成部分。
`--restart` 放弃本次中断进度并开始新的检查；`--fresh` 则不复用已发布清单。
请求并发有统一速率限制，错误最多重试三次。

## Wiki 资源规则

图片使用 Wiki 正文或图片列表的资源，去掉 `x-oss-process` 图片处理参数，
保留原始文件格式，不把显示尺寸当成真实像素尺寸。

官网同时扫描新闻频道和独立视频频道，解析正文视频与 `sExt.video-url`。
视频沿着 Wiki 已关联的米游社帖子，匹配规范化标题相同的官网帖子。
保留作品类型、角色、版本等语义；重复标题只能用关联帖日期进一步消歧。
同名的官网新闻与视频频道内容会额外检查 HTTP 文件大小和强 ETag。确认是同一份资源时，
统一关联新闻页并保留等价来源。文件不同则最多读取每端各 1 MiB 的 MP4 元数据，
在标题、时长与编码一致时按分辨率、平均总码率选择版本；不能确认时仍保留歧义。
这些核对结果随清单缓存，不会每次重复请求。
近似标题只是待确认候选，不自动选用。确认关联且官网有视频时优先选官网直链；
否则使用已关联米游社视频的最高分辨率，分辨率相同时比较码率和大小。
官网抓取失败会显式记录，不会伪装成没有官网视频。

JSON 使用 schema_version 2，并按“每条记录一行”输出：去掉无意义缩进，仍可逐条查看 Git diff。
帖子 ID 使用 `posts` 的字典键；链接通过文件头的 `url_template` 生成，官网视频频道条目
另标记 `source_kind: "video"`。`parser_version` 提升到文件级，记录仅保留签名与检查时间。
成功状态、空数组、空密码、原始显示链接、图片尺寸及正文解析来源不重复写入。

帖子图片是原图 URL 数组。视频是选定版本的扁平对象，不再同时存储 `variants` 和 `selected`。
保留链接、已知大小、清晰度/码率、时长、有效期等实际影响下载或未来版本选择的数据。
重复资源核对仅保留 ETag、检查时间和必要的已验证视频参数，完整响应头及比对证据不进入清单。

`wiki.json` 每个 `entries[id]` 内保存：

- `folders`：完整且已处理 Windows 字符的目录路径表，每个目录仅出现一次。
- `resources`：最终下载资源，包含稳定 ID、`kind`、`url`、`filename` 和目录编号 `folder`。
  有多处归档位置时使用 `folders` 编号数组。目录取自该条目自己的路径表。
- `miyoushe_ids`：关联帖子 ID，替代多份重复的米游社 URL。
- `match`：官网匹配状态、选定官网 ID 和匹配方法，不保留候选列表和重复证据。

例如资源的 `folder: 0` 表示目录为 `entries[id].folders[0]`，与 `filename` 拼接后
就是完整相对文件路径；下载 URL 已经解析完成，不需要再次读取正文。
默认来源是 Wiki；官网或米游社视频额外保存 `source`、`post_id` 与 `video_id`。
大小、ETag、签名到期时间等字段仅在实际已知时存在。

Wiki 原始资源不再另存一整份 `wiki_resources`，目录树也不与资源目录重复存储。
重新匹配所需的 Wiki 图片可以从最终资源恢复；若原始 Wiki 视频被官网版本替代，
仅将那条尚未选用的资源保留为 `wiki_fallbacks`，避免以后失去备用来源。
精简文件本身保留完整增量状态和必要选择依据，不依赖本地缓存才能继续运行。

Minas 分享可能需要认证或临时下载令牌，因此只保存分享地址和公开帖中的提取码，
不把短期有效的网盘令牌当作永久资源链接。它们是辅助来源，不替代 Wiki 直接图片。米游社部分视频直链有有效期，
读取清单时应检查 `expires_at`，不可把它当成永久链接。

## GitHub Actions

`.github/workflows/update_metadata.yml`（Actions 名称：`Update metadata - incremental source crawl`）
每日 UTC 08:17（北京时间 16:17）增量更新，跳过旧正文，
也可手动触发并勾选 `full_scan` 核对全量历史索引。
运行离线测试后生成三份清单，校验成功才提交当前分支；失败保留上一批文件，
并上传中断进度。工作流进入默认分支后才会按计划运行。

## 历史资源

`downloads/` 与 `docs/` 保留历史媒体和已发布网页快照，不再由本轮程序更新。
其中有 Windows 不允许的历史路径字符（例如 `|`）。本机使用稀疏检出，
排除了 `downloads/` 与 `docs/thumbs/`；文件仍保存在 Git 历史及仓库对象中。
新清单中的目录名已处理 Windows 保留字符，文件名冲突有稳定后缀。
