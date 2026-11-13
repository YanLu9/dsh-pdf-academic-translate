"""用 BabelDOC 出「保版式」PDF：术语表预载 + 自动术语抽取 + 双/单语输出。

为什么要有这一层：
  - BabelDOC 装在独立 venv（`../babeldoc-env`），不能污染系统 Python；
    但 venv 里的 python.exe 在本机被安全软件拦写（EACCES），所以这里统一用
    `sys._base_executable`（系统 Python）+ PYTHONPATH 指向 venv 的 site-packages 来跑。
  - API key 从 DSH 的凭据库读，不写进任何文件。

用法：
    python tools/run_babeldoc.py ../../translation_test_en.pdf --to zh \
        --glossary glossary/out/babeldoc_en_zh.csv --out out/test_en_zh
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 三种跑法：
#   1) 开发机：../babeldoc-env 这个 venv
#   2) 插件安装后的便携运行时：BABELDOC_SITE 指向 runtime/site-packages
#   3) 完全自带解释器：BABELDOC_PYTHON 指向 runtime/python/python.exe
VENV = Path(os.environ.get("BABELDOC_VENV") or (ROOT.parent / "babeldoc-env"))
SITE = Path(os.environ["BABELDOC_SITE"]) if os.environ.get("BABELDOC_SITE") else VENV / "Lib" / "site-packages"
CRED = Path.home() / ".dsh" / ".credentials.yaml"


def dsh_refs() -> dict[str, str]:
    """读 DSH 凭据库里的 `refs:` 段（键名 → 令牌）。

    格式（`~/.dsh/.credentials.yaml`）：
        refs:
          DEEPSEEK_API_KEY: sk-xxxx
    没有 pyyaml 依赖，只认这段固定的「两空格缩进的键值」。
    """
    if not CRED.exists():
        return {}
    text = CRED.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^refs:\s*$\n((?:[ \t]+\S+:[ \t]*\S+[ \t]*\n?)*)", text, re.M)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, _, v = line.strip().partition(":")
        if k and v.strip():
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _resolve_key() -> tuple[str, str]:
    """返回 (key, 供应商)。供应商用来决定 base_url，避免拿 OpenAI 的 key 去调 DeepSeek。

    优先级：环境变量 > DSH 凭据库 refs 段（按名字挑） > 凭据库里第一条 sk-。
    """
    for name, provider in (("BABELDOC_API_KEY", "deepseek"), ("OPENAI_API_KEY", "openai")):
        key = os.environ.get(name)
        if key:
            return key, provider

    refs = dsh_refs()
    # 先按名字挑：DEEPSEEK_API_KEY 优先，其次任何 *_API_KEY
    for name in sorted(refs, key=lambda n: (not n.startswith("DEEPSEEK"), n)):
        val = refs[name]
        if not name.endswith("_API_KEY") or not val:
            continue
        provider = "openai" if "OPENAI" in name else "deepseek"
        return val, provider

    if CRED.exists():
        found = re.findall(r"sk-[A-Za-z0-9_\-]{20,}", CRED.read_text(encoding="utf-8"))
        if found:
            return found[0], "deepseek"
    raise SystemExit(
        "找不到可用的 API Key。\\n"
        "  1) 面板「环境」里填一个 DeepSeek 的 sk- key（最省事）；\\n"
        "  2) 或设环境变量 BABELDOC_API_KEY / OPENAI_API_KEY；\\n"
        "  3) 旧版 DSH 会把 key 存在 %s 的 refs 段（新版存的是登录态，不是 key，会 401）。" % CRED
    )


def api_key() -> str:
    """只给调用方要的那串 key（兼容原来的签名）。"""
    return _resolve_key()[0]


def api_provider() -> str:
    """这个 key 属于哪家（deepseek / openai），用来对齐 base_url。"""
    return _resolve_key()[1]


def base_python() -> str:
    """用哪个解释器跑 babeldoc_runner。

    插件装了便携运行时时用 BABELDOC_PYTHON（自带解释器）；开发机上退回系统 Python
    （venv 里的 python.exe 在本机被安全软件拦写，不能用它）。
    """
    return os.environ.get("BABELDOC_PYTHON") or getattr(sys, "_base_executable", None) or sys.executable


def build(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    p = argparse.ArgumentParser(prog="run_babeldoc")
    p.add_argument("pdf")
    p.add_argument("--to", default="zh", choices=["zh", "en"])
    p.add_argument("--from", dest="src", default=None, help="源语言，默认按 --to 反推")
    p.add_argument("--out", default=None)
    p.add_argument("--glossary", default=None,
                   help="BabelDOC CSV（source,target,tgt_lng），多个用英文逗号分隔")
    p.add_argument("--pages", default=None, help="只处理这些页，如 1-2")
    p.add_argument("--model", default=os.environ.get("BABELDOC_MODEL", "deepseek-chat"))
    p.add_argument("--base-url", default=os.environ.get("BABELDOC_BASE_URL", ""))
    p.add_argument("--term-model", default=None, help="术语抽取单独用别的模型")
    p.add_argument("--no-auto-glossary", action="store_true", help="关掉自动术语抽取")
    p.add_argument("--keep-auto-glossary", action="store_true", default=True,
                   help="把自动抽出的术语存成 CSV（默认开）")
    p.add_argument("--mono-only", action="store_true", help="只出单语，不出双语对照")
    p.add_argument("--dual-only", action="store_true", help="只出双语对照")
    p.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="原样透传给 BabelDOC")
    a = p.parse_args(argv)

    pdf = Path(a.pdf).expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"找不到文件：{pdf}")
    out = Path(a.out).expanduser().resolve() if a.out else ROOT / "out" / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    src = a.src or ("en" if a.to == "zh" else "zh")

    # 没显式给 base_url 时，按 key 的归属选端点：DSH 里配的是 DeepSeek 就走 DeepSeek，
    # 是 OpenAI 的 key 就走 OpenAI，别拿错家的 key 去调另一家（会 401）
    if not a.base_url:
        a.base_url = ("https://api.openai.com/v1" if api_provider() == "openai"
                      else "https://api.deepseek.com/v1")

    cmd = [
        # 用脚本路径而不是 -m：便携 Python 的 python._pth 会忽略 PYTHONPATH，
        # 用脚本路径能把 tools 目录自动放进 sys.path[0]，不依赖环境变量
        base_python(), str(ROOT / "tools" / "babeldoc_runner.py"),
        "--files", str(pdf),
        "--lang-in", src,
        "--lang-out", a.to,
        "--output", str(out),
        "--openai",
        "--openai-model", a.model,
        "--openai-base-url", a.base_url,
        "--openai-api-key", api_key(),
        "--watermark-output-mode", "no_watermark",
        "--skip-scanned-detection",
    ]
    if a.term_model:
        cmd += ["--openai-term-extraction-model", a.term_model]
    if a.glossary:
        paths = []
        for raw in str(a.glossary).split(","):
            raw = raw.strip()
            if not raw:
                continue
            g = Path(raw).expanduser().resolve()
            if not g.exists():
                raise SystemExit(f"找不到术语表：{g}")
            paths.append(str(g))
        cmd += ["--glossary-files", ",".join(paths)]
    if a.pages:
        cmd += ["--pages", a.pages]
    if a.no_auto_glossary:
        cmd += ["--no-auto-extract-glossary"]
    elif a.keep_auto_glossary:
        cmd += ["--save-auto-extracted-glossary"]
    if a.mono_only:
        cmd += ["--no-dual"]
    if a.dual_only:
        cmd += ["--no-mono"]
    cmd += list(a.extra)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 不缓冲：否则 rich 的进度会攒到结束才一股脑吐出来，界面上的进度条等于没有
    env["PYTHONUNBUFFERED"] = "1"
    # babeldoc_runner 在 tools/ 下；配对日志落到本次输出目录
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "tools"), str(SITE)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["DSH_PAIRS_LOG"] = str(out / "pairs.jsonl")
    env["DSH_MASK_LOG"] = str(out / "mask_stats.jsonl")
    print(f"[babeldoc] {pdf.name}: {src} -> {a.to}  输出 {out}", file=sys.stderr, flush=True)
    print(f"[babeldoc] 术语表：{a.glossary or '无（仅自动抽取）'}", file=sys.stderr, flush=True)
    return cmd, env


STAGE_RE = re.compile(
    r"^\s*(Parse PDF and Create[^\r\n]*?|Parse Page Layout|Parse Paragraphs|"
    r"Parse Formulas and Styles|Automatic Term Extraction|Translate Paragraphs|"
    r"Typesetting|Add Fonts|Generate drawing instructions|Subset font|Save PDF)"
    r"\s*\(\d+/\d+\)\s*-+"
)
TOKEN_RE = re.compile(
    r"(Total tokens|Prompt tokens|Completion tokens|Cache hit prompt tokens|"
    r"Term extraction tokens)[\s:]+([\d]+)",
    re.I,
)


def _run(cmd: list[str], env: dict[str, str], as_json: bool) -> int:
    """跑 BabelDOC，顺手把它日志里的阶段和 token 数摘成结构化事件。

    --json 时：stdout 只有 NDJSON 事件，原始日志一律走 stderr（插件靠这个画进度条）。
    """
    t0 = time.time()
    tokens: dict[str, str] = {}
    stages: list[str] = []

    def emit(obj: dict) -> None:
        if as_json:
            obj = {"ts": round(time.time() - t0, 2), **obj}
            print(json.dumps(obj, ensure_ascii=False), flush=True)

    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdout is not None

    # BabelDOC 的进度条在非 tty 下会攒到最后才吐，所以额外发心跳，
    # 让前端至少能显示"已用时 / 还在跑"，而不是一直 0%。
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(2.0):
            emit({"event": "heartbeat", "elapsed_s": round(time.time() - t0, 1)})

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        print(line, file=sys.stderr, flush=True)

        m = TOKEN_RE.search(line.strip())
        if m:
            key = m.group(1).lower().replace(" ", "_")
            val = re.sub(r"\s+", "", m.group(2))
            tokens[key] = val
            emit({"event": "tokens", "key": key, "value": val})
            continue

        m = STAGE_RE.match(line)
        if m:
            name = m.group(1).strip()
            stages.append(name)
            emit({"event": "stage", "name": name,
                  "index": len(stages)})

        # babeldoc_runner 退出时打的真实用量（比日志里的 "Total tokens" 可靠）
        if line.startswith("DSH_USAGE "):
            kv = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
            if kv.get("total"):
                tokens["total_tokens"] = kv["total"]
                tokens["prompt_tokens"] = kv.get("prompt", "")
                tokens["completion_tokens"] = kv.get("completion", "")
                for k, v in tokens.items():
                    if v:
                        emit({"event": "tokens", "key": k, "value": v})

    rc = proc.wait()
    stop.set()
    duration = round(time.time() - t0, 1)
    emit({"event": "babeldoc_done", "code": rc, "duration_s": duration,
          "stages": stages, "tokens": tokens})
    return rc


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    as_json = "--json" in args
    cmd, env = build([x for x in args if x != "--json"])
    return _run(cmd, env, as_json)


if __name__ == "__main__":
    raise SystemExit(main())
