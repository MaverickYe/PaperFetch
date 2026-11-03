# GitHub 上传清单

## 1. 上传源码仓库

上传当前项目根目录中的源码、`docs/`、`.github/`、`literature_downloader/`、`README.md`、`LICENSE`、`NOTICE.md`、`CHANGELOG.md`、`WINDOWS_VERIFY.md` 和 `verify_windows.bat`。

不要上传 `PaperFetch-Ready/`、`GitHub-Release-v1.1.1/`、数据库、PDF、浏览器配置、锁文件或 Python 虚拟环境；`.gitignore` 已经排除这些本地/发布目录。

## 2. 创建 Release

建议使用标签 `v1.1.1` 创建 GitHub Release，并上传 `GitHub-Release-v1.1.1/` 中的两个附件：

- `paperfetch_1.1.1_amd64.deb`
- `PaperFetch-Windows-v1.1.1.zip`

## 3. 用户安装方式

Ubuntu/Debian 64 位：

```bash
sudo apt install ./paperfetch_1.1.1_amd64.deb
```

Windows 10/11 64 位：解压 `PaperFetch-Windows-v1.1.1.zip`，保留整个 `PaperFetch-Ready` 文件夹，双击 `PaperFetch.exe`。

## 4. Actions

推送 `v1.1.1` 标签后，`Build Windows executable` 工作流会在 Windows runner 上重新构建目录版 Windows 包并执行冒烟测试。当前本机已验证的 ZIP 仍可直接作为 Release 附件。
