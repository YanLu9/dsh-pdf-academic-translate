// 便携运行时安装器：把包里自带的 python + 流水线脚本装到用户目录，再用 pip 装 BabelDOC。
//
// 为什么装到 %LOCALAPPDATA%：DSH 的工作区目录带 ACL，从那里跑的 python 写不了用户目录
// （实测 PermissionError）。%LOCALAPPDATA% 下没有这个问题。
import { execFileSync, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, cpSync, existsSync, mkdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { Readable } from 'node:stream'
import { fileURLToPath } from 'node:url'

const PKG_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const DATA_BASE = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local')

export const DATA_DIR = join(DATA_BASE, 'dsh-pdf-translate')
export const RT_DIR = join(DATA_DIR, 'runtime')
export const RT_PY = join(RT_DIR, 'python', 'python.exe')
// 便携 python 的包目录就是它自己的 Lib\site-packages（get-pip 也装在这儿）
export const RT_SITE = join(RT_DIR, 'python', 'Lib', 'site-packages')
export const WF_DIR = join(DATA_DIR, 'workflow')
export const BABELDOC_VERSION = '0.6.4'

export const hasRuntime = () => existsSync(RT_PY)
export const hasBabeldoc = () => existsSync(join(RT_SITE, 'babeldoc'))
export const hasWorkflow = () => existsSync(join(WF_DIR, 'tools', 'translate.py'))

/** 离线运行时包去哪找：环境变量 > 数据目录根 > 插件包内。都没有就联网 pip。 */
export function runtimeZip() {
  const cands = []
  if (process.env.DSH_PDF_RUNTIME_ZIP) cands.push(process.env.DSH_PDF_RUNTIME_ZIP)
  cands.push(join(DATA_DIR, 'babeldoc-runtime.zip'))

  cands.push(join(PKG_ROOT, 'runtime', 'babeldoc-runtime.zip'))
  return cands.find((p) => p && existsSync(p)) || ''
}

// 市场版没法把 291MB 的离线包塞进 npm 包（npm 单包上限 100MB），只能从外部拉。
// 单一地址一定会有人连不上（GitHub 在国内常年抽风），所以按顺序试多个源：
//   环境变量 > 国内对象存储（快） > GitHub Release（海外） > pip（最后兜底）
const DEFAULT_URLS = [
  'https://github.com/YanLu9/dsh-pdf-translate-runtime/releases/latest/download/babeldoc-runtime.zip',
]

export function runtimeUrls() {
  const extra = (process.env.DSH_PDF_RUNTIME_ZIP_URL || '')
    .split(/[,\s]+/).filter(Boolean)
  return [...extra, ...DEFAULT_URLS].filter((u) => u && !u.includes('<'))
}

export const isRuntimeUrlConfigured = () => runtimeUrls().length > 0

// 下载后校验（防中间人/半截文件）。留空则不校验；填上发布时 zip 的 sha256 即可。
export const RUNTIME_SHA256 = '3CBA815843831965F8D9FE7D05F84616756E58BA2D0FB13EB7CF8D47112BED93'

function sha256Of(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex')
}

/** 从第一个能通的源下载离线包，返回本地路径（全失败返回空串）。 */
async function downloadRuntime(log) {
  const dest = join(DATA_DIR, 'babeldoc-runtime.zip')
  const urls = runtimeUrls()
  if (!urls.length) {
    log('! 没有配置下载地址')
    return ''
  }
  log('下载离线运行时包（约 290 MB，网络慢的话等一会儿）…')
  for (let i = 0; i < urls.length; i += 1) {
    log(`  源 ${i + 1}/${urls.length}：${urls[i]}`)
    // 先试 Node 的 fetch（直连/环境变量代理时最省事），失败再用 curl 走系统代理。
    // 国内绝大多数人是「浏览器能开、Node 连不上」——因为 Node 不读 Windows 系统代理。
    let ok = await fetchTo(urls[i], dest, log)
    if (!ok) {
      log('  fetch 不行，改用 curl（会读系统代理）…')
      ok = await curlTo(urls[i], dest, log)
    }
    if (ok) {
      // 校验放在这里，fetch / curl 两条路径都过一遍
      if (RUNTIME_SHA256) {
        const got = sha256Of(dest)
        if (got.toLowerCase() !== RUNTIME_SHA256.toLowerCase()) {
          log('  ! 校验和不匹配（期望 ' + RUNTIME_SHA256.slice(0, 12) + '…，实际 '
            + got.slice(0, 12) + '…），删掉重来')
          try {
            unlinkSync(dest)
          } catch {}
          ok = false
        } else {
          log('  校验和 OK')
        }
      }
      if (ok) return dest
    }
    if (i + 1 < urls.length) log('  这个源不行，换下一个…')
  }
  log('! 所有下载源都失败了')
  return ''
}

/** 读 Windows 系统代理（HKCU\...\Internet Settings），没开返回空串。 */
function systemProxy() {
  try {
    const q = (key, val) => {
      const out = execFileSync('reg', ['query',
        'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings',
        '/v', val], { encoding: 'utf8', windowsHide: true })
      const m = out.match(new RegExp(val + '\\s+REG_\\w+\\s+(\\S+)'))
      return m ? m[1].trim() : ''
    }
    if (q('x', 'ProxyEnable') !== '0x1') return ''
    let server = q('x', 'ProxyServer')
    if (!server) return ''
    // 形如 127.0.0.1:7890，或 http=..;https=..
    if (server.includes('=')) {
      const part = server.split(';').find((s) => s.trim().startsWith('https='))
        || server.split(';')[0]
      server = part.split('=').pop()
    }
    server = server.trim()
    if (!/^\w+:\/\//.test(server)) server = 'http://' + server
    return server
  } catch {
    return ''
  }
}

/** 用系统自带的 curl.exe 下载（能走系统代理）。成功返回 true。 */
function curlTo(url, dest, log) {
  return new Promise((resolve) => {
    const proxy = systemProxy()
    log(proxy ? `  走系统代理 ${proxy}` : '  直连（没检测到系统代理）')
    const args = ['-L', '--fail', '--retry', '2', '--connect-timeout', '20',
      '--progress-bar', '-o', dest]
    if (proxy) args.push('-x', proxy)
    args.push(url)
    const curl = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'curl.exe')
    const p = spawn(curl, args, { windowsHide: true })
    let lastPct = -1
    let tail = ''
    const onChunk = (buf) => {
      const s = buf.toString('utf8')
      tail = (tail + s).slice(-400)
      for (const m of s.matchAll(/(\d{1,3})\s*%/g)) {
        const pct = Number(m[1])
        if (pct >= lastPct + 10) {
          lastPct = pct
          log(`  已下载 ${pct}%`)
        }
      }
    }
    p.stdout.on('data', onChunk)
    p.stderr.on('data', onChunk)
    p.on('error', (e) => {
      log('  ! curl 起不来：' + e.message)
      resolve(false)
    })
    p.on('close', (code) => {
      if (code !== 0) {
        log('  ! curl 退出码 ' + code + (tail ? '：' + tail.trim().split(/\r?\n/).pop() : ''))
        resolve(false)
        return
      }
      const size = existsSync(dest) ? statSync(dest).size : 0
      if (size < 50 * 1048576) {
        log('  ! 只下到 ' + Math.round(size / 1048576) + ' MB，不完整')
        resolve(false)
        return
      }
      log('  下载完成：' + Math.round(size / 1048576) + ' MB')
      resolve(true)
    })
  })
}

/** 从单个 URL 边下边存；成功返回 true。 */
async function fetchTo(url, dest, log) {
  let res
  try {
    res = await fetch(url, { redirect: 'follow' })
  } catch (e) {
    log('  ! 连接失败：' + ((e && e.message) || e))
    return false
  }
  if (!res.ok) {
    log('  ! 服务器返回 ' + res.status)
    return false
  }
  const total = Number(res.headers.get('content-length') || 0)
  let got = 0
  let lastPct = -1
  const out = createWriteStream(dest)
  try {
    for await (const chunk of Readable.fromWeb(res.body)) {
      out.write(chunk)
      got += chunk.length
      if (total) {
        const pct = Math.floor((got / total) * 100)
        if (pct >= lastPct + 10) {
          lastPct = pct
          log(`  已下载 ${pct}%（${(got / 1048576).toFixed(0)} / ${(total / 1048576).toFixed(0)} MB）`)
        }
      }
    }
  } catch (e) {
    log('  ! 下载中断：' + ((e && e.message) || e))
    try {
      out.destroy()
    } catch {}
    return false
  } finally {
    out.end()
  }
  await new Promise((r) => out.on('close', r))
  const size = existsSync(dest) ? statSync(dest).size : 0
  if (size < 50 * 1048576) {
    log('  ! 只下到 ' + Math.round(size / 1048576) + ' MB，像是不完整')
    return false
  }
  log('  下载完成：' + Math.round(size / 1048576) + ' MB')
  if (RUNTIME_SHA256) {
    const got = sha256Of(dest)
    if (got.toLowerCase() !== RUNTIME_SHA256.toLowerCase()) {
      log('  ! 校验和不匹配，删掉重来')
      try {
        unlinkSync(dest)
      } catch {}
      return false
    }
    log('  校验和 OK')
  }
  return true
}

function run(cmd, args, opts, log) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { ...opts, windowsHide: true })
    const pipe = (buf) => {
      for (const line of buf.toString('utf8').split(/\r?\n/)) {
        if (line.trim()) log(line.trim())
      }
    }
    p.stdout.on('data', pipe)
    p.stderr.on('data', pipe)
    p.on('error', (e) => {
      log('! ' + e.message)
      resolve(-1)
    })
    p.on('close', (code) => resolve(code))
  })
}

/** 便携 python 默认不跑 site（拿不到 Lib\site-packages），把 `import site` 打开。 */
function wirePth() {
  const pth = join(RT_DIR, 'python', 'python313._pth')
  let text = existsSync(pth) ? readFileSync(pth, 'utf8') : 'python313.zip\n.\n'
  const lines = text.split(/\r?\n/).filter((l) => l.trim())
  const has = lines.some((l) => l.trim() === 'import site')
  if (!has) {
    lines.push('import site')
    writeFileSync(pth, lines.join('\n') + '\n', 'utf8')
  }
}

/**
 * 写 sitecustomize：把流水线的 tools 目录塞进 sys.path。
 *
 * 为什么必须这么干：带了 python._pth 的解释器会忽略 PYTHONPATH，也不把脚本目录加进
 * sys.path，于是 `import backcheck` / `import run_babeldoc` 全失联（实测第一版就栽在这）。
 * sitecustomize 在每次启动时由 site 自动导入，路径当字符串传，空格/中文都不怕。
 */
function wireSiteTools() {
  const tools = join(WF_DIR, 'tools')
  const code = [
    '# 由 dsh-pdf-translate 安装器生成：让流水线里的兄弟模块能被 import',
    'import os, sys',
    `_TOOLS = os.environ.get("DSH_PDF_TOOLS") or r"${tools}"`,
    'if _TOOLS and os.path.isdir(_TOOLS) and _TOOLS not in sys.path:',
    '    sys.path.append(_TOOLS)',
    '',
  ].join('\n')
  mkdirSync(RT_SITE, { recursive: true })
  writeFileSync(join(RT_SITE, 'sitecustomize.py'), code, 'utf8')
}

/**
 * 安装（幂等）：复制运行时 → 复制流水线 → 装 pip → 装 BabelDOC → 自检。
 * onLog 收日志行，返回 { ok, error }。
 */
export async function install(onLog) {
  const log = (m) => onLog(String(m))
  mkdirSync(DATA_DIR, { recursive: true })

  if (!hasRuntime()) {
    log('复制便携 Python…')
    cpSync(join(PKG_ROOT, 'runtime', 'python'), join(RT_DIR, 'python'), {
      recursive: true, force: true, errorOnExist: false,
    })
  } else {
    log('便携 Python 已存在，跳过复制')
  }
  mkdirSync(RT_SITE, { recursive: true })
  wirePth()
  if (!hasWorkflow()) {
    log('复制翻译流水线脚本…')
    cpSync(join(PKG_ROOT, 'workflow'), WF_DIR, { recursive: true, force: true })
  } else {
    log('流水线脚本已存在，跳过复制')
  }

  if (!hasBabeldoc()) {
    // 优先用离线包：把打包好的 site-packages 解压即可，不需要 pip 解析依赖。
    // 网上一片不通时（实测 PyPI/清华/阿里云全超时）这条路是唯一能装上的。
    let zip = runtimeZip()
    if (!zip && isRuntimeUrlConfigured()) zip = await downloadRuntime(log)
    if (zip) {
      log('发现离线运行时包：' + zip)
      log('解压中（约 1–3 分钟，不要关窗口）…')
      mkdirSync(RT_SITE, { recursive: true })
      const rc = await run('tar', ['-xf', zip, '-C', RT_SITE], { cwd: RT_DIR }, log)
      if (rc !== 0) return { ok: false, error: '离线包解压失败（文件损坏？）' }
      log('离线包解压完成')
    } else {
      if (!existsSync(join(RT_SITE, 'pip'))) {
        // get-pip.py 得先落到运行时目录（之前漏了这步，干净机器上必报找不到）
        const gp = join(RT_DIR, 'get-pip.py')
        if (!existsSync(gp)) cpSync(join(PKG_ROOT, 'runtime', 'get-pip.py'), gp)
        log('安装 pip…')
        const rcPip = await run(RT_PY, [gp, '--no-warn-script-location',
          '--disable-pip-version-check'], { cwd: RT_DIR }, log)
        if (rcPip !== 0) {
          return {
            ok: false,
            error: 'pip 装不上：网络不通。把 babeldoc-runtime.zip 放到 '
              + join(DATA_DIR, 'babeldoc-runtime.zip') + ' 再点一次即可离线安装',
          }
        }
      }
      log('安装 BabelDOC 及依赖（首次约 1GB，慢一些）…')
      // 用 requirements.txt 锁死 88 个包的版本：不锁的话 babeldoc 会拉到不兼容的传递依赖
      // （实测 bitstring 5.0 删了 BitStream，装完即崩）。版本来自一台跑通过的机器。
      const req = join(PKG_ROOT, 'runtime', 'requirements.txt')
      const pipArgs = ['-m', 'pip', 'install', '--no-warn-script-location',
        '--disable-pip-version-check', '--no-input', '-r', req]
      let rc = await run(RT_PY, pipArgs, { cwd: RT_DIR }, log)
      if (rc !== 0) {
        log('默认源失败，换清华镜像重试…')
        rc = await run(RT_PY, [...pipArgs, '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
          { cwd: RT_DIR }, log)
      }
      if (rc !== 0) return { ok: false, error: 'BabelDOC 安装失败（网络或磁盘？）' }
    }
  } else {
    log('BabelDOC 已安装，跳过')
  }

  // 放在最后：解压离线包可能覆盖 site-packages，这一步要在它之后
  wireSiteTools()

  log('自检…')
  const code = await run(RT_PY, ['-c', 'import babeldoc, pymupdf; print("ok")'], { cwd: RT_DIR }, log)
  if (code !== 0) return { ok: false, error: '自检失败：装上了但导入不了' }
  // 兄弟模块（backcheck / run_babeldoc 等）能不能 import，也是装好与否的一部分
  const code2 = await run(RT_PY, ['-c',
    'import backcheck, run_babeldoc, make_fixups; print("tools ok")'],
    { cwd: RT_DIR, env: { ...process.env, DSH_PDF_TOOLS: join(WF_DIR, 'tools') } }, log)
  if (code2 !== 0) return { ok: false, error: '自检失败：流水线脚本互相导入不了' }
  log('装好了')
  return { ok: true }
}
