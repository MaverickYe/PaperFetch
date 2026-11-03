# Windows 版本验证包

这是 PaperFetch 1.1.1 的 Windows 本机验证包。它包含完整源码、Windows 构建脚本、GitHub Actions 配置和自动冒烟测试脚本。

## 需要的环境

- Windows 10 或 Windows 11，64 位；
- Python 3.12，建议从 python.org 安装并保留 `py` 启动器（仅源码构建需要）；
- 构建时需要联网下载 Python 依赖；
- 手动机构访问阶段需要 Chrome、Chromium 或 Edge。

如果不想在 Windows 安装 Python，可以把本包上传到 GitHub，运行 Actions 自动生成 Windows ZIP。

## 一键构建和验证

解压本目录后，双击：

```text
verify_windows.bat
```

脚本会依次：

1. 检查 Python 3.12；
2. 安装运行和构建依赖；
3. 生成 `dist\\PaperFetch\\PaperFetch.exe` 及其 `_internal` 依赖目录；
4. 执行 Excel 导入、标题查询、下载入口等冒烟测试；
5. 启动本地程序。

## 手动功能验证

打开页面后建议测试：

- 上传 DOI Excel；
- 手动输入 DOI；
- 获取标题；
- 勾选部分论文；
- 显示多个手动下载链接；
- 自动下载、暂停、继续；
- 手动下载后标记“已完成”；
- 关闭并重新打开后进度是否保留；
- 选择保存文件夹；
- 浏览器中打开机构访问链接；
- 切换浅色/深色主题。

## 产生 Windows 发布包

验证没有问题后，将整个 `dist\\PaperFetch` 文件夹压缩为 Windows 发布包：

```text
dist\\PaperFetch\\PaperFetch.exe
dist\\PaperFetch\\_internal\\...
```

不要只上传 EXE，也不要把 BAT 作为最终用户入口。更推荐直接使用 GitHub Actions 生成的 `PaperFetch-Windows.zip` 作为公开发布文件。
