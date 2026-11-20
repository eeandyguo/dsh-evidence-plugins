/**
 * Host-side directory watch service for the evidence toolkit.
 *
 * Not a tool: it registers nothing on ctx.tools. One watcher per configured
 * directory, one debounce timer and one cooldown floor per watcher:
 *
 *   fs.watch(dir) -> collect paths -> debounce -> cooldown -> one burst
 *
 * A burst always appends one JSONL line, and when the entry names a Session it
 * also sends one prompt through the Host Session controller, which resumes a
 * cold Session and wakes its Agent.
 *
 * fs.watch is a trigger signal, not an audit trail: Windows may coalesce or drop
 * events, so a burst lists what was observed, not what necessarily changed.
 */
import { randomUUID } from 'node:crypto'
import { watch as watchDirectory } from 'node:fs'
import { appendFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** <workspace>/plugins/<package>/watch.js -> <workspace> */
const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const DEFAULT_LOG = '.dsh/evidence-watch/events.jsonl'
const DEFAULT_DEBOUNCE_MS = 500
const DEFAULT_COOLDOWN_MS = 15000
const MAX_RECORDED_PATHS = 50
const MAX_PROMPT_PATHS = 20
const MAX_PROMPT_PATH_LENGTH = 120
const PROMPT_TIMEOUT_MS = 15000

/**
 * Coerce one numeric setting into a safe range.
 * @param {unknown} value - configured value.
 * @param {number} fallback - value used when the setting is absent or not a finite number.
 * @param {number} min - smallest accepted value.
 * @param {number} max - largest accepted value.
 * @returns {number} the clamped integer.
 */
function clampInteger(value, fallback, min, max) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(value)))
}

/**
 * Render one error for a log line without losing its code.
 * @param {unknown} error - thrown value.
 * @returns {string} stable single-line text.
 */
function errorText(error) {
  if (error !== null && typeof error === 'object') {
    const name = typeof error.name === 'string' ? error.name : 'Error'
    const message = typeof error.message === 'string' ? error.message : String(error)
    const code = typeof error.code === 'string' && error.code !== '' ? ` [${error.code}]` : ''
    return `${name}: ${message}${code}`
  }
  return String(error)
}

/** Normalize a watched relative path to forward slashes for the log and the prompt. */
function normalized(relative) {
  return relative.replaceAll('\\', '/')
}

/**
 * Make one observed path safe to embed in a prompt line: no newlines, no C0
 * control characters, bounded length. A crafted file name must not be able to
 * forge extra instruction lines.
 * @param {unknown} value - observed relative path.
 * @returns {string} single-line, bounded path text.
 */
export function sanitizePromptPath(value) {
  const text = String(value)
    .replace(/[\r\n]+/g, ' ')
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .trim()
  return text.length <= MAX_PROMPT_PATH_LENGTH
    ? text
    : `${text.slice(0, MAX_PROMPT_PATH_LENGTH - 1)}…`
}

/**
 * Build the model-facing prompt for one burst.
 * @param {{ dir: string }} state - watcher state.
 * @param {string[]} paths - observed relative paths.
 * @returns {string} prompt text; it states that it is not user input.
 */
function renderPromptText(state, paths) {
  const lines = [
    '[evidence-watch] 目录监听事件（后台服务生成，不是用户输入）',
    `目录：${sanitizePromptPath(state.dir)}`,
    `时间：${new Date().toLocaleString('zh-CN', { hour12: false })}`,
    `变化：${paths.length} 处`,
  ]
  for (const relative of paths.slice(0, MAX_PROMPT_PATHS)) lines.push(`- ${sanitizePromptPath(relative)}`)
  if (paths.length > MAX_PROMPT_PATHS) lines.push(`… 另有 ${paths.length - MAX_PROMPT_PATHS} 处`)
  lines.push('需要处理时再用 evidence 等工具；不需要可以忽略这条。')
  return lines.join('\n')
}

/**
 * Start the configured watchers and return their disposer.
 *
 * Malformed settings never throw: each rejected entry is reported in the start
 * record and through ctx.logger, and the remaining entries still run.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - Host context; `sessionController` is looked up lazily per burst.
 * @param {unknown} watchConfig - the `watch` object of the plugin row config.
 * @returns {() => void} disposer closing every watcher and timer.
 */
export function startWatch(ctx, watchConfig) {
  const config = watchConfig !== null && typeof watchConfig === 'object' ? watchConfig : {}
  const configuredRoot = typeof process.env.DSH_EVIDENCE_WORKSPACE === 'string'
    ? process.env.DSH_EVIDENCE_WORKSPACE.trim()
    : ''
  const root = configuredRoot !== '' ? path.resolve(configuredRoot) : PACKAGE_ROOT
  const configuredLog = typeof config.log === 'string' && config.log.trim() !== ''
    ? config.log.trim()
    : DEFAULT_LOG
  const logPath = path.resolve(root, configuredLog)
  const entries = Array.isArray(config.entries) ? config.entries : []

  /** @type {Array<Record<string, any>>} */
  const states = []
  /** @type {Array<{ dir: string, error: string }>} */
  const errors = []
  let stopped = false
  let writeChain = Promise.resolve()

  // The log directory may not exist yet; every append waits for this first.
  const ready = mkdir(path.dirname(logPath), { recursive: true }).catch((error) => {
    ctx.logger.warn(`evidence-watch: 无法创建日志目录 ${path.dirname(logPath)}：${errorText(error)}`)
  })

  /**
   * Append one JSON line, serialized behind earlier writes.
   * @param {Record<string, unknown>} record - event record.
   * @returns {Promise<void>} settles after this line is written or the failure is logged.
   */
  function appendRecord(record) {
    const line = `${JSON.stringify(record)}\n`
    writeChain = writeChain
      .then(() => ready)
      .then(() => appendFile(logPath, line, 'utf8'))
      .catch((error) => {
        ctx.logger.warn(`evidence-watch: 写 ${logPath} 失败：${errorText(error)}`)
      })
    return writeChain
  }

  /**
   * Arm or re-arm one watcher's flush at the later of the debounce and cooldown boundaries.
   * @param {Record<string, any>} state - watcher state.
   */
  function scheduleFlush(state) {
    if (stopped) return
    if (state.timer !== null) clearTimeout(state.timer)
    const now = Date.now()
    const readyAt = Math.max(now + state.debounceMs, state.lastFire + state.cooldownMs)
    state.timer = setTimeout(() => {
      state.timer = null
      void flush(state)
    }, Math.max(0, readyAt - now))
  }

  /**
   * Collect one observed path and schedule its burst.
   * @param {Record<string, any>} state - watcher state.
   * @param {string | Buffer | null} filename - name reported by fs.watch.
   */
  function onEvent(state, filename) {
    if (stopped) return
    try {
      const raw = typeof filename === 'string' && filename !== '' ? normalized(filename) : '(unnamed)'
      const relative = path.isAbsolute(raw) ? normalized(path.relative(state.dir, raw)) : raw
      if (relative === '' || relative === '.' || relative === state.logRelative) return
      state.pending.add(relative)
      scheduleFlush(state)
    } catch (error) {
      ctx.logger.warn(`evidence-watch: 处理 ${state.dir} 的事件失败：${errorText(error)}`)
    }
  }

  /**
   * Emit one burst: log it, then optionally wake its Session.
   * @param {Record<string, any>} state - watcher state.
   */
  async function flush(state) {
    if (stopped || state.pending.size === 0) return
    const paths = [...state.pending]
    state.pending.clear()
    state.lastFire = Date.now()
    const record = {
      kind: 'burst',
      at: new Date().toISOString(),
      dir: state.dir,
      count: paths.length,
      paths: paths.slice(0, MAX_RECORDED_PATHS),
      truncated: Math.max(0, paths.length - MAX_RECORDED_PATHS),
      delivered: false,
      error: '',
    }

    if (state.sessionId !== '') {
      const controller = ctx.get('sessionController')
      if (controller === undefined || controller === null) {
        record.error = 'Host 没有 sessionController 服务'
      } else {
        try {
          await controller.prompt({
            requestId: `watch-${randomUUID()}`,
            sessionId: state.sessionId,
            mode: state.mode,
            content: [{ type: 'text', text: renderPromptText(state, paths) }],
          }, AbortSignal.timeout(PROMPT_TIMEOUT_MS))
          record.delivered = true
        } catch (error) {
          record.error = errorText(error)
        }
      }
      if (record.delivered !== true) {
        ctx.logger.warn(`evidence-watch: 投递到 ${state.sessionId} 失败：${record.error}`)
      }
    }

    await appendRecord(record)
  }

  for (const entry of entries) {
    const rawDir = entry !== null && typeof entry === 'object' && typeof entry.dir === 'string'
      ? entry.dir.trim()
      : ''
    if (rawDir === '') {
      errors.push({ dir: '', error: 'dir 缺失或不是字符串' })
      ctx.logger.warn('evidence-watch: 跳过一条缺少 dir 的监听项')
      continue
    }
    const dir = path.resolve(root, rawDir)
    const state = {
      dir,
      label: rawDir,
      recursive: entry.recursive !== false,
      debounceMs: clampInteger(entry.debounceMs, DEFAULT_DEBOUNCE_MS, 50, 60000),
      cooldownMs: clampInteger(entry.cooldownMs, DEFAULT_COOLDOWN_MS, 0, 600000),
      sessionId: typeof entry.sessionId === 'string' ? entry.sessionId.trim() : '',
      mode: entry.mode === 'steer' ? 'steer' : 'queue',
      pending: new Set(),
      timer: null,
      lastFire: 0,
      watcher: null,
      logRelative: normalized(path.relative(dir, logPath)),
    }
    try {
      const watcher = watchDirectory(dir, { recursive: state.recursive }, (eventType, filename) => {
        onEvent(state, filename)
      })
      watcher.on('error', (error) => {
        ctx.logger.warn(`evidence-watch: 监听 ${dir} 出错：${errorText(error)}`)
        try { watcher.close() } catch { /* the watcher may already be closed */ }
      })
      state.watcher = watcher
      states.push(state)
    } catch (error) {
      errors.push({ dir, error: errorText(error) })
      ctx.logger.warn(`evidence-watch: 无法监听 ${dir}：${errorText(error)}`)
    }
  }

  void appendRecord({
    kind: 'start',
    at: new Date().toISOString(),
    root,
    log: logPath,
    watches: states.map((state) => ({
      dir: state.dir,
      recursive: state.recursive,
      sessionId: state.sessionId,
      mode: state.mode,
      debounceMs: state.debounceMs,
      cooldownMs: state.cooldownMs,
    })),
    errors,
  })

  return function stop() {
    stopped = true
    for (const state of states) {
      if (state.timer !== null) {
        clearTimeout(state.timer)
        state.timer = null
      }
      if (state.watcher !== null) {
        try { state.watcher.close() } catch { /* ignore an already-closed watcher */ }
        state.watcher = null
      }
      state.pending.clear()
    }
  }
}
