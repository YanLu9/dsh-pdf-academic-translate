r"""对一批译文做体检：有没有整页/整段没译、有没有叠字、中英混排有多严重。

不调用模型，纯规则，跑一次几秒。用来回答「这次翻得怎么样」，而不是「翻得对不对」。

用法：
    python tools/eval_quality.py --source 原文.pdf --translated 译文.pdf
    python tools/eval_quality.py --source a.pdf --translated b.pdf --json out.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path

import pymupdf

CJK = re.compile(r"[\u4e00-\u9fff]")
WORD = re.compile(r"[A-Za-z]{4,}")

# 图注/表注常保留英文（BabelDOC 对 caption 的处理不稳），单独统计不算「没译」
CAPTION = re.compile(r"^\s*(Table|Figure|Fig\.|Algorithm|Listing)\s*\d", re.I)


def page_stats(page) -> tuple[int, int]:
    text = page.get_text()
    return len(CJK.findall(text)), len(re.findall(r"[A-Za-z]", text))


def untranslated_blocks(page, min_latin: int = 120) -> list[str]:
    """整段还是英文的块（>=120 个字母、没有汉字、像句子）。"""
    hits = []
    for b in page.get_text("blocks"):
        text = re.sub(r"\s+", " ", (b[4] or "")).strip()
        if CAPTION.match(text):
            continue
        cjk = len(CJK.findall(text))
        latin = len(re.findall(r"[A-Za-z]", text))
        words = re.findall(r"[A-Za-z]{2,}", text)
        lower = sum(1 for w in words if w.islower())
        if cjk == 0 and latin >= min_latin and len(words) >= 12 and lower >= 8:
            hits.append(text[:80])
    return hits


def duplicate_lines(page) -> list[str]:
    """同一位置叠了两行字——叠字。

    只比文字会误伤表格（VGG 那种连续相同的卷积行本来就重复），
    所以要求两行的包围盒横向、纵向都重合一半以上。已用一份确认叠字的样张验证过：
    坏稿每页都能抓出 1-7 处，干净稿为 0。
    """
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            text = "".join(sp["text"] for sp in ln["spans"]).strip()
            if len(text) >= 6:
                lines.append((text, pymupdf.Rect(ln["bbox"])))
    out = []
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, ra = lines[i]
            b, rb = lines[j]
            inter = ra & rb
            if inter.is_empty:
                continue
            if (inter.height / max(1e-6, min(ra.height, rb.height)) > 0.5
                    and inter.width / max(1e-6, min(ra.width, rb.width)) > 0.5):
                if difflib.SequenceMatcher(None, a, b).ratio() > 0.6:
                    out.append(a[:40])
                    break
    return out


def mixed_terms(page) -> list[str]:
    """中文段落里夹着的英文词（术语没译或译名混用）。"""
    hits = []
    for b in page.get_text("blocks"):
        text = re.sub(r"\s+", " ", (b[4] or "")).strip()
        if len(CJK.findall(text)) < 40:
            continue
        for w in WORD.findall(text):
            low = w.lower()
            if low in {"http", "https", "https", "html", "softmax", "gpu", "cpu", "transformer"}:
                continue
            hits.append(w)
    return hits


def reference_pages(pdf) -> set[int]:
    """直接复用流水线的参考文献判定，避免体检口径和实际跳过页对不上。"""
    from translate import reference_pages as _ref

    return set(_ref(Path(pdf)))


def evaluate(source: Path, translated: Path) -> dict:
    src = pymupdf.open(source)
    dst = pymupdf.open(translated)
    refs = reference_pages(source)
    pages = []
    total_untr = 0
    total_dup = 0
    terms: dict[str, int] = {}
    for i in range(dst.page_count):
        page = dst[i]
        cjk, latin = page_stats(page)
        untr = untranslated_blocks(page) if (i + 1) not in refs else []
        dup = duplicate_lines(page) if (i + 1) not in refs else []
        mixed = mixed_terms(page) if (i + 1) not in refs else []
        for t in mixed:
            terms[t] = terms.get(t, 0) + 1
        total_untr += len(untr)
        total_dup += len(dup)
        pages.append({
            "page": i + 1, "cjk": cjk, "latin": latin,
            "untranslated": untr, "duplicates": dup, "mixed": len(mixed),
        })
    src.close()
    dst.close()
    untrans_pages = [p["page"] for p in pages
                     if p["page"] not in refs and p["latin"] > 300 and p["cjk"] < p["latin"] * 0.05]
    return {
        "source": str(source), "translated": str(translated),
        "pages": len(pages),
        "reference_pages": sorted(refs),
        "untranslated_pages": untrans_pages,
        "untranslated_blocks": total_untr,
        "duplicate_pages": sorted({p["page"] for p in pages if p["duplicates"]}),
        "duplicate_lines": total_dup,
        "mixed_terms_top": sorted(terms.items(), key=lambda kv: -kv[1])[:10],
        "detail": pages,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval_quality")
    p.add_argument("--source", required=True)
    p.add_argument("--translated", required=True)
    p.add_argument("--json", default=None)
    a = p.parse_args(argv)

    r = evaluate(Path(a.source), Path(a.translated))
    print(f"## {Path(a.translated).name}")
    print(f"- 页数：{r['pages']}（其中参考文献页 {len(r['reference_pages'])} 页，按规则保留原文）")
    print(f"- 疑似整页未译：{len(r['untranslated_pages'])} 页 {r['untranslated_pages']}")
    print(f"- 整段未译的块：{r['untranslated_blocks']} 处")
    print(f"- 叠字页：{len(r['duplicate_pages'])} 页 {r['duplicate_pages']}，疑似 {r['duplicate_lines']} 处")
    top = "、".join(f"{w}×{n}" for w, n in r["mixed_terms_top"][:6]) or "无"
    print(f"- 中英混排高频词：{top}")
    if a.json:
        Path(a.json).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"- 明细：{a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
