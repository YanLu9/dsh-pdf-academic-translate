// 发布前自检：改包名时最容易漏的两处（patch 的 name、client 的模块 id）必须与包名一致。
// 用法：node scripts/verify-package.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(join(root, p), 'utf8')
const pkg = JSON.parse(read('package.json'))
const fail = []
const ok = []

// 1) cordis.patch.yml 的 name 必须等于包名（否则 install 时 ERR_MODULE_NOT_FOUND）
const patch = read('cordis.patch.yml')
const patchName = (patch.match(/^\s*name:\s*'([^']+)'/m) || [])[1] || ''
if (patchName === pkg.name) ok.push(`patch.name = ${patchName}`)
else fail.push(`cordis.patch.yml 的 name 是 "${patchName}"，应为 "${pkg.name}"`)

// 2) client.js 里 __ModuleLoader__.load({ id }) 必须等于包名（否则页面不认领）
const client = read('lib/client.js')
const clientId = (client.match(/__ModuleLoader__\.load\(\{\s*\n?\s*id:\s*'([^']+)'/) || [])[1] || ''
if (clientId === pkg.name) ok.push(`client id = ${clientId}`)
else fail.push(`lib/client.js 的 load({ id }) 是 "${clientId}"，应为 "${pkg.name}"`)

// 3) 前后端路由前缀必须一致（它跟包名无关，但不能被一起改掉）
const route = (read('lib/index.js').match(/const P = '([^']+)'/) || [])[1] || ''
const base = (client.match(/const B = '([^']+)'/) || [])[1] || ''
if (route && route === base) ok.push(`路由前缀 = ${route}（前后端一致）`)
else fail.push(`路由前缀不一致：index.js "${route}" vs client.js "${base}"`)

// 4) 包里不该残留旧包名
// 路由前缀 /dsh-pdf-translate 是有意的，先剔掉再查残留
const stripped = (client + patch).replace(/\/dsh-pdf-translate/g, '')
if (/\bdsh-pdf-translate\b/.test(stripped)) {
  fail.push('仍出现旧包名 dsh-pdf-translate（路由前缀 /dsh-pdf-translate 除外）')
}

for (const line of ok) console.log('  ✓ ' + line)
if (fail.length) {
  for (const line of fail) console.error('  ✗ ' + line)
  process.exit(1)
}
console.log(`\n自检通过：${pkg.name}@${pkg.version}`)
