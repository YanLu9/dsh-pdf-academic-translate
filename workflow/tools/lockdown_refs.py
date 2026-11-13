r"""参考文献保护 + 小数点修复：把「不该被翻译」的东西还原回去。

为什么要有这一步（实测）：
    英文论文译成中文后，参考文献里的
        DOI 10.3842/SIGMA.2019.034; arXiv:1901.01566
    整段 DOI/arXiv 号消失，引用变得不可验证。
    中文学术期刊的惯例本来就是参考文献保留原语言——所以直接把整条还原成原文。

顺带修一类确定性的排版损伤：
    定理 5。1  ← 小数点被模型换成了中文句号

用法：
    python tools/lockdown_refs.py --pairs out\xxx\pairs.jsonl \
        --translated out\xxx\final2.pdf --source ..\paper.pdf \
        --out out\xxx\edits.refs.csv
    python tools\patch_pdf.py out\xxx\final2.pdf out\xxx\edits.refs.csv -o out\xxx\final3.pdf
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pymupdf

from backcheck import clean

# 参考文献条目：以 [12] 开头，且带年份
REF_ENTRY = re.compile(r"^\s*\[\d{1,3}\]\s*\S")
YEAR = re.compile(r"\b(19|20)\d{2}\b")
# 小数点被换成句号：定理 5。1
BAD_DOT = re.compile(r"(?<=\d)。(?=\d)")


def is_ref_entry(text: str) -> bool:
    """参考文献条目：以 [12] 开头，且带年份。"""
    return bool(REF_ENTRY.match(text)) and bool(YEAR.search(text))


def surname_key(text: str) -> str:
    """取条目里的第一个作者姓，作为源/译两边的配对钥匙（人名一般不译）。"""
    t = re.sub(r"^\s*(\[\d{1,3}\]|\{v\d+\}|\d+\.)\s*", "", text)
    m = re.match(r"([A-Z][A-Za-z\-]{2,})", t)
    return m.group(1).lower() if m else ""


def restore_references(source_pdf: Path, translated_pdf: Path) -> list[dict]:
    """逐页把「参考文献条目」还原成原文。

    为什么按页 + 按姓配对：参考文献列表在源/译两边顺序一致，人名基本不译，
    用它当钥匙比按段落号配对稳。
    """
    src_doc = pymupdf.open(source_pdf)
    tgt_doc = pymupdf.open(translated_pdf)
    edits = []
    for pno in range(min(src_doc.page_count, tgt_doc.page_count)):
        src_blocks = [re.sub(r"\s+", " ", (b[4] or "")).strip() for b in src_doc[pno].get_text("blocks")]
        tgt_blocks = [re.sub(r"\s+", " ", (b[4] or "")).strip() for b in tgt_doc[pno].get_text("blocks")]
        if not src_blocks:
            continue
        for sb in src_blocks:
            if not is_ref_entry(sb):
                continue
            key = surname_key(sb)
            if not key:
                continue
            for tb in tgt_blocks:
                if key in tb.lower() and tb != sb:
                    # 参考文献一律用原文：中文期刊惯例如此，也顺手保住 DOI / arXiv 号
                    edits.append({"page": pno + 1, "old": tb, "new": sb})
                    break
    src_doc.close()
    tgt_doc.close()
    return edits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lockdown_refs")
    p.add_argument("--pairs", default=None, help="段落对；有它就按条目直接还原")
    p.add_argument("--translated", required=True, help="译文 PDF（用来扫小数点问题）")
    p.add_argument("--source", default=None)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)

    edits = []
    restored = fixed_dots = 0

    # ① 参考文献：整条还原成原文（按页 + 作者姓配对，不依赖段落号）
    if a.source and Path(a.source).exists():
        ref_edits = restore_references(Path(a.source), Path(a.translated))
        edits.extend(ref_edits)
        restored = len(ref_edits)

    # ② 小数点：扫译文 PDF 的块，把 digit。digit 改回 digit.digit
    doc = pymupdf.open(Path(a.translated))
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("blocks"):
            text = re.sub(r"[ \t]+", " ", (b[4] or "")).strip()
            if not BAD_DOT.search(text):
                continue
            fixed = BAD_DOT.sub(".", text)
            edits.append({"page": pno + 1, "old": text, "new": fixed})
            fixed_dots += 1
    doc.close()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["page", "old", "new"])
        w.writeheader()
        w.writerows(edits)

    print(f"参考文献还原 {restored} 条，小数点修复 {fixed_dots} 处 -> {out}")
    for e in edits[:5]:
        print(f"  p{e['page'] or '?'} {e['new'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
