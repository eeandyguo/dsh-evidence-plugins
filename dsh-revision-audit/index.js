import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, readdirSync } from 'node:fs'
import { readFile, readdir, rm, stat } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { startWatch } from './watch.js'

// Host 半边：只做调度。所有检查逻辑在 engine/*.py，本文件不碰业务规则。
// PATH 上没有 python，必须用绝对路径；config.pythonPath / DSH_EVIDENCE_PYTHON 可覆盖。
const DEFAULT_PYTHON = guessDefaultPython()

/** 优先用显式配置/环境变量；否则遍历 DSH 运行时目录，最后退回 PATH。 */
function guessDefaultPython() {
  const explicit = typeof process.env.DSH_EVIDENCE_PYTHON === 'string'
    ? process.env.DSH_EVIDENCE_PYTHON.trim()
    : ''
  if (explicit !== '') return explicit
  const home = typeof process.env.DSH_HOME === 'string' && process.env.DSH_HOME !== ''
    ? process.env.DSH_HOME
    : path.join(homedir(), '.dsh')
  const runtimes = path.join(home, 'dsh-runtimes')
  const exe = process.platform === 'win32' ? 'python.exe' : 'python3'
  try {
    for (const name of readdirSync(runtimes)) {
      const candidate = path.join(runtimes, name, 'dependencies', 'python', exe)
      if (existsSync(candidate)) return candidate
    }
  } catch { /* fall through */ }
  return exe
}
const ENGINE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), 'engine')
const REVISION_ENGINE = path.join(ENGINE_DIR, 'revision_audit.py')
const STATS_ENGINE = path.join(ENGINE_DIR, 'stats_test.py')
const BACKUP_ENGINE = path.join(ENGINE_DIR, 'backup_verify.py')
const OFFICE_ENGINE = path.join(ENGINE_DIR, 'office_lint.py')
const WORD_SCRIPT = path.join(path.dirname(ENGINE_DIR), 'word-pdf.ps1')
/** sha256 of word-pdf.ps1; regenerate whenever that script changes. */
const WORD_SCRIPT_SHA256 = '962936dbf7c21a867f30f5d8d64d88c8ace35c6b36f48ccc6a6dbf77e52452d3'

const DEFAULT_CODE = 'print(1)'
const PROBE_TIMEOUT_MS = 15000
const REVISION_TIMEOUT_MS = 120000
const STATS_TIMEOUT_MS = 120000
const BACKUP_TIMEOUT_MS = 300000
const OFFICE_TIMEOUT_MS = 240000
const WORD_TIMEOUT_MS = 150000
const PAGE_CAP = 120
const RENDER_LIMITS = { maxPixels: 8 * 1024 * 1024, maxDimension: 8192, maxBytes: 64 * 1024 * 1024 }
const WORD_PDF_CACHE_LIMIT = 8
const WORD_PDF_CACHE_TTL_MS = 10 * 60 * 1000

/** Exported PDF bytes keyed by path + size + mtime; short-lived to avoid repeated Word starts. */
const WORD_PDF_CACHE = new Map()

export const inject = ['tools']

/** Shared model-facing schema for one finding entry. */
function findingSchema() {
  return {
    type: 'object',
    properties: {
      severity: { type: 'string', enum: ['error', 'warn', 'info'] },
      code: { type: 'string' },
      where: { type: 'string' },
      detail: { type: 'string' },
      evidence: { type: 'string' },
    },
    required: ['severity', 'code', 'where', 'detail', 'evidence'],
    additionalProperties: false,
  }
}

/** Shared model-facing schema for the receipt. */
function receiptSchema() {
  return {
    type: 'object',
    properties: {
      createdAt: { type: 'string' },
      engineVersion: { type: 'string' },
    },
    required: ['createdAt', 'engineVersion'],
    additionalProperties: false,
  }
}

/**
 * Spawn one process and collect its streams.
 * Never throws: a failed spawn is reported as exitCode -1 with the reason in stderr,
 * because telling EPERM/ENOENT apart from a real run is part of the diagnosis.
 * @param {string} executable - absolute interpreter path.
 * @param {string[]} argv - arguments.
 * @param {number} timeoutMs - hard budget before the child is killed.
 * @param {string} [input] - optional stdin text.
 * @param {{killOnTimeout?: boolean}} [options] - set killOnTimeout false to let the child finish its own cleanup.
 * @returns {Promise<{exitCode: number, stdout: string, stderr: string, durationMs: number}>}
 */
function runProcess(executable, argv, timeoutMs, input, options = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now()
    let stdout = ''
    let stderr = ''
    let settled = false
    let timer

    const finish = (exitCode, note) => {
      if (settled) return
      settled = true
      if (timer !== undefined) clearTimeout(timer)
      const combined = note === undefined
        ? stderr
        : (stderr === '' ? note : `${stderr}\n${note}`)
      resolve({
        exitCode,
        stdout,
        stderr: combined,
        durationMs: Date.now() - startedAt,
      })
    }

    let child
    try {
      child = spawn(executable, argv, {
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        stdio: [input === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'],
      })
    } catch (error) {
      finish(-1, `spawn threw: ${error instanceof Error ? error.message : String(error)}`)
      return
    }

    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    if (input !== undefined && child.stdin !== null) {
      child.stdin.on('error', () => { /* the child may exit before reading stdin */ })
      child.stdin.end(input)
    }
    child.on('error', (error) => {
      const name = typeof error?.code === 'string' && error.code !== '' ? ` [${error.code}]` : ''
      finish(-1, `spawn error: ${error instanceof Error ? error.message : String(error)}${name}`)
    })
    child.on('close', (exitCode, signal) => {
      finish(exitCode === null ? -1 : exitCode, signal === null ? undefined : `terminated by signal ${String(signal)}`)
    })

    timer = setTimeout(() => {
      if (options.killOnTimeout !== false) {
        try { child.kill() } catch { /* the child may already be gone */ }
      }
      finish(-1, `timeout after ${timeoutMs} ms`)
    }, timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()
  })
}

/** Run an engine, require exit 0, and parse its single JSON result. */
async function runEngine(python, engine, argv, timeoutMs, label, input) {
  const run = await runProcess(python, [engine, ...argv], timeoutMs, input)
  if (run.exitCode !== 0) {
    const tail = run.stderr.trim().slice(0, 500)
    throw new Error(`${label} 引擎退出码 ${run.exitCode}${tail === '' ? '' : `：${tail}`}`)
  }
  try {
    return JSON.parse(run.stdout)
  } catch {
    throw new Error(`${label} 引擎输出不是 JSON（前 300 字）：${run.stdout.slice(0, 300)}`)
  }
}

function windowsPowerShell() {
  const root = typeof process.env.SystemRoot === 'string' && process.env.SystemRoot !== ''
    ? process.env.SystemRoot
    : 'C:\\Windows'
  const candidates = [
    path.join(root, 'SysWOW64', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
  ]
  return candidates.find((candidate) => existsSync(candidate)) ?? ''
}

function cachePdf(key) {
  const entry = WORD_PDF_CACHE.get(key)
  if (entry === undefined) return undefined
  if (Date.now() - entry.at > WORD_PDF_CACHE_TTL_MS) {
    WORD_PDF_CACHE.delete(key)
    return undefined
  }
  entry.at = Date.now()
  return entry
}

function rememberPdf(key, entry) {
  WORD_PDF_CACHE.set(key, entry)
  while (WORD_PDF_CACHE.size > WORD_PDF_CACHE_LIMIT) {
    const oldest = WORD_PDF_CACHE.keys().next().value
    WORD_PDF_CACHE.delete(oldest)
  }
}

/** Whether a PowerShell run failed because the execution policy refused the script. */
function policyDenied(run) {
  const text = `${run.stdout}\n${run.stderr}`
  return /running scripts is disabled|PSSecurityException|UnauthorizedAccess|执行策略|禁止运行脚本|无法加载.*脚本/i.test(text)
}

/**
 * List Word owner files (`~$*`) in one directory.
 * @param {string} directory - directory holding the document.
 * @returns {Promise<Set<string>>} owner-file names, empty when the directory cannot be read.
 */
async function listWordLocks(directory) {
  try {
    const entries = await readdir(directory)
    return new Set(entries.filter((name) => name.startsWith('~$')))
  } catch {
    return new Set()
  }
}

/**
 * Run word-pdf.ps1 once. Prefers RemoteSigned; if the machine policy refuses
 * the script, retries with Bypass and reports the reason through `warn`.
 * @param {string} shell - PowerShell executable.
 * @param {string[]} args - script arguments.
 * @param {(message: string) => void} warn - logger sink.
 * @returns {Promise<{run: object, policyNote: string}>} process result and an optional policy note.
 */
async function runWordScript(shell, args, warn) {
  const base = ['-NoProfile', '-NonInteractive', '-File', WORD_SCRIPT, ...args]
  const strict = await runProcess(shell, ['-ExecutionPolicy', 'RemoteSigned', ...base], WORD_TIMEOUT_MS, undefined, { killOnTimeout: false })
  if (strict.exitCode !== 0 && policyDenied(strict)) {
    const reason = `${strict.stdout}\n${strict.stderr}`.trim().slice(0, 200)
    warn(`evidence-toolkit: RemoteSigned 拒绝执行 word-pdf.ps1（${reason}），改用 Bypass 重试`)
    const relaxed = await runProcess(shell, ['-ExecutionPolicy', 'Bypass', ...base], WORD_TIMEOUT_MS, undefined, { killOnTimeout: false })
    return { run: relaxed, policyNote: 'PowerShell 执行策略回退 Bypass' }
  }
  return { run: strict, policyNote: '' }
}

/**
 * Export one .docx to PDF with Microsoft Word over COM (32-bit PowerShell).
 * The PDF bytes are cached by path + size + mtime for a short while. A timeout
 * only abandons this call; word-pdf.ps1 owns Word cleanup, nothing is killed.
 * @param {string} resolved - absolute .docx path.
 * @param {(message: string) => void} warn - logger sink.
 * @returns {Promise<{version: string, bytes: Uint8Array, cached: boolean, policyNote: string}>} PDF bytes.
 */
async function exportPdfWithWord(resolved, warn) {
  const shell = windowsPowerShell()
  if (shell === '') throw new Error('没有可用的 Windows PowerShell')
  if (!existsSync(WORD_SCRIPT)) throw new Error('缺少 word-pdf.ps1')
  const actual = createHash('sha256').update(await readFile(WORD_SCRIPT)).digest('hex')
  if (actual !== WORD_SCRIPT_SHA256) {
    throw new Error('word-pdf.ps1 完整性校验失败（sha256 不匹配），拒绝走 Word COM')
  }
  let info
  try {
    info = await stat(resolved)
  } catch {
    throw new Error('读不到待处理文件')
  }
  const cacheKey = `${resolved}|${info.size}|${Math.round(info.mtimeMs)}`
  const cached = cachePdf(cacheKey)
  if (cached !== undefined) {
    return { version: cached.version, bytes: cached.bytes, cached: true, policyNote: '' }
  }

  const pdfPath = path.join(tmpdir(), `dsh-word-pdf-${randomUUID()}.pdf`)
  const sourceDir = path.dirname(resolved)
  const locksBefore = await listWordLocks(sourceDir)
  try {
    const { run, policyNote } = await runWordScript(
      shell, ['-Source', resolved, '-Pdf', pdfPath], warn,
    )
    const output = `${run.stdout}\n${run.stderr}`.trim()
    if (run.exitCode === 3 || output.includes('BUSY')) {
      throw new Error('Word 已在运行，跳过 COM 以免影响用户会话')
    }
    if (run.exitCode !== 0 || !output.includes('OK ')) {
      throw new Error(`Word COM 导出失败：${output === '' ? `退出码 ${run.exitCode}` : output.slice(0, 300)}`)
    }
    let pdfInfo
    try {
      pdfInfo = await stat(pdfPath)
    } catch {
      throw new Error('Word 没有生成 PDF 文件')
    }
    if (!pdfInfo.isFile() || pdfInfo.size === 0) throw new Error('Word 生成的 PDF 是空文件')
    const version = `${pdfInfo.size}-${Math.round(pdfInfo.mtimeMs)}`
    const bytes = new Uint8Array(await readFile(pdfPath))
    rememberPdf(cacheKey, { bytes, version, at: Date.now() })
    return { version, bytes, cached: false, policyNote }
  } finally {
    await rm(pdfPath, { force: true }).catch(() => {})
    for (const name of await listWordLocks(sourceDir)) {
      if (locksBefore.has(name)) continue
      await rm(path.join(sourceDir, name), { force: true }).catch(() => {})
    }
  }
}

/**
 * Render every page (capped) of one source through the Host Office kit.
 * @param {object} kit - mounted officeKit service.
 * @param {{extension: string, source: object}} request - document format and source.
 * @returns {Promise<{pageCount: number, images: object[], missingFonts: string[]}>} render result.
 */
async function renderDocumentPages(kit, { extension, source }) {
  const probe = await kit.renderImages(
    { extension, source, pages: [1], dpi: 96, maxImages: 1, ...RENDER_LIMITS },
    AbortSignal.timeout(120000),
  )
  const pageCount = Number(probe.pageCount) || 0
  let missingFonts = Array.isArray(probe.missingFonts) ? probe.missingFonts : []
  let images = pageCount <= 1 ? (probe.images ?? []) : []
  if (pageCount > 1) {
    const wanted = Math.min(pageCount, PAGE_CAP)
    const full = await kit.renderImages(
      { extension, source, dpi: 96, maxImages: wanted, ...RENDER_LIMITS },
      AbortSignal.timeout(180000),
    )
    images = full.images ?? []
    if (Array.isArray(full.missingFonts) && full.missingFonts.length > 0) missingFonts = full.missingFonts
  }
  return { pageCount, images, missingFonts }
}

function formatP(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '?'
  if (value === 0) return '0'
  if (value < 1e-3) return value.toExponential(3)
  return value.toFixed(6)
}

function renderProbe(_args, value) {
  const out = typeof value.stdout === 'string' ? value.stdout.trim() : ''
  if (value.exitCode === 0 && out !== '') return [{ type: 'text', text: out }]

  const lines = [`exitCode: ${value.exitCode}`]
  if (out !== '') lines.push(`stdout: ${out}`)
  const err = typeof value.stderr === 'string' ? value.stderr.trim() : ''
  if (err !== '') lines.push(`stderr: ${err}`)
  lines.push(`durationMs: ${value.durationMs}`)
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderRevision(_args, value) {
  const stats = value.stats ?? {}
  const findings = Array.isArray(value.findings) ? value.findings : []
  const total = typeof value.findingsTotal === 'number' ? value.findingsTotal : findings.length
  const lines = [
    `verdict: ${value.verdict}　改动块: ${stats.hunks ?? '?'}　发现项: ${total}`
      + (total > findings.length ? `（仅列 ${findings.length}）` : ''),
    `旧: ${stats.oldPath ?? '?'}（${stats.oldLines ?? '?'} 行）`,
    `新: ${stats.newPath ?? '?'}（${stats.newLines ?? '?'} 行）`,
  ]
  const declaration = value.declaration ?? {}
  if (typeof declaration.path === 'string' && declaration.path !== '') {
    lines.push(
      `申报单: ${declaration.items} 条（可锚定 ${declaration.anchored}，命中 ${declaration.matched}）`
        + `　未申报 ${declaration.undeclaredTotal} 块　覆盖不确定 ${(declaration.uncertain ?? []).length} 块`,
    )
    for (const item of (declaration.undeclared ?? []).slice(0, 10)) {
      lines.push(`- 未申报 ${item.where}：${item.preview}`)
    }
    if (Array.isArray(declaration.missing) && declaration.missing.length > 0) {
      lines.push(`- 申报未改：${declaration.missing.join(', ')}`)
    }
  }
  for (const item of findings.slice(0, 20)) {
    const evidence = typeof item.evidence === 'string' && item.evidence !== '' ? `　例：${item.evidence}` : ''
    lines.push(`- [${item.severity}] ${item.code} ${item.where}：${item.detail}${evidence}`)
  }
  if (findings.length > 20) lines.push(`… 其余 ${findings.length - 20} 项见 JSON 逐块明细`)
  if (typeof value.reportPath === 'string' && value.reportPath !== '') lines.push(`报告：${value.reportPath}`)
  if (value.verdict === 'unknown' && typeof declaration.path === 'string' && declaration.path === '') {
    lines.push('注：未给申报单时本工具只报客观改动块，不判「已申报/未申报」。')
  }
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderStats(_args, value) {
  const stats = value.stats ?? {}
  const findings = Array.isArray(value.findings) ? value.findings : []
  const total = typeof value.findingsTotal === 'number' ? value.findingsTotal : findings.length
  const lines = [
    `verdict: ${value.verdict}　检验: ${stats.test ?? '?'}　数据: ${stats.used ?? '?'}/${stats.rows ?? '?'} 行`,
  ]
  if (typeof stats.dropped === 'number' && stats.dropped > 0) {
    lines.push(`- 剔除 ${stats.dropped} 行（非数字或缺分组标签）`)
  }
  const result = stats.result
  if (result) {
    lines.push(
      `- 结果: ${result.method}　statistic=${typeof result.statistic === 'number' ? result.statistic.toFixed(4) : result.statistic}`
        + `　df=${result.df}　p=${formatP(result.p)}　${result.effectName}=${typeof result.effectValue === 'number' ? result.effectValue.toFixed(4) : result.effectValue}`,
    )
  } else {
    lines.push('- 结果: 未执行（见发现项）')
  }
  const assumptions = Array.isArray(stats.assumptions) ? stats.assumptions : []
  if (assumptions.length > 0) {
    lines.push(`- 前提检查: ${assumptions.map((item) => `${item.name}=${item.status}`).join('；')}`)
  }
  for (const item of findings.slice(0, 10)) {
    const evidence = typeof item.evidence === 'string' && item.evidence !== '' ? `（${item.evidence}）` : ''
    lines.push(`- [${item.severity}] ${item.code} ${item.where}：${item.detail}${evidence}`)
  }
  if (findings.length > 10) lines.push(`… 其余 ${findings.length - 10} 项见 JSON`)
  if (typeof value.reportPath === 'string' && value.reportPath !== '') lines.push(`报告：${value.reportPath}`)
  if (value.verdict === 'unknown') lines.push('注：verdict=unknown 表示前提检查未全过，p 值不能当结论用。')
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderOfficeLint(_args, value) {
  const stats = value.stats ?? {}
  const findings = Array.isArray(value.findings) ? value.findings : []
  const total = typeof value.findingsTotal === 'number' ? value.findingsTotal : findings.length
  const renderer = stats.renderer === 'word-com' ? 'Word COM' : 'LibreOffice'
  const lines = [
    `verdict: ${value.verdict}　${stats.extension ?? '?'}　${renderer}　页 ${stats.renderedPages ?? '?'}/${stats.pages ?? '?'} @ ${stats.dpi ?? '?'} DPI`,
    `- 空白页 ${(stats.blankPages ?? []).length}　图片 ${stats.imagesChecked ?? '?'}（低分辨率 ${stats.lowDpiImages ?? '?'}，最低 ${stats.minImageDpi ?? '?'} DPI）`,
  ]
  if (typeof stats.rendererNote === 'string' && stats.rendererNote !== '') {
    lines.push(`- 渲染回退：${stats.rendererNote}`)
  }
  const fonts = Array.isArray(stats.missingFonts) ? stats.missingFonts : []
  if (fonts.length > 0) lines.push(`- 缺失字体: ${fonts.join(', ')}`)
  if (typeof stats.slides === 'number' && stats.slides > 0) {
    lines.push(`- 幻灯片 ${stats.slides}　自动缩字 ${stats.autofitShrunk}　图表 ${stats.charts}（引用不一致 ${stats.chartRefMismatches}）`)
  }
  for (const item of findings.slice(0, 15)) {
    const evidence = typeof item.evidence === 'string' && item.evidence !== '' ? `（${item.evidence}）` : ''
    lines.push(`- [${item.severity}] ${item.code} ${item.where}：${item.detail}${evidence}`)
  }
  if (findings.length > 15) lines.push(`… 其余 ${findings.length - 15} 项见 JSON`)
  if (typeof value.reportPath === 'string' && value.reportPath !== '') lines.push(`报告：${value.reportPath}`)
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderBackup(_args, value) {
  const stats = value.stats ?? {}
  const findings = Array.isArray(value.findings) ? value.findings : []
  const total = typeof value.findingsTotal === 'number' ? value.findingsTotal : findings.length
  const byKind = stats.sampledByKind && typeof stats.sampledByKind === 'object'
    ? Object.entries(stats.sampledByKind).map(([kind, count]) => `${kind}=${count}`).join(',')
    : ''
  const lines = [
    `verdict: ${value.verdict}　来源: ${stats.kind ?? '?'}　文件: ${stats.files ?? '?'} → 还原 ${stats.restoredFiles ?? '?'}`,
    `- 哈希不符 ${stats.mismatched ?? '?'}　缺失 ${stats.missing ?? '?'}　多出 ${stats.extra ?? '?'}`
      + `　抽样 请求 ${stats.sampleRequested ?? '?'} · 成功 ${stats.sampleOpened ?? '?'}`
      + ` · 跳过（临时文件）${stats.sampleSkipped ?? '?'} · 失败 ${stats.sampleFailed ?? '?'}`
      + `${byKind === '' ? '' : `（${byKind}）`}　耗时 ${stats.durationMs ?? '?'} ms`,
    `- 临时目录已清理: ${stats.tempRemoved === true ? '是' : '否'}`,
  ]
  for (const item of findings.slice(0, 15)) {
    const evidence = typeof item.evidence === 'string' && item.evidence !== '' ? `（${item.evidence}）` : ''
    lines.push(`- [${item.severity}] ${item.code} ${item.where}：${item.detail}${evidence}`)
  }
  if (findings.length > 15) lines.push(`… 其余 ${findings.length - 15} 项见 JSON`)
  if (typeof value.reportPath === 'string' && value.reportPath !== '') lines.push(`报告：${value.reportPath}`)
  return [{ type: 'text', text: lines.join('\n') }]
}

export function apply(ctx, config) {
  const configured = typeof config?.pythonPath === 'string' ? config.pythonPath.trim() : ''
  const fromEnv = typeof process.env.DSH_EVIDENCE_PYTHON === 'string' ? process.env.DSH_EVIDENCE_PYTHON.trim() : ''
  const python = configured || fromEnv || DEFAULT_PYTHON
  const rendererMode = config !== null && typeof config === 'object' && config.renderer === 'libreoffice'
    ? 'libreoffice'
    : 'auto'
  const warn = (message) => {
    try { ctx.logger.warn(message) } catch { /* logging is best effort */ }
  }

  // 目录监听是后台服务，不是工具：它不注册 schema，只在自己的 effect 里起停。
  const watchConfig = config !== null && typeof config === 'object' ? config.watch : undefined
  if (watchConfig !== undefined && watchConfig !== null) {
    ctx.effect(() => startWatch(ctx, watchConfig), 'evidence-toolkit: watch service')
  }

  const probeDefinition = {
    name: 'probe',
    description: 'Run a short Python snippet with the local interpreter and return its stdout. '
      + 'Diagnostic probe for the evidence toolkit bundle: it confirms whether a Host plugin can start Python and read its output. '
      + 'A spawn failure is reported as exitCode -1 with the reason in stderr.',
    parameters: {
      type: 'object',
      properties: {
        code: {
          type: 'string',
          description: 'Python source passed to the interpreter as -c. Omit to run print(1).',
        },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          python: { type: 'string', description: 'Interpreter path actually spawned.' },
          code: { type: 'string', description: 'Source that was run.' },
          exitCode: { type: 'integer', description: 'Process exit code; -1 when the process never ran or was killed.' },
          stdout: { type: 'string' },
          stderr: { type: 'string', description: 'Empty on a clean run; carries the spawn or timeout reason otherwise.' },
          durationMs: { type: 'integer' },
        },
        required: ['python', 'code', 'exitCode', 'stdout', 'stderr', 'durationMs'],
        additionalProperties: false,
      },
      render: renderProbe,
    },
    async execute(args) {
      const requested = args !== null && typeof args === 'object' && typeof args.code === 'string'
        ? args.code.trim()
        : ''
      const code = requested !== '' ? args.code : DEFAULT_CODE
      const run = await runProcess(python, ['-c', code], PROBE_TIMEOUT_MS)
      return { python, code, ...run }
    },
  }

  const revisionDefinition = {
    name: 'revision_audit',
    description: 'Compare an old and a new LaTeX draft and report the objective change blocks: pure insertions, '
      + 'pure deletions, LaTeX command additions or removals, plus per-block intensity and line ranges. '
      + 'With a declaration sheet (a 直改清单 Markdown table), every block is matched to a declared item through '
      + 'the backticked fragment in that item\'s 原文 column: a block matching no item is 未申报 (fail), '
      + 'an anchored item matching no block is 申报未改 (fail). Items without such a fragment make the verdict unknown '
      + 'instead of guessing. Without a declaration sheet the verdict stays unknown. '
      + 'Pass absolute paths; the optional report path receives the full Markdown detail.',
    parameters: {
      type: 'object',
      properties: {
        old: { type: 'string', description: 'Absolute path of the old .tex file.' },
        new: { type: 'string', description: 'Absolute path of the new .tex file.' },
        declaration: {
          type: 'string',
          description: 'Optional absolute path of the declaration sheet: a Markdown table with 位置/原文 columns (fragments in backticks become anchors) or 条目/文件/块数 columns.',
        },
        report: { type: 'string', description: 'Optional absolute path; when given, the per-block Markdown detail is written there.' },
      },
      required: ['old', 'new'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          tool: { type: 'string' },
          target: { type: 'string' },
          hash: { type: 'string', description: 'sha256 of the new file.' },
          verdict: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
          findings: { type: 'array', items: findingSchema() },
          findingsTotal: { type: 'integer', description: 'Total finding count before the delivery cap.' },
          declaration: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Empty when no declaration sheet was supplied.' },
              sha256: { type: 'string' },
              items: { type: 'integer' },
              anchored: { type: 'integer', description: 'Items carrying at least one usable anchor.' },
              matched: { type: 'integer', description: 'Items whose anchor matched at least one block.' },
              undeclared: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    hunk: { type: 'integer' },
                    where: { type: 'string' },
                    preview: { type: 'string' },
                  },
                  required: ['hunk', 'where', 'preview'],
                  additionalProperties: false,
                },
              },
              undeclaredTotal: { type: 'integer' },
              uncertain: { type: 'array', items: { type: 'integer' }, description: 'Block numbers that cannot be attributed mechanically.' },
              missing: { type: 'array', items: { type: 'string' }, description: 'Anchored item ids that matched no block.' },
            },
            required: ['path', 'sha256', 'items', 'anchored', 'matched', 'undeclared', 'undeclaredTotal', 'uncertain', 'missing'],
            additionalProperties: false,
          },
          stats: {
            type: 'object',
            properties: {
              oldPath: { type: 'string' },
              newPath: { type: 'string' },
              oldHash: { type: 'string' },
              newHash: { type: 'string' },
              oldLines: { type: 'integer' },
              newLines: { type: 'integer' },
              hunks: { type: 'integer' },
              tagCounts: { type: 'object' },
              commandsOld: { type: 'object' },
              commandsNew: { type: 'object' },
              expect: { type: 'integer', description: 'Declared direct-edit count, or -1 when not given.' },
              todo: { type: 'integer', description: 'Declared TODO count, or -1 when not given.' },
            },
            required: [
              'oldPath', 'newPath', 'oldHash', 'newHash', 'oldLines', 'newLines',
              'hunks', 'tagCounts', 'commandsOld', 'commandsNew', 'expect', 'todo',
            ],
            additionalProperties: false,
          },
          reportPath: { type: 'string', description: 'Empty when no report was requested.' },
          receipt: receiptSchema(),
        },
        required: [
          'tool', 'target', 'hash', 'verdict', 'findings', 'findingsTotal',
          'declaration', 'stats', 'reportPath', 'receipt',
        ],
        additionalProperties: false,
      },
      render: renderRevision,
    },
    async execute(args) {
      const oldPath = args !== null && typeof args === 'object' && typeof args.old === 'string' ? args.old.trim() : ''
      const newPath = args !== null && typeof args === 'object' && typeof args.new === 'string' ? args.new.trim() : ''
      if (oldPath === '' || newPath === '') throw new Error('revision_audit 需要 old 与 new 两个 .tex 绝对路径')

      const argv = [REVISION_ENGINE, '--old', oldPath, '--new', newPath]
      const reportPath = args !== null && typeof args === 'object' && typeof args.report === 'string' ? args.report.trim() : ''
      if (reportPath !== '') argv.push('--report', reportPath)
      const declarationPath = args !== null && typeof args === 'object' && typeof args.declaration === 'string'
        ? args.declaration.trim()
        : ''
      if (declarationPath !== '') argv.push('--declaration', declarationPath)

      return await runEngine(python, REVISION_ENGINE, argv.slice(1), REVISION_TIMEOUT_MS, 'revision_audit')
    },
  }

  const statsDefinition = {
    name: 'stats_test',
    description: 'Run one statistical test on a CSV file and report the statistic, p value, effect size, '
      + 'and the preconditions it could check: Welch t-test (ttest), one-way ANOVA (anova), Pearson chi-square (chi2), '
      + 'Mann-Whitney U (mannwhitney), or Kruskal-Wallis (kruskal). '
      + 'p values are computed locally without scipy; normality is NOT tested, skew is only a hint. '
      + 'verdict reflects whether the preconditions hold, not whether the result is significant. '
      + 'For chi2 pass group and group2; for the others pass group and value.',
    parameters: {
      type: 'object',
      properties: {
        data: { type: 'string', description: 'Absolute path of the CSV file (utf-8 or gb18030).' },
        test: { type: 'string', enum: ['ttest', 'anova', 'chi2', 'mannwhitney', 'kruskal'] },
        group: { type: 'string', description: 'Column holding the group label.' },
        value: { type: 'string', description: 'Numeric column to test (required for every test except chi2).' },
        group2: { type: 'string', description: 'Second categorical column (chi2 only).' },
        report: { type: 'string', description: 'Optional absolute path for the Markdown report.' },
      },
      required: ['data', 'test', 'group'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          tool: { type: 'string' },
          target: { type: 'string' },
          hash: { type: 'string' },
          verdict: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
          findings: { type: 'array', items: findingSchema() },
          findingsTotal: { type: 'integer' },
          stats: {
            type: 'object',
            properties: {
              data: { type: 'string' },
              test: { type: 'string' },
              value: { type: 'string' },
              group: { type: 'string' },
              group2: { type: 'string' },
              rows: { type: 'integer' },
              used: { type: 'integer' },
              dropped: { type: 'integer' },
              groups: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    name: { type: 'string' },
                    n: { type: 'integer' },
                    mean: { type: 'number' },
                    sd: { type: 'number' },
                    median: { type: 'number' },
                    skew: { type: 'number' },
                  },
                  required: ['name', 'n'],
                  additionalProperties: false,
                },
              },
              result: {
                type: 'object',
                properties: {
                  method: { type: 'string' },
                  statistic: { type: 'number' },
                  df: { type: 'string' },
                  p: { type: 'number' },
                  effectName: { type: 'string' },
                  effectValue: { type: 'number' },
                },
                required: ['method', 'statistic', 'df', 'p', 'effectName', 'effectValue'],
                additionalProperties: false,
              },
              assumptions: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    name: { type: 'string' },
                    status: { type: 'string', enum: ['ok', 'warn', 'na'] },
                    detail: { type: 'string' },
                  },
                  required: ['name', 'status', 'detail'],
                  additionalProperties: false,
                },
              },
            },
            required: ['data', 'test', 'value', 'group', 'group2', 'rows', 'used', 'dropped', 'groups', 'assumptions'],
            additionalProperties: false,
          },
          reportPath: { type: 'string' },
          receipt: receiptSchema(),
        },
        required: ['tool', 'target', 'hash', 'verdict', 'findings', 'findingsTotal', 'stats', 'reportPath', 'receipt'],
        additionalProperties: false,
      },
      render: renderStats,
    },
    async execute(args) {
      const data = args !== null && typeof args === 'object' && typeof args.data === 'string' ? args.data.trim() : ''
      if (data === '') throw new Error('stats_test 需要 data（CSV 绝对路径）')
      const argv = ['--data', data]
      const test = args !== null && typeof args === 'object' && typeof args.test === 'string' ? args.test.trim() : ''
      if (test === '') throw new Error('stats_test 需要 test（ttest/anova/chi2/mannwhitney/kruskal）')
      argv.push('--test', test)
      for (const [key, flag] of [['group', '--group'], ['value', '--value'], ['group2', '--group2'], ['report', '--report']]) {
        const raw = args !== null && typeof args === 'object' && typeof args[key] === 'string' ? args[key].trim() : ''
        if (raw !== '') argv.push(flag, raw)
      }
      return await runEngine(python, STATS_ENGINE, argv, STATS_TIMEOUT_MS, 'stats_test')
    },
  }

  const backupDefinition = {
    name: 'backup_verify',
    description: 'Restore a backup (a .zip or a directory) into a temporary directory, compare every file\'s sha256 '
      + 'between the backup and the restored copy, then sample-open docx/pptx/xlsx (OOXML unpack), sqlite '
      + '(integrity_check) and zip (testzip) members. '
      + 'It proves the backup restores losslessly; it cannot prove the backup matches the original machine. '
      + 'The temporary directory is removed before returning.',
    parameters: {
      type: 'object',
      properties: {
        source: { type: 'string', description: 'Absolute path of the backup: a .zip file or a directory.' },
        report: { type: 'string', description: 'Optional absolute path for the Markdown report.' },
      },
      required: ['source'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          tool: { type: 'string' },
          target: { type: 'string' },
          hash: { type: 'string', description: 'Aggregate sha256 over the sorted file manifest.' },
          verdict: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
          findings: { type: 'array', items: findingSchema() },
          findingsTotal: { type: 'integer' },
          stats: {
            type: 'object',
            properties: {
              source: { type: 'string' },
              kind: { type: 'string', enum: ['dir', 'zip', 'unsupported'] },
              files: { type: 'integer' },
              bytes: { type: 'integer' },
              restoredFiles: { type: 'integer' },
              restoredBytes: { type: 'integer' },
              mismatched: { type: 'integer' },
              missing: { type: 'integer' },
              extra: { type: 'integer' },
              sampleRequested: { type: 'integer' },
              sampleOpened: { type: 'integer' },
              sampleSkipped: { type: 'integer', description: 'Entries skipped because they are Office lock or OS temp files.' },
              sampleFailed: { type: 'integer', description: 'Sampled entries that opened with a problem.' },
              sampledByKind: { type: 'object' },
              durationMs: { type: 'integer' },
              tempRemoved: { type: 'boolean' },
            },
            required: [
              'source', 'kind', 'files', 'bytes', 'restoredFiles', 'restoredBytes',
              'mismatched', 'missing', 'extra', 'sampleRequested', 'sampleOpened',
              'sampleSkipped', 'sampleFailed', 'sampledByKind', 'durationMs', 'tempRemoved',
            ],
            additionalProperties: false,
          },
          reportPath: { type: 'string' },
          receipt: receiptSchema(),
        },
        required: ['tool', 'target', 'hash', 'verdict', 'findings', 'findingsTotal', 'stats', 'reportPath', 'receipt'],
        additionalProperties: false,
      },
      render: renderBackup,
    },
    async execute(args) {
      const source = args !== null && typeof args === 'object' && typeof args.source === 'string' ? args.source.trim() : ''
      if (source === '') throw new Error('backup_verify 需要 source（.zip 或目录的绝对路径）')
      const argv = ['--source', source]
      const reportPath = args !== null && typeof args === 'object' && typeof args.report === 'string' ? args.report.trim() : ''
      if (reportPath !== '') argv.push('--report', reportPath)
      return await runEngine(python, BACKUP_ENGINE, argv, BACKUP_TIMEOUT_MS, 'backup_verify')
    },
  }

  const officeDefinition = {
    name: 'office_lint',
    description: 'Lint a .docx or .pptx for objective layout defects: blank pages found by actually rendering the file, '
      + 'fonts the renderer had to substitute, image effective resolution (displayed size against pixel size), '
      + 'text auto-shrink recorded by PowerPoint, and chart series references whose row count disagrees with the cached points. '
      + 'It does not judge aesthetics. For .docx on Windows it first tries Microsoft Word over COM (read-only, exported to PDF) '
      + 'so pagination matches Word; when Word is unavailable or busy it falls back to the Host Office kit (LibreOffice) '
      + 'and reports which renderer ran. Pass an absolute path; the optional report path receives the Markdown detail.',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: 'Absolute path of the .docx or .pptx to lint.' },
        report: { type: 'string', description: 'Optional absolute path for the Markdown report.' },
      },
      required: ['file'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          tool: { type: 'string' },
          target: { type: 'string' },
          hash: { type: 'string', description: 'sha256 of the linted file.' },
          verdict: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
          findings: { type: 'array', items: findingSchema() },
          findingsTotal: { type: 'integer' },
          stats: {
            type: 'object',
            properties: {
              file: { type: 'string' },
              extension: { type: 'string' },
              kind: { type: 'string', enum: ['word', 'powerpoint'] },
              pages: { type: 'integer' },
              renderedPages: { type: 'integer' },
              dpi: { type: 'integer' },
              minInkRatio: { type: 'number' },
              blankPages: { type: 'array', items: { type: 'integer' } },
              missingFonts: { type: 'array', items: { type: 'string' } },
              imagesChecked: { type: 'integer' },
              lowDpiImages: { type: 'integer' },
              minImageDpi: { type: 'number' },
              slides: { type: 'integer' },
              autofitShrunk: { type: 'integer' },
              charts: { type: 'integer' },
              chartRefMismatches: { type: 'integer' },
              renderMs: { type: 'integer' },
              renderError: { type: 'string', description: 'Empty on a successful render; the failure reason otherwise.' },
              renderer: { type: 'string', enum: ['word-com', 'libreoffice'], description: 'Which renderer produced the pages.' },
              rendererNote: { type: 'string', description: 'Why Word COM was skipped or fell back; empty when Word COM was not attempted or succeeded.' },
              durationMs: { type: 'integer' },
            },
            required: [
              'file', 'extension', 'kind', 'pages', 'renderedPages', 'dpi', 'minInkRatio', 'blankPages',
              'missingFonts', 'imagesChecked', 'lowDpiImages', 'minImageDpi', 'slides', 'autofitShrunk',
              'charts', 'chartRefMismatches', 'renderMs', 'renderError', 'renderer', 'rendererNote', 'durationMs',
            ],
            additionalProperties: false,
          },
          reportPath: { type: 'string' },
          receipt: receiptSchema(),
        },
        required: ['tool', 'target', 'hash', 'verdict', 'findings', 'findingsTotal', 'stats', 'reportPath', 'receipt'],
        additionalProperties: false,
      },
      render: renderOfficeLint,
    },
    async execute(args) {
      const file = args !== null && typeof args === 'object' && typeof args.file === 'string' ? args.file.trim() : ''
      if (file === '') throw new Error('office_lint 需要 file（.docx / .pptx 绝对路径）')
      const resolved = path.resolve(file)
      const extension = path.extname(resolved).slice(1).toLowerCase()
      if (extension !== 'docx' && extension !== 'pptx') {
        throw new Error(`office_lint 只支持 docx / pptx，收到 .${extension}`)
      }
      const kit = ctx.get('officeKit')
      if (kit === undefined || kit === null) {
        throw new Error('office_lint 需要 Host 的 Office 渲染服务（officeKit 未挂载）')
      }

      let info
      try {
        info = await stat(resolved)
      } catch {
        throw new Error(`找不到文件：${resolved}`)
      }
      if (!info.isFile()) throw new Error(`不是普通文件：${resolved}`)
      const bytes = new Uint8Array(await readFile(resolved))
      const version = `${info.size}-${Math.round(info.mtimeMs)}`
      const source = {
        key: `${resolved}|${version}`,
        version,
        bytes: bytes.byteLength,
        read: async () => ({ bytes, version }),
      }
      const limits = RENDER_LIMITS
      const startedAt = Date.now()
      let pageCount = 0
      let missingFonts = []
      let images = []
      let renderError = ''
      let renderer = 'libreoffice'
      let rendererNote = ''
      // 先验 zip 魔数：坏容器直接跳过渲染，别为一份坏文件起 Word/PowerShell。
      const isZip = bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b
        && bytes[2] === 0x03 && bytes[3] === 0x04
      if (!isZip) {
        renderError = '文件不是有效的 OOXML（zip）包，跳过渲染；结构检查会给出具体原因'
      } else {
        // 渲染失败不该连结构检查一起丢：捕获后照常跑引擎，由引擎报 RENDER_FAILED + unknown。
        try {
          if (extension === 'docx' && rendererMode === 'auto') {
            try {
              const exported = await exportPdfWithWord(resolved, warn)
              const wordSource = {
                key: `${resolved}|${exported.version}`,
                version: exported.version,
                bytes: exported.bytes.byteLength,
                read: async () => ({ bytes: exported.bytes, version: exported.version }),
              }
              const rendered = await renderDocumentPages(kit, { extension: 'pdf', source: wordSource })
              pageCount = rendered.pageCount
              missingFonts = rendered.missingFonts
              images = rendered.images
              renderer = 'word-com'
            } catch (error) {
              rendererNote = error instanceof Error ? error.message : String(error)
            }
          }
          if (renderer !== 'word-com') {
            const rendered = await renderDocumentPages(kit, { extension, source })
            pageCount = rendered.pageCount
            missingFonts = rendered.missingFonts
            images = rendered.images
          }
        } catch (error) {
          renderError = error instanceof Error ? error.message : String(error)
        }
      }

      const pages = images.map((image) => ({
        page: typeof image.page === 'number' ? image.page : null,
        width: image.width,
        height: image.height,
        png: Buffer.from(image.png).toString('base64'),
      }))
      const payload = JSON.stringify({
        pages,
        pageCount,
        dpi: 96,
        missingFonts,
        renderMs: Date.now() - startedAt,
        renderError,
        renderer,
        rendererNote,
      })

      const argv = ['--file', resolved]
      const reportPath = args !== null && typeof args === 'object' && typeof args.report === 'string'
        ? args.report.trim()
        : ''
      if (reportPath !== '') argv.push('--report', reportPath)
      return await runEngine(python, OFFICE_ENGINE, argv, OFFICE_TIMEOUT_MS, 'office_lint', payload)
    },
  }

  const definitions = [revisionDefinition]
  const byName = new Map(definitions.map((definition) => [definition.name, definition]))

  const toolbox = {
    name: 'revision_audit',
    description: revisionDefinition.description,
    parameters: revisionDefinition.parameters,
    output: revisionDefinition.output,
    async execute(args, exec) {
      return await revisionDefinition.execute(args, exec)
    },
  }

  ctx.effect(() => ctx.tools.register(toolbox), 'revision-audit: revision_audit')
}
