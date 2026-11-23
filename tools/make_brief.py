#!/usr/bin/env python3
"""把一次飞控日志分析压缩成 3 页「事故速读」，可以直接微信发给客户。

和 tools/make_delivery.py 的区别：那个出的是 14 页正式报告，这个出的是
3 页——客户在手机上看得完，你也不用解释每一页。

    第 1 页  一句话结论 + 四个关键数字 + 事件时间线
    第 2 页  三条最硬的证据（每条：是什么 / 数据 / 为什么重要）
    第 3 页  三条建议 + 边界声明 + 需要客户回答的两个问题

用法：

    python tools/make_brief.py samples/accident_259.bin --client "某植保队" --case "AA-2026-001"

产物：deliveries/<单号>_<客户>/事故速读.pdf（以及同名 .html，便于改）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from fla.analyzer import analyze  # noqa: E402
from fla.model import fmt_t  # noqa: E402
from fla.tune import evaluate as tune_evaluate  # noqa: E402

from make_delivery import find_chrome, html_to_pdf, plain, slug  # noqa: E402

SEV_ORDER = {"critical": 0, "warn": 1, "info": 2}
SEV_CN = {"critical": "严重", "warn": "警告", "info": "提示"}

CSS = """
  @page { size: A4; margin: 14mm 15mm 12mm; }
  * { box-sizing: border-box; }
  body { font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
         color: #1f2937; font-size: 10.5pt; line-height: 1.6; margin: 0; }
  h1 { font-size: 20pt; margin: 0 0 2mm; letter-spacing: .5px; }
  .top { display: flex; justify-content: space-between; align-items: flex-end;
         border-bottom: 3px solid #1d4ed8; padding-bottom: 3mm; margin-bottom: 6mm; }
  .top .meta { font-size: 9pt; color: #64748b; text-align: right; line-height: 1.7; }
  .kicker { font-size: 8.5pt; letter-spacing: .16em; color: #64748b; margin-bottom: 1mm; }
  .verdict { border-left: 5px solid #1d4ed8; background: #f1f5fd; padding: 4mm 5mm;
             font-size: 11pt; line-height: 1.65; }
  .verdict b { color: #1d4ed8; }
  .kv { display: flex; gap: 3mm; margin: 6mm 0 0; }
  .kv div { flex: 1; border: 1px solid #e2e8f0; border-radius: 2mm; padding: 3mm 3.5mm; }
  .kv .n { font-size: 8.5pt; color: #64748b; }
  .kv .v { font-size: 15pt; font-weight: 700; color: #0f172a; margin-top: .5mm; }
  .kv .u { font-size: 8.5pt; color: #94a3b8; font-weight: 400; }
  h2 { font-size: 12pt; margin: 7mm 0 2.5mm; padding-left: 2.5mm;
       border-left: 4px solid #1d4ed8; }
  table { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
  th { text-align: left; color: #64748b; font-weight: 500; font-size: 8.5pt;
       border-bottom: 1px solid #cbd5e1; padding: 1.5mm 2mm; }
  td { border-bottom: 1px solid #eef2f7; padding: 1.8mm 2mm; vertical-align: top; }
  td.t { color: #1d4ed8; font-variant-numeric: tabular-nums; white-space: nowrap; width: 22mm; }
  .card { border: 1px solid #e2e8f0; border-radius: 2mm; padding: 3.5mm 4mm; margin-bottom: 3.5mm;
          break-inside: avoid; }
  .card .h { font-weight: 700; margin-bottom: 1.5mm; }
  .card .d { font-size: 9.5pt; color: #334155; }
  .card .w { font-size: 9.5pt; color: #1d4ed8; margin-top: 1.5mm; }
  .note { background: #fafafa; border: 1px solid #e5e7eb; padding: 3.5mm 4mm; font-size: 9pt;
          color: #475569; break-inside: avoid; }
  ol, ul { margin: 0; padding-left: 6mm; }
  li { margin-bottom: 1.5mm; }
  .pb { page-break-before: always; }
  footer { position: fixed; bottom: 0; left: 0; right: 0; font-size: 7.5pt; color: #94a3b8;
           border-top: 1px solid #eef2f7; padding-top: 1.5mm; }
"""


def pick_findings(a, n=3):
    fs = sorted(a.findings, key=lambda f: (SEV_ORDER.get(f.severity, 9), f.t_start or 0))
    return fs[:n]


def evidence_lines(f, limit=2):
    out = []
    for e in (f.evidence or [])[:limit]:
        out.append(html.escape(str(e)))
    return out


def build_html(a, meta):
    findings = pick_findings(a, 3)
    log = a.log
    sev = {"critical": 0, "warn": 0, "info": 0}
    for f in a.findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1

    # 第一页：一句话 + 四个数字 + 时间线
    verdict = plain((a.verdict_text or "").strip().split("\n")[0])
    if len(verdict) > 190:
        verdict = verdict[:190] + "…"

    t_first = min([f.t_start for f in a.findings if f.t_start is not None], default=None)
    kpis = [
        ("结论", a.verdict or "—", ""),
        ("证据强度", f"{a.score:.0f}", "/100"),
        ("最早异常", fmt_t(t_first) if t_first is not None else "—", ""),
        ("全程时长", fmt_t(log.duration()), ""),
    ]
    kv_html = "".join(
        f'<div><div class="n">{k}</div><div class="v">{html.escape(str(v))}'
        f'<span class="u">{u}</span></div></div>' for k, v, u in kpis
    )

    rows = []
    for f in sorted(a.findings, key=lambda x: (x.t_start if x.t_start is not None else 1e9))[:9]:
        rows.append(
            f'<tr><td class="t">{fmt_t(f.t_start) if f.t_start is not None else "—"}</td>'
            f'<td><b>{html.escape(f.title)}</b><br><span style="color:#64748b;font-size:9pt">'
            f'{SEV_CN.get(f.severity, f.severity)} · 置信度 {f.confidence:.0%}</span></td></tr>'
        )
    timeline = "".join(rows)

    cards = []
    for f in findings:
        ev = "".join(f"<li>{x}</li>" for x in evidence_lines(f))
        why = plain(f.conclusion or "")
        cards.append(
            f'<div class="card"><div class="h">{html.escape(f.title)} '
            f'<span style="color:#94a3b8;font-weight:400;font-size:9pt">'
            f'{SEV_CN.get(f.severity, "")} · 置信度 {f.confidence:.0%}</span></div>'
            f'<div class="d"><ul>{ev}</ul></div>'
            + (f'<div class="w">为什么重要：{html.escape(why)}</div>' if why else "")
            + "</div>"
        )

    # 第三页：调参一句话 + 建议
    tune = a.tune or {}
    tune_line = ""
    if tune.get("verdict"):
        tune_line = plain(str(tune.get("verdict")))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>事故速读 {html.escape(meta['case'])}</title><style>{CSS}</style></head>
<body>
<div class="top">
  <div>
    <div class="kicker">FLIGHT LOG BRIEF</div>
    <h1>事故速读</h1>
  </div>
  <div class="meta">
    {html.escape(meta['client'])}<br>
    单号 {html.escape(meta['case'])} · {meta['date']}<br>
    {html.escape(Path(meta['log']).name)}
  </div>
</div>

<div class="verdict"><b>{html.escape(a.verdict or "—")}</b>　{html.escape(verdict)}</div>

<div class="kv">{kv_html}</div>

<h2>发生了什么（按时间）</h2>
<table><tr><th>时刻</th><th>事件</th></tr>{timeline}</table>

<div class="pb"></div>
<h2>三条最硬的证据</h2>
<div style="font-size:9pt;color:#64748b;margin-bottom:3mm">
  下面每条都能在原始日志里逐帧复核；报告里其余结论强度低于这三条。
</div>
{"".join(cards) if cards else '<div class="note">该日志没有触发任何检测规则。</div>'}

<div class="pb"></div>
<h2>建议（按优先级）</h2>
<ol>
  <li><b>先查被顶到上限的那一路</b>——不是掉得最狠的那一路。掉下去的多半是被飞控压下去配平的。</li>
  <li><b>复现一次地面测试</b>：拆桨、单独给每路电机 30% 油门，看电流与转速是否一致；不一致的那一路就是故障源。</li>
  <li><b>改一个参数之前先看振动</b>{("：" + html.escape(tune_line)) if tune_line else "。"}</li>
</ol>

<h2>这份速读能说明什么、不能说明什么</h2>
<div class="note">
  能说明：飞控在什么时刻观察到了什么，以及这些数据之间是否自相矛盾。<br>
  不能说明：机械损伤的成因（桨叶断裂、电调烧毁都发生在日志之外）、现场环境、操作是否违规。<br>
  本文件是<b>技术复盘</b>，不是责任认定，也不能作为诉讼/理赔的鉴定结论使用。
  证据强度 {a.score:.0f}/100 表示「日志里的证据有多硬」，不是「事故发生的概率」。
</div>

<h2>要请你确认的两件事</h2>
<div class="note">
  1. 事发前是否换过桨/电机/电调？换的是哪一路？<br>
  2. 现场有没有视频或残骸照片？这两样能把上面「不能说明」的部分补上一半。
</div>

<footer>本速读由 flightlog-autopsy 生成 ·
{html.escape(meta.get('vendor', 'BELIEF-Afa 飞控日志分析'))}</footer>
</body></html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 3 页事故速读")
    ap.add_argument("log")
    ap.add_argument("--client", default="客户")
    ap.add_argument("--case", default="")
    ap.add_argument("--outdir", default="deliveries")
    ap.add_argument("--vendor", default="BELIEF-Afa 飞控日志分析")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args(argv)

    log_path = Path(args.log).expanduser().resolve()
    if not log_path.exists():
        print(f"日志不存在：{log_path}", file=sys.stderr)
        return 2

    case = args.case or _dt.datetime.now().strftime("%Y%m%d-%H%M")
    meta = {
        "client": args.client,
        "case": case,
        "date": _dt.date.today().isoformat(),
        "log": str(log_path),
        "vendor": args.vendor,
    }

    print(f"读日志：{log_path.name}")
    a = analyze(str(log_path))
    if a.tune is None:
        try:
            a.tune = tune_evaluate(a.log)
        except Exception as exc:  # noqa: BLE001
            print(f"  调参体检跳过：{exc}")
    print(f"  {a.verdict}（证据强度 {a.score:.0f}）/ {len(a.findings)} 条发现")

    outdir = Path(args.outdir).expanduser() / f"{slug(case)}_{slug(args.client)}"
    outdir.mkdir(parents=True, exist_ok=True)

    html_path = outdir / "事故速读.html"
    html_path.write_text(build_html(a, meta), encoding="utf-8")
    print(f"  写出 {html_path}")

    if not args.no_pdf:
        chrome = find_chrome()
        if not chrome:
            print("  没找到 Chrome/Chromium，跳过 PDF（HTML 可以直接打印）")
        else:
            pdf_path = outdir / "事故速读.pdf"
            ok, msg = html_to_pdf(chrome, html_path, pdf_path)
            print(f"  {'PDF ' + str(pdf_path) if ok else 'PDF 失败：' + msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
