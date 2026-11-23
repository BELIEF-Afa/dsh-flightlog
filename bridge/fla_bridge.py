#!/usr/bin/env python3
"""dsh-flightlog 的 Python 桥。

插件通过 ``<venv python> fla_bridge.py <cmd> ...`` 调这里，用一行
``@@FLA@@{json}`` 把结果交回 Node 侧。这样插件本身零 Python 依赖，
只要求机器上有一个装好 fla 依赖的解释器（默认自动探测工作区的 .venv）。

子命令：
  analyze <log> [--format md|html|docx|all] [--outdir DIR] [--no-figures] [--map amap|osm|off]
  tune    <log>
  doctor
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

SENTINEL = "@@FLA@@"


def _jsonable(obj):
    for attr in ("item", "tolist"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def emit(payload: dict) -> None:
    sys.stdout.write(SENTINEL + json.dumps(payload, ensure_ascii=False, default=_jsonable) + "\n")
    sys.stdout.flush()


def _round(value, digits: int = 2):
    try:
        return round(float(value), digits)
    except Exception:
        return value


# --------------------------------------------------------------------------
# 压缩：模型只看工具返回的文本，灌 1000 多条参数没有意义
# --------------------------------------------------------------------------
SUMMARY_KEYS = (
    "firmware", "log_format", "duration_s", "armed_s", "distance_m",
    "max_alt_m", "max_alt_t", "max_speed_ms", "max_speed_t", "max_vibe",
    "min_sats", "volt_min", "volt_max", "mode", "vehicle", "session_count",
    "finding_count", "critical_count", "warn_count", "crashed",
)


def compact_summary(summary: dict) -> dict:
    out = {}
    for key in SUMMARY_KEYS:
        if key in summary and summary[key] is not None:
            value = summary[key]
            out[key] = _round(value) if isinstance(value, (int, float)) else value
    return out


def compact_finding(f) -> dict:
    return {
        "id": f.id,
        "title": f.title,
        "category": f.category,
        "severity": f.severity,
        "t_start": None if f.t_start is None else _round(f.t_start),
        "t_end": None if f.t_end is None else _round(f.t_end),
        "evidence": [str(e) for e in (f.evidence or [])][:8],
        "conclusion": f.conclusion,
        "confidence": _round(f.confidence),
    }


def compact_tune(tune: dict | None) -> dict | None:
    if not tune:
        return None
    rate = tune.get("rate") or {}
    delay = tune.get("delay") or {}
    return {
        "verdict": tune.get("verdict"),
        "imu_hz": _round(tune.get("imu_hz")),
        "gate": [
            {"level": g.get("level"), "title": g.get("title"), "detail": g.get("detail")}
            for g in (tune.get("gate") or [])
        ],
        "rate": {k: _round(rate[k]) for k in
                 ("fs", "roll_rms", "roll_follow", "pitch_rms", "pitch_follow") if k in rate},
        "delay": {k: _round(delay[k]) for k in
                  ("delay_ms", "coherence", "fltt_hz", "residual_ms", "usable") if k in delay},
        "suggestions": [
            {k: s.get(k) for k in ("param", "current", "suggested", "level", "reason")}
            for s in (tune.get("suggestions") or [])[:14]
        ],
        "figure": tune.get("figure"),
    }


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def compact_track(log) -> dict | None:
    """航迹摘要：起点/终点/最远点。没有定位数据就返回 None。"""
    try:
        from fla.track import track_facts

        facts = track_facts(log)
    except Exception:
        return None
    if not facts:
        return None
    home_lat, home_lng, home_t = facts["home"]
    end_lat, end_lng, end_t = facts["end"]
    peak_lat, peak_lng, peak_t = facts["max_range_at"]

    def ll(lat, lng):
        return {"lat": round(lat, 6), "lng": round(lng, 6)}

    return {
        "home": ll(home_lat, home_lng),
        "end": ll(end_lat, end_lng),
        "max_range_m": round(facts["max_range_m"], 1),
        "max_range_at": {**ll(peak_lat, peak_lng), "t": round(peak_t, 1)},
        "path_m": round(facts["path_m"], 1),
        "final_offset_m": round(facts["final_offset_m"], 1),
        "alt_range_m": ([round(facts["alt_min"], 1), round(facts["alt_max"], 1)]
                        if "alt_max" in facts else None),
        "max_speed_ms": round(facts["max_speed_ms"], 1) if "max_speed_ms" in facts else None,
    }



def do_analyze(opts) -> dict:
    from fla.analyzer import analyze
    from fla.figures import build_figures, explain
    from fla.report import render_markdown

    t0 = time.time()
    log = Path(opts.log).expanduser().resolve()
    if not log.is_file():
        raise FileNotFoundError(f"日志不存在：{log}")
    outdir = Path(opts.outdir).expanduser().resolve() if opts.outdir else log.parent / "fla-reports"
    outdir.mkdir(parents=True, exist_ok=True)
    fmt = opts.format or "md"
    base = outdir / f"{log.stem}_report"

    analysis = analyze(str(log))
    figures, notes, warnings = [], [], []
    if not opts.no_figures:
        try:
            figures = build_figures(analysis, outdir, prefix=base.name + "_fig",
                                    map_provider=getattr(opts, "map", "amap"))
            notes = explain(analysis)
        except Exception as exc:  # 画图失败不阻塞报告
            warnings.append(f"图表生成失败：{type(exc).__name__}: {exc}")
        tune = analysis.tune or {}
        if tune.get("rate"):
            try:
                from fla.figures import build_tune_figure
                fig = build_tune_figure(analysis.log, tune, outdir / (base.name + "_tune.png"))
                if fig is not None:
                    tune["figure"] = str(fig.path)
                    tune["figure_caption"] = fig.caption
            except Exception as exc:
                warnings.append(f"调参图生成失败：{type(exc).__name__}: {exc}")

    markdown = render_markdown(analysis, figures=figures, notes=notes)
    reports = []
    if fmt in ("md", "all"):
        path = base.with_suffix(".md")
        path.write_text(markdown, encoding="utf-8")
        reports.append({"format": "md", "path": str(path)})
    if fmt in ("html", "all"):
        try:
            from fla.export import markdown_to_html
            path = base.with_suffix(".html")
            markdown_to_html(markdown, path, title=f"{log.stem} 飞控日志分析", base_dir=outdir)
            reports.append({"format": "html", "path": str(path)})
        except Exception as exc:
            if fmt == "html":
                raise RuntimeError(f"HTML 导出失败：{type(exc).__name__}: {exc}") from exc
            warnings.append(f"HTML 导出失败：{type(exc).__name__}: {exc}")
    if fmt in ("docx", "all"):
        try:
            from fla.export import markdown_to_docx
            path = base.with_suffix(".docx")
            markdown_to_docx(docx_safe_markdown(markdown), path,
                             title=f"{log.stem} 飞控日志分析", base_dir=outdir)
            reports.append({"format": "docx", "path": str(path)})
        except Exception as exc:
            if fmt == "docx":
                raise RuntimeError(
                    f"Word 导出失败：{type(exc).__name__}: {exc}。"
                    "fla 的 docx 导出是纯标准库实现，若报错请改用 format=md 或 html。"
                ) from exc
            warnings.append(f"Word 导出失败：{type(exc).__name__}: {exc}")

    findings = analysis.sorted_findings()
    return {
        "ok": True,
        "command": "analyze",
        "file": str(log),
        "outdir": str(outdir),
        "firmware": analysis.summary.get("firmware") if isinstance(analysis.summary, dict) else None,
        "verdict": analysis.verdict,
        "score": analysis.score,
        "verdictText": analysis.verdict_text,
        "summary": compact_summary(analysis.summary or {}),
        "counts": {
            "total": len(findings),
            "critical": sum(1 for f in findings if f.severity == "critical"),
            "warn": sum(1 for f in findings if f.severity == "warn"),
        },
        "findings": [compact_finding(f) for f in findings],
        "skipped": [{"detector": s.detector, "reason": s.reason} for s in (analysis.skipped or [])][:20],
        "warnings": list(getattr(analysis.log, "warnings", []) or [])[:10] + warnings,
        "reports": reports,
        "figures": [str(f.path) for f in figures],
        "tune": compact_tune(analysis.tune),
        "track": compact_track(analysis.log),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def do_tune(opts) -> dict:
    from fla.readers import load
    from fla.tune import evaluate

    t0 = time.time()
    log_path = Path(opts.log).expanduser().resolve()
    if not log_path.is_file():
        raise FileNotFoundError(f"日志不存在：{log_path}")
    log = load(str(log_path))
    tune = evaluate(log)
    payload = {"ok": True, "command": "tune", "file": str(log_path),
               "tune": compact_tune(tune), "elapsed_ms": int((time.time() - t0) * 1000)}
    payload["firmware"] = None
    return payload


def do_doctor(_opts) -> dict:
    info = {"ok": True, "command": "doctor", "python": sys.executable,
            "python_version": sys.version.split()[0], "cwd": os.getcwd(),
            "packages": {}}
    # docx 导出是纯标准库拼 OOXML，不需要 python-docx，别列进来误导人
    for name in ("numpy", "pymavlink", "pyulog", "matplotlib"):
        try:
            mod = __import__(name)
            info["packages"][name] = getattr(mod, "__version__", "已装")
        except Exception as exc:
            info["packages"][name] = f"缺失（{type(exc).__name__}）"
    try:
        import fla
        info["fla"] = getattr(fla, "__version__", "开发版")
        info["fla_root"] = str(Path(fla.__file__).resolve().parent.parent)
    except Exception as exc:
        info["ok"] = False
        info["error"] = f"import fla 失败：{type(exc).__name__}: {exc}"
    return info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fla_bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze")
    a.add_argument("log")
    a.add_argument("--format", default="md", choices=("md", "html", "docx", "all"))
    a.add_argument("--outdir", default=None)
    a.add_argument("--no-figures", action="store_true")
    a.add_argument("--map", default="amap", choices=("amap", "osm", "off"))

    t = sub.add_parser("tune")
    t.add_argument("log")

    sub.add_parser("doctor")
    return parser


#: Word 里没有可靠的彩色 emoji 字体（WPS/LibreOffice 会把 🟨 渲染成怪字形），
#: docx 导出前把严重度图标换成中日韩字体一定有的方括号标签，md/html 仍保留 emoji。
SEV_BADGE = {"\U0001F7E5": "【严重】", "\U0001F7E8": "【警告】", "\U0001F7E6": "【提示】"}


def docx_safe_markdown(markdown: str) -> str:
    out = markdown
    for icon, badge in SEV_BADGE.items():
        out = out.replace(icon + " ", badge).replace(icon, badge)
    return out


def main(argv: list[str]) -> int:
    opts = build_parser().parse_args(argv[1:])
    handlers = {"analyze": do_analyze, "tune": do_tune, "doctor": do_doctor}
    try:
        emit(handlers[opts.command](opts))
    except Exception as exc:
        emit({"ok": False, "command": opts.command,
              "error": f"{type(exc).__name__}: {exc}",
              "trace": traceback.format_exc()[-4000:]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
