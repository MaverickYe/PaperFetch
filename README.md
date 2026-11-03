# PaperFetch（文献快下）

本地运行的 DOI 文献下载工具：导入 Excel/CSV/TSV/文本 DOI 清单，自动获取标题和开放获取 PDF；无法自动下载的论文会保留多个手动入口。

## 版本与平台

- **Ubuntu/Debian：** `amd64`，推荐 Ubuntu 22.04 或 24.04。
- **Windows：** Windows 10/11 `x64`。`.exe` 由 Windows 或 GitHub Actions 构建。
- **源码构建：** 推荐 Python 3.12；需要 `requests`、`openpyxl`、`PyMuPDF` 和 `playwright`。
- **浏览器：** 只在机构登录、验证码或人工下载阶段使用 Chrome/Chromium/Edge；程序不会绕过访问控制。

## Ubuntu 安装

从 GitHub Releases 下载 `paperfetch_1.1.1_amd64.deb`，在下载目录执行：

```bash
sudo apt install ./paperfetch_1.1.1_amd64.deb
```

随后从应用菜单启动“文献快下”。卸载：

```bash
sudo apt remove paperfetch
```

卸载不会自动删除文献数据库和 PDF；如需清空数据，请先在软件中确认保存位置，再手动删除对应文件夹。

## Windows 使用

从 GitHub Releases 下载 `PaperFetch-Windows-v1.1.1-GitHub.zip`，解压到普通可写目录，双击 `PaperFetch-Ready/PaperFetch.exe`。必须保留同目录下的 `_internal` 文件夹；不需要 Python 或 BAT 文件。程序会自动打开 Windows 默认浏览器。若 Windows Defender SmartScreen 首次拦截，请确认文件来自你信任的发布页后再运行。

## GitHub Release 附件

- Ubuntu/Debian 64 位：`paperfetch_1.1.1_amd64.deb`，安装命令为 `sudo apt install ./paperfetch_1.1.1_amd64.deb`。
- Windows 10/11 64 位：`PaperFetch-Windows-v1.1.1.zip`，解压后双击 `PaperFetch-Ready/PaperFetch.exe`。

源码仓库不要提交 `PaperFetch-Ready/`、`data/`、下载的 PDF、浏览器配置或运行锁文件；它们只应作为 Release 附件或本地运行数据存在。

## 从源码运行

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r literature_downloader/requirements.txt
PAPERFETCH_PYTHON="$PWD/.venv/bin/python" bash literature_downloader/启动文献快下.sh
```

构建 Ubuntu `.deb`：

```bash
.venv/bin/python -m pip install -r literature_downloader/build-requirements.txt
bash literature_downloader/构建Ubuntu版本.sh
bash literature_downloader/构建Ubuntu安装包.sh
```

Windows 构建请在 Windows 中运行 `powershell -ExecutionPolicy Bypass -File literature_downloader/build_windows_release.ps1`，或使用仓库中的 GitHub Actions。构建结果位于 `dist/PaperFetch/`。

## 基本使用

1. 上传 DOI 表，或直接输入 DOI。
2. 勾选需要处理的论文，可按序号范围选择。
3. 需要时点击“获取标题”，标题查询不会阻塞下载。
4. 先启动自动下载；未完成项目会保留多个手动下载链接。
5. 手动下载完成后点击“标记已完成”，论文会移动到“已完成”。
6. 可随时暂停、继续或结束批次；刷新或重新打开软件后会恢复本地进度。

## 数据与隐私

程序只监听本机 `127.0.0.1`，不需要云端账户。冻结版本的数据默认保存在：

- Ubuntu：`~/.local/share/paperfetch/`
- Windows：`%LOCALAPPDATA%\\PaperFetch\\`

网页界面不会显示用户名、上传文件名或绝对路径。不要把 `data/`、下载的 PDF、浏览器登录配置或个人导出清单提交到公开仓库。

## 许可证与限制

代码采用 MIT License。程序不破解付费墙，不绕过密码、验证码、多因素认证或机构权限；请遵守学校、出版社和数据源的使用条款。

详细说明见 [`docs/使用说明_CN.md`](docs/使用说明_CN.md)、[`docs/安装与部署.md`](docs/安装与部署.md) 和 [`docs/版本兼容性.md`](docs/版本兼容性.md)。
