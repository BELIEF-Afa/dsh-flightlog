# 架构与调试

这份文档给两类人看：改这个插件的人，和排查"为什么模型跑了半天没出结果"的人。

## 三层

```
DeepSeek Harness (node)                python                  fla (wheel 或本地检出)
┌──────────────────────┐   execFile   ┌──────────────┐  import  ┌────────────────────┐
│ index.js             │ ───────────> │ fla_bridge.py│ ───────> │ fla.analyzer       │
│  resolveEnv()        │   一次调用    │  analyze     │          │ fla.report         │
│  runBridge()         │ <─────────── │  tune        │          │ fla.figures/maps   │
│  renderAnalyze()     │  @@FLA@@json │  doctor      │          │ fla.tune/export    │
└──────────────────────┘              └──────────────┘          └────────────────────┘
```

- **index.js**：注册三个工具（`ctx.tools.register`），负责参数校验、环境探测、起子进程、
  把 JSON 结果渲染成模型能读的文字。它不 import 任何 `@deepseek-ai/*` 包，也没有 npm 依赖。
- **fla_bridge.py**：一次性脚本。每次调用新起一个 python 进程，读参数 → 调 fla → 把结果压成
  一行 `@@FLA@@{json}` 打到 stdout。**改它的代码下一次调用就生效**。
- **fla**：分析引擎本体。可以来自环境变量、本地检出、或包内 wheel 解出来的 venv。

## 为什么桥接是"一次调用一个新进程"

简单、可崩溃隔离、不污染宿主 node 进程，也不用管 python 常驻进程的生命周期。
代价是每次启动要 import numpy/pymavlink（约 1–2 秒），以及 matplotlib 的冷启动。
托管 venv 里这些都已经装好，warm 状态下 doctor 0.6 秒、analyze 3–6 秒。

## 环境探测顺序（resolveEnv）

1. `FLA_PYTHON` + `FLA_HOME`（显式指定，优先级最高）
2. 从 `process.cwd()`、插件目录、日志所在目录逐级向上找同时含 `flightlog-autopsy/pyproject.toml`
   与 `fla/analyzer.py` 的目录
3. 托管 venv：`~/.dsh-flightlog/venv`（`FLA_MANAGED_DIR` 可改）。不存在或 probe 失败时，
   用 `wheels/flightlog_autopsy-*.whl`（按版本号取最新）+ `matplotlib` 建一次
4. 最后才试系统 `python3` / `python`

每个候选都要跑一次 `-c "import fla.analyzer, numpy"` 才算可用；结果缓存在模块级变量里，
一个 node 进程只探一次。`FLA_NO_BOOTSTRAP=1` 关闭第 3 步的自动安装。

## JSON 契约

`fla_bridge.py` 输出一行：

```
@@FLA@@{"ok": true, "command": "analyze", "verdict": "...", "score": 100, ..., "env_info": {...}}
```

- `ok: false` 时带 `error` 字段，node 侧抛错；工具结果里仍带 `env_info`（排查环境问题用）。
- `env_info` 由 `describeEnv()` 压平：`source / python / fla_root / fla_version / installed / seconds / managed_dir`。
- 报告文件路径、图表路径都在 JSON 里，node 侧只负责拼成给人看的文字。

## 渲染

`output.render(args, value)` 返回 `ContentBlock[]`（`[{type:'text', text}]`）。文字里：
结论截断到 600 字、每条 finding 只列前 4 条证据 + conclusion、调参只列质量门与 4 条建议、
最后给报告与图表路径。**模型看不到的细节一律留在文件里**，不要让 token 花在长表格上。

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `日志不存在` | 相对路径解析到了宿主 cwd | 传绝对路径，或用宿主 cwd 下的相对路径 |
| `doctor: ok=false` | python 里没有 fla 或依赖 | 看 `env_info.source`；`missing` 时删掉 `~/.dsh-flightlog/venv` 重来 |
| 首次调用特别慢（25–35 s） | 正在建托管 venv | 正常，之后 0.6 s |
| 航迹图是灰白格子 | 取不到瓦片 | 换 `map: off`，或检查网络/代理 |
| 改了 index.js 没生效 | host 插件不热加载 | 重启 `dsh web` |

## 重新打 wheel

插件用 wheel 分发分析引擎。改了 `flightlog-autopsy/` 之后：

```bash
cd flightlog-autopsy
../.venv/bin/pip wheel . --no-deps -w /tmp/fla-wheel
cp /tmp/fla-wheel/flightlog_autopsy-<ver>-py3-none-any.whl ../dsh-flightlog/wheels/
cd ../dsh-flightlog && npm pack --pack-destination dist
```

然后把 `package.json`、`index.js`（`PINNED_SPEC`）、README、NOTICE 里的版本号一起改掉。
`~/.dsh-flightlog/venv` 会被版本号驱动重建，不用手动删。
