# Changelog

## 0.2.0 — 2026-11-23

### 新增

- **航迹图**：报告里多一张「航迹与位置」图——底图 + 时间着色轨迹 + 起点/终点 + 关键事件实际落点 + 比例尺。
- **航迹章节**：起点/终点经纬度、最大离起点距离及时刻、水平航迹总长、终点偏移、相对高度范围、
  关键事件坐标与距起点距离。
- `map` 参数：`amap`（高德，默认）/ `osm` / `off`（本地网格，完全离线）。
- 坐标纠偏：高德瓦片是 GCJ-02，对轨迹逐点做 WGS-84 → GCJ-02（不纠正会整体偏 100–500 m）。
- 瓦片缓存 `~/.cache/fla-tiles`（`FLA_TILE_CACHE` 可改）；取不到瓦片自动降级为网格图，报告不会失败。

### 变更

- `flightlog_analyze` 的模型可见输出新增一行航迹摘要。
- 依赖版本同步为 `flightlog-autopsy 0.2.0`（`wheels/` 放入 0.2.0 的 wheel）。

## 0.1.0 — 2026-11-23

### 新增

- 三个工具：`flightlog_analyze` / `flightlog_tune` / `flightlog_doctor`。
- 托管环境：首次调用自动在 `~/.dsh-flightlog/venv` 建 venv 并装依赖（自带 wheel + PyPI 补 matplotlib），
  之后 0.6 秒启动；`FLA_NO_BOOTSTRAP=1` 可关闭。
- 环境探测三级：`FLA_HOME` / `FLA_PYTHON` → 本地 fla 检出 → 托管 venv。
- 报告落地 Markdown / HTML / Word（docx 走 fla 自带的纯标准库导出，不需要 python-docx）。
- Word 里的严重度标记：🟥/🟨/🟦 → `【严重】/【警告】/【提示】`（避开 WPS 缺彩色 emoji 字体的问题）。
- 许可证与第三方声明：MIT + NOTICE.md。
