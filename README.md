# gh-latency · GitHub 延迟监视器

一个极简的 **GitHub 连通性监视器**（Windows）：实时看清各条访问通道的健康状况——直连各段、SSH 通道、本地代理，延迟一目了然，超时一眼定位。

![截图](https://github.com/Simiely/gh-latency/raw/main/docs/screenshot.png)

## 它解决什么问题

国内访问 GitHub 的典型痛点不是"连不上"，而是**时好时坏、不知道哪条路是通的**：

- 网页图片裂图、raw 文件超时，但 git clone 又是通的——到底是哪一层的病？
- 换了加速工具也不知道效果如何，全凭感觉

gh-latency 把这个问题变成一张表 + 一条曲线：**绿=通畅，黄=慢，红=超时**，每条通道独立测量。

## 下载与使用

到 [Releases](https://github.com/Simiely/gh-latency/releases) 下载 `gh-latency.exe`，双击即用。

- **零依赖**：单文件、纯 Python 标准库、不需要安装 Python
- **零配置**：打开自动开始测量，默认每 1 秒刷新（自动进入快速模式）
- **刷新间隔可输入**：顶部输入 1-3600 任意秒数回车即可（1 秒也安全，见下）
- **走势图**：点击表格任意一行，下方走势图切换到该目标的延迟曲线（超时在底部标红块）

### 测量目标

| 通道 | 含义 |
|---|---|
| 直连 ×5 | github.com / api / raw / codeload / avatars（按 DNS 顺序试连，和浏览器行为一致） |
| SSH 通道 | ssh.github.com:443（git 的稳定备用通道，实测比 HTTPS 段更抗抽风） |
| 本地反代 | 检测到系统代理时自动出现（经代理到 GitHub 的延迟） |
| 基线 | www.baidu.com（区分"GitHub 的问题"还是"你断网了"） |

### 1 秒刷新安全吗？

安全。按 Windows 动态端口预算（16384 个 / TIME_WAIT 240s ≈ 68 连接/秒安全线）计算：

- 间隔 <5s 自动进入**快速模式**：只测每个目标的已知最优 IP（7 连接/秒，占安全线 10%）
- 每 30 秒自动插入一次**全量扫描**纠偏（IP 漂移自动跟进）
- 每轮流量 KB 级，对带宽零感知

## 从源码构建

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name gh-latency gh-latency.py
```

需要 Python 3.10+（含 tkinter）。详见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 文档

- [AGENTS.md](AGENTS.md) —— 项目规则（技术栈 / 关键坑 / 约定）
- [DEVELOPMENT.md](DEVELOPMENT.md) —— 架构说明与关键问题记录
- [CHANGELOG.md](CHANGELOG.md) —— 版本历史

## License

MIT
