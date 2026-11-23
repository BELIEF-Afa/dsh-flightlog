/**
 * @local/dsh-flightlog — 飞控日志分析（flightlog-autopsy）的 DSH 插件。
 *
 * 只做一件事：把 fla 的 analyze / tune 能力包成模型可调用的工具。
 * 所有重活都在 Python 侧（bridge/fla_bridge.py），这里负责路径探测、
 * 进程调用、以及把结果压成模型友好的一段中文。
 */
import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

export const inject = ['tools'];

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const BRIDGE = path.join(PLUGIN_DIR, 'bridge', 'fla_bridge.py');
const SENTINEL = '@@FLA@@';

/* ------------------------------------------------------------------ */
/* 环境探测：fla 源码目录 + 装了依赖的 python                          */
/* ------------------------------------------------------------------ */

/** 从 start 开始逐级向上的目录列表（含起点）。 */
function ancestors(start, limit = 8) {
  const out = [];
  let cur = path.resolve(start);
  for (let i = 0; i < limit; i += 1) {
    out.push(cur);
    const up = path.dirname(cur);
    if (up === cur) break;
    cur = up;
  }
  return out;
}

function isFile(p) {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

function isExecutable(p) {
  try {
    const st = statSync(p);
    return st.isFile() && (st.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

/** 相对路径可能相对的根：宿主 cwd + 插件所在工作区 + 显式环境变量。 */
function searchRoots() {
  const roots = [];
  if (process.env.DSH_WORKSPACE) roots.push(process.env.DSH_WORKSPACE);
  roots.push(process.cwd(), path.dirname(PLUGIN_DIR), PLUGIN_DIR);
  return [...new Set(roots)];
}

function looksLikeFla(dir) {
  return isFile(path.join(dir, 'fla', 'analyzer.py')) && isFile(path.join(dir, 'pyproject.toml'));
}

function findFlaRoot() {
  const fromEnv = process.env.FLA_HOME;
  if (fromEnv) {
    for (const cand of [fromEnv, path.join(fromEnv, 'flightlog-autopsy')]) {
      if (looksLikeFla(cand)) return cand;
    }
  }
  for (const start of searchRoots()) {
    for (const dir of ancestors(start)) {
      for (const cand of [path.join(dir, 'flightlog-autopsy'), dir]) {
        if (looksLikeFla(cand)) return cand;
      }
    }
  }
  return null;
}

function probe(python, flaRoot) {
  return new Promise((resolve) => {
    const env = { ...process.env };
    if (flaRoot) {
      env.PYTHONPATH = [flaRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    }
    execFile(
      python,
      ['-c', 'import fla.analyzer, numpy; print(getattr(__import__("fla"), "__version__", "unknown"))'],
      { cwd: flaRoot || os.tmpdir(), env, timeout: 60000, maxBuffer: 4 << 20 },
      (err, stdout) => {
        if (err) resolve(null);
        else resolve(String(stdout).trim().split('\n').pop() || 'unknown');
      },
    );
  });
}

/* ------------------------------------------------------------------ */
/* 托管环境：机器上没有 fla 检出时，自己建 venv 从 PyPI / 包内 wheel 装  */
/* ------------------------------------------------------------------ */

const MANAGED_DIR = path.resolve(
  process.env.FLA_MANAGED_DIR || path.join(os.homedir(), '.dsh-flightlog'),
);
const MANAGED_VENV = path.join(MANAGED_DIR, 'venv');
const WHEELS_DIR = path.join(PLUGIN_DIR, 'wheels');
const PINNED_SPEC = process.env.FLA_SPEC || 'flightlog-autopsy[figures]==0.2.0';

/** venv 里解释器的位置（Windows 是 Scripts\python.exe）。 */
function venvPython(dir) {
  return process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'python.exe')
    : path.join(dir, 'bin', 'python');
}

/** 子进程执行，失败时把 stderr 尾巴塞进错误里。 */
function execPromise(file, args, { timeout = 600_000, signal } = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      file,
      args,
      { timeout, signal, windowsHide: true, maxBuffer: 32 << 20, env: process.env },
      (err, stdout, stderr) => {
        if (err) {
          if (signal && signal.aborted) {
            reject(new Error('安装已取消。'));
            return;
          }
          const tail = String(stderr || stdout || err.message).trim().slice(-1200);
          reject(new Error(`${path.basename(file)} ${args.slice(0, 2).join(' ')} 失败：${tail}`));
          return;
        }
        resolve({ stdout: String(stdout || ''), stderr: String(stderr || '') });
      },
    );
  });
}

async function pickBasePython() {
  const groups =
    process.platform === 'win32'
      ? [['python3', []], ['python', []], ['py', ['-3']]]
      : [['python3', []], ['python', []]];
  for (const [cmd, prefix] of groups) {
    try {
      await execPromise(cmd, [...prefix, '-c', 'import venv'], { timeout: 20000 });
      return { cmd, prefix };
    } catch {
      /* 试下一个 */
    }
  }
  return null;
}

/** 先看包内有没有自带 wheel，有就本地装（能离线）；没有就走 PyPI。 */
function installSpecs() {
  if (existsSync(WHEELS_DIR)) {
    const wheel = readdirSync(WHEELS_DIR)
      .filter((f) => f.endsWith('.whl') && f.startsWith('flightlog_autopsy'))
      .sort()
      .pop();
    if (wheel) return [path.join(WHEELS_DIR, wheel), 'matplotlib>=3.8'];
  }
  return [PINNED_SPEC];
}

let bootstrapping = null;

/** 返回可用的托管环境；没有就装一个（并发调用共用同一次安装）。 */
async function ensureManagedEnv({ signal } = {}) {
  const py = venvPython(MANAGED_VENV);
  const existing = existsSync(py) ? await probe(py, null) : null;
  if (existing) {
    return { python: py, flaRoot: null, flaVersion: existing, source: 'managed' };
  }
  if (process.env.FLA_NO_BOOTSTRAP) {
    throw new Error(`托管环境不存在（${MANAGED_VENV}），且设置了 FLA_NO_BOOTSTRAP=1，跳过自动安装。`);
  }
  if (bootstrapping) return bootstrapping;

  bootstrapping = (async () => {
    const started = Date.now();
    const base = await pickBasePython();
    if (!base) {
      throw new Error(
        '没有找到可用的 Python 3（试过 python3 / python）。装一个 Python 3.10 以上版本，' +
          '或设置环境变量 FLA_PYTHON 指向一个装了 fla 的解释器。',
      );
    }
    mkdirSync(MANAGED_DIR, { recursive: true });
    await execPromise(base.cmd, [...base.prefix, '-m', 'venv', '--clear', MANAGED_VENV], {
      timeout: 180000,
      signal,
    });
    const args = ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', ...installSpecs()];
    if (existsSync(WHEELS_DIR)) args.push('--find-links', WHEELS_DIR);
    if (process.env.FLA_PIP_INDEX) args.push('--index-url', process.env.FLA_PIP_INDEX);
    await execPromise(py, args, { timeout: 600000, signal });
    const version = await probe(py, null);
    if (!version) throw new Error('依赖装完了，但 import fla.analyzer 仍然失败。');
    return {
      python: py,
      flaRoot: null,
      flaVersion: version,
      source: 'managed',
      installed: true,
      seconds: Math.round((Date.now() - started) / 1000),
    };
  })().finally(() => {
    bootstrapping = null;
  });
  return bootstrapping;
}

let cachedEnv = null;

/**
 * 找出「怎么跑 fla」：先是本地检出（开发机），再退到托管 venv（普通用户）。
 * probeRequired=false 时不做 import 试探（doctor 自检要能报告坏掉的环境）。
 */
async function resolveEnv({ probeRequired = true, signal } = {}) {
  if (cachedEnv) return cachedEnv;
  const flaRoot = findFlaRoot();

  if (flaRoot) {
    const candidates = [];
    if (process.env.FLA_PYTHON) candidates.push(process.env.FLA_PYTHON);
    for (const dir of ancestors(flaRoot || PLUGIN_DIR, 4)) {
      candidates.push(path.join(dir, '.venv', 'bin', 'python'));
      candidates.push(path.join(dir, '.venv', 'bin', 'python3'));
    }
    candidates.push('python3', 'python');
    const usable = [...new Set(candidates)].filter((c) => !c.includes(path.sep) || isExecutable(c));

    const tried = [];
    for (const cand of usable) {
      tried.push(cand);
      const version = await probe(cand, flaRoot);
      if (version) {
        cachedEnv = { python: cand, flaRoot, flaVersion: version, source: 'local', tried };
        return cachedEnv;
      }
    }

    if (!probeRequired) {
      return { python: usable[0] || 'python3', flaRoot, flaVersion: null, source: 'local', tried };
    }
    // 项目在、但本机解释器都 import 不了 → 还能退到托管环境
    try {
      const env = await ensureManagedEnv({ signal });
      cachedEnv = env;
      return env;
    } catch (bootErr) {
      throw new Error(
        `找到 fla 项目（${flaRoot}），但没有可用的 Python 解释器。已尝试：${tried.join('、')}。\n` +
          `托管环境也没装上：${bootErr.message}\n` +
          '请把依赖装进项目的 .venv（numpy / pymavlink / pyulog / matplotlib），' +
          '或用 FLA_PYTHON 指定解释器后重启 dsh web，再用 flightlog_doctor 复查。',
      );
    }
  }

  try {
    const env = await ensureManagedEnv({ signal });
    cachedEnv = env;
    return env;
  } catch (err) {
    if (!probeRequired) {
      return {
        python: 'python3',
        flaRoot: null,
        flaVersion: null,
        source: 'missing',
        bootstrapError: String((err && err.message) || err),
      };
    }
    throw new Error(
      '本机没有 flightlog-autopsy 项目，自动安装也没成功：' + (err && err.message ? err.message : err),
    );
  }
}

/** 把解析到的环境压成一小段结构化信息，附在每次结果里。 */
function describeEnv(env) {
  if (!env) return null;
  const info = {
    source: env.source || 'local',
    python: env.python || null,
    fla_root: env.flaRoot || null,
    fla_version: env.flaVersion || null,
  };
  if (env.installed) {
    info.installed = true;
    info.seconds = env.seconds ?? null;
    info.managed_dir = MANAGED_DIR;
  }
  if (env.bootstrapError) info.bootstrap_error = env.bootstrapError;
  return info;
}

/* ------------------------------------------------------------------ */
/* 调用 bridge                                                         */
/* ------------------------------------------------------------------ */

function runBridge(command, args, { timeoutMs = 600_000, signal, probeRequired = true } = {}) {
  return resolveEnv({ probeRequired, signal }).then(
    (envInfo) =>
      new Promise((resolve, reject) => {
        const { python, flaRoot } = envInfo;
        const env = {
          ...process.env,
          PYTHONPATH: [flaRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
          PYTHONIOENCODING: 'utf-8',
          PYTHONUNBUFFERED: '1',
          MPLBACKEND: 'Agg',
          MPLCONFIGDIR: process.env.MPLCONFIGDIR || path.join(os.tmpdir(), 'dsh-flightlog-mpl'),
        };
        execFile(
          python,
          [BRIDGE, command, ...args],
          { cwd: flaRoot || MANAGED_DIR, env, timeout: timeoutMs, maxBuffer: 64 << 20, signal },
          (err, stdout, stderr) => {
            if (signal && signal.aborted) {
              reject(new Error('调用已取消。'));
              return;
            }
            const text = String(stdout || '');
            const at = text.lastIndexOf(SENTINEL);
            let payload = null;
            if (at >= 0) {
              try {
                payload = JSON.parse(text.slice(at + SENTINEL.length));
              } catch {
                payload = null;
              }
            }
            if (payload) {
              resolve({ ...payload, env_info: describeEnv(envInfo) });
              return;
            }
            const tail = (String(stderr || '').trim() || text.trim()).slice(-3000);
            if (err && err.killed) {
              reject(new Error(`分析进程超时（${Math.round(timeoutMs / 1000)} 秒）。${tail}`));
              return;
            }
            reject(new Error(`分析进程没有返回结果：${(err && err.message) || '未知错误'}\n${tail}`));
          },
        );
      }),
  );
}

/** 把用户给的路径解析成绝对路径（相对路径按宿主 cwd / 工作区依次试）。 */
function resolveInput(raw, { mustExist = true } = {}) {
  const input = String(raw || '').trim();
  if (!input) throw new Error('缺少日志路径。');
  const expanded = input.startsWith('~/') ? path.join(os.homedir(), input.slice(2)) : input;
  if (path.isAbsolute(expanded)) return expanded;
  const bases = [process.cwd(), ...searchRoots()];
  let fallback = null;
  for (const base of bases) {
    const cand = path.resolve(base, expanded);
    if (fallback === null) fallback = cand;
    if (!mustExist || existsSync(cand)) return cand;
  }
  return fallback;
}

const FIELD_ALIASES = new Set(['.bin', '.log', '.ulg', '.csv', '.tlog', '.bbl', '.bfl']);

function describeInput(file) {
  const ext = path.extname(file).toLowerCase();
  if (ext === '.bbl' || ext === '.bfl') {
    throw new Error(
      `Betaflight 黑匣子（${ext}）不在 fla 的支持范围内。` +
        '先用 blackbox_decode 转成 CSV，再以 .csv 传入；或直接说明要按 Betaflight 分析，换别的工具链。',
    );
  }
  if (ext && !FIELD_ALIASES.has(ext)) {
    // 不拦截，只提示，fla 会给出自己的错误
    return file;
  }
  return file;
}

/* ------------------------------------------------------------------ */
/* 文本渲染                                                            */
/* ------------------------------------------------------------------ */

function fmtClock(sec) {
  if (typeof sec !== 'number' || !Number.isFinite(sec)) return '';
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, '0');
  return `${String(m).padStart(2, '0')}:${s}`;
}

function clip(text, limit) {
  const s = String(text ?? '');
  return s.length > limit ? `${s.slice(0, limit)}…` : s;
}

function severityTag(sev) {
  if (sev === 'critical') return '[critical]';
  if (sev === 'warn') return '[warn]';
  return `[${sev || 'info'}]`;
}

function statsLine(summary) {
  if (!summary || typeof summary !== 'object') return '';
  const bits = [];
  if (summary.log_format) bits.push(String(summary.log_format));
  if (typeof summary.duration_s === 'number') bits.push(`时长 ${summary.duration_s}s`);
  if (typeof summary.armed_s === 'number') bits.push(`解锁 ${summary.armed_s}s`);
  if (typeof summary.distance_m === 'number') bits.push(`航程 ${Math.round(summary.distance_m)}m`);
  if (typeof summary.max_alt_m === 'number') bits.push(`最大高度 ${summary.max_alt_m}m`);
  if (typeof summary.max_speed_ms === 'number') bits.push(`最高速度 ${summary.max_speed_ms}m/s`);
  if (summary.vehicle) bits.push(String(summary.vehicle));
  return bits.join(' · ');
}

/** 托管环境相关的补充说明（只在需要时出现）。 */
function envNote(value) {
  const info = value && value.env_info;
  if (!info) return null;
  if (info.installed) {
    return `环境：首次运行已自动安装 fla（${info.seconds ?? '?'}s，托管 venv 在 ${info.managed_dir || MANAGED_DIR}）`;
  }
  if (info.bootstrap_error) return `环境：${clip(info.bootstrap_error, 300)}`;
  return null;
}

function renderAnalyze(value) {
  const lines = [];
  lines.push(`飞控日志分析 · ${path.basename(value.file || '')}`);
  const firmware = value.firmware || (value.summary && value.summary.firmware) || '未知固件';
  lines.push(`固件 ${firmware} · ${statsLine(value.summary)}`);
  const counts = value.counts || {};
  lines.push(
    `结论：${clip(value.verdictText || value.verdict || '未知', 600)}` +
      `\n评分 ${value.score ?? '?'}/100（证据强度）· 检出 ${counts.total ?? 0} 条` +
      `（critical ${counts.critical ?? 0} / warn ${counts.warn ?? 0}）`,
  );

  const findings = Array.isArray(value.findings) ? value.findings : [];
  if (findings.length) {
    lines.push('', '证据：');
    for (const f of findings) {
      const at = f.t_start != null ? ` @${fmtClock(f.t_start)}` : '';
      lines.push(`${severityTag(f.severity)} ${f.title}${at}`);
      const evidence = Array.isArray(f.evidence) ? f.evidence : [];
      for (const ev of evidence.slice(0, 4)) lines.push(`    · ${clip(ev, 300)}`);
      if (evidence.length > 4) lines.push(`    · …另有 ${evidence.length - 4} 条，见报告`);
      if (f.conclusion) lines.push(`    → ${clip(f.conclusion, 300)}`);
    }
  }

  const tune = value.tune;
  if (tune) {
    const gate = (tune.gate || [])
      .filter((g) => g.level !== 'ok')
      .map((g) => `${g.title}：${g.detail}`);
    const sugg = (tune.suggestions || []).slice(0, 4).map(
      (s) => `${s.param} ${s.current} → ${s.suggested}（${s.level}）`,
    );
    lines.push('', `调参体检：${tune.verdict || '—'} · IMU ${tune.imu_hz ?? '?'} Hz`);
    if (gate.length) lines.push(`  质量门：${gate.join('；')}`);
    if (sugg.length) lines.push(`  建议：${sugg.join('；')}`);
  }

  const track = value.track;
  if (track && track.max_range_m != null) {
    const home = track.home ? `${track.home.lat.toFixed(5)}, ${track.home.lng.toFixed(5)}` : '—';
    const peak = track.max_range_at
      ? ` @${fmtClock(track.max_range_at.t)}（${track.max_range_at.lat.toFixed(5)}, ${track.max_range_at.lng.toFixed(5)}）`
      : '';
    lines.push(
      '',
      `航迹：起点 ${home} · 最远 ${track.max_range_m} m${peak} · 航迹长 ${track.path_m} m` +
        ` · 终点偏移 ${track.final_offset_m} m` +
        (Array.isArray(track.alt_range_m)
          ? ` · 高度 ${track.alt_range_m[0]}~${track.alt_range_m[1]} m`
          : ''),
    );
  }

  const skipped = Array.isArray(value.skipped) ? value.skipped : [];
  if (skipped.length) {
    lines.push('', `未覆盖：${skipped.slice(0, 4).map((s) => `${s.detector}（${s.reason}）`).join('；')}`);
  }
  const warnings = Array.isArray(value.warnings) ? value.warnings : [];
  if (warnings.length) {
    lines.push('', `注意：${warnings.slice(0, 4).map((w) => clip(w, 200)).join('；')}`);
  }

  const reports = Array.isArray(value.reports) ? value.reports : [];
  if (reports.length) {
    lines.push('', `报告：${reports.map((r) => `${r.format.toUpperCase()} ${r.path}`).join('\n      ')}`);
  }
  const figures = Array.isArray(value.figures) ? value.figures : [];
  if (figures.length) {
    lines.push(`图表（${figures.length} 张）：${figures.join('\n      ')}`);
  }
  const note = envNote(value);
  if (note) lines.push(note);
  lines.push(`用时 ${((value.elapsed_ms ?? 0) / 1000).toFixed(1)}s · 输出目录 ${value.outdir || ''}`);
  return lines.join('\n');
}

function renderTune(value) {
  const tune = value.tune || {};
  const lines = [`调参体检 · ${path.basename(value.file || '')}`];
  lines.push(`结论：${tune.verdict || '—'} · IMU ${tune.imu_hz ?? '?'} Hz`);
  const gate = tune.gate || [];
  if (gate.length) {
    lines.push('', '质量门：');
    for (const g of gate) lines.push(`  [${g.level}] ${g.title}：${g.detail}`);
  }
  const rate = tune.rate || {};
  if (Object.keys(rate).length) {
    lines.push(
      '',
      `速率环：记录 ${rate.fs ?? '?'} Hz，横滚 RMS ${rate.roll_rms ?? '?'}°/s（跟随 ${rate.roll_follow ?? '?'}），` +
        `俯仰 RMS ${rate.pitch_rms ?? '?'}°/s（跟随 ${rate.pitch_follow ?? '?'}）`,
    );
  }
  const delay = tune.delay || {};
  if (Object.keys(delay).length) {
    lines.push(
      `延迟：${delay.delay_ms ?? '?'} ms（相干度 ${delay.coherence ?? '?'}，FLTT ${delay.fltt_hz ?? '?'} Hz，` +
        `残差 ${delay.residual_ms ?? '?'} ms）`,
    );
  }
  const sugg = tune.suggestions || [];
  if (sugg.length) {
    lines.push('', '建议：');
    for (const s of sugg) {
      lines.push(`  [${s.level}] ${s.param}：${s.current} → ${s.suggested}`);
      if (s.reason) lines.push(`      ${clip(s.reason, 400)}`);
    }
  }
  const note = envNote(value);
  if (note) lines.push('', note);
  lines.push('', `用时 ${((value.elapsed_ms ?? 0) / 1000).toFixed(1)}s`);
  return lines.join('\n');
}

function renderDoctor(value) {
  const lines = ['环境自检（flightlog-autopsy / dsh-flightlog）'];
  lines.push(`python：${value.python}（${value.python_version}）`);
  lines.push(`fla：${value.fla || '未知'} @ ${value.fla_root || '未知'}`);
  const info = value.env_info;
  if (info) {
    const where =
      info.source === 'managed'
        ? `托管 venv（${info.managed_dir || MANAGED_DIR}）${info.installed ? '，本次刚装好' : ''}`
        : info.source === 'local'
          ? '本地项目检出'
          : '未就绪';
    lines.push(`来源：${where}`);
    if (info.python) lines.push(`解释器：${info.python}`);
    if (info.bootstrap_error) lines.push(`自动安装失败：${clip(info.bootstrap_error, 400)}`);
  }
  const packages = value.packages || {};
  lines.push(`依赖：${Object.entries(packages).map(([k, v]) => `${k} ${v}`).join('，')}`);
  if (value.error) lines.push(`错误：${value.error}`);
  return lines.join('\n');
}

/* ------------------------------------------------------------------ */
/* 工具定义                                                            */
/* ------------------------------------------------------------------ */

const textBlock = (text) => [{ type: 'text', text }];

const boundarySchema = {
  type: 'object',
  additionalProperties: true,
  properties: { ok: { type: 'boolean' } },
  required: ['ok'],
};

function defineTool(definition) {
  return definition;
}

const analyzeTool = defineTool({
  name: 'flightlog_analyze',
  description:
    '分析 ArduPilot(.bin/.log) / PX4(.ulg) 飞控日志，产出事故结论、评分、证据链（每条含时间与证据）、' +
    '调参体检摘要，并写出 Markdown/HTML/Word 报告与图表文件。用于坠机、失控、动力失效、日志中断等事故判读，' +
    '也用于飞行后体检。Betaflight 黑匣子(.bbl) 不支持，先用 blackbox_decode 转 CSV。',
  parameters: {
    type: 'object',
    additionalProperties: false,
    properties: {
      log: { type: 'string', description: '日志文件路径（绝对路径，或相对工作区的路径）。' },
      format: {
        type: 'string',
        enum: ['md', 'html', 'docx', 'all'],
        description: '报告格式，默认 md。html 适合浏览器打开/打印 PDF；docx 由 fla 自带 OOXML 导出器生成，不依赖 python-docx。',
      },
      figures: { type: 'boolean', description: '是否生成图表（四联图 + 航迹图 + 调参图），默认 true。' },
      map: {
        type: 'string',
        enum: ['amap', 'osm', 'off'],
        description: '航迹底图：amap 高德（默认，中国大陆最稳）/ osm / off（本地网格，离线用）。',
      },
      outdir: { type: 'string', description: '报告输出目录，默认是日志同级的 fla-reports/。' },
    },
    required: ['log'],
  },
  output: {
    schema: boundarySchema,
    render(_args, value) {
      return textBlock(renderAnalyze(value));
    },
  },
  timeoutMs: 600_000,
  isConcurrencySafe: false,
  async execute(args, exec) {
    const file = describeInput(resolveInput(args.log));
    if (!isFile(file)) throw new Error(`日志不存在：${file}`);
    const outdir = args.outdir ? resolveInput(args.outdir, { mustExist: false }) : undefined;
    const format = args.format || 'md';
    const argv = [file, '--format', format];
    if (outdir) argv.push('--outdir', outdir);
    if (args.figures === false) argv.push('--no-figures');
    if (args.map) argv.push('--map', args.map);
    const result = await runBridge('analyze', argv, { signal: exec && exec.signal });
    if (!result.ok) {
      throw new Error(`${result.error || '分析失败'}\n${clip(result.trace || '', 1500)}`);
    }
    return result;
  },
});

const tuneTool = defineTool({
  name: 'flightlog_tune',
  description:
    '对 ArduPilot 日志做调参体检：质量门（振动/削顶/IMU 记录率/电机饱和）、速率环跟踪、' +
    '响应延迟与相干度、参数建议（含 MOT_THST_HOVER / 陷波 / PID 是否该动）。' +
    '质量门不过时不会给 PID 数值——这是设计，别当缺项。',
  parameters: {
    type: 'object',
    additionalProperties: false,
    properties: {
      log: { type: 'string', description: '日志文件路径（绝对路径，或相对工作区的路径）。' },
    },
    required: ['log'],
  },
  output: {
    schema: boundarySchema,
    render(_args, value) {
      return textBlock(renderTune(value));
    },
  },
  timeoutMs: 300_000,
  isConcurrencySafe: true,
  async execute(args, exec) {
    const file = describeInput(resolveInput(args.log));
    if (!isFile(file)) throw new Error(`日志不存在：${file}`);
    const result = await runBridge('tune', [file], { timeoutMs: 300_000, signal: exec && exec.signal });
    if (!result.ok) {
      throw new Error(`${result.error || '调参体检失败'}\n${clip(result.trace || '', 1500)}`);
    }
    return result;
  },
});

const doctorTool = defineTool({
  name: 'flightlog_doctor',
  description:
    '自检飞控日志分析环境：报告用的是哪个 python、fla 源码在哪、依赖（numpy/pymavlink/pyulog/matplotlib）是否齐全。' +
    '分析工具报错或换机器后先跑它。',
  parameters: { type: 'object', additionalProperties: false, properties: {} },
  output: {
    schema: boundarySchema,
    render(_args, value) {
      return textBlock(renderDoctor(value));
    },
  },
  timeoutMs: 120_000,
  isConcurrencySafe: true,
  async execute(_args, exec) {
    const result = await runBridge('doctor', [], { probeRequired: false, signal: exec && exec.signal });
    return { ...result, ok: true };
  },
});

export function apply(ctx) {
  ctx.tools.register(analyzeTool);
  ctx.tools.register(tuneTool);
  ctx.tools.register(doctorTool);
}
