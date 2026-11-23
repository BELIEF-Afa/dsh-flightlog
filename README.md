# @local/dsh-flightlog

把 [flightlog-autopsy](../flightlog-autopsy/) 的飞控日志分析能力挂进 DeepSeek Harness：
模型可以直接分析 ArduPilot / PX4 日志，得到事故结论、证据链、调参体检，并落地报告文件。

## 工具

| 工具 | 作用 |
| --- | --- |
| `flightlog_analyze` | 事故/飞行分析：结论、评分、逐条证据（时间 + 证据 + 结论）、调参摘要；输出 `.md` / `.html` / `.docx` 与图表 |
| `flightlog_tune` | 调参体检：质量门、速率环跟踪、响应延迟/相干度、参数建议 |
| `flightlog_doctor` | 环境自检：用的是哪个 python、fla 在哪、依赖齐不齐 |

报告默认写到**日志同级的 `fla-reports/`**，可用 `outdir` 覆盖。

## 环境探测

插件自己找 fla 源码和解释器，顺序：

1. 环境变量 `FLA_HOME`（fla 项目根目录）、`FLA_PYTHON`（解释器路径）
2. 从宿主 cwd / 插件目录 / 日志路径逐级向上找 `flightlog-autopsy/pyproject.toml` 与 `fla/analyzer.py`
3. 解释器依次试 `../.venv/bin/python`（沿 fla 项目向上）、PATH 里的 `python3` / `python`
4. 每个候选都会先跑一次 `import fla.analyzer, numpy` 验证，能 import 才算数

因此依赖必须装在 `.venv` 里（numpy / pymavlink / pyulog / matplotlib）；
`docx` 导出是 fla 自带的纯标准库实现，不需要 python-docx。

## Word 里的严重度标记

`fla` 的报告在 Markdown 里用 🟥/🟨/🟦 标严重度。Word 没有可靠的彩色 emoji 字体
（WPS / LibreOffice 常把 🟨 渲染成怪字形，比如一个 "K"），所以 **导出 docx 前**
插件会把它们换成中文字体一定有的标签：`【严重】/【警告】/【提示】`。
`.md` 与 `.html` 保留 emoji。

## 来源与许可

- 插件本体 **MIT**；分析引擎 [flightlog-autopsy](../flightlog-autopsy/)（同一作者的 MIT 项目）以 wheel 形式随包分发，**未修改源码**。
- 运行时依赖由 pip 装进 `~/.dsh-flightlog/venv`，不随包分发：`pymavlink`（**LGPL-3.0**）、`pyulog`（BSD-3）、`numpy`（BSD-3）、`matplotlib`（Matplotlib License）。
- **不含** ArduPilot / PX4 / Betaflight 的源码或二进制；判据来自公开日志字段与固件行为观察，结论由本项目代码独立实现。
- 分析全程本机完成，不上传日志；唯一联网动作是首次自动装依赖（可用 `FLA_NO_BOOTSTRAP=1` 走离线）。

完整的第三方清单、商标声明与再分发要求见 [NOTICE.md](NOTICE.md)。

## 不支持

- Betaflight 黑匣子 `.bbl` / `.bfl`：先用 `blackbox_decode` 转 CSV 再传。
- `--llm` 叙事（需要 API key）没有暴露；需要时直接在 CLI 里跑。

## 安装

从 GitHub 装（推荐给使用者）：

```
dsh plugin add github:BELIEF-Afa/dsh-flightlog
```

装完重启 `dsh web`；首次跑 `flightlog_doctor` 会自动建 venv 并装依赖（约 25 秒，之后 0.6 秒）。

从本目录装（改代码时）：

```
dsh plugin add /绝对路径/dsh-flightlog
```

改完 `index.js` 要重启 `dsh web` 才生效；改 `bridge/fla_bridge.py` 是下一次调用就生效（每次新起进程）。

## 依赖怎么来

插件包自带 `wheels/flightlog_autopsy-0.1.0-py3-none-any.whl`；首次调用时在本机建 `~/.dsh-flightlog/venv`，
优先用自带 wheel、再补 `matplotlib`。换目录可用 `FLA_MANAGED_DIR` 指定；离线环境设 `FLA_NO_BOOTSTRAP=1`
并自行 `pip install` 到 `FLA_PYTHON` 指定的解释器。
