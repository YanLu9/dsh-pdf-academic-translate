# dsh-pdf-academic-translate

在 DSH 设置页里加一个「PDF 翻译」面板：保版式翻译 → 自动修复 → 体检 → 术语一致性，最后给出成品 PDF。

**Windows 10/11（64 位）**。插件自带 Python 和翻译流水线，装完点一次「一键安装」就能用，
不需要自己装 Python、也不用配环境变量。

**需要自己准备一个 API Key**（DeepSeek 的 sk-...，或任意 OpenAI 兼容接口）：
DSH 的登录态**不是** API Key——它调不了模型；旧版 DSH 曾把 key 存在凭据库的 efs: 段，
从 0.2.x 起改成了 ecords: 登录令牌，因此插件无法再自动取到 key。

## 安装

**方式一：能直连 GitHub（海外或配好代理）**

```powershell
dsh plugin --profile desktop add https://github.com/YanLu9/dsh-pdf-academic-translate/releases/download/v0.6.2/dsh-pdf-academic-translate-0.6.2.tgz
```

**方式二：连不上 GitHub（国内常见）**

1. 浏览器打开上面那个链接下载 `.tgz`（浏览器走系统代理，通常能下）；
2. 本机安装：

```powershell
dsh plugin --profile desktop add file:<下载好的 tgz 路径>
```

> ⚠️ pnpm **不读 Windows 系统代理**。开着 Clash 之类但没给 pnpm 配代理的机器，方式一会卡住不报错。
> 想让方式一可用，先配一次：`dsh` 自带的 pnpm 执行 `config set proxy http://127.0.0.1:7890`（端口换成你的）。

装完**重启 DSH**（插件有宿主侧代码，刷新页面不够）。

## 首次使用：点一下「一键安装」

1. 点 **一键安装**：插件会把自带的便携 Python 和流水线脚本解到
   `%LOCALAPPDATA%\dsh-pdf-translate\`，然后用 pip 装 BabelDOC。
   **首次约 1 GB 下载**（模型 + 依赖），10 分钟左右；界面里有实时日志，可以关掉页面等它装完。
2. 装完卡片消失，**工作目录**自动填好，「方向」默认就是「英文 → 中文」。
3. 点 **选择文件…** 选 PDF（可多选），也可以一行粘一个路径。
4. 点 **开始翻译**，队列按顺序一篇篇跑；参考文献页按规则保留原文。

装完之后不再需要任何配置。**换个版本更新插件不会重装运行时**，除非你手动删了
`%LOCALAPPDATA%\dsh-pdf-translate`。

### 网络不通时：离线安装

首次安装要从 PyPI 下依赖（约 700 MB）。机器连不上 PyPI 时，用离线包：

1. 拿到 `babeldoc-runtime.zip`（约 290 MB，内含 BabelDOC 全部依赖）；
2. 放到 `%LOCALAPPDATA%\dsh-pdf-translate\babeldoc-runtime.zip`；
3. 再点一次「一键安装」——检测到离线包就直接解压，**全程不需要网络**，约 20 秒。

也可以用环境变量指定位置：`DSH_PDF_RUNTIME_ZIP=<zip 路径>`。

## 可选：专业词库

**专业方向**（可选）：填「粒子物理 / 医疗器械 / 合同法」这类词，点 **生成词库** ——
一次模型调用约 10 秒，产出 `glossary/<方向>.csv`，每行标 `status=candidate`；
生成后自动填进「术语表」，也可以从下拉框挑已有词库。
⚠️ 这是**候选**词表：机器给的译名可能有错，要在面板下方的「词库确认」里勾选成 `confirmed` 才会生效。

## 不开面板也能翻（对话里直接丢 PDF）

把 PDF 路径发出来、说「翻译一下」即可：工作区的 `AGENTS.md` 写了调用方式，跑的是同一套流水线。

```powershell
cd translation-workflow
python tools\translate_auto.py "C:\path\to\paper.pdf"          # 自动判方向
python tools\translate_auto.py "C:\path\to\paper.pdf" --glossary glossary\<你的领域>.csv --pages 1-10
```

## 队列与历史

- 一次可丢多篇，顺序执行（不并发，避免抢额度）；排队中的可以「移除」；
- **进度按片显示**（第 2/5 片）；一片失败只重这一片，重跑时已完成的片直接复用；
- 每篇跑完立刻出结果：状态、评分、必须修、建议看，可单独「打开」输出目录；
- **历史记录**存在 `~/.dsh/pdf-translate-history.json`，重启后还在，带**评分列**（>15 标红），可「目录」或「重跑」；
- 「取消」杀掉当前进程，未开始的标为已取消；
- 「参考文献」开关默认打开：参考文献页 + 目录页保留原文（保住 DOI / arXiv 号、避免目录页码粘连）。

## 词库确认

生成或选中的词库会在面板下方列出来：勾选、直接改中文列、保存。
只有 `confirmed` 的术语才当硬约束参与体检；`candidate` 只是提示。

## 面板会给出什么

| 数字/按钮 | 含义 |
|---|---|
| 必须修 | 数字丢失、方向反转等硬伤（红底表示 >0） |
| 建议看 | 残留未译、日期拆碎等 |
| 自动修复 | 本轮自动改掉的条数（方向反转、叠字重译、补译） |
| 打开成品 PDF | `final.pdf`（已应用自动修复） |
| 双语对照 | BabelDOC 出的双语文档 |
| 体检报告 / 术语一致性 | 两张 Markdown 表 |

## 限制（与流水线一致）

- 只处理**有文本层**的 PDF；扫描件要 OCR（未接）。
- 图内文字不译（图注会译）。
- 公式段里的编号可能丢（低频）。
- 进度条是“不确定”样式：BabelDOC 在非终端下会把进度攒着吐，界面靠心跳显示已用时。

## 卸载

```powershell
dsh plugin --profile desktop remove dsh-pdf-academic-translate
```
配置留在 `~/.dsh/pdf-translate.json`，可手动删除。

## API Key 怎么配

面板「环境」卡片里有三个输入框：**模型 / 接口地址 / API Key**。默认是 DeepSeek：

```
模型       deepseek-chat
接口地址   https://api.deepseek.com/v1
API Key    sk-...（你自己的）
```

> **为什么不能像以前那样自动取 key？**
> DSH 0.2.x 改了凭据库格式：以前是 `refs: DEEPSEEK_API_KEY: sk-...`，现在是 `records:` 里存**登录令牌**。
> 登录令牌不是 API Key，拿它调 `api.deepseek.com` 会 401。所以插件现在只能：
> ① 面板里填的 key → ② 环境变量 `BABELDOC_API_KEY` / `OPENAI_API_KEY` → ③ 旧格式 `refs:` 段（老版本 DSH 才有）。
> 三者都没有时，面板顶部会直接显示「需要 API Key」，而不是等你点了才报错。

## 命名对照（四个名字，别混）

| 名称 | 值 | 用在哪 |
|---|---|---|
| 包名 / 仓库名 | `dsh-pdf-academic-translate` | `dsh plugin add`、卸载、市场条目 |
| patch 行名 / 客户端模块 id | `dsh-pdf-academic-translate` | `cordis.patch.yml`、`client.js` 的 `load({id})`，**必须等于包名** |
| 面板显示名 | `PDF 翻译` | 设置页里看到的 |
| 后端路由前缀 / 数据目录 | `dsh-pdf-translate` | `lib/index.js` 的 `P`、`client.js` 的 `B`、`%LOCALAPPDATA%\dsh-pdf-translate`，**改名时不要动** |

改包名时只需要改前两行，后两行保持不变；`node scripts/verify-package.mjs` 会帮你检查。
## 分发（发到市场 / 给别人装）

- **插件包里不含任何术语表**：`package.json` 的 `files` 只放 `lib`、`cordis.patch.yml`、`README.md`，
  `npm pack` 出来的 tgz 里不会带 `glossary/*.csv`。
- **词库是每个用户自己的**：装在 `translation-workflow/glossary/` 下，由面板的「生成词库」按领域现生成，
  或者用户自己写。路径存在各自的 `~/.dsh/pdf-translate.json` 里，不进包。
- **不填词库也能跑**（`glossary` 留空，流水线直接跳过术语注入），所以新用户开箱可用。
- 只导 `status=confirmed` 的条目参与术语约束，`candidate` 只做提示——用户生成完记得在面板里勾选确认。
