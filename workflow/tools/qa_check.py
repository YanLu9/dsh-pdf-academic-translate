"""译后体检：拿源 PDF 和译文 PDF 对一遍，只看「机器能确定错了」的地方。

检查项（全部是规则，不调模型，跑一次几秒）：
  1. 漏译残留——目标中文时，译文里不该有成串的英文；目标英文时不该有成串汉字。
  2. 术语未遵循——主术语表里 source 在源文出现过，target 就必须在译文里出现。
  3. 数字/单位丢失——源文的数值集合（含 °C、m/s、%）与译文比对，缺的列出来。
  4. 公式符号减少——∑ ∫ ± 等在译文里数量骤降，说明公式区被误动过。

用法：
    python tools/qa_check.py --source ../translation_test_en.pdf \
        --translated out/test_en_zh_v3/xxx.mono.pdf \
        --glossary glossary/heat_island.csv --to zh -o out/qa.md
"""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf

# 允许原样保留的东西：数学变量、缩写、单位
ALLOW = {
    "UHI", "PDF", "ISSN", "DOI", "URL", "AI", "Fig", "Table",
    "Turb", "Trur", "UHI", "R2", "N", "T", "P",
}
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
FORMULA_CHARS = "∑∫√±≈≤≥∂∇"
# 日期被拆成「2025year5month15day」这种，是短字段被逐块翻译的典型症状
DATE_GLITCH = re.compile(r"\d{1,4}\s*(?:year|month|day)s?\b", re.I)


def page_texts(pdf: Path) -> list[str]:
    doc = pymupdf.open(pdf)
    texts = [p.get_text() for p in doc]
    doc.close()
    return texts


def numbers(text: str) -> Counter:
    """只比「必须原样活下来」的数：小数（1.2、0.4）和年份/大数（2015、2024）。

    个位数、两位数经常被合法地改写成文字（6—8 月 → June–August），比了只会制造噪声。
    """
    out = Counter()
    for m in NUM_RE.finditer(text):
        # 「7 600」和「7600」是同一个数，只差排版时的千位分隔空
        tok = re.sub(r"[\s\u00a0\u2009\u202f]", "", m.group(0)).rstrip(".,")
        if "." in tok or (tok.isdigit() and len(tok) >= 4):
            out[tok] += 1
    return out


def check_lang_residue(trans_pages: list[str], to: str,
                       page_nos: list[int] | None = None) -> list[dict]:
    """目标中文却留着成串英文 / 目标英文却留着成串汉字。

    单个英文词（Galois、Hessian、LHCb）在中文论文里是合法的，不能当漏译；
    真正的问题是连续三个以上英文单词——那是整句没翻。
    """
    # 只匹配「恰好 3 个词」的写法会让下面的 len(words) >= 4 永远不成立——
    # 结果就是这个检查一直是死的（实测整段英文摘要漏译也没报）。改成匹配 3 个以上。
    run_re = re.compile(r"(?:[A-Za-z][A-Za-z\-]*[\s,]+){3,}")
    STOP = {"the", "of", "and", "is", "are", "was", "were", "for", "with", "that",
            "this", "these", "in", "on", "by", "to", "we", "it", "as", "from"}
    issues = []
    for i, text in enumerate(trans_pages, 1):
        # 只检查了部分页时，列表下标和真实页码对不上（之前报的 p11 其实是 p14）
        pno = page_nos[i - 1] if page_nos and i - 1 < len(page_nos) else i
        if to == "zh":
            bad = []
            for m in run_re.finditer(text):
                run = m.group(0).strip()
                words = re.findall(r"[A-Za-z\-]+", run)
                # 作品名/机构名（A Disordered Universe、La Ricotta Summer）保持英文是对的，
                # 真正的漏译是带功能词的整句（Existing studies mostly show…）
                if len(words) >= 4 and sum(w.lower() in STOP for w in words) >= 2:
                    bad.append(run[:40])
        else:
            bad = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        if bad:
            issues.append({
                "page": pno,
                "kind": "疑似漏译",
                "detail": "、".join(sorted(set(bad))[:6]),
                "count": len(bad),
            })
    return issues


def check_glossary(master: Path, src_text: str, trans_text: str, to: str = "zh") -> list[dict]:
    """按方向查：中译时看英文源词该出现的译名；英译时看中文源词该出现的英文译名。"""
    rows = list(csv.DictReader(master.open(encoding="utf-8-sig", newline="")))
    issues = []
    for row in rows:
        en, zh = (row.get("en") or "").strip(), (row.get("zh") or "").strip()
        if not en or not zh:
            continue
        # 候选词（自举产物）只作参考，不能当成"必须出现"的硬指标
        if (row.get("status") or "").strip().lower() == "candidate":
            continue
        if to == "zh":
            if en.lower() in src_text.lower() and zh not in trans_text:
                issues.append({"kind": "术语未遵循", "detail": f"{en} → 应为「{zh}」，译文里没出现"})
        else:
            if zh in src_text and en.split()[0].lower() not in trans_text.lower():
                issues.append({"kind": "术语未遵循", "detail": f"{zh} → 应为「{en}」，译文里没出现"})
    return issues


def unit_variants(src_text: str, tok: str) -> set[str]:
    """「3.6 billion → 36 亿」「1.28 million → 128 万」是合法的单位换算，不是丢数据。

    1 billion = 10 亿，1 million = 100 万，1 trillion = 1000 亿，1 thousand = 1 千。
    返回译文里可能出现的等价数字写法（只留数字部分，单位是中文字符，flat 里本来就没有）。
    """
    factor = {"billion": 10, "million": 100, "trillion": 1000, "thousand": 1}
    out: set[str] = set()
    try:
        base = float(tok)
    except ValueError:
        return out
    # 排版会在行尾把词断开：bil-\nlion → 先去掉连字符换行再匹配
    norm = re.sub(r"-\s*\n\s*", "", src_text)
    # 单位前面可能是一串用斜杠连起来的数：15.3/19.6 billion FLOPs
    for m in re.finditer(r"((?:\d+(?:\.\d+)?\s*/\s*)*\d+(?:\.\d+)?)\s*"
                         r"(billion|million|trillion|thousand)", norm, re.I):
        if tok in [n.strip() for n in m.group(1).split("/")]:
            out.add(f"{base * factor[m.group(2).lower()]:g}")
    return out


def check_numbers(src_text: str, trans_text: str) -> list[dict]:
    want, got = numbers(src_text), numbers(trans_text)
    flat = re.sub(r"[\s\u00a0\u2009\u202f]", "", trans_text)  # 只看数字本身，「7 600」也能对上
    issues = []
    for tok, n in want.items():
        if got.get(tok, 0) >= n:
            continue
        if flat.count(tok) >= n:  # 排版加了千位空格，数据没丢
            continue
        # 单位换算（3.6 billion → 36 亿）不是丢数据——实测 ResNet 因此报了 5 条假「必须修」
        if got.get(tok, 0) + sum(flat.count(v) for v in unit_variants(src_text, tok)) >= n:
            continue
        # 年份常被改写成「20 世纪 90 年代」「上世纪七十年代」，这不是丢数据
        if re.fullmatch(r"(19|20)\d\d", tok):
            decade = tok[-2:]
            cn = {"70": "七十", "80": "八十", "90": "九十", "00": "零"}.get(decade, "")
            if f"{decade}年代" in flat or (cn and f"{cn}年代" in flat):
                continue
        issues.append({"kind": "数字/单位缺失",
                       "detail": f"{tok}（源文 {n} 次，译文 {got.get(tok, 0)} 次）"})
    return issues


def check_date_glitch(trans_text: str) -> list[dict]:
    """日期/编号这类短字段被拆碎后逐块翻译，会出现 2025year5month15day。"""
    issues = []
    for hit in DATE_GLITCH.findall(trans_text)[:5]:
        issues.append({"kind": "日期被拆碎", "detail": f"「{hit}」应合并成正常日期"})
    return issues


def check_formula(src_text: str, trans_text: str) -> list[dict]:
    issues = []
    for ch in FORMULA_CHARS:
        a, b = src_text.count(ch), trans_text.count(ch)
        if a and b < a * 0.5:
            issues.append({"kind": "公式符号减少", "detail": f"{ch}: 源文 {a} 次 → 译文 {b} 次"})
    return issues


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qa_check")
    p.add_argument("--source", required=True)
    p.add_argument("--translated", required=True)
    p.add_argument("--glossary", default="glossary/heat_island.csv")
    p.add_argument("--to", default="zh", choices=["zh", "en"])
    p.add_argument("--pages", default=None, help="只检查这些页，如 1-6；默认自动识别被翻译过的页")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--json-summary", default=None,
                   help="把计数写成 JSON（插件/流水线读这个，不用解析 md）")
    a = p.parse_args(argv)

    src_pages = page_texts(Path(a.source))
    trans_pages = page_texts(Path(a.translated))

    # 只检查「真的被翻译过」的页：整篇对比时，未翻的原语言页会制造几百条假漏译
    def sig(text: str) -> str:
        return re.sub(r"\s+", "", text)[:400]

    checked = [i for i in range(min(len(src_pages), len(trans_pages)))
               if sig(src_pages[i]) != sig(trans_pages[i])]
    if a.pages:
        wanted = []
        for part in a.pages.split(","):
            first, _, last = part.partition("-")
            wanted += list(range(int(first) - 1, int(last or first)))
        checked = [i for i in checked if i in wanted]
    if not checked:
        checked = list(range(min(len(src_pages), len(trans_pages), 3)))
        print("! 没检测到被翻译的页（源与译文文本一致），退化为检查前 3 页", file=sys.stderr)

    def norm_num(t: str) -> str:
        t = unicodedata.normalize("NFKC", t)
        # 「定理 5。1」是小数点被换成了句号，数值没丢，先归一化再比
        return re.sub(r"(?<=\d)。(?=\d)", ".", t)

    src_text = norm_num("\n".join(src_pages[i] for i in checked))
    trans_text = norm_num("\n".join(trans_pages[i] for i in checked))

    # 给每条问题标上是哪一页出的：用户要按页改，没页码等于没法改
    def pages_with(token: str, pages: list[str], limit: int = 3) -> str:
        hits = []
        for i in checked:
            if token and token in norm_num(pages[i]):
                hits.append(i + 1)
                if len(hits) >= limit:
                    break
        return ",".join(str(x) for x in hits)

    gloss_path = Path(a.glossary)
    gloss = check_glossary(gloss_path, src_text, trans_text, a.to) if gloss_path.exists() else []
    nums = check_numbers(src_text, trans_text)
    for it in nums:
        tok = re.sub(r"（.*", "", it["detail"])
        it["page"] = pages_with(tok.strip(), src_pages)
    for it in gloss:
        term = it["detail"].split("→")[0].strip()
        it["page"] = pages_with(term, src_pages)
    must = nums + gloss
    checked_trans = [trans_pages[i] for i in checked]
    maybe = (check_lang_residue(checked_trans, a.to, [c + 1 for c in checked])
             + check_formula(src_text, trans_text)
             + check_date_glitch(trans_text))

    lines = [f"# 译后体检：{Path(a.translated).name}", ""]
    lines.extend([
        f"- 源文档：{Path(a.source).name}（{len(src_pages)} 页，{len(src_text)} 字符）",
        f"- 译文：{len(trans_pages)} 页，{len(trans_text)} 字符",
        f"- 本次检查的页：{len(checked)} 页（{checked[0] + 1 if checked else 0}–{checked[-1] + 1 if checked else 0}）",
        "",
    ])

    def dump(title: str, items: list[dict], level: str) -> None:
        lines.append(f"## {title}（{len(items)}）")
        if not items:
            lines.extend(["- 无", ""])
            return
        for it in items[:20]:
            where = f"p{it['page']} " if it.get("page") else ""
            lines.append(f"- {level} {where}{it['kind']}：{it['detail']}")
        if len(items) > 20:
            lines.append(f"- …另有 {len(items) - 20} 条")
        lines.append("")

    dump("必须修", must, "🔴")
    dump("建议看", maybe, "🟡")

    report = "\n".join(lines)
    out = Path(a.out) if a.out else Path(a.translated).with_suffix(".qa.md")
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n-> {out}")

    if a.json_summary:
        import json
        summary = {
            "source": str(Path(a.source).resolve()),
            "translated": str(Path(a.translated).resolve()),
            "report": str(out.resolve()),
            "pages": len(trans_pages),
            "checked_pages": [i + 1 for i in checked],
            "must_fix": len(must),
            "maybe": len(maybe),
            "by_kind": dict(Counter(x["kind"] for x in must + maybe)),
            "items": [
                {"level": lvl, **it}
                for lvl, items in (("must", must), ("maybe", maybe))
                for it in items
            ][:60],
        }
        Path(a.json_summary).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
