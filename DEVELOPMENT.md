# DEVELOPMENT.md · 开发文档

## 项目概览

单文件 tkinter 应用（约 420 行），纯标准库。三段架构：

```
PROBE_PLAN（声明式目标清单）
      ↓ build_jobs()：按模式展开为 (host,ip) 级并发任务
run_round()：ThreadPoolExecutor 并发执行，每个结果即回调 on_result
      ↓ root.after(0, ...) 回主线程
App：渐进上屏（出一行亮一行）+ history 缓冲 + Canvas 走势图
```

双速探测模型：

- **全量模式**（间隔 ≥5s 或每 30s 强制）：每目标所有 DNS IP 并发，选最优 IP 并更新 `known_best`
- **快速模式**（间隔 <5s）：只测 `known_best` 里每个目标的已知最优 IP，连接数 = 目标数（7）
- 端口安全预算：Windows 动态端口 16384 / TIME_WAIT 240s ≈ 68 连接/秒；快速模式 7/s（10%）、全量 5.6/s（8%），均安全

## 关键问题与方案

## 问题：整轮刷新最坏 12 秒，界面"不流畅"

**TL;DR**：单域名多 IP 被 `socket.create_connection` 按序回退，4 IP 全死时单行 12s，整轮被最慢行拖死。

- 问题：用户反馈刷新慢、体感卡顿
- 根因：v1 把"目标"当并发单位，而域名内部 4 个 IP 是串行回退；死窗口期 raw/avatars 单行 12s
- 解决：v2 把并发单位降到 (host,ip) 级——`build_jobs` 为每个 IP 建独立任务，整轮上限 = 单个 timeout（实测 12.0s → 2.54s）
- 附加收益：能拿到每 IP 的延迟 → 备注升级为「N/M IP 活」，信息量更大
- 预防：涉及"多候选地址"的探测，第一反应就该是 (host,ip) 粒度并发

## 问题：双击 exe 窗口半天不出来 / 拖动卡顿、文字发虚

**TL;DR**：两个独立原因——①tkinter 未声明 DPI 感知（高分屏位图拉伸）；②onefile 启动要先解压 13.6MB。

- 问题：高分屏（125%/150% 缩放）下文字发虚、拖窗口掉帧；启动 1.6~1.9s
- 根因：tkinter 默认 DPI-unaware，Windows 对整窗做位图插值缩放
- 解决：`ctypes.windll.shcore.SetProcessDpiAwareness(1)` 放在 `import tkinter` **之前**（晚了无效，多来源一致）；启动解包是 onefile 固有代价，本机 1.7s 可接受，追求极致启动速度可换 `--onedir`
- 预防：tkinter 项目模板第一行就写 DPI 声明

## 问题：单实例互斥不生效，重复双击开了多个

**TL;DR**：`windll.kernel32.GetLastError()` 会被 ctypes 内部调用重置，永远读不到 183。

- 问题：CreateMutexW 后判断 `GetLastError()==ERROR_ALREADY_EXISTS` 失效
- 根因：ctypes 的 FFI 调用（如 LoadLibrary）会覆写线程 last error
- 解决：`ctypes.WinDLL("kernel32", use_last_error=True)` + `ctypes.get_last_error()`；句柄存模块全局防止 GC 释放
- 预防：凡 ctypes 错误码，一律 use_last_error 范式

## 问题：exe 打包"成功"但窗口不出现（无报错无日志）

**TL;DR**：构建解释器没有 tkinter 时 PyInstaller 可能静默产出坏 exe；且 --noconsole 会吞掉一切报错。

- 问题：exe 启动后进程活着但窗口不出现
- 根因：托管精简版 Python 无 tkinter；运行时异常被 --noconsole 吞掉
- 解决：换带 tkinter 的官方解释器构建；**入口必须包 try/except 写崩溃日志文件**（latency-crash.txt），无控制台时这是唯一可见的报错通道
- 预防：交付清单 = 崩溃日志 + 渲染截图验证（启动→FindWindowW→截图→亮度判主题）+ taskkill 后再打包

## 问题：走势图数据里混进了 baidu 的延迟

**TL;DR**：循环里建 lambda 未用默认参数绑定，闭包按引用捕获循环变量。

- 问题：SSH 行测出的延迟其实是 baidu 的
- 根因：`for tgt in PLAN: jobs.append(lambda: f(tgt))` —— tgt 执行时已是最后一个值
- 解决：默认参数绑定 `lambda t=tgt: f(t)`；同类坑：`tcp_latency(ip, to)` 位置参数把超时传成了端口号
- 预防：函数内建 lambda 引用循环变量时，100% 走默认参数绑定

## 测试约定

- 交付前跑无 GUI 冒烟：直接 import 调 `probe_round()`，断言行数与关键字段
- 渲染验证：`verify-latency2.py`（启动 → FindWindowW → SetForegroundWindow → 截图）
- 反向对照：改坏一处逻辑喂进断言，确认断言会红
