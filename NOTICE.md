# 来源与第三方声明（NOTICE）

本文件回答一个具体问题：**这个插件用到哪些"别人的东西"，哪些是本项目自己写的。**
写它是为了发布/分发时能一眼看清许可边界，也为了别人核对时不产生误会。

## 1. 哪些是本项目自己的代码

| 部分 | 说明 |
| --- | --- |
| 插件本体（Node 侧） | `index.js`、`bridge/fla_bridge.py`、`cordis.patch.yml`、locale、图标 —— 本项目原创，MIT |
| 分析引擎 `fla` | 同一作者的 [flightlog-autopsy](https://pypi.org/project/flightlog-autopsy/)，MIT；本插件**只调用其公开 API，未修改源码**，以 wheel 形式随包分发（`wheels/flightlog_autopsy-0.2.0-py3-none-any.whl`，内含该项目的 LICENSE） |

**没有「改别人的工具」这一步**：`fla` 是本项目自己写的，插件没有 fork、没有 patch、没有复制第三方源码。

## 2. 运行时依赖（第三方）

这些库在**首次运行时**由 pip 装进 `~/.dsh-flightlog/venv`，不随插件包分发：

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| `pymavlink` | 解析 ArduPilot `.bin` / `.log` / `.tlog` | **LGPL-3.0** |
| `pyulog` | 解析 PX4 `.ulg` | BSD-3-Clause |
| `numpy` | 数值计算 | BSD-3-Clause（并含 0BSD / MIT / Zlib / CC0-1.0 片段） |
| `matplotlib` | 报告配图（四联图 / 调参图 / 航迹地图） | Matplotlib License（PSF 风格） |

许可说明：
- 上面四个库都**未被修改**，以独立的 Python 包形式安装（动态加载，非静态链接），符合各自许可对再分发的常见要求。
- 若你要**整机分发** `~/.dsh-flightlog/venv`（而不是让它在目标机上现装），请一并附上 `pymavlink` 的 LGPL-3.0 文本及获取源码的说明。

## 3. 明确没有包含的东西

- **不含 ArduPilot / PX4 / Betaflight 的任何源代码或二进制文件。**
  判据（如 ArduPilot 的 `MODE.Rsn` 模式原因、电机饱和判定、`MOT_THST_HOVER` 推力域换算、EKF 状态含义）来自公开的日志字段文档与对固件行为的观察，结论均由本项目代码独立实现，未复制其源码（这些项目是 GPLv3，抄代码会传染）。
- **不含 PlotJuggler / Flight Review / UAV Log Viewer 的代码**：它们解决的是"画曲线"，本项目解决"出结论 + 证据链"，属功能互补。
- **不上传日志、不联网分析**：全流程在本机完成，报告写在本机目录。

## 4. 名称与商标

ArduPilot、PX4、Betaflight、PlotJuggler、Flight Review、DeepSeek Harness 等名称归各自所有者。
本插件仅在「兼容哪些日志格式 / 运行在哪个宿主」的意义上提及它们，**无隶属、无背书、无官方关系**。

## 5. 数据处理与网络行为

| 动作 | 是否联网 | 说明 |
| --- | --- | --- |
| 解析 / 检测 / 出报告 | 否 | 纯本地 |
| 首次自动装依赖 | **是**（仅一次） | 访问 PyPI；可用自带 wheel + `FLA_NO_BOOTSTRAP=1` 走离线 |
| 大模型叙事（`fla --llm`） | 否（未开放） | 本插件未暴露 `--llm`，报告不含任何模型调用 |
| 航迹底图瓦片 | **是**（画图时） | 按 `z/x/y` 向地图服务取栅格瓦片：默认高德（`wprd0x.is.autonavi.com`），或 `osm`（`tile.openstreetmap.org`）；`--map off` 完全离线。瓦片缓存在 `~/.cache/fla-tiles`。 |
| 底图版权 | — | 高德瓦片 © AutoNavi，OSM 瓦片 © OpenStreetMap contributors（ODbL）。图上已带署名；高德栅格接口非公开 API，对外商业分发请改用官方 API 或 `osm` / 自建瓦片。 |

## 6. 二次分发

插件本体 MIT：保留 `LICENSE` 与 `NOTICE.md` 即可。
随包的 `fla` wheel 是 MIT，保留其包内 LICENSE 即可。第三方依赖按第 2 节处理。
