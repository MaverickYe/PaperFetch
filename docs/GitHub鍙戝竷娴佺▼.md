# GitHub 发布流程

## 建议的仓库内容

解压 `PaperFetch-v1.1.1-source.zip`，把其中的文件和目录上传到 GitHub 仓库根目录。仓库根目录应能直接看到：

```text
README.md
LICENSE
literature_downloader/
docs/
.github/workflows/build-windows.yml
```

不要上传数据库、PDF、日志、浏览器配置、个人 Excel 或项目中的实验数据库。

## 发布 Ubuntu 和 Windows 文件

在 GitHub 新建 Release，标签使用 `v1.1.1`，上传：

1. `paperfetch_1.1.1_amd64.deb`；
2. `PaperFetch-v1.1.1-source.zip`；
3. GitHub Actions 构建完成后的 `PaperFetch-Windows.zip`。

推送 `v1.1.1` 标签后，`.github/workflows/build-windows.yml` 会自动构建 Windows 版本并运行冒烟测试。也可以在仓库的 Actions 页面手动运行 `Build Windows executable`。

## 本地校验

发布前使用 `PaperFetch-v1.1.1-SHA256SUMS.txt` 校验下载文件：

```bash
sha256sum -c PaperFetch-v1.1.1-SHA256SUMS.txt
```
