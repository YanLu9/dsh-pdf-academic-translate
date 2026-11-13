// dsh-pdf-academic-translate · 界面半边
// 设置页里一个「PDF 翻译」面板：配置 → 选文件（可多选）→ 队列进度 → 结果表。
// 不引任何 UI 库，只用 React.createElement，避免构建步骤。
// ⚠️ id 必须等于 package.json 的 name（DSH 的模块加载器按包名认领），
//    但下面 const B 是后端路由前缀，两边保持一致即可，**不要**跟着改。
window.__ModuleLoader__.load({
  id: 'dsh-pdf-academic-translate',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')
    const { useState, useEffect } = React
    const h = React.createElement

    const inject = ['slots']
    const B = '/dsh-pdf-translate'

    const get = (p) => fetch(B + p, { cache: 'no-store' }).then((r) => r.json()).catch(() => null)
    const post = (p, body) =>
      fetch(B + p, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      })
        .then((r) => r.json())
        .catch(() => null)

    const CSS = `
      .dpt-wrap{display:flex;flex-direction:column;gap:16px;max-width:900px;color:#1f2937}
      .dpt-card{border:1px solid #e5e7eb;border-radius:12px;padding:16px;background:#ffffff;color:#1f2937}
      .dpt-title{font-size:14px;font-weight:600;margin-bottom:12px;color:#111827}
      .dpt-row{display:flex;gap:8px;align-items:flex-start;margin-bottom:10px;flex-wrap:wrap}
      .dpt-label{font-size:12px;color:#4b5563;width:96px;flex:none;padding-top:8px}
      .dpt-input{flex:1;min-width:240px;padding:7px 10px;border-radius:8px;border:1px solid #d1d5db;background:#ffffff;color:#1f2937;font-size:13px}
      .dpt-input::placeholder{color:#9ca3af}
      .dpt-area{flex:1;min-width:240px;min-height:64px;padding:8px 10px;border-radius:8px;border:1px solid #d1d5db;background:#ffffff;color:#1f2937;font-size:13px;font-family:ui-monospace,Consolas,monospace;resize:vertical}
      .dpt-btn{padding:7px 14px;border-radius:8px;border:1px solid #d1d5db;background:#ffffff;color:#1f2937;cursor:pointer;font-size:13px}
      .dpt-btn:hover{background:#f3f4f6}
      .dpt-btn[disabled]{opacity:.45;cursor:default}
      .dpt-btn-primary{background:#2563eb;border-color:#2563eb;color:#ffffff}
      .dpt-btn-primary:hover{background:#1d4ed8}
      .dpt-hint{font-size:12px;color:#4b5563;line-height:1.6}
      .dpt-log{font-family:ui-monospace,Consolas,monospace;font-size:11px;line-height:1.5;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:8px;max-height:150px;overflow:auto;white-space:pre-wrap;color:#374151;margin-top:8px}
      .dpt-bar{height:6px;border-radius:99px;background:#eef0f3;overflow:hidden;margin:8px 0}
      .dpt-bar-fill{height:100%;width:40%;background:#2563eb;animation:dpt-slide 1.4s ease-in-out infinite}
      @keyframes dpt-slide{0%{margin-left:-40%}100%{margin-left:100%}}
      .dpt-nums{display:flex;gap:12px;flex-wrap:wrap}
      .dpt-num{border-radius:10px;padding:10px 14px;min-width:96px;border:1px solid #e5e7eb;background:#ffffff}
      .dpt-num b{display:block;font-size:20px;line-height:1.2;color:#111827}
      .dpt-num span{font-size:12px;color:#6b7280}
      .dpt-bad{border-color:#fca5a5;background:#fef2f2}
      .dpt-ok{border-color:#86efac;background:#f0fdf4}
      .dpt-list{font-size:12px;line-height:1.8;margin:0;padding-left:18px;color:#374151}
      .dpt-table{width:100%;border-collapse:collapse;font-size:12px}
      .dpt-table th,.dpt-table td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f3}
      .dpt-table th{color:#6b7280;font-weight:500}
      .dpt-chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;border:1px solid #d1d5db}
      .dpt-chip-run{background:#eff6ff;border-color:#93c5fd;color:#1d4ed8}
      .dpt-chip-done{background:#f0fdf4;border-color:#86efac;color:#15803d}
      .dpt-chip-fail{background:#fef2f2;border-color:#fca5a5;color:#b91c1c}
      .dpt-chip-wait{background:#f9fafb;color:#6b7280}
      .dpt-mono{font-family:ui-monospace,Consolas,monospace}
    `

    const nameOf = (p) => String(p || '').split(/[\\/]/).pop()

    function Card(props) {
      return h('div', { className: 'dpt-card' },
        props.title ? h('div', { className: 'dpt-title' }, props.title) : null,
        props.children)
    }

    function Row(props) {
      return h('div', { className: 'dpt-row' },
        h('span', { className: 'dpt-label' }, props.label),
        props.children)
    }

    function Chip(props) {
      const cls = { running: 'dpt-chip-run', done: 'dpt-chip-done', failed: 'dpt-chip-fail', cancelled: 'dpt-chip-fail' }[props.status] || 'dpt-chip-wait'
      const text = { running: '进行中', done: '完成', failed: '失败', cancelled: '已取消', pending: '排队中' }[props.status] || props.status
      return h('span', { className: 'dpt-chip ' + cls }, text)
    }

    function useJob() {
      const [s, setS] = useState({ running: false, current: null, queue: [], results: [] })
      useEffect(() => {
        let alive = true
        const tick = async () => {
          const r = await get('/job')
          if (alive && r && r.ok) setS(r)
        }
        tick()
        const id = setInterval(tick, 1200)
        return () => {
          alive = false
          clearInterval(id)
        }
      }, [])
      return s
    }

    function Page() {
      const [cfg, setCfg] = useState(null)
      const [checks, setChecks] = useState(null)
      const [workflowExists, setWorkflowExists] = useState(true)
      const [keySource, setKeySource] = useState(null)
      const [text, setText] = useState('')
      const [to, setTo] = useState('zh')
      const [pages, setPages] = useState('')
      const [msg, setMsg] = useState('')
      const [history, setHistory] = useState([])
      const [domain, setDomain] = useState('')
      const [glossaries, setGlossaries] = useState([])
      const [building, setBuilding] = useState(false)
      const [gItems, setGItems] = useState([])
      const [gMsg, setGMsg] = useState('')
      const [gSearch, setGSearch] = useState('')
      const [gPage, setGPage] = useState(1)
      const [rePages, setRePages] = useState('')
      const [reMsg, setReMsg] = useState('')
      const [reBusy, setReBusy] = useState(false)
      const [setupI, setSetupI] = useState(null)
      const [setupBusy, setSetupBusy] = useState(false)
      const jobState = useJob()
      const busy = !!jobState.running

      const loadHistory = async () => {
        const r = await get('/history')
        if (r && r.ok) setHistory(r.items || [])
      }

      const loadGlossaries = async () => {
        const r = await get('/glossary/list')
        if (r && r.ok) setGlossaries(r.items || [])
      }

      const loadGlossaryItems = async (path) => {
        if (!path) {
          setGItems([])
          return
        }
        const r = await get('/glossary/entries?path=' + encodeURIComponent(path))
        setGItems(r && r.ok ? r.items || [] : [])
      }

      const buildGlossary = async () => {
        const d = domain.trim()
        if (!d) {
          setMsg('先填专业方向，例如「粒子物理」')
          return
        }
        setBuilding(true)
        setMsg('正在生成「' + d + '」的术语库…（一次模型调用，约 10 秒）')
        const r = await post('/glossary/build', { domain: d })
        setBuilding(false)
        if (r && r.ok) {
          setMsg(`已生成 ${r.added} 条（总 ${r.total} 条，状态=candidate）→ ${r.path}`)
          setCfg({ ...(cfg || {}), glossary: r.path })
          await post('/config', { glossary: r.path })
          loadGlossaries()
        } else {
          setMsg((r && r.error) || '生成失败')
        }
      }

      useEffect(() => {
        loadHistory()
        loadGlossaries()
      }, [])

      // 选中的词库变了就去取条目，供逐条确认
      useEffect(() => {
        if (cfg && cfg.glossary) loadGlossaryItems(cfg.glossary)
      }, [cfg && cfg.glossary])

      const setItem = (i, patch) =>
        setGItems(gItems.map((it, j) => (j === i ? { ...it, ...patch } : it)))
      const toggleItem = (i) =>
        setItem(i, { status: gItems[i].status === 'confirmed' ? 'candidate' : 'confirmed' })
      // 返回 [{it, idx}]：idx 是原始下标，筛选后仍能改对条目
      const gFiltered = () => {
        const q = gSearch.trim().toLowerCase()
        return gItems
          .map((it, idx) => ({ it, idx }))
          .filter(({ it }) => {
            if (!q) return true
            return (it.en + ' ' + it.zh + ' ' + (it.note || '')).toLowerCase().includes(q)
          })
      }
      const confirmAll = () => {
        const ids = new Set(gFiltered().map((x) => x.idx))
        setGItems(gItems.map((it, j) => (ids.has(j) ? { ...it, status: 'confirmed' } : it)))
      }
      // 分页：一页 30 条（60 条一屏太长，翻到底像爬墙）；搜索或换词库时回到第 1 页
      const G_PAGE_SIZE = 30
      const gPages = Math.max(1, Math.ceil(gFiltered().length / G_PAGE_SIZE))
      const gPageSafe = Math.min(gPage, gPages)
      const gPageItems = gFiltered().slice((gPageSafe - 1) * G_PAGE_SIZE, gPageSafe * G_PAGE_SIZE)
      const saveGlossary = async () => {
        if (!cfg || !cfg.glossary) return
        const r = await post('/glossary/save', { path: cfg.glossary, items: gItems })
        setGMsg(r && r.ok ? `已保存：${r.confirmed}/${r.total} 条为已确认` : ((r && r.error) || '保存失败'))
      }

      const retranslate = async () => {
        const lastOut = (jobState.current && jobState.current.outDir) ||
          (history[0] && history[0].outDir) || ''
        if (!lastOut) {
          setReMsg('还没有可作底的成品：先完整跑一次')
          return
        }
        if (!rePages.trim()) {
          setReMsg('先填页码，例如 5 或 5,12')
          return
        }
        setReBusy(true)
        setReMsg('重译中（只跑这几页，约 1 分钟/页）…')
        const r = await post('/retranslate', { outDir: lastOut, pages: rePages })
        setReBusy(false)
        if (r && r.ok) {
          setReMsg('已生成：' + r.path)
          post('/open', { path: r.path })
        } else {
          setReMsg((r && r.error) || '重译失败')
        }
      }

      // 队列每出一篇结果就刷新历史，让「刚跑完的」立刻出现在下面
      useEffect(() => {
        if ((jobState.results || []).length) loadHistory()
      }, [jobState.results && jobState.results.length])

      const dequeue = async (i) => {
        await post('/dequeue', { index: i })
      }

      const retry = async (it) => {
        const r = await post('/retry', { pdf: it.pdf, to: it.to || 'auto' })
        setMsg(r && r.ok ? '已重新排队：' + nameOf(it.pdf) : ((r && r.error) || '重跑失败'))
        loadHistory()
      }

      const clearHistory = async () => {
        await post('/history/clear', {})
        setHistory([])
      }

      useEffect(() => {
        let alive = true
        get('/state').then((r) => {
          if (!alive || !r || !r.ok) return
          setCfg(r.config || {})
          setWorkflowExists(!!r.workflowExists)
          setKeySource(r.keySource || null)
        })
        return () => {
          alive = false
        }
      }, [])

      useEffect(() => {
        let alive = true
        const tick = async () => {
          const r = await get('/setup/status')
          if (!alive || !r || !r.ok) return
          setSetupI(r)
          setSetupBusy(!!r.installing)
        }
        tick()
        const id = setInterval(tick, 3000)
        return () => {
          alive = false
          clearInterval(id)
        }
      }, [])

      const installRt = async () => {
        const r = await post('/setup/install', {})
        if (!r || !r.ok) {
          setMsg((r && r.error) || '启动安装失败')
          return
        }
        setSetupBusy(true)
        const t = await get('/state')
        if (t && t.ok) {
          setCfg(t.config || {})
          setKeySource(t.keySource || null)
        }
      }

      const set = (k) => (e) => setCfg({ ...(cfg || {}), [k]: e.target.value })

      const save = async () => {
        const r = await post('/config', cfg || {})
        setMsg(r && r.ok ? '配置已保存' : '保存失败')
      }

      const doCheck = async () => {
        setMsg('自检中…')
        await post('/config', cfg || {})
        const r = await post('/check', {})
        setChecks((r && r.checks) || [])
        setMsg('')
      }

      const pick = async () => {
        setMsg('等文件选择框…（可能弹在窗口后面）')
        const r = await post('/pick', {})
        setMsg('')
        if (!r || !r.ok) return
        const paths = r.paths || []
        if (!paths.length) return
        setText((prev) => (prev ? prev.trimEnd() + '\n' : '') + paths.join('\n'))
      }

      const start = async () => {
        const list = text.split(/\r?\n/).map((s) => s.trim().replace(/^"|"$/g, '')).filter(Boolean)
        if (!list.length) {
          setMsg('先选文件或粘路径（一行一个）')
          return
        }
        setMsg('')
        const r = await post('/run', { pdfs: list, to, pages })
        if (!r || !r.ok) setMsg((r && r.error) || '启动失败')
        else if (to === 'auto') setMsg('已加入队列：方向按每个文件自动判断')
      }

      const cancel = async () => {
        await post('/cancel', {})
      }

      const openPath = (p) => () => post('/open', { path: p })
      const cur = jobState.current
      const rep = cur && cur.report
      const outs = (rep && rep.outputs) || {}
      const qa = (rep && rep.qa) || {}

      return h('div', { className: 'dpt-wrap' },

        keySource === 'none'
          ? h(Card, { title: '需要 API Key' },
              h('div', { className: 'dpt-hint', style: { color: '#b45309' } },
                '翻译和「生成词库」都要调模型，但当前没有可用的 API Key。'),
              h('div', { className: 'dpt-hint' },
                '在下面「环境」卡片里填一个 DeepSeek 的 sk- 开头的 key（或任意 OpenAI 兼容接口的地址 + key），保存即可生效。'),
              h('div', { className: 'dpt-hint' },
                '注意：DSH 登录态不是 API Key，不能直接拿来调用模型。'))
          : null,

        setupI && !setupI.ready
          ? h(Card, { title: '运行环境（未安装）' },
              h('div', { className: 'dpt-hint' },
                '第一次用要先装便携运行时：自带 Python + BabelDOC，约 1 GB，装一次以后不用再管。'),
              h(Row, { label: '' },
                h('button', {
                  className: 'dpt-btn dpt-btn-primary',
                  onClick: installRt,
                  disabled: setupBusy,
                }, setupBusy ? '安装中…（可以关掉这个页面，装完再来）' : '一键安装'),
                setupI.error
                  ? h('span', { className: 'dpt-hint', style: { color: '#b91c1c' } }, '失败：' + setupI.error)
                  : null),
              (setupI.log || []).length
                ? h('div', { className: 'dpt-log' },
                    (setupI.log || []).slice(-14).map((l, i) => h('div', { key: i }, l)))
                : null)
          : null,

        h(Card, { title: '环境' },
          workflowExists
            ? h('div', { className: 'dpt-hint' }, '工作目录已就绪。')
            : h('div', { className: 'dpt-hint', style: { color: '#b45309' } },
                '还没配置工作目录：填 translation-workflow 文件夹的完整路径（里面有 tools/translate.py）。'),
          h(Row, { label: '工作目录' },
            h('input', { className: 'dpt-input', value: (cfg && cfg.workflowDir) || '', onChange: set('workflowDir'), placeholder: 'D:\\...\\translation-workflow' })),
          h(Row, { label: 'Python' },
            h('input', { className: 'dpt-input', value: (cfg && cfg.python) || '', onChange: set('python'), placeholder: 'python' })),
          h(Row, { label: '模型' },
            h('input', { className: 'dpt-input', value: (cfg && cfg.model) || '', onChange: set('model') })),
          h(Row, { label: '接口地址' },
            h('input', { className: 'dpt-input', value: (cfg && cfg.baseUrl) || '', onChange: set('baseUrl') })),
          h(Row, { label: 'API Key' },
            h('input', { className: 'dpt-input', type: 'password', value: (cfg && cfg.apiKey) || '', onChange: set('apiKey'), placeholder: '留空则用环境变量 / DSH 凭据' })),
          h(Row, { label: '专业方向' },
            h('input', {
              className: 'dpt-input',
              value: domain,
              onChange: (e) => setDomain(e.target.value),
              placeholder: '例如：粒子物理 / 医疗器械 / 合同法',
            }),
            h('button', { className: 'dpt-btn', onClick: buildGlossary, disabled: building },
              building ? '生成中…' : '生成词库')),
          h(Row, { label: '术语表' },
            h('select', {
              className: 'dpt-input',
              style: { maxWidth: 240, flex: 'none' },
              value: '',
              onChange: (e) => {
                if (!e.target.value) return
                setCfg({ ...(cfg || {}), glossary: e.target.value })
                post('/config', { glossary: e.target.value })
              },
            },
              h('option', { value: '' }, '选择已有词库…'),
              glossaries.map((g, i) => h('option', { key: i, value: g.path }, g.name))),
            h('input', { className: 'dpt-input', value: (cfg && cfg.glossary) || '', onChange: set('glossary'), placeholder: '可留空：没有词表也能跑' })),
          h('div', { className: 'dpt-row' },
            h('button', { className: 'dpt-btn', onClick: save }, '保存配置'),
            h('button', { className: 'dpt-btn', onClick: doCheck }, '环境自检')),
          checks ? h('ul', { className: 'dpt-list' },
            checks.map((c, i) => h('li', { key: i, style: { color: c.ok ? '#15803d' : '#b91c1c' } },
              (c.ok ? '✓ ' : '✗ ') + c.name + '：' + c.detail))) : null),

        gItems.length
          ? h(Card, { title: '词库确认（候选 → 已确认）' },
            h('div', { className: 'dpt-hint', style: { marginBottom: 8 } },
              nameOf(cfg && cfg.glossary) + ' · 共 ' + gItems.length + ' 条 · 已确认 ' +
              gItems.filter((x) => x.status === 'confirmed').length + ' 条。' +
              '只有「已确认」的才当作硬约束参与翻译。'),
            h('div', { className: 'dpt-row', style: { marginBottom: 8 } },
              h('input', {
                className: 'dpt-input',
                value: gSearch,
                onChange: (e) => {
                  setGSearch(e.target.value)
                  setGPage(1)
                },
                placeholder: '搜索英文/中文/备注',
              })),
            h('table', { className: 'dpt-table' },
              h('thead', null, h('tr', null,
                h('th', { style: { width: 48 } }, '确认'),
                h('th', null, '英文'),
                h('th', null, '中文（可改）'),
                h('th', null, '备注'))),
              h('tbody', null,
                gPageItems.map((x) => h('tr', { key: x.idx },
                  h('td', null, h('input', {
                    type: 'checkbox',
                    checked: x.it.status === 'confirmed',
                    onChange: () => toggleItem(x.idx),
                  })),
                  h('td', { className: 'dpt-mono' }, x.it.en),
                  h('td', null, h('input', {
                    className: 'dpt-input',
                    style: { minWidth: 120, padding: '3px 6px' },
                    value: x.it.zh,
                    onChange: (e) => setItem(x.idx, { zh: e.target.value }),
                  })),
                  h('td', { style: { color: '#6b7280' } }, (x.it.note || '').slice(0, 40)))))),

            // 翻页
            h('div', {
              className: 'dpt-row',
              style: { marginTop: 8, alignItems: 'center', gap: 8 },
            },
              h('button', {
                className: 'dpt-btn',
                disabled: gPageSafe <= 1,
                onClick: () => setGPage(Math.max(1, gPageSafe - 1)),
              }, '上一页'),
              h('span', { className: 'dpt-hint' },
                `第 ${gPageSafe} / ${gPages} 页` +
                `（共 ${gFiltered().length} 条${gSearch.trim() ? '，已筛选' : ''}，每页 ${G_PAGE_SIZE} 条）`),
              h('button', {
                className: 'dpt-btn',
                disabled: gPageSafe >= gPages,
                onClick: () => setGPage(Math.min(gPages, gPageSafe + 1)),
              }, '下一页'),
              h('input', {
                className: 'dpt-input',
                style: { width: 70 },
                placeholder: '跳页',
                onKeyDown: (e) => {
                  if (e.key !== 'Enter') return
                  const n = parseInt(e.target.value, 10)
                  if (!Number.isNaN(n)) setGPage(Math.min(gPages, Math.max(1, n)))
                  e.target.value = ''
                },
              })),

            gSearch.trim() && !gFiltered().length
              ? h('div', { className: 'dpt-hint' }, '没有匹配的条目')
              : null,
            h('div', { className: 'dpt-row', style: { marginTop: 10 } },
              h('button', { className: 'dpt-btn', onClick: confirmAll }, '全部设为已确认'),
              h('button', { className: 'dpt-btn dpt-btn-primary', onClick: saveGlossary }, '保存')),
            gMsg ? h('div', { className: 'dpt-hint', style: { marginTop: 6 } }, gMsg) : null)
          : null,

        h(Card, { title: '翻译' },
          h(Row, { label: '文件' },
            h('textarea', {
              className: 'dpt-area',
              value: text,
              onChange: (e) => setText(e.target.value),
              placeholder: '一行一个 PDF 路径；也可以点右边的「选择文件」',
            }),
            h('button', { className: 'dpt-btn', onClick: pick }, '选择文件…')),
          h(Row, { label: '方向' },
            h('select', { className: 'dpt-input', value: to, onChange: (e) => setTo(e.target.value) },
              h('option', { value: 'zh' }, '英文 → 中文'),
              h('option', { value: 'auto' }, '自动判断（按每个文件）'))),
          h(Row, { label: '页码' },
            h('input', { className: 'dpt-input', value: pages, onChange: (e) => setPages(e.target.value), placeholder: '留空=全部；试译写 1-10' })),
          h(Row, { label: '单页重译' },
            h('input', {
              className: 'dpt-input',
              value: rePages,
              onChange: (e) => setRePages(e.target.value),
              placeholder: '只重跑这几页，例如 5 或 5,12（以最近一次成品为底）',
            }),
            h('button', { className: 'dpt-btn', onClick: retranslate, disabled: reBusy },
              reBusy ? '重译中…' : '重译这几页')),
          reMsg ? h('div', { className: 'dpt-hint' }, reMsg) : null,
          h(Row, { label: '参考文献' },
            h('label', { className: 'dpt-hint', style: { display: 'flex', gap: 6, alignItems: 'center', paddingTop: 6 } },
              h('input', {
                type: 'checkbox',
                checked: cfg ? cfg.skipRefs !== false : true,
                onChange: (e) => setCfg({ ...(cfg || {}), skipRefs: e.target.checked }),
              }),
              '参考文献页保留原文（推荐：保住 DOI / arXiv 号）')),
          h(Row, { label: '并行片数' },
            h('select', {
              className: 'dpt-input',
              style: { maxWidth: 220, flex: 'none' },
              value: String((cfg && cfg.parallel) || 2),
              onChange: (e) => setCfg({ ...(cfg || {}), parallel: Number(e.target.value) }),
            },
              h('option', { value: '1' }, '1（串行，最稳）'),
              h('option', { value: '2' }, '2（推荐，快约一倍）'),
              h('option', { value: '3' }, '3（最快，API 压力大）')),
            h('span', { className: 'dpt-hint' }, '只影响速度，不影响译文')),
          h('div', { className: 'dpt-row' },
            h('button', { className: 'dpt-btn dpt-btn-primary', onClick: start, disabled: busy }, busy ? '队列进行中…' : '开始翻译'),
            busy ? h('button', { className: 'dpt-btn', onClick: cancel }, '取消') : null),
          msg ? h('div', { className: 'dpt-hint' }, msg) : null),

        cur ? (() => {
          const real = typeof cur.progress === 'number' ? cur.progress : null
          const est = typeof cur.estProgress === 'number' ? cur.estProgress : null
          // 分片进度是「按片跳」的（2 片就只会 0%→50%→100%），所以有条时间估算时以它为主，
          // 片数只作为文字补充，避免进度条长时间停在 0%
          const shown = est != null ? est : real
          return h(Card, { title: '进度' },
            h('div', { className: 'dpt-hint' },
              nameOf(cur.pdf) + ' · ' + (cur.stage || '') + ' · 已用 ' + (cur.elapsed || 0) + 's'),
            h('div', { className: 'dpt-bar' },
              h('div', {
                className: 'dpt-bar-fill',
                style: shown != null
                  ? {
                      width: Math.round(shown * 100) + '%',
                      animation: 'none',
                      transition: 'width .3s',
                      background: real == null ? '#93c5fd' : '#2563eb',
                    }
                  : {},
              })),
            shown != null
              ? h('div', { className: 'dpt-hint' },
                  (real != null && cur.chunk
                    ? `已完成 ${cur.chunk.done}/${cur.chunk.total} 片 · `
                    : '')
                  + (est != null
                    ? `估算 ${Math.round(est * 100)}%（按预估时长推算）`
                    : `完成 ${Math.round(real * 100)}%`))
              : null,
          cur.error ? h('div', { className: 'dpt-hint', style: { color: '#b91c1c' } }, '错误：' + cur.error) : null,
          rep ? h('div', { className: 'dpt-nums', style: { marginTop: 10 } },
            h('div', { className: 'dpt-num ' + ((qa.must_fix || 0) > 0 ? 'dpt-bad' : 'dpt-ok') },
              h('b', null, String(qa.must_fix || 0)), h('span', null, '必须修')),
            h('div', { className: 'dpt-num' }, h('b', null, String(qa.maybe || 0)), h('span', null, '建议看')),
            h('div', { className: 'dpt-num' }, h('b', null, String(outs.fixups || 0)), h('span', null, '自动修复'))) : null,
          rep ? h('div', { className: 'dpt-row', style: { marginTop: 12 } },
            outs.final ? h('button', { className: 'dpt-btn dpt-btn-primary', onClick: openPath(outs.final) }, '成品 PDF') : null,
            outs.dual ? h('button', { className: 'dpt-btn', onClick: openPath(outs.dual) }, '双语对照') : null,
            outs.qa_md ? h('button', { className: 'dpt-btn', onClick: openPath(outs.qa_md) }, '体检报告') : null,
            outs.consistency ? h('button', { className: 'dpt-btn', onClick: openPath(outs.consistency) }, '术语一致性') : null,
            outs.refine ? h('button', { className: 'dpt-btn', onClick: openPath(outs.refine) }, '质量评分') : null,
            cur.outDir ? h('button', { className: 'dpt-btn', onClick: openPath(cur.outDir) }, '输出目录') : null) : null,
          (cur.qaTop && cur.qaTop.length)
            ? h('div', { style: { marginTop: 10 } },
                h('div', { className: 'dpt-hint', style: { fontWeight: 600 } }, '问题清单（前几条）'),
                h('ul', { className: 'dpt-list' },
                  cur.qaTop.map((it, i) => h('li', { key: i, style: { color: it.level === 'must' ? '#b91c1c' : '#92400e' } },
                    (it.page ? 'p' + it.page + ' ' : '') + it.kind + '：' + String(it.detail || '').slice(0, 60)))))
            : null)
        })()
          : null,

        jobState.queue && jobState.queue.length
          ? h(Card, { title: '队列' },
            h('table', { className: 'dpt-table' },
              h('thead', null, h('tr', null,
                h('th', null, '文件'), h('th', null, '状态'), h('th', null, '必须修'), h('th', null, '建议看'), h('th', null, ''))),
              h('tbody', null,
                jobState.queue.map((q, i) => {
                  const res = (jobState.results || []).find((r) => r.pdf === q.pdf)
                  return h('tr', { key: i },
                    h('td', { className: 'dpt-mono' }, nameOf(q.pdf)),
                    h('td', null, h(Chip, { status: res ? res.status : q.status })),
                    h('td', null, res && res.must != null ? String(res.must) : '—'),
                    h('td', null, res && res.maybe != null ? String(res.maybe) : '—'),
                    h('td', null,
                      res && res.outDir
                        ? h('button', { className: 'dpt-btn', onClick: openPath(res.outDir) }, '打开')
                        : (!res && q.status === 'pending'
                            ? h('button', { className: 'dpt-btn', onClick: () => dequeue(i) }, '移除')
                            : null)))
                }))))
          : null,

        history.length
          ? h(Card, { title: '历史记录' },
            h('table', { className: 'dpt-table' },
              h('thead', null, h('tr', null,
                h('th', null, '文件'), h('th', null, '时间'), h('th', null, '状态'),
                h('th', null, '评分'), h('th', null, '必须修'), h('th', null, '建议看'), h('th', null, ''))),
              h('tbody', null,
                history.slice(0, 8).map((it, i) => h('tr', { key: i },
                  h('td', { className: 'dpt-mono' }, nameOf(it.pdf)),
                  h('td', null, new Date(it.at).toLocaleString('zh-CN', { hour12: false })),
                  h('td', null, h(Chip, { status: it.status })),
                  h('td', { style: { color: (it.score || 0) > 15 ? '#b91c1c' : '#374151' } },
                    it.score != null ? String(it.score) : '—'),
                  h('td', null, it.must != null ? String(it.must) : '—'),
                  h('td', null, it.maybe != null ? String(it.maybe) : '—'),
                  h('td', null,
                    it.outDir ? h('button', { className: 'dpt-btn', onClick: openPath(it.outDir) }, '目录') : null,
                    h('button', { className: 'dpt-btn', style: { marginLeft: 6 }, onClick: () => retry(it) }, '重跑')))))),
            h('div', { className: 'dpt-row', style: { marginTop: 8 } },
              h('button', { className: 'dpt-btn', onClick: clearHistory }, '清空记录')))
          : null,
      )
    }

    function apply(ctx) {
      const slots = ctx.get('slots')
      if (slots === undefined) return

      ctx.effect(() => {
        const style = document.createElement('style')
        style.dataset.plugin = 'pdf-translate'
        style.textContent = CSS
        document.head.appendChild(style)
        return () => style.remove()
      })

      slots.inject('settings.section', () =>
        slots.register({ name: 'settings.section', id: 'pdf-translate', order: 58, label: 'PDF 翻译' }, Page),
      )
    }

    exports.inject = inject
    exports.apply = apply
    return module.exports
  },
})
