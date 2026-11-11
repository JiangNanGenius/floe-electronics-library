# Floe Electronics Library

独立维护的二维电子元件库生产仓库：把 KiCad、SparkFun、Espressif 和 LibrePCB
库转换成统一、可追溯的交换数据，自动生成完整离线包和按上游划分的下载包。
二维数据可完整下载；三维提供固定版本、路径和 Git 源对象校验信息的目录，供 AI／客户端
逐个下载。CI 不批量下载模型文件。模型的位置、缩放和旋转引用保留在元件记录中。

This repository produces reproducible, attributed 2D electronics data. Its
normalized exchange format is separate from Floe's current native library format;
**App installation and rendering of this new feed are not integrated yet**.

## 自动维护

GitHub Actions 每天 03:23 UTC 检查一次，也可手动运行
**Refresh and publish 2D libraries**。流程是：

1. 检查白名单来源：KiCad 的符号与封装选择同一稳定版本；SparkFun、Espressif
   选择稳定标签；LibrePCB 跟踪配置的正式分支。每次都固定到真实提交 SHA。
2. 通过稀疏检出获取二维文件和许可证；不运行上游代码，不下载三维模型。
3. 核对许可证文本摘要。条款变化会停止候选发布，保留上一版。
4. 转换并保留元件身份、来源、几何、引脚／焊盘、依赖和兼容状态。
   未支持内容明确标记，保留原件；不能静默丢掉后计为可用。
5. 生成有大小与 SHA-256 的搜索索引、分块记录、原件和资源包。
6. 与上一版比较，来源丢失或二维记录／已转换记录／模型目录减少超过 5% 时停止自动发布。
7. 在草稿 Release 上传、下载核验，再公开并独立读取公共下载字节。
   已发布版本不覆盖；没有变化时不重复发布。

The build job is read-only. Only the separate publication job receives
`contents: write`; it publishes data releases in this repository. No App code,
TestFlight, App Store, existing content feed, signing key, or device is changed.

## 来源和许可证

[`upstreams.json`](upstreams.json) 当前列出 52 个启用来源：KiCad 符号与封装、
SparkFun、Espressif，以及 LibrePCB-Libraries 中现有的 48 个 `.lplib` 仓库。
新来源需要显式加入清单；不会自动信任后来出现的任意仓库。
其中部分上游目前只有库说明或分类，没有实际元件；目录会明确标为 `empty-upstream`，
保留其来源而不会虚构元件数量。

- 维护工具：**MPL-2.0**，见 [LICENSE](LICENSE)。
- KiCad／Espressif 数据与相应转换：**CC BY-SA 4.0 + KiCad 设计例外**。
- SparkFun 数据：按其 README 声明的 **CC BY 4.0** 保留来源及归属。
- LibrePCB 官方组织库：**CC0-1.0**，逐库核验许可证。
- Würth：保留发现入口，**未启用自动再分发**；自定义条款不能按开放库推定授权。

仓库根目录的软件许可不替换第三方数据许可。不同来源独立打包，元件记录绑定原始文件，
包内保留许可全文、署名和修改说明。转换格式并不免除原有许可义务。
详见 [LICENSES.md](LICENSES.md)。

## 本地运行

需要 Python **3.12+** 和 Git；生产流程仅使用 Python 标准库。macOS 如默认
`python3` 仍为系统 3.9，可显式使用 `/opt/homebrew/bin/python3.12`。

```sh
python3.12 -m unittest discover -s tests/normalizers -v
python3.12 -m unittest discover -s tests/pipeline -v

# 使用仓库保留的固定来源快照复现
python3.12 tools/library.py fetch
python3.12 tools/library.py build --output dist
python3.12 tools/library.py verify --output dist
python3.12 tools/library.py bundle --output dist --release-dir release

# 解析新的候选；不会修改已有 lock 或发布物
python3.12 tools/library.py resolve --lock candidate.lock.json
python3.12 tools/library.py fetch --lock candidate.lock.json
```

`upstreams.lock.json` 是首次交付保留的复现基线。每个自动发布版本都附有它自己的
`upstreams.lock.json`，代表该版本实际使用的来源。输出目录必须为空；工具拒绝覆盖旧产物。

## 下载与接入

[Releases](https://github.com/JiangNanGenius/floe-electronics-library/releases) 提供：

- `floe-electronics-all-2d.zip`：一次下载全部二维库。
- `<source-id>-2d.zip`：只下载一个上游库。
- `catalog.json`、`manifest.json`、`upstreams.lock.json`：目录、分块、大小、摘要与来源。
- `models.jsonl.gz`：三维模型目录；模型文件在使用时单独下载，不包含在完整二维包中。
- `release.json`、`release-SHA256SUMS`：外层下载资产的绑定和独立校验。

完整数据包含 `indexes/`、`records/`、`inventory/`、`originals/`。搜索索引指向具体分块、
行和记录摘要；客户端可以全量保存二维数据，再只读取当前使用的记录。
`converted` 说明交换格式转换状态，**不表示已在 Floe 原生编辑器验收可放置**。
格式与未支持内容见 [docs/FORMAT.md](docs/FORMAT.md)。
三维下载标识和校验方式见 [docs/MODELS.md](docs/MODELS.md)。

App 的目录与下载消费者可使用这套交换数据；原生元件安装和三维渲染需要各自的格式适配与
兼容性验证。消费者应复用受信任的资源更新、版本固定和原子提交机制。
已有项目的元件快照保留其固定版本，数据源更新不会自动替换它们。
