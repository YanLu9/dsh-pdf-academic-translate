r"""把「机器能确定的错」变成修订清单（edits.csv），交给 patch_pdf 就地改。

目前处理两类：
    方向反了   英文页被翻成中文 → 用原文英文替换回去（不调模型，零风险）
    源文叠字   源文被解析成「奇奇奇…」→ 用 PyMuPDF 的干净原文重译一次 → 替换

为什么不让 BabelDOC 自己修：
    这两类问题分别出在它的语言判定和字符解析上，改参数不一定管用；
    而「找到这一段、换成对的文字」这件事 patch_pdf 已经能做，确定性更高。

用法：
    python tools/make_fixups.py --pairs out\pairs_p6\pairs.jsonl \
        --source ..\论文.pdf --to en --out out\pairs_p6\edits.csv [--retranslate]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
from pathlib import Path

import pymupdf

from backcheck import CJK, DUP_CJK, clean, locate, page_index
from run_babeldoc import api_key

BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"


def lang_of(text: str) -> str:
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return "en"
    if latin < cjk * 0.15:
        return "zh"
    return "mixed"


def retranslate(text: str, to: str, terms: dict[str, str] | None = None,
                extra_instruction: str = "") -> str:
    hint = ""
    hits: list[tuple[str, str]] = []
    if terms:
        hits = [(zh, en) for zh, en in terms.items() if zh in text]
        if hits:
            hint = "\n术语必须按下表译，不得自创：\n" + "\n".join(
                f"- {zh} → {en}" for zh, en in hits[:12])

    def call(extra: str) -> str:
        if to == "zh":
            head = "把下面的英文翻译成简体中文，只输出译文，不加解释。保持公式、符号、编号与数字不变。"
        else:
            head = "把下面的中文翻译成学术英语，只输出译文，不加解释。保持术语准确、公式符号与数字不变。"
        prompt = f"{head}{hint}{extra_instruction}{extra}\n\n{text}"
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode()
        import urllib.request
        req = urllib.request.Request(
            BASE_URL, data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"].strip()

    out = call("")
    # 模型偶尔无视术语提示（实测「费米镜」会被译回 Fermi-Mirror），验一遍再催一次
    missing = [en for _, en in hits if en.split()[0].lower() not in out.lower()]
    if missing:
        out = call("\n特别注意：译文里必须原样出现这些英文：" + "；".join(missing))
    return out


def batch_translate(texts: list[str], terms: dict[str, str], to: str = "zh") -> list[str]:
    """一次翻多条，省调用；失败返回同长度空串列表。

    方向必须跟着作业方向走：本项目是英→中，之前这里写死「中文翻成学术英语」，
    结果把已经译好的中文段落又「翻」回英文，整本成品被打回英文。
    """
    if not texts:
        return []
    out: list[str] = []
    if to == "zh":
        head = ("把下面每段英文翻译成简体中文，只输出 JSON 数组（与输入顺序一致，"
                "每项是一段译文），不要解释。保持公式、符号、编号与数字不变。")
    else:
        head = ("把下面每段中文翻译成学术英语，只输出 JSON 数组（与输入顺序一致，"
                "每项是一段译文），不要解释。")
    for i in range(0, len(texts), 5):
        chunk = texts[i:i + 5]
        num = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(chunk))
        prompt = head + "\n\n" + num
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode()
        try:
            import urllib.request
            req = urllib.request.Request(
                BASE_URL, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key()}"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            m = re.search(r"\[.*\]", content, re.S)
            parsed = json.loads(m.group(0) if m else content)
            got = [str(x).strip() for x in parsed]
            out.extend((got + [""] * len(chunk))[: len(chunk)])
        except Exception as e:
            print(f"! 批量翻译失败：{e}", file=sys.stderr)
            out.extend([""] * len(chunk))
    return out


def find_untranslated(pdf: Path, pages: list[int] | None = None,
                      to: str = "zh") -> list[dict]:
    """在译文 PDF 里找「本该是目标语言、却还是源语言」的整块。

    英→中（本项目唯一在用的方向）：找「整段还是英文」的块——没有汉字、像句子
    （至少 8 个词、其中 5 个以上是小写词），避免把公式、表格线里的字母当成漏译。

    中→英：找「整段还是中文」的块（保留旧逻辑，不再重点维护）。
    """
    doc = pymupdf.open(pdf)
    hits = []
    for pno in range(doc.page_count):
        if pages and (pno + 1) not in pages:
            continue
        for b in doc[pno].get_text("blocks"):
            text = re.sub(r"\s+", " ", (b[4] or "")).strip()
            cjk = len(CJK.findall(text))
            latin = len(re.findall(r"[A-Za-z]", text))
            if to == "zh":
                words = re.findall(r"[A-Za-z]{2,}", text)
                lower = sum(1 for w in words if w.islower())
                if cjk == 0 and latin >= 40 and len(words) >= 8 and lower >= 5:
                    hits.append({"page": pno + 1, "text": text})
            elif cjk >= 8 and latin < cjk * 0.2 and len(text) >= 20:
                hits.append({"page": pno + 1, "text": text})
    doc.close()
    return hits


def find_blocks_with(pdf: Path, tokens: list[str], pages: list[int] | None = None,
                     to: str = "zh") -> list[dict]:
    """按体检点名的残留词，找出它所在的文字块（用于补译很短的标题）。

    to="zh" 时残留的是英文词（如 Abstract），块里本来就没几个汉字；
    to="en" 时残留的是中文词，才要求块里至少有 2 个汉字。
    """
    doc = pymupdf.open(pdf)
    hits, seen = [], set()
    for pno in range(doc.page_count):
        if pages and (pno + 1) not in pages:
            continue
        for b in doc[pno].get_text("blocks"):
            text = re.sub(r"\s+", " ", (b[4] or "")).strip()
            if not text or text in seen:
                continue
            if any(tok in text for tok in tokens):
                if to != "zh" and len(CJK.findall(text)) < 2:
                    continue
                seen.add(text)
                hits.append({"page": pno + 1, "text": text})
    doc.close()
    return hits


NUMBERING_RE = re.compile(
    r"\(\d+(?:[.\-]\d+)+\)"                      # (5.1) (2-3)
    r"|\b(?:Theorem|Lemma|Corollary|Proposition|Definition|Remark|Section|Chapter)\s*\d+(?:\.\d+)*"
    r"|\b(?:Fig\.?|Figure|Table|Eq\.?|Equation)\s*\(?\d+(?:[.\-]\d+)*\)?"
    r"|(?:图|表|式|定理|引理|推论|定义)\s*\(?\d+(?:[.\-]\d+)*\)?"
)


def numbering_tokens(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", m.group(0)).strip() for m in NUMBERING_RE.finditer(text)}


def retranslate_keep_numbers(text: str, to: str, terms: dict[str, str] | None,
                             must_keep: list[str]) -> str:
    """重译并强制保留编号（定理/公式/图表引用）。"""
    extra = ""
    if must_keep:
        extra = "\n译文必须原样保留这些编号，一个字符都不能改：" + "、".join(must_keep[:10])
    return retranslate(text, to, terms, extra)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="make_fixups")
    p.add_argument("--pairs", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--to", default="en", choices=["zh", "en"])
    p.add_argument("--out", default=None)
    p.add_argument("--retranslate", action="store_true", help="叠字段落用干净原文重译")
    p.add_argument("--glossary", default="glossary/physics.exotic-hadrons.csv")
    p.add_argument("--translated", default=None,
                   help="译文 PDF；给了就顺带补译「整段没被翻译」的内容")
    p.add_argument("--pages", default=None, help="补译时只看这些页，如 1-6")
    p.add_argument("--qa-json", default=None,
                   help="读体检报告里的「疑似漏译」词，按词反查块补译（覆盖很短的标题）")
    a = p.parse_args(argv)

    terms: dict[str, str] = {}
    gp = Path(a.glossary)
    if gp.exists():
        with gp.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                en, zh = (row.get("en") or "").strip(), (row.get("zh") or "").strip()
                # 只认人工确认过的译名；candidate 混进重译提示里会把普通词钉错
                if (row.get("status") or "").strip().lower() not in ("", "confirmed"):
                    continue
                if en and zh:
                    terms[zh] = en

    pages = page_index(Path(a.source))
    clean_src_by_page = {}
    doc = pymupdf.open(a.source)
    for i, page in enumerate(doc):
        clean_src_by_page[i + 1] = re.sub(r"\s+", "", page.get_text())
    doc.close()

    edits = []
    for line in Path(a.pairs).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        src_raw, dst_raw = obj.get("src") or "", obj.get("dst") or ""
        src, _ = clean(src_raw)
        dst, had_ph = clean(dst_raw)
        if len(src) < 6 or len(dst) < 2 or had_ph:
            continue
        # 叠字的源文在页文本里是干净的，定位必须用还原后的文字
        uniq = re.sub(r"([\u4e00-\u9fff])\1{2,}", r"\1", src)
        pno = locate(uniq, pages)

        # ① 方向反了：把中文译文换回原文
        if a.to == "en" and lang_of(src) == "en" and lang_of(dst) == "zh":
            edits.append({"page": pno or "", "old": dst, "new": src, "why": "方向反转"})
            continue

        # ② 源文叠字：重译（可选）
        dup = DUP_CJK.search(src)
        if dup and a.retranslate and pno:
            clean_text = clean_source_span(clean_src_by_page.get(pno, ""), src)
            if clean_text:
                try:
                    fresh = retranslate(clean_text, a.to, terms)
                except Exception as e:
                    print(f"! 重译失败 p{pno}：{e}", file=sys.stderr)
                    continue
                edits.append({"page": pno, "old": dst, "new": fresh, "why": "源文叠字重译"})
            continue

        # ③ 编号丢失：定理/公式/图表引用在译文里没了 → 强制带编号重译
        if a.retranslate and not dup:
            if a.to == "zh":
                # 英→中时「Section 4」本来就该译成「第4节」，要求英文原词出现在中文里
                # 只会制造无意义的改写（同一段能连出 4 条）——只核对数字有没有丢
                num_re = r"\d+(?:[.\-]\d+)*"
                want = set(re.findall(num_re, src))
                got = set(re.findall(num_re, dst))
            else:
                want = numbering_tokens(src)
                got = numbering_tokens(dst)
            missing = sorted(want - got)
            if missing and pno:
                try:
                    fresh = retranslate_keep_numbers(src, a.to, terms, missing)
                except Exception as e:
                    print(f"! 编号重译失败 p{pno}：{e}", file=sys.stderr)
                    continue
                if numbering_tokens(fresh) & set(missing):
                    edits.append({"page": pno, "old": dst, "new": fresh,
                                  "why": "编号丢失重译"})

    out = Path(a.out) if a.out else Path(a.pairs).parent / "edits.csv"

    # ③ 整段没被翻译的（声明页、摘要标题等）：BabelDOC 压根没送它们进去，
    #    所以没有段落对，只能从成品 PDF 里反查，再补译。
    #
    #    ⚠ 只在「中→英」开这条路。英→中是本项目唯一在用的方向，而它不需要：
    #    BabelDOC 会把原文英文留在页面底层（用白块盖住），按文字块反查会把
    #    「藏在下层的英文」全当成漏译，再拿英文去盖中文——成品直接被打回英文。
    if a.translated and a.to != "zh":
        page_filter = None
        if a.pages:
            first, _, last = a.pages.partition("-")
            page_filter = list(range(int(first), int(last or first) + 1))
        missing = find_untranslated(Path(a.translated), page_filter, a.to)
        # 体检报告里点名的残留词（往往是「摘要」这种两三个字的标题，块太小上面抓不到）
        if a.qa_json and Path(a.qa_json).exists():
            try:
                data = json.loads(Path(a.qa_json).read_text(encoding="utf-8"))
                tokens = []
                for it in data.get("items", []):
                    if it.get("kind") == "疑似漏译":
                        # 只收「短词」：这里本来是给「摘要」这种标题补译用的。
                        # 一旦把整句英文当 token 塞进去，会反查到整块正文并把它改坏。
                        tokens += [t.strip() for t in (it.get("detail") or "").split("、")
                                   if t.strip() and len(t.strip()) <= 24 and " " not in t.strip()]
                if tokens:
                    missing += find_blocks_with(Path(a.translated), tokens, page_filter, a.to)
            except Exception as e:
                print(f"! 读体检报告失败：{e}", file=sys.stderr)
        # 同一块别重复排
        seen = set()
        missing = [m for m in missing if not (m["text"] in seen or seen.add(m["text"]))]
        if missing:
            fresh = batch_translate([m["text"] for m in missing], terms, a.to)
            for m, tr in zip(missing, fresh):
                if tr:
                    edits.append({"page": m["page"], "old": m["text"], "new": tr,
                                  "why": "整段未译补译"})

    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["page", "old", "new"], extrasaction="ignore")
        w.writeheader()
        written = 0
        dropped = 0
        dup = 0
        kept_by_page: dict[str, list[tuple[str, str]]] = {}
        for e in edits:
            old, new = str(e.get("old") or ""), str(e.get("new") or "")
            # ① 英→中作业里，「把中文换成英文」的修订一律丢弃（等于把译文改回原文）
            if not safe_edit(old, new, a.to):
                dropped += 1
                continue
            # ② 同一段常被拆成好几条（只差几个空格/标点），同页高度相似的只留一条，
            #    否则 patch_pdf 会把同一段文字叠着写好几遍
            page_key = str(e.get("page"))
            norm_old = re.sub(r"\s+", "", old)
            norm_new = re.sub(r"\s+", "", new)
            kept = kept_by_page.setdefault(page_key, [])
            if any(
                difflib.SequenceMatcher(None, prev_old, norm_old).ratio() > 0.75
                or difflib.SequenceMatcher(None, prev_new, norm_new).ratio() > 0.85
                for prev_old, prev_new in kept
            ):
                dup += 1
                continue
            kept.append((norm_old, norm_new))
            w.writerow(e)
            written += 1
        if dropped:
            print(f"! 丢弃 {dropped} 条「把译文改回原文」的可疑修订", file=sys.stderr)
        if dup:
            print(f"! 丢弃 {dup} 条同一段的重复修订", file=sys.stderr)

    print(f"修订清单 {written} 条 -> {out}")
    for e in edits[:6]:
        print(f"  p{e['page']} [{e['why']}] {str(e['new'])[:50]}")
    return 0


def safe_edit(old: str, new: str, to: str) -> bool:
    """这条修订会不会把译文改回原文？

    英→中作业里，凡是「原文是中文、新文没有中文/中文少一大半」的一律不认——
    那等于拿英文原文盖掉已经译好的中文。这正是成品被整本打回英文的原因。
    """
    if to != "zh":
        return True
    cjk_old = len(re.findall(r"[\u4e00-\u9fff]", old))
    cjk_new = len(re.findall(r"[\u4e00-\u9fff]", new))
    if cjk_old >= 4 and cjk_new + 2 < cjk_old:
        return False
    if len(new) > 15 and cjk_new == 0:
        return False
    return True


def clean_source_span(page_nospace: str, dup_src: str) -> str:
    """在页文本里找到那段被叠字污染的原文，返回干净版本。

    做法：把「奇奇奇」还原成「奇」，再去页文本里按前缀定位，取等长片段。
    """
    if not page_nospace:
        return ""
    uniq = re.sub(r"([\u4e00-\u9fff])\1{2,}", r"\1", dup_src)
    key = re.sub(r"\s+", "", uniq)[:12]
    idx = page_nospace.find(key)
    if idx < 0:
        return ""
    return page_nospace[idx: idx + len(re.sub(r"\s+", "", uniq))]


if __name__ == "__main__":
    raise SystemExit(main())
