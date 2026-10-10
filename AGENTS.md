# AGENTS.md · 项目规则

> 📌 **文档基线**：2026-10-11（v1.0.1 默认刷新 1s）· 2026-10-08（v1.0.0 首发）完成四件套
> **更新文档/代码后，请更新此行**（日期 + 新 tag），并在 CHANGELOG 追加版本

## 技术栈

- Python 3.14.6 + tkinter（**纯标准库，零第三方依赖**——这是硬约定，加依赖前必须先论证）
- 打包：PyInstaller `--onefile --noconsole`，用带 tkinter 的解释器构建（托管精简版 Python 无 tkinter，构建会"成功"但 exe 起不来）
- 目标环境：Windows 10/11，DPI 缩放 100%~200%

## 关键坑（每条都实测踩过）

- **DPI 感知必须在 `import tkinter` 之前声明**（`ctypes.windll.shcore.SetProcessDpiAwareness(1)`），否则高分屏整窗位图拉伸：文字发虚 + 拖动掉帧
- **单域名多 IP 千万不要串行试连**：socket.create_connection 对多 IP 域名按序回退，4 IP 全死 = 4×timeout（实测一轮 12s）；必须展开成 (host,ip) 级并发任务
- **ctypes 取错误码用 `WinDLL(use_last_error=True)` + `ctypes.get_last_error()`**；`windll.kernel32.GetLastError()` 会被 ctypes 内部调用重置（单实例互斥曾因此失效）
- **lambda 捕获循环变量必须用默认参数绑定**（`lambda ip=ip:`），否则测的都是最后一项
- **StringVar 取值必须 `.get()`**；崩溃处理器里用到的模块（sys/os）必须顶层导入，否则日志写不出来掩盖真相
- **打包前先 `taskkill /F /IM <exe>`**：旧进程占用会让 PyInstaller 静默使用旧文件
- **启动测量在后台线程，UI 更新一律 `root.after(0, ...)` 回主线程**；渐进上屏（as_completed 每结果一回调）是"流畅感"的关键

## 约定

- UI 标签与注释用中文；界面深色主题（#1e1f24 系）
- 不引入第三方依赖；功能克制，单文件交付
- 刷新间隔支持手输 1-3600s：<5s 走快速模式（只测已知最优 IP，7 连接/s < 端口安全线 68/s）

## 常用命令

- 构建：`pyinstaller --onefile --noconsole --name gh-latency gh-latency.py`
- 渲染验证：启动 exe → `FindWindowW` 按标题找窗 → 截图 → 亮度落 45~55 判深色生效
- 冒烟：无 GUI 直接调 `measure_all/probe_round` 看返回行

## 详细规则（按需 @引用）

- @DEVELOPMENT.md（架构 + 问题记录）
