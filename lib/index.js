// dsh-pdf-translate · 宿主半边
// 职责：存配置、挑文件、按队列起 Python 子进程、把进度和产物回传给界面。
// 翻译逻辑全在 translation-workflow/tools/translate.py 里，这边不重复实现。
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import {
  RT_PY, RT_SITE, WF_DIR, hasRuntime, hasBabeldoc, hasWorkflow, install as installRuntime,
} from './setup.js'

export const name = 'pdf-translate'
export const inject = ['webServer']

const P = '/dsh-pdf-translate'
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const CFG_PATH = join(DSH_HOME, 'pdf-translate.json')
const HIST_PATH = join(DSH_HOME, 'pdf-translate-history.json')
const HIST_MAX = 50

const DEFAULTS = {
  workflowDir: '',
  python: 'python',
  model: 'deepseek-chat',
  baseUrl: 'https://api.deepseek.com/v1',
  apiKey: '',
  glossary: '',
  pages: 'all',
  skipRefs: true,
  parallel: 2,
}

const HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'access-control-allow-origin': '*',
  'access-control-allow-headers': '*',
  'access-control-allow-methods': 'GET,POST,OPTIONS',
  'access-control-allow-private-network': 'true',
  'cache-control': 'no-store',
}

// 用 Windows 自带的选择框：DSH 是 Electron，弹窗会出现在桌面上（可能不在窗口前面）
const PICK_SCRIPT = `
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = '选择要翻译的 PDF'
$d.Filter = 'PDF 文件|*.pdf|所有文件|*.*'
$d.Multiselect = $true
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  foreach ($f in $d.FileNames) { Write-Output $f }
}
`

function loadCfg() {
  let cfg
  try {
    cfg = { ...DEFAULTS, ...JSON.parse(readFileSync(CFG_PATH, 'utf8')) }
  } catch {
    cfg = { ...DEFAULTS }
  }
  // 装了便携运行时的话，流水线目录就有默认值了，用户不用再自己填
  if (!cfg.workflowDir && hasWorkflow()) cfg.workflowDir = WF_DIR
  if (!cfg.python && hasRuntime()) cfg.python = RT_PY
  return cfg
}

function saveCfg(patch) {
  const next = { ...loadCfg(), ...patch }
  mkdirSync(DSH_HOME, { recursive: true })
  writeFileSync(CFG_PATH, JSON.stringify(next, null, 2), 'utf8')
  return next
}

// ---------- 历史记录：重启后还能找回上次的产物 ----------
function loadHistory() {
  try {
    const arr = JSON.parse(readFileSync(HIST_PATH, 'utf8'))
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function pushHistory(entry) {
  const list = loadHistory()
  list.unshift({ ...entry, at: Date.now() })
  try {
    mkdirSync(DSH_HOME, { recursive: true })
    writeFileSync(HIST_PATH, JSON.stringify(list.slice(0, HIST_MAX), null, 2), 'utf8')
  } catch {}
  return list
}

// ---------- 极简 CSV 读写（术语表用；只处理带引号字段这一种情况） ----------
function parseCsv(text) {
  const rows = []
  let row = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        field += '"'
        i++
      } else if (ch === '"') {
        quoted = false
      } else {
        field += ch
      }
    } else if (ch === '"') {
      quoted = true
    } else if (ch === ',') {
      row.push(field)
      field = ''
    } else if (ch === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (ch !== '\r') {
      field += ch
    }
  }
  if (field.length || row.length) {
    row.push(field)
    rows.push(row)
  }
  return rows.filter((r) => r.some((c) => String(c).trim() !== ''))
}

function toCsv(rows) {
  const esc = (v) => {
    const s = String(v == null ? '' : v)
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
  }
  return rows.map((r) => r.map(esc).join(',')).join('\n') + '\n'
}

function readGlossary(path) {
  const raw = readFileSync(path, 'utf8').replace(/^\uFEFF/, '')
  const rows = parseCsv(raw)
  if (!rows.length) return { header: ['en', 'zh', 'note', 'source', 'status'], items: [] }
  const header = rows[0].map((h) => h.trim())
  const idx = {
    en: header.indexOf('en'),
    zh: header.indexOf('zh'),
    note: header.indexOf('note'),
    source: header.indexOf('source'),
    status: header.indexOf('status'),
  }
  const items = rows.slice(1).map((r) => ({
    en: (r[idx.en] || '').trim(),
    zh: (r[idx.zh] || '').trim(),
    note: idx.note >= 0 ? (r[idx.note] || '').trim() : '',
    source: idx.source >= 0 ? (r[idx.source] || '').trim() : '',
    status: (idx.status >= 0 ? (r[idx.status] || '').trim() : '') || 'candidate',
  })).filter((x) => x.en || x.zh)
  return { header, items }
}

function writeGlossary(path, items) {
  const rows = [['en', 'zh', 'note', 'source', 'status']]
  for (const it of items) {
    if (!it || (!it.en && !it.zh)) continue
    rows.push([it.en || '', it.zh || '', it.note || '', it.source || '', it.status || 'candidate'])
  }
  writeFileSync(path, toCsv(rows), 'utf8')
}

export function apply(ctx) {
  const say = (m) => console.log('[PDF翻译] ' + m)
  const reply = (res, code, obj) => {
    res.writeHead(code, HEADERS)
    res.end(JSON.stringify(obj))
  }
  const readJson = (req) =>
    new Promise((resolve) => {
      let raw = ''
      req.on('data', (c) => {
        raw += c
        if (raw.length > 200000) req.destroy()
      })
      req.on('end', () => {
        try {
          resolve(JSON.parse(raw || '{}'))
        } catch {
          resolve({})
        }
      })
      req.on('error', () => resolve({}))
    })

  const route = (path, handler) =>
    ctx.effect(() =>
      ctx.webServer.register({
        kind: 'exact',
        path,
        handler: async (req, res) => {
          if (req.method === 'OPTIONS') {
            res.writeHead(204, HEADERS)
            res.end()
            return
          }
          try {
            await handler(req, res)
          } catch (error) {
            say('路由 ' + path + ' 出错：' + (error && error.message))
            try {
              reply(res, 500, { ok: false, error: String(error && error.message) })
            } catch {}
          }
        },
      }),
    )

  // ---------- 便携运行时：一键安装（自带 python + pip 装 BabelDOC） ----------
  route(P + '/setup/status', async (req, res) => {
    reply(res, 200, {
      ok: true,
      runtime: hasRuntime(),
      babeldoc: hasBabeldoc(),
      workflow: hasWorkflow(),
      ready: hasRuntime() && hasBabeldoc() && hasWorkflow(),
      installing: setup.running,
      error: setup.error,
      log: setup.log.slice(-40),
    })
  })

  route(P + '/setup/install', async (req, res) => {
    if (setup.running) {
      reply(res, 200, { ok: false, error: '正在安装，别重复点' })
      return
    }
    setup.running = true
    setup.error = null
    setup.log = []
    setup.startedAt = Date.now()
    reply(res, 200, { ok: true, started: true })
    say('开始安装便携运行时')
    void (async () => {
      try {
        const r = await installRuntime((line) => {
          setup.log.push(line)
          if (setup.log.length > 400) setup.log.shift()
        })
        if (!r.ok) {
          setup.error = r.error
        } else {
          saveCfg({ workflowDir: WF_DIR, python: RT_PY })
        }
      } catch (e) {
        setup.error = String((e && e.message) || e)
      } finally {
        setup.running = false
        say('运行时安装结束：' + (setup.error || 'ok'))
      }
    })()
  })

  // ---------- 队列（顺序执行，一次一篇） ----------
  const state = { running: false, current: null, queue: [], results: [] }
  const setup = { running: false, log: [], error: null, startedAt: 0 }

  // 装了便携运行时就用它（自带 python + 自己的 site-packages），否则退回系统 python
  const rtReady = () => hasRuntime()
  const runPython = (args, extraEnv = {}) =>
    spawn(rtReady() ? RT_PY : (loadCfg().python || 'python'), ['-X', 'utf8', ...args], {
      cwd: loadCfg().workflowDir || (hasWorkflow() ? WF_DIR : undefined),
      env: {
        ...process.env,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        ...(rtReady() ? { BABELDOC_PYTHON: RT_PY, BABELDOC_SITE: RT_SITE, DSH_PDF_TOOLS: join(WF_DIR, 'tools') } : {}),
        ...extraEnv,
      },
    })

  // 读前两页数汉字，判断「源语言」（不是目标语言！之前这里返回了反过来的值，
  // 导致英文论文被判成要译成英文，白跑一遍 en→en）
  const detectLang = (pdf) =>
    new Promise((resolve) => {
      const code = [
        'import sys,re,pymupdf',
        'd=pymupdf.open(sys.argv[1])',
        't="".join(d[i].get_text() for i in range(min(2,d.page_count)))',
        'c=len(re.findall(r"[\\u4e00-\\u9fff]",t)); l=len(re.findall(r"[A-Za-z]",t))',
        'print("zh" if c/(c+l+1)>0.3 else "en")',
      ].join('\n')
      const p = runPython(['-c', code, pdf])
      let out = ''
      p.stdout.on('data', (c) => (out += c))
      p.on('error', () => resolve('en'))
      p.on('close', () => resolve(out.trim() === 'zh' ? 'zh' : 'en'))
    })

  const pushEvent = (ev) => {
    const job = state.current
    if (!job) return
    job.elapsed = Math.round((Date.now() - job.startedAt) / 100) / 10
    if (ev.event === 'heartbeat') {
      job.elapsed = ev.elapsed_s || job.elapsed
      // BabelDOC 的日志常常攒到结束才吐，中途只有心跳；这里按预估时长给个「估算进度」，
      // 否则用户看到的就是一行不动的「预计 …」，以为卡死了
      if (job.phase === 'translate' && !job.chunks && job.estimate && job.translateT0) {
        const est = Math.max(30, job.estimate.minutes * 60)
        const frac = Math.min(0.95, (Date.now() - job.translateT0) / 1000 / est)
        job.estProgress = Math.round(frac * 100) / 100
        job.stage = `翻译中…已用 ${Math.round((Date.now() - job.translateT0) / 1000)}s`
          + `（预计 ${job.estimate.minutes} 分钟）`
      }
      return
    }
    if (ev.event === 'stage') {
      job.stage = ev.status === 'done' ? ev.name + ' 完成' : ev.name + '…'
      job.phase = ev.name
      if (ev.name === 'translate' && ev.status === 'start') job.translateT0 = Date.now()
      // 翻译之后的阶段没有「片」的概念，进度条回到不确定态，别停在 100% 让人以为卡住
      if (ev.name !== 'translate') job.progress = null
    }
    if (ev.event === 'chunk') {
      // 并行跑时 start 事件会一次性全发出来，所以进度要按「已完成片数」算，
      // 不能拿最后一条 start 的 index 当进度（那会一开跑就显示到最后一篇）
      job.chunks = job.chunks || {}
      job.chunks[ev.index] = { pages: ev.pages, status: ev.status }
      const total = ev.total
      const entries = Object.entries(job.chunks)
      const done = entries.filter(([, c]) => c.status === 'done' || c.status === 'cached').length
      const running = entries.filter(([, c]) => c.status === 'start')
      job.chunk = { index: ev.index, total, pages: ev.pages, status: ev.status, done }
      job.progress = total ? done / total : 0
      if (done >= total) {
        job.stage = `分片全部完成（${total}/${total}）`
      } else if (running.length > 1) {
        job.stage = `并行 ${running.length} 路 · 已完成 ${done}/${total} 片`
      } else if (running.length === 1) {
        job.stage = `正在翻第 ${running[0][0]}/${total} 片（${running[0][1].pages}）`
          + ` · 已完成 ${done}/${total}`
      } else {
        job.stage = `分片翻译 · 已完成 ${done}/${total} 片`
      }
    }
    if (ev.event === 'estimate') {
      job.estimate = { pages: ev.pages, minutes: ev.minutes, tokens: ev.tokens }
      if (!job.chunks) {
        job.stage = `预计 ${ev.pages} 页 ≈ ${ev.minutes} 分钟 ≈ ${Math.round(ev.tokens / 1000)}k token`
      }
    }
    if (ev.event === 'plan') job.outDir = ev.out || ''
    if (ev.event === 'error') job.error = ev.message
  }

  const runOne = (item) =>
    new Promise((resolve) => {
      const cfg = loadCfg()
      const job = {
        pdf: item.pdf,
        status: 'running',
        stage: '准备中',
        startedAt: Date.now(),
        elapsed: 0,
        outDir: '',
        report: null,
        consistency: null,
        error: null,
        code: null,
        stderr: '',
        _proc: null,
      }
      state.current = job

      const args = ['tools/translate.py', item.pdf, '--to', item.to, '--json']
      if (item.pages && item.pages !== 'all') args.push('--pages', item.pages)
      if (item.glossary) args.push('--glossary', item.glossary)
      if (cfg.skipRefs === false) args.push('--translate-refs')
      if (cfg.parallel && Number(cfg.parallel) > 1) args.push('--parallel', String(cfg.parallel))

      const p = runPython(args, {
        BABELDOC_MODEL: cfg.model || '',
        BABELDOC_BASE_URL: cfg.baseUrl || '',
        BABELDOC_API_KEY: cfg.apiKey || '',
      })
      job._proc = p

      let buf = ''
      p.stdout.on('data', (chunk) => {
        buf += chunk.toString('utf8')
        const lines = buf.split(/\r?\n/)
        buf = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('{')) continue
          try {
            pushEvent(JSON.parse(line))
          } catch {}
        }
      })
      p.stderr.on('data', (chunk) => {
        job.stderr = (job.stderr + chunk.toString('utf8')).slice(-4000)
      })
      p.on('error', (e) => {
        job.error = String(e.message)
      })
      p.on('close', (code) => {
        job.code = code
        job.status = job.status === 'cancelled' ? 'cancelled' : code === 0 ? 'done' : 'failed'
        if (job.status === 'failed' && !job.error) {
          const tail = job.stderr.split(/\r?\n/).filter(Boolean).slice(-3).join(' / ')
          job.error = tail || '退出码 ' + code
        }
        try {
          if (job.outDir) {
            const rp = join(job.outDir, 'report.json')
            if (existsSync(rp)) job.report = JSON.parse(readFileSync(rp, 'utf8'))
            const cp = join(job.outDir, 'consistency.json')
            if (existsSync(cp)) job.consistency = JSON.parse(readFileSync(cp, 'utf8'))
          }
        } catch {}
        const qa = (job.report && job.report.qa) || {}
        let qaTop = []
        try {
          if (job.outDir) {
            const qp = join(job.outDir, 'qa.json')
            if (existsSync(qp)) qaTop = (JSON.parse(readFileSync(qp, 'utf8')).items || []).slice(0, 6)
          }
        } catch {}
        const entry = {
          pdf: item.pdf,
          to: item.to,
          status: job.status,
          outDir: job.outDir,
          must: qa.must_fix,
          maybe: qa.maybe,
          fixups: (job.report && job.report.outputs && job.report.outputs.fixups) || 0,
          inconsistent: job.consistency ? job.consistency.inconsistent : null,
          score: (qa.must_fix || 0) * 3 + (qa.maybe || 0) +
                 ((job.consistency && job.consistency.inconsistent) || 0) * 2,
          note: item.note || null,
          qaTop,
          error: job.error,
        }
        state.results.push(entry)
        try {
          pushHistory(entry)
        } catch {}
        say('完成 ' + item.pdf.split(/[\\/]/).pop() + ' → ' + job.status)
        resolve(job)
      })
    })

  const processQueue = async () => {
    state.running = true
    for (const item of state.queue) {
      if (item.status !== 'pending') continue
      item.status = 'running'
      // 方向：没选就按每个文件各判一次；选了但和源语言相同，自动掉头并记一笔
      const srcLang = await detectLang(item.pdf)
      if (!item.to || item.to === 'auto') {
        item.to = srcLang === 'zh' ? 'en' : 'zh'
      } else if (item.to === srcLang) {
        item.to = srcLang === 'zh' ? 'en' : 'zh'
        item.note = `方向已自动纠正：源语言是 ${srcLang}，不能译成同一语言`
      }
      await runOne(item)
      const cancelled = state.current && state.current.status === 'cancelled'
      item.status = cancelled ? 'cancelled' : 'done'
      state.current = null
      if (cancelled) break
    }
    state.running = false
    state.current = null
  }

  // ---------- 接口 ----------
  route(P + '/state', async (req, res) => {
    const cfg = loadCfg()
    reply(res, 200, {
      ok: true,
      config: cfg,
      workflowExists: !!cfg.workflowDir && existsSync(join(cfg.workflowDir, 'tools', 'translate.py')),
      running: state.running,
    })
  })

  route(P + '/config', async (req, res) => {
    const body = await readJson(req)
    reply(res, 200, { ok: true, config: saveCfg(body || {}) })
  })

  route(P + '/check', async (req, res) => {
    const cfg = loadCfg()
    const dir = cfg.workflowDir
    const result = { ok: true, checks: [] }
    if (!dir || !existsSync(join(dir, 'tools', 'translate.py'))) {
      result.checks.push({ name: '工作目录', ok: false, detail: '找不到 tools/translate.py，请在设置里填 translation-workflow 的路径' })
      reply(res, 200, result)
      return
    }
    result.checks.push({ name: '工作目录', ok: true, detail: dir })

    await new Promise((resolve) => {
      const p = runPython(['-c', 'import sys;print(sys.version.split()[0])'])
      let out = ''
      p.stdout.on('data', (c) => (out += c))
      p.on('error', (e) => {
        result.checks.push({ name: 'Python', ok: false, detail: String(e.message) })
        resolve()
      })
      p.on('close', (code) => {
        result.checks.push({ name: 'Python', ok: code === 0, detail: code === 0 ? '版本 ' + out.trim() : '退出码 ' + code })
        resolve()
      })
    })

    await new Promise((resolve) => {
      const p = runPython(['tools/translate.py', '--help'])
      p.on('error', () => {
        result.checks.push({ name: '流水线', ok: false, detail: '无法启动' })
        resolve()
      })
      p.on('close', (code) => {
        result.checks.push({ name: '流水线', ok: code === 0, detail: code === 0 ? 'translate.py 可用' : '退出码 ' + code })
        resolve()
      })
    })
    reply(res, 200, result)
  })

  // 弹 Windows 文件选择框（可多选）
  route(P + '/pick', async (req, res) => {
    const out = await new Promise((resolve) => {
      const p = spawn('powershell.exe', ['-NoProfile', '-STA', '-Command', PICK_SCRIPT])
      let buf = ''
      p.stdout.on('data', (c) => (buf += c.toString('utf8')))
      p.on('error', () => resolve({ ok: false, paths: [], error: 'powershell 不可用' }))
      p.on('close', () => {
        const paths = buf.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
        resolve({ ok: true, paths })
      })
    })
    reply(res, 200, out)
  })

  route(P + '/detect', async (req, res) => {
    const body = await readJson(req)
    const pdf = String(body.pdf || '')
    if (!pdf || !existsSync(pdf)) {
      reply(res, 200, { ok: false, error: '文件不存在：' + pdf })
      return
    }
    const code = [
      'import sys,re,pymupdf',
      'd=pymupdf.open(sys.argv[1])',
      't="".join(d[i].get_text() for i in range(min(2,d.page_count)))',
      'c=len(re.findall(r"[\\u4e00-\\u9fff]",t)); l=len(re.findall(r"[A-Za-z]",t))',
      'print("zh" if c/(c+l+1)>0.3 else "en")',
    ].join('\n')
    const p = runPython(['-c', code, pdf])
    let out = ''
    p.stdout.on('data', (c) => (out += c))
    p.on('close', () => {
      const lang = out.trim() === 'zh' ? 'zh' : 'en'
      reply(res, 200, { ok: true, lang, to: lang === 'zh' ? 'en' : 'zh' })
    })
  })

  route(P + '/run', async (req, res) => {
    if (state.running) {
      reply(res, 200, { ok: false, error: '已有任务在跑，先等它结束或取消' })
      return
    }
    const body = await readJson(req)
    const cfg = loadCfg()
    const raw = Array.isArray(body.pdfs) ? body.pdfs : [body.pdf]
    const pdfs = raw.map((x) => String(x || '').replace(/^"|"$/g, '').trim()).filter(Boolean)
    if (!pdfs.length) {
      reply(res, 200, { ok: false, error: '没收到文件路径' })
      return
    }
    const missing = pdfs.filter((x) => !existsSync(x))
    if (missing.length) {
      reply(res, 200, { ok: false, error: '文件不存在：' + missing[0] })
      return
    }
    // 方向：这个插件只服务「英→中」，没给就用 zh；'auto' 要原样传下去，
    // 否则会被当成 en（= 译成英文），等于白跑一遍
    const to = ['zh', 'en', 'auto'].includes(body.to) ? body.to : 'zh'
    const pages = String(body.pages || '').trim()
    const glossary = String(body.glossary || cfg.glossary || '').trim()

    state.queue = pdfs.map((pdf) => ({ pdf, to, pages, glossary, status: 'pending' }))
    state.results = []
    void processQueue()
    reply(res, 200, { ok: true, count: pdfs.length })
  })

  route(P + '/job', async (req, res) => {
    const cur = state.current
    reply(res, 200, {
      ok: true,
      running: state.running,
      current: cur
        ? {
            pdf: cur.pdf,
            status: cur.status,
            stage: cur.stage,
            phase: cur.phase || null,
            chunk: cur.chunk || null,
            progress: typeof cur.progress === 'number' ? cur.progress : null,
            estProgress: (() => {
              if (cur.status !== 'running' || cur.phase !== 'translate') return null
              if (!cur.estimate || !cur.translateT0) return null
              const est = Math.max(30, cur.estimate.minutes * 60)
              const frac = Math.min(0.95, (Date.now() - cur.translateT0) / 1000 / est)
              return Math.round(frac * 100) / 100
            })(),
            estimate: cur.estimate || null,
            // 分片并行时子进程不吐心跳，elapsed 只能用宿主自己的时钟现算
            elapsed: cur.status === 'running'
              ? Math.round((Date.now() - cur.startedAt) / 100) / 10
              : cur.elapsed,
            outDir: cur.outDir,
            report: cur.report,
            error: cur.error,
            code: cur.code,
          }
        : null,
      queue: state.queue.map((q) => ({ pdf: q.pdf, status: q.status })),
      results: state.results,
    })
  })

  // 按专业方向生成术语库（模型产出，落地为 candidate，等人工确认）
  route(P + '/glossary/build', async (req, res) => {
    const body = await readJson(req)
    const domain = String(body.domain || '').trim()
    if (!domain) {
      reply(res, 200, { ok: false, error: '先填一个专业方向，例如「粒子物理」' })
      return
    }
    const cfg = loadCfg()
    if (!cfg.workflowDir) {
      reply(res, 200, { ok: false, error: '还没配置工作目录' })
      return
    }
    const out = await new Promise((resolve) => {
      const p = runPython(['tools/build_glossary.py', '--domain', domain, '--json'], {
        BABELDOC_MODEL: cfg.model || '',
        BABELDOC_BASE_URL: cfg.baseUrl || '',
        BABELDOC_API_KEY: cfg.apiKey || '',
      })
      let buf = ''
      p.stdout.on('data', (c) => (buf += c.toString('utf8')))
      p.on('error', (e) => resolve({ ok: false, error: String(e.message) }))
      p.on('close', () => {
        try {
          resolve(JSON.parse(buf.trim().split(/\r?\n/).pop()))
        } catch {
          resolve({ ok: false, error: '生成失败，看宿主日志' })
        }
      })
    })
    reply(res, 200, out)
  })

  // 列出已有词库，供面板下拉选择
  route(P + '/glossary/list', async (req, res) => {
    const cfg = loadCfg()
    const dir = cfg.workflowDir ? join(cfg.workflowDir, 'glossary') : ''
    const items = []
    try {
      if (dir && existsSync(dir)) {
        for (const f of readdirSync(dir)) {
          if (f.endsWith('.csv')) items.push({ name: f, path: join(dir, f) })
        }
      }
    } catch {}
    reply(res, 200, { ok: true, items })
  })

  route(P + '/cancel', async (req, res) => {
    let killed = false
    if (state.current && state.current._proc) {
      try {
        spawn('taskkill', ['/pid', String(state.current._proc.pid), '/T', '/F'])
        state.current.status = 'cancelled'
        killed = true
      } catch {}
    }
    state.queue = state.queue.map((q) =>
      q.status === 'pending' ? { ...q, status: 'cancelled' } : q,
    )
    reply(res, 200, { ok: killed })
  })

  route(P + '/dequeue', async (req, res) => {
    const body = await readJson(req)
    const idx = Number(body.index)
    const targets = state.queue.filter((q) => q.status === 'pending')
    const target = targets[idx]
    if (!target) {
      reply(res, 200, { ok: false, error: '没有这个排队项' })
      return
    }
    target.status = 'cancelled'
    reply(res, 200, { ok: true })
  })

  route(P + '/retry', async (req, res) => {
    if (state.running) {
      reply(res, 200, { ok: false, error: '队列还在跑，先等它结束' })
      return
    }
    const body = await readJson(req)
    const pdf = String(body.pdf || '')
    if (!pdf || !existsSync(pdf)) {
      reply(res, 200, { ok: false, error: '文件不存在：' + pdf })
      return
    }
    state.queue = [{
      pdf,
      to: body.to || 'auto',
      pages: String(body.pages || '').trim(),
      glossary: String(body.glossary || ''),
      status: 'pending',
    }]
    state.results = []
    void processQueue()
    reply(res, 200, { ok: true })
  })

  route(P + '/history', async (req, res) => {
    reply(res, 200, { ok: true, items: loadHistory() })
  })

  route(P + '/history/clear', async (req, res) => {
    try {
      writeFileSync(HIST_PATH, '[]', 'utf8')
    } catch {}
    reply(res, 200, { ok: true })
  })

  // 读某个词库的条目（供面板逐条确认）
  route(P + '/glossary/entries', async (req, res) => {
    const u = new URL(req.url, 'http://127.0.0.1')
    const p = String(u.searchParams.get('path') || '')
    if (!p || !existsSync(p)) {
      reply(res, 200, { ok: false, error: '找不到词库：' + p })
      return
    }
    try {
      const { items } = readGlossary(p)
      reply(res, 200, { ok: true, path: p, items })
    } catch (e) {
      reply(res, 200, { ok: false, error: String(e.message) })
    }
  })

  // 保存确认结果（整表写回）
  route(P + '/glossary/save', async (req, res) => {
    const body = await readJson(req)
    const p = String(body.path || '')
    const items = Array.isArray(body.items) ? body.items : []
    if (!p || !existsSync(p)) {
      reply(res, 200, { ok: false, error: '找不到词库：' + p })
      return
    }
    try {
      writeGlossary(p, items)
      const confirmed = items.filter((x) => (x.status || '') === 'confirmed').length
      reply(res, 200, { ok: true, total: items.length, confirmed })
    } catch (e) {
      reply(res, 200, { ok: false, error: String(e.message) })
    }
  })

  // 单页重译：只重跑指定页，再拼回成品
  route(P + '/retranslate', async (req, res) => {
    const body = await readJson(req)
    const outDir = String(body.outDir || '')
    const pages = String(body.pages || '').trim()
    if (!outDir || !existsSync(outDir)) {
      reply(res, 200, { ok: false, error: '找不到输出目录：' + outDir })
      return
    }
    if (!pages) {
      reply(res, 200, { ok: false, error: '先填要重译的页码，例如 5 或 5,12' })
      return
    }
    const cfg = loadCfg()
    const out = await new Promise((resolve) => {
      const p = runPython(['tools/retranslate_pages.py', '--out-dir', outDir, '--pages', pages], {
        BABELDOC_MODEL: cfg.model || '',
        BABELDOC_BASE_URL: cfg.baseUrl || '',
        BABELDOC_API_KEY: cfg.apiKey || '',
      })
      let buf = ''
      p.stdout.on('data', (c) => (buf += c.toString('utf8')))
      p.stderr.on('data', (c) => (buf += c.toString('utf8')))
      p.on('error', (e) => resolve({ ok: false, error: String(e.message) }))
      p.on('close', (code) => {
        let finalPath = join(outDir, 'final-merged.pdf')
        try {
          const mp = join(outDir, 'merge_result.json')
          if (existsSync(mp)) {
            const info = JSON.parse(readFileSync(mp, 'utf8'))
            if (info && info.path && existsSync(info.path)) finalPath = info.path
          }
        } catch {}
        if (code === 0 && existsSync(finalPath)) {
          resolve({ ok: true, path: finalPath, log: buf.slice(-400) })
        } else {
          resolve({ ok: false, error: buf.split(/\r?\n/).filter(Boolean).slice(-2).join(' / ') })
        }
      })
    })
    reply(res, 200, out)
  })

  route(P + '/open', async (req, res) => {
    const body = await readJson(req)
    const target = String(body.path || '')
    if (!target || !existsSync(target)) {
      reply(res, 200, { ok: false, error: '找不到：' + target })
      return
    }
    try {
      spawn('cmd', ['/c', 'start', '', target], { detached: true, stdio: 'ignore' }).unref()
      reply(res, 200, { ok: true })
    } catch (e) {
      reply(res, 200, { ok: false, error: String(e.message) })
    }
  })

  say('已加载，配置：' + CFG_PATH)
}
