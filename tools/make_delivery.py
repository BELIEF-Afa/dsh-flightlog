#!/usr/bin/env python3
"""把一次飞控日志分析打包成可以直接发给客户的交付目录。

产物（默认写到 ``deliveries/<单号>_<客户端名>/``）：

    00_交付清单.txt      文件清单 + 一句话说明
    01_报告.pdf          带封面、可打印、可直接转发
    02_报告.docx         客户可以自己改/盖章的版本
    03_图表/             报告里的图，单独一份便于插 PPT
    04_证据附录.md       全部原始证据、事件表、未执行检查、方法与免责
    05_给客户的话.txt    非技术版说明（怎么读、能得出什么、下一步）

用法：

    python tools/make_delivery.py samples/accident_259.bin \\
        --client "某农业服务公司" --case "AA-2026-001"

PDF 依赖本机 Chrome/Chromium 的 headless 打印；没有时自动跳过并提示。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fla.analyzer import analyze  # noqa: E402
from fla.export import markdown_to_docx, markdown_to_html  # noqa: E402
from fla.figures import build_figures, build_tune_figure  # noqa: E402
from fla.model import fmt_t  # noqa: E402
from fla.report import render_markdown  # noqa: E402
from fla.tune import evaluate as tune_evaluate  # noqa: E402

CHROME_CANDIDATES = [
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
]

COVER_CSS = """
  .cover { display: flex; flex-direction: column; justify-content: space-between;
           box-sizing: border-box; padding: 6mm 0 4mm; }
  .cover .kicker { font-size: 13px; letter-spacing: .18em; color: #6b7a90; }
  .cover h1 { font-size: 32px; line-height: 1.35; margin: 10px 0 6px; }
  .cover .sub { font-size: 15px; color: #4b5c74; }
  .cover .rule { height: 4px; width: 96px; background: #4f8cff; margin: 22px 0 26px; }
  .cover .verdict { border-left: 5px solid #4f8cff; background: #f2f6fd; padding: 16px 18px;
                    font-size: 15px; color: #1f2937; }
  .cover .verdict b { color: #12233f; }
  .cover table { font-size: 14px; }
  .cover .fine { font-size: 12px; color: #6b7a90; line-height: 1.7; }
  .cover .sign { display: flex; gap: 28px; margin-top: 10px; font-size: 13px; color: #4b5c74; }
  .cover .sign div { flex: 1; border-top: 1px solid #cbd5e1; padding-top: 6px; }
  @media print { .cover { height: 252mm; page-break-after: always; } }
"""

SEV_CN = {"critical": "严重", "warn": "警告", "info": "提示"}


# --------------------------------------------------------------------------- #
# 小工具
def slug(text: str, limit: int = 24) -> str:
    keep = re.sub(r"[\\/:*?\"<>|\s]+", "_", (text or "").strip())
    return (keep or "客户")[:limit].strip("_")


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    for guess in ("/usr/bin/google-chrome-stable", "/usr/bin/chromium",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(guess).is_file():
            return guess
    return None


def html_to_pdf(chrome: str, html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--virtual-time-budget=15000",
        "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "Chrome 打印超时（180 s）"
    if pdf_path.is_file() and pdf_path.stat().st_size > 2000:
        return True, ""
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    return False, "；".join(tail) or f"返回码 {proc.returncode}"


def plain(text: str) -> str:
    """去掉 Markdown 的行内标记，封面/信件里不露星号。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.replace("**", "").replace("__", "")


def one_line_verdict(a) -> str:
    text = plain((a.verdict_text or "").strip().replace("\n", ""))
    return text[:220] + ("…" if len(text) > 220 else "")


# --------------------------------------------------------------------------- #
# 封面
def build_cover(meta: dict, a) -> str:
    crit = sum(1 for f in a.findings if f.severity == "critical")
    warn = sum(1 for f in a.findings if f.severity == "warn")
    info = sum(1 for f in a.findings if f.severity == "info")
    log = a.log
    rows = [
        ("委托方", meta["client"]),
        ("委托单号", meta["case"]),
        ("送检日志", Path(log.path).name if getattr(log, "path", None) else meta["log_name"]),
        ("机型 / 固件", f"{meta['frame']} · {meta['firmware']}"),
        ("日志时长", f"{log.t_end:.1f} s（解锁 {meta['armed']:.1f} s）"),
        ("分析日期", meta["date"]),
        ("出具方", meta["vendor"]),
    ]
    table = "".join(
        f"<tr><th style='width:92px'>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    counts = f"严重 {crit} 条 · 警告 {warn} 条 · 提示 {info} 条"
    return f"""
<section class="cover">
  <div>
    <div class="kicker">FLIGHT LOG AUTOPSY · 飞行日志分析报告</div>
    <h1>{html.escape(meta['client'])}<br>飞行日志分析报告</h1>
    <div class="sub">编号 {html.escape(meta['case'])} · {html.escape(meta['date'])}</div>
    <div class="rule"></div>
    <div class="verdict">
      <b>结论：{html.escape(a.verdict)}</b>（证据强度 {a.score}/100）<br>
      {html.escape(one_line_verdict(a))}
    </div>
    <p class="sub" style="margin-top:14px">检出：{html.escape(counts)}</p>
    <table style="margin-top:20px">{table}</table>
  </div>
  <div>
    <div class="fine">
      本报告依据委托方提供的飞行日志离线分析得出，结论以日志记录为唯一证据来源；
      未记录在日志中的数据（如机械损伤、现场环境）不在本报告判断范围内。
      证据强度反映的是日志证据的充分程度，不等同于事故发生的概率。
      报告仅供委托方内部使用，未经书面同意请勿对外公开或用于第三方责任认定。
    </div>
    <div class="sign">
      <div>分析人（签字）：</div>
      <div>日期：</div>
      <div>委托方确认（签字/盖章）：</div>
    </div>
  </div>
</section>
"""


def inject_cover(report_html: str, cover: str) -> str:
    out = report_html.replace("</style>", COVER_CSS + "</style>", 1)
    out = out.replace("<body><main>", "<body>" + cover + "<main>", 1)
    if out == report_html:  # 结构变了也尽量插进去
        out = cover + report_html
    return out


# --------------------------------------------------------------------------- #
# 附录与信件
def build_appendix(meta: dict, a) -> str:
    log = a.log
    lines = [
        f"# 证据附录 · {meta['case']}",
        "",
        f"委托方：{meta['client']}　分析日期：{meta['date']}　出具方：{meta['vendor']}",
        "",
        "## 1. 日志元信息",
        "",
        f"- 文件：`{meta['log_name']}`",
        f"- 机型 / 固件：{meta['frame']} · {meta['firmware']}",
        f"- 时长：{log.t_end:.1f} s；解锁时长：{meta['armed']:.1f} s",
        f"- 消息帧：" + "、".join(f"{k}={v}" for k, v in sorted(meta["counts"].items())[:12]),
        "",
        "## 2. 检出项与原始证据",
        "",
    ]
    for i, f in enumerate(a.sorted_findings(), 1):
        when = fmt_t(f.t_start, log.t_end) if f.t_start is not None else "全程"
        lines += [
            f"### {i}. [{SEV_CN.get(f.severity, f.severity)}] {f.title} @ {when}",
            "",
            f"- 检测器：`{f.id}`　类别：{f.category}　置信度：{f.confidence:.0%}"
            + (f"　区间：{fmt_t(f.t_start, log.t_end)} → {fmt_t(f.t_end, log.t_end)}" if f.t_end else ""),
        ]
        for e in f.evidence:
            lines.append(f"- {e}")
        if f.conclusion:
            lines += ["", f"> {f.conclusion}"]
        lines.append("")
    if a.skipped:
        lines += ["## 3. 未执行的检查", ""]
        for s in a.skipped:
            lines.append(f"- `{s.detector}`：{s.reason}")
        lines.append("")
    if log.warnings:
        lines += ["## 4. 解析告警", ""] + [f"- {w}" for w in log.warnings] + [""]
    if a.tune:
        t = a.tune
        lines += ["## 5. 调参体检", "", f"- 结论：{t.get('verdict', '—')}"]
        gate = t.get("gate") or []
        if gate:
            lines.append("- 质量门：")
            for g in gate:
                lines.append(f"  - {g}")
        for s in (t.get("suggestions") or [])[:10]:
            lines.append(f"- 建议：{s}")
        lines.append("")
    lines += [
        "## 6. 方法与边界",
        "",
        "- 解析：pymavlink / pyulog 逐帧解析，时间统一归零到日志起点。",
        "- 判据：推力损失、姿态失控、动力饱和、振动与削顶、电气、导航与模式切换等规则，"
        "每条结论都对应上文列出的日志行级证据。",
        "- 评分：Σ(权重×置信度)，封顶 100；衡量证据强度，不是事故概率。",
        "- 边界：不替代结构探伤、动力台架测试与现场勘查；日志缺失的段落不做推断。",
        "",
    ]
    return "\n".join(lines)


def build_letter(meta: dict, a) -> str:
    crit = [f for f in a.sorted_findings() if f.severity == "critical"]
    warn = [f for f in a.sorted_findings() if f.severity == "warn"]
    head = crit[0].title if crit else (warn[0].title if warn else "未发现明显异常")
    when = fmt_t(crit[0].t_start, a.log.t_end) if crit and crit[0].t_start is not None else ""
    return f"""您好，

附件是 {meta['client']} 送检飞行日志的分析报告（编号 {meta['case']}），
依据 {meta['log_name']} 离线分析得出，分析日期 {meta['date']}。

一句话结论
---------
{a.verdict}（证据强度 {a.score}/100）。最需要关注的是「{head}」{('，出现在 ' + when) if when else ''}。

报告怎么看
---------
1. 01_报告.pdf：直接打印或转发；第 1 页是结论摘要，后面是时间线、证据链和图表。
2. 02_报告.docx：需要改措辞、加抬头或盖章时用这个版本。
3. 03_图表/：报告里的图，可单独插进 PPT 或周报。
4. 04_证据附录.md：给机务/研发看的技术底稿，每条结论都能追到具体日志行。

这份报告能回答什么
-----------------
- 出事前飞控看到了什么：姿态、推力、电流、电压、振动、模式切换的先后顺序；
- 各部件在什么时候开始不对劲，先到极限的是哪一路；
- 下一次飞之前必须先改什么（调参体检那一节）。

这份报告不能回答什么
-------------------
- 机械损伤、装配问题、现场风况、人为操作细节——这些不在日志里，需要现场与台架佐证；
- 证据强度不等于事故概率，它衡量的是"日志能支持多确定的结论"。

需要的话，我们可以按同一套流程做多架次对比、复飞前检查清单，或到现场配合复现。

{meta['vendor']}
{meta['date']}
"""


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把飞控日志分析打包成客户交付目录")
    ap.add_argument("log", help="日志文件（.bin/.ulg/.csv）")
    ap.add_argument("--client", default="客户", help="委托方名称（会印在封面）")
    ap.add_argument("--case", default="", help="委托单号，默认按日期自动生成")
    ap.add_argument("--date", default="", help="分析日期，默认今天")
    ap.add_argument("--outdir", default="deliveries", help="交付目录的上级目录")
    ap.add_argument("--map", default="amap", choices=["amap", "osm", "off"], help="航迹底图")
    ap.add_argument("--figures", action="store_true", default=True, help="生成图表（默认开）")
    ap.add_argument("--no-figures", dest="figures", action="store_false")
    ap.add_argument("--no-pdf", action="store_true", help="不生成 PDF（没有 Chrome 时自动跳过）")
    ap.add_argument("--vendor", default=os.environ.get("FLA_VENDOR", "BELIEF-Afa 飞控日志分析"),
                    help="出具方名称，也可用环境变量 FLA_VENDOR")
    args = ap.parse_args(argv)

    log_path = Path(args.log).expanduser()
    if not log_path.is_file():
        print(f"日志不存在：{log_path}", file=sys.stderr)
        return 2

    today = _dt.date.today().isoformat()
    meta = {
        "client": args.client.strip() or "客户",
        "case": args.case.strip() or f"FLA-{_dt.date.today():%Y%m%d}-{slug(args.client, 8)}",
        "date": args.date.strip() or today,
        "vendor": args.vendor.strip(),
        "log_name": log_path.name,
    }
    out_dir = Path(args.outdir) / f"{slug(meta['case'], 32)}_{slug(meta['client'])}"
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "03_图表"
    fig_dir.mkdir(exist_ok=True)

    print(f"分析 {log_path.name} …")
    a = analyze(log_path)
    a.tune = tune_evaluate(a.log)
    iv = a.log.armed_intervals()
    meta.update(
        frame=str(a.log.meta.get("frame") or "未知机型"),
        firmware=" ".join(x for x in (a.log.meta.get("vehicle_family"), a.log.meta.get("firmware_version")) if x)
        or a.log.firmware or "未知固件",
        armed=sum(b - s0 for s0, b in iv),
        counts=dict(a.log.message_counts or {}),
    )

    figures: list = []
    if args.figures:
        print("出图 …")
        figures = [f for f in build_figures(a, fig_dir, prefix="fig", map_provider=args.map)
                   if getattr(f, "path", None)]
        tf = build_tune_figure(a.log, a.tune, fig_dir / "fig4_tune.png")
        if tf and getattr(tf, "path", None):
            figures.append(tf)

    print("写报告 …")
    title = f"{meta['client']} 飞行日志分析报告（{meta['case']}）"
    md = render_markdown(a, title="飞行日志分析报告", figures=figures or None)
    base = out_dir / f"01_{slug(meta['client'])}_报告"
    html_path = base.with_suffix(".html")
    report_html = markdown_to_html(md, html_path, title=title, base_dir=fig_dir).read_text(encoding="utf-8")
    html_path.write_text(inject_cover(report_html, build_cover(meta, a)), encoding="utf-8")
    markdown_to_docx(md, base.with_suffix(".docx"), title=title, base_dir=fig_dir)

    pdf_note = ""
    pdf_path = base.with_suffix(".pdf")
    chrome = None if args.no_pdf else find_chrome()
    if chrome:
        print("打印 PDF …")
        ok, err = html_to_pdf(chrome, html_path, pdf_path)
        if not ok:
            pdf_note = f"PDF 生成失败：{err}"
    else:
        pdf_note = "未找到 Chrome/Chromium，已跳过 PDF（可直接打印 01_*.html，或装 Chrome 后重跑）"

    (out_dir / "04_证据附录.md").write_text(build_appendix(meta, a), encoding="utf-8")
    (out_dir / "05_给客户的话.txt").write_text(build_letter(meta, a), encoding="utf-8")

    # 让 md 也留一份，便于以后改
    (out_dir / f"01_{slug(meta['client'])}_报告.md").write_text(md, encoding="utf-8")

    files = sorted([p for p in out_dir.rglob("*") if p.is_file()])
    lines = [
        f"交付清单 · {meta['case']}",
        f"委托方：{meta['client']}　分析日期：{meta['date']}　出具方：{meta['vendor']}",
        "",
    ]
    for p in files:
        lines.append(f"{p.relative_to(out_dir).as_posix():<44} {p.stat().st_size / 1024:8.1f} KB")
    if pdf_note:
        lines += ["", pdf_note]
    (out_dir / "00_交付清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("")
    print(f"交付目录：{out_dir}")
    for p in sorted([p for p in out_dir.rglob("*") if p.is_file()]):
        print(f"  {p.relative_to(out_dir).as_posix():<46}{p.stat().st_size / 1024:9.1f} KB")
    if pdf_note:
        print(f"\n注意：{pdf_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
