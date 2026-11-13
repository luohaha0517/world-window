# 世界一隅 · World Window

一个放在 Windows 桌面上的小报刊架。悬停展开图文新闻，移开收起；打开其他应用时，报刊架留在桌面底层。

## 功能

- 可拖动的小报刊架，显示媒体提供的新闻缩略图，记住摆放位置。
- 悬停约 0.35 秒展开；移开约 0.85 秒收起，点击可固定阅读。
- 普通应用窗口可以盖住报刊架，切换应用时新闻面板自动收起。
- 北京时间每天 08:30 更新；未运行或未开机时，下次启动补更。更新失败保留缓存并重试。
- 联合国新闻、BBC World、The Guardian、Al Jazeera、中国新闻网国际 RSS，按来源轮换，最多 12 条。
- 点击“翻译摘要”，在当前面板显示标题和简介的中文译文，再点可恢复原文。成功译文缓存于本机。
- 托盘菜单支持立即更新、开机启动、退出。默认不自动启用开机启动，可自行勾选。

## 运行

需要 Windows 10/11、Python 3.10 或更高版本，以及可访问相应来源的网络。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe world_window.py
```

右键报刊架或托盘图标打开菜单。按 Esc 收起阅读面板。开机启动使用当前程序位置，移动安装目录后请重新设置。

## 打包为独立程序

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm world-window.spec
```

运行 `dist/WorldWindow/WorldWindow.exe`，请保留整个文件夹。构建应在干净的 Python 虚拟环境中执行。项目提供的 spec 排除可能从其他软件混入的 ICU 库，使用 Windows 自带 ICU。

## 数据与网络

缓存、图片、译文及设置存放在 `%LOCALAPPDATA%\WorldWindow`，不会上传到项目仓库。网络请求优先沿用 Windows 代理，失败后尝试直连。

翻译使用 [MyMemory API](https://mymemory.translated.net/doc/spec.php)，仅在用户点击时发送新闻标题和简介。服务的免费额度及网络条件可能影响可用性；失败时保留原文并允许重试。原文链接会在默认浏览器中打开。

新闻和图片来自媒体 RSS；缺少配图时明确显示说明，不生成新闻或图片。多来源展示不能保证完全消除信息茧房，也没有对报道立场进行自动评分。日更需要程序运行，不会唤醒关机的电脑。

## 许可

应用源码采用 [MIT 许可](LICENSE)。PySide6/Qt 等第三方组件保留各自许可，参见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。媒体文章与照片不属于本项目的 MIT 授权范围，本仓库不包含新闻缓存和媒体图片。
