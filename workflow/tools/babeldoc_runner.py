"""BabelDOC 启动器：跑原样流程，但顺手把「源段落 ↔ 译文」成对记下来。

为什么要包一层：
    BabelDOC 的译文和原文只活在中间表示里，落地成 PDF 后段落边界会被合并/拆分，
    再从 PDF 反推配对必然错位（实测：版权声明配到了保密说明）。
    而校对、回译校验、术语学习全都需要**准确的段落对**，所以在这里挂钩子：

      ILTranslator.translate(docs) 调用前先快照每段的 unicode（源文），
      调用后同一批段落对象的 unicode 已被换成译文（见 il_translator.py:1004），
      前后一对比就得到成对的 (src, dst)。

由 run_babeldoc.py 以 `python -m babeldoc_runner ...` 调用，
配对日志路径在环境变量 DSH_PAIRS_LOG 里。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

LOG_PATH = os.environ.get("DSH_PAIRS_LOG")
_MAX = int(os.environ.get("DSH_PAIRS_MAX", "50000"))

# 送翻前先「锁住」的东西：引用方括号、定理/图表编号。
# 模型把它们当普通文字会随意挪位（实测会产出「JH [7, 的映射 X + H 定理 5。1], ，」），
# 换成 {cN} 这种标记后，它只敢原样保留——BabelDOC 自己的公式占位符 {vN} 就是这么躲开的。
CITE = re.compile(r"\[[0-9][0-9,;\s–\-]{0,40}\]")
REF_EN = re.compile(
    r"\b(?:Theorem|Lemma|Corollary|Proposition|Definition|Remark|Section|Chapter|"
    r"Fig\.?|Figure|Table|Eq\.?|Equation)\s*\(?\d+(?:[.\-]\d+)*\)?"
)
REF_ZH = re.compile(r"(?:图|表|式|定理|引理|推论|定义|节|章)\s*\(?\d+(?:[.\-]\d+)*\)?")
MASK_STYLE = os.environ.get("DSH_MASK_STYLE", "off")
# 遮蔽标记用 BabelDOC 自己的 {vN} 外形：它的系统提示词本来就要求模型原样保留这类占位符，
# 换别的形状（⟦C1⟧、#C1#）实测会被模型直接丢掉。
_MASK_OFFSET = 9000


def _marker(n: int) -> str:
    if MASK_STYLE == "bracket":
        return "⟦C%d⟧" % n
    if MASK_STYLE == "hash":
        return "#C%d#" % n
    if MASK_STYLE == "brace":
        return "{c%d}" % n
    return "{v%d}" % (_MASK_OFFSET + n)


def _marker_re() -> re.Pattern:
    if MASK_STYLE == "bracket":
        return re.compile(r"⟦C(\d+)⟧")
    if MASK_STYLE == "hash":
        return re.compile(r"#C(\d+)#")
    if MASK_STYLE == "brace":
        return re.compile(r"\{c(\d+)\}")
    return re.compile(r"\{v(9\d{3,})\}")

_state = {"fh": None, "n": 0}


def _log(obj: dict) -> None:
    """遮蔽统计单独记一行，方便验证「锁住的东西有没有原样回来」。"""
    path = os.environ.get("DSH_MASK_LOG")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


def mask_citations(text: str) -> tuple[str, dict[str, str]]:
    """把引用和编号换成 {cN}，返回 (遮蔽后文本, {标记: 原文})。"""
    table: dict[str, str] = {}
    counter = [0]

    def repl(m: re.Match) -> str:
        token = m.group(0)
        counter[0] += 1
        key = _marker(counter[0])
        table[key] = token
        return key

    out = CITE.sub(repl, text)
    out = REF_EN.sub(repl, out)
    out = REF_ZH.sub(repl, out)
    return out, table


def unmask_citations(text: str, table: dict[str, str]) -> tuple[str, list[str]]:
    """把 {cN} 换回原文；返回 (还原后文本, 丢失的标记列表)。

    模型偶尔会把标记吃掉或改写，丢了的记下来——这一段会被质检点名。
    """
    missing = []

    def repl(m: re.Match) -> str:
        key = m.group(0)
        if key in table:
            return table[key]
        missing.append(key)
        return ""

    out = _marker_re().sub(repl, text)
    for key in table:
        if key not in text:
            missing.append(key)
    return out, missing


def _open_log():
    if _state["fh"] or not LOG_PATH:
        return _state["fh"]
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _state["fh"] = open(LOG_PATH, "a", encoding="utf-8")
    return _state["fh"]


def _iter_paragraphs(docs):
    """兼容不同版本的结构：docs.page[*].pdf_paragraph[*]。"""
    pages = getattr(docs, "page", None) or []
    for page in pages:
        for para in getattr(page, "pdf_paragraph", None) or []:
            yield para


def _write_pair(src: str, dst: str, engine: str) -> None:
    fh = _open_log()
    if fh is None or _state["n"] >= _MAX:
        return
    if not src or not dst or src == dst:
        return
    fh.write(json.dumps({"src": src, "dst": dst, "engine": engine,
                         "ts": round(time.time(), 3)}, ensure_ascii=False) + "\n")
    fh.flush()
    _state["n"] += 1


def _install_hook() -> None:
    try:
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    except Exception:
        ILTranslator = None
    try:
        from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
            ILTranslatorLLMOnly,
        )
    except Exception:
        ILTranslatorLLMOnly = None

    for cls in [c for c in (ILTranslator, ILTranslatorLLMOnly) if c is not None]:
        if getattr(cls, "_dsh_hooked", False):
            continue
        original = cls.translate

        def make(orig, name):
            def translate(self, docs):  # noqa: ANN001
                # 只用来抓「源段 ↔ 译文」配对；遮蔽放在引擎调用处（见下面 do_llm_translate）
                snapshot = {}
                try:
                    for para in _iter_paragraphs(docs):
                        snapshot[id(para)] = getattr(para, "unicode", "") or ""
                except Exception as e:
                    _log({"event": "snapshot_error", "error": str(e)})
                result = orig(self, docs)
                try:
                    for para in _iter_paragraphs(docs):
                        src = snapshot.get(id(para))
                        dst = getattr(para, "unicode", "") or ""
                        if src:
                            _write_pair(src, dst, name)
                except Exception as e:
                    _log({"event": "pair_error", "error": str(e)})
                return result

            return translate

        cls.translate = make(original, cls.__name__)
        cls._dsh_hooked = True

    # 真正的遮蔽点：引擎收到文本的那一刻。
    # 在 ILTranslator 层改 paragraph.unicode 没用——它调用前会重新算一遍段落文本。
    try:
        from babeldoc.translator.translator import BaseTranslator

        def wrap(owner) -> None:
            # 真正被调用的是 llm_translate；do_llm_translate 只是能力探测，也一并包上兜底
            for name in ("llm_translate", "do_llm_translate"):
                if "_dsh_mask_" + name in owner.__dict__:
                    continue
                orig_fn = getattr(owner, name, None)
                if orig_fn is None:
                    continue

                def make(orig):
                    def wrapper(self, text, *args, **kwargs):  # noqa: ANN001
                        if not isinstance(text, str) or not text:
                            return orig(self, text, *args, **kwargs)
                        # 遮蔽实验未通过（标记会被吞 / 只覆盖到部分调用），默认关闭
                        if MASK_STYLE == "off":
                            return orig(self, text, *args, **kwargs)
                        masked, table = mask_citations(text)
                        out = orig(self, masked, *args, **kwargs)
                        if table and isinstance(out, str):
                            out, missing = unmask_citations(out, table)
                            _log({"event": "mask", "fn": name, "tokens": len(table),
                                  "restored": len(table) - len(missing),
                                  "missing": len(missing), "lost": missing[:6]})
                        return out

                    return wrapper

                setattr(owner, name, make(orig_fn))
                setattr(owner, "_dsh_mask_" + name, True)

        def walk(cls) -> None:
            wrap(cls)
            for sub in cls.__subclasses__():
                walk(sub)

        walk(BaseTranslator)
    except Exception as e:
        _log({"event": "mask_hook_error", "error": str(e)})


def _install_usage_hook() -> None:
    """统计本次翻译实际消耗的 token。

    BabelDOC 的日志里只有部分版本会打 "Total tokens"，靠日志猜成本经常是 0。
    这里直接裹住 openai 的 chat.completions.create，从 response.usage 累计。
    """
    try:
        import openai.resources.chat.completions as _cc
    except Exception as e:
        _log({"event": "usage_hook_error", "error": str(e)})
        return

    usage = {"prompt": 0, "completion": 0, "total": 0}

    def _acc(resp) -> None:
        try:
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["prompt"] += int(getattr(u, "prompt_tokens", 0) or 0)
                usage["completion"] += int(getattr(u, "completion_tokens", 0) or 0)
                usage["total"] += int(getattr(u, "total_tokens", 0) or 0)
        except Exception:
            pass

    # BabelDOC 用的是同步 openai.OpenAI（translator.py 里 self.client.chat.completions.create）
    sync_orig = _cc.Completions.create
    if not getattr(sync_orig, "_dsh_usage_hooked", False):
        def create(self, *args, **kwargs):  # noqa: ANN001
            resp = sync_orig(self, *args, **kwargs)
            _acc(resp)
            return resp

        create._dsh_usage_hooked = True
        _cc.Completions.create = create

    async_orig = _cc.AsyncCompletions.create
    if not getattr(async_orig, "_dsh_usage_hooked", False):
        async def acreate(self, *args, **kwargs):  # noqa: ANN001
            resp = await async_orig(self, *args, **kwargs)
            _acc(resp)
            return resp

        acreate._dsh_usage_hooked = True
        _cc.AsyncCompletions.create = acreate

    import atexit

    def _dump() -> None:
        if usage["total"]:
            print(f"DSH_USAGE total={usage['total']} "
                  f"prompt={usage['prompt']} completion={usage['completion']}", flush=True)

    atexit.register(_dump)


def main() -> int:
    sys.argv = ["babeldoc", *sys.argv[1:]]
    _install_hook()
    _install_usage_hook()
    from babeldoc.main import cli

    cli()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
