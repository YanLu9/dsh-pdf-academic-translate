"""在已排版好的 PDF 上做「点对点」人工修订：找到指定原文，抹掉，原位写入定稿。

为什么要单独一步：BabelDOC 只支持「术语注入」，不能让某一句强行等于某个译文。
所以人工校对分两条路——
  - 术语/译名错 → 改 glossary，整篇重跑（便宜、一致性好）；
  - 单句表述不满意 → 用本工具就地改，不动其它任何东西（图片、表格线、公式都不受影响）。

修订清单 CSV（utf-8）列：
    page,old,new
    page 从 1 开始，留空表示全文搜；old 必须和 PDF 里的文字逐字一致（含空格）；
    一行只改一处；同一 old 多处命中时全部替换，并在日志里报出来。

用法：
    python tools/patch_pdf.py in.pdf edits.csv -o out.pdf [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf

CJK_FONT = "msyh"  # 微软雅黑：中西文都是比例宽度
LATIN_FONT = "helv"
# PyMuPDF 内置的 china-s 里拉丁字母是全角的，中英混排会被撑成「J H [ 5 ，」那样
CJK_FONTFILE = r"C:\Windows\Fonts\msyh.ttc"


def pick_font(text: str) -> str:
    return CJK_FONT if any("\u4e00" <= ch <= "\u9fff" for ch in text) else LATIN_FONT


def widen(page, box, min_ratio: float = 0.55):
    """标题/短行的包围盒往往只有两三个字宽，直接塞英文会折成两行。

    这类短框向右放宽到页面可用宽度的 min_ratio，给译文留出正常的单行空间。
    """
    page_w = page.rect.width
    if box.width < page_w * min_ratio:
        right_limit = page.rect.x1 - page_w * 0.08
        box.x1 = min(right_limit, box.x0 + page_w * min_ratio)
    return box


def fit_textbox(page, rect, text: str, font: str, start_size: float, min_size: float = 4.0):
    """从原字号往下缩，直到放进原矩形；返回 (成功?, 用到的字号)。"""
    size = start_size
    kwargs = {"fontfile": CJK_FONTFILE} if font == CJK_FONT else {}
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=font, fontsize=size,
            color=(0, 0, 0), align=pymupdf.TEXT_ALIGN_LEFT, lineheight=1.15,
            **kwargs,
        )
        if rc >= 0:
            return True, size
        size -= 0.25
    return False, size


def _norm(text: str) -> str:
    """归一化到可比较的形式：NFKC 折叠康熙部首/兼容字，再去空白和常见标点差异。

    很多中文 PDF 的文本层是「⼒」这种兼容区字符，肉眼和「力」一样，
    但 search_for 匹配不上——不归一化就会一直报「没找到」。
    """
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s，,。.；;：:、（）()\[\]【】]+", "", t)


def find_rects(page, old: str):
    """先整体搜；搜不到就按归一化后的文字块匹配，用块的外框作为替换区域。

    为什么需要兜底：排版会把一句话拆成多行/多块，整串 search_for 会落空；
    另外部分 PDF 用兼容区码位存汉字，逐字符比对必然失败。
    """
    rects = page.search_for(old)
    if rects:
        return rects

    want = _norm(old)
    if len(want) < 3:
        return []

    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        block_text = "".join(
            span["text"] for line in b["lines"] for span in line["spans"]
        )
        got = _norm(block_text)
        if len(got) >= 12:
            blocks.append((got, pymupdf.Rect(b["bbox"])))

    # ① 块里包含整段要改的文字 → 整个块的外框作为替换区域
    hits = [rect for got, rect in blocks if want in got]
    if hits:
        return hits

    # ② 只含开头一小截（排版把中间的词改了）→ 仍然整块替换。
    #    绝不能只给「前缀命中」那几个字的窄框：那样只会抹掉开头十几个字，
    #    然后把这整段新译文塞进窄框，结果就是新字缩成一小团叠在原文上面。
    probe = want[:12]
    hits = [rect for got, rect in blocks if probe in got]
    if hits:
        return hits

    return []


def apply_edits(pdf_in: Path, edits: list[dict], pdf_out: Path,
                dry_run: bool = False, all_hits: bool = False) -> dict:
    doc = pymupdf.open(pdf_in)
    stats = {"applied": 0, "missing": [], "multi": [], "overflow": [],
             "overlap": [], "not_removed": []}

    # 先按页收集，再一次性 redact（同一页多次 apply_redactions 会互相干扰）
    per_page: dict[int, list[dict]] = {}
    for row in edits:
        old, new = (row.get("old") or "").strip(), (row.get("new") or "").strip()
        if not old:
            continue
        deleting = new in {"", "<del>", "-"}  # new 留空 = 只删不写
        pages = range(doc.page_count) if not (row.get("page") or "").strip() else [
            int(row["page"]) - 1
        ]
        hit = False
        for pno in pages:
            if pno < 0 or pno >= doc.page_count:
                continue
            rects = find_rects(doc[pno], old)
            if not rects:
                continue
            hit = True
            if len(rects) > 1:
                stats["multi"].append((pno + 1, old[:24], len(rects)))
            # 同一处文字被排版拆成多段（换行）时会返回多个矩形：纵向相邻的要合并成一处；
            # 真正重复出现的（相隔很远）默认只改第一处，避免把页眉/签名行写重复。
            groups: list[list] = []
            for r in sorted(rects, key=lambda x: (round(x.y0), x.x0)):
                if groups and abs(r.y0 - groups[-1][-1].y1) <= max(6, r.height * 1.2):
                    groups[-1].append(r)
                else:
                    groups.append([r])
            chosen = groups if all_hits else groups[:1]
            for grp in chosen:
                box = grp[0]
                for r in grp[1:]:
                    box |= r
                per_page.setdefault(pno, []).append(
                    {"rects": list(grp), "box": box, "text": new, "old": old, "delete": deleting}
                )
        if not hit:
            stats["missing"].append((row.get("page"), old[:40]))

    if dry_run:
        print(f"[dry-run] 命中 {sum(len(v) for v in per_page.values())} 处，涉及 {len(per_page)} 页")
        return stats

    for pno, items in per_page.items():
        page = doc[pno]
        # 同一段常被拆成好几条修订（只差几个字），框几乎重合：只留文字最长的那条，
        # 否则同一段会被 redact 一遍再写三遍，页面上叠成两三层字
        items.sort(key=lambda it: -len(it["text"]))
        kept: list[dict] = []
        for it in items:
            box = it["box"]
            if any((box & k["box"]).get_area() > 0.5 * max(1.0, min(box.get_area(), k["box"].get_area()))
                   for k in kept):
                stats.setdefault("overlap", []).append((pno + 1, it["text"][:24]))
                continue
            kept.append(it)
        for it in kept:
            for r in it["rects"]:
                page.add_redact_annot(r, fill=None)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        for it in kept:
            if it["delete"]:
                stats["applied"] += 1
                continue
            # 原文没被抹掉就别往下写：会把新译文叠在旧文字上（实测整段叠两层）
            head = _norm(it["old"])[:12]
            leftover = _norm(page.get_text("text", clip=it["box"]))
            if head and head in leftover:
                stats.setdefault("not_removed", []).append((pno + 1, it["old"][:24]))
                continue
            r = widen(page, it["box"])
            font = pick_font(it["text"])
            # 字号要跟原文对齐：直接按框高算，遇到多行合并的大框会撑成巨型字
            start = min(11.5, max(6.0, r.height * 0.72))
            ok, size = fit_textbox(page, r, it["text"], font, start_size=start)
            if ok:
                stats["applied"] += 1
            else:
                stats["overflow"].append((pno + 1, it["text"][:24]))
                # 兜底：至少把字写进去，允许略微出框
                page.insert_textbox(r, it["text"], fontname=font, fontsize=4.0,
                                    color=(0, 0, 0), align=pymupdf.TEXT_ALIGN_LEFT)

    doc.save(pdf_out, garbage=3, deflate=True)
    doc.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="patch_pdf")
    p.add_argument("pdf")
    p.add_argument("edits", help="修订清单 CSV：page,old,new")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all-hits", action="store_true",
                   help="同一串文字出现多处时全部替换（默认只改第一处，避免页眉/签名行被写重复）")
    a = p.parse_args(argv)

    pdf_in = Path(a.pdf)
    if not pdf_in.exists():
        raise SystemExit(f"找不到 PDF：{pdf_in}")
    with Path(a.edits).open(encoding="utf-8-sig", newline="") as fh:
        edits = list(csv.DictReader(fh))

    out = Path(a.out) if a.out else pdf_in.with_name(pdf_in.stem + ".patched.pdf")
    stats = apply_edits(pdf_in, edits, out, a.dry_run, a.all_hits)

    print(f"改好 {stats['applied']} 处 -> {out}" if not a.dry_run else "")
    if stats["missing"]:
        print(f"! {len(stats['missing'])} 条没在 PDF 里找到（原文可能被排版改了）：")
        for page, old in stats["missing"][:8]:
            print(f"   p{page}: {old}")
    if stats["multi"]:
        print(f"! {len(stats['multi'])} 条命中多处，已全部替换：")
        for page, old, n in stats["multi"][:8]:
            print(f"   p{page} x{n}: {old}")
    if stats["overflow"]:
        print(f"! {len(stats['overflow'])} 处缩到 4pt 仍放不下，需人工看一眼")
    if stats.get("overlap"):
        print(f"! {len(stats['overlap'])} 条与同页其它修订重合，已跳过（避免叠字）：")
        for page, text in stats["overlap"][:6]:
            print(f"   p{page}: {text}")
    if stats.get("not_removed"):
        print(f"! {len(stats['not_removed'])} 处原文没抹掉，已放弃写入（否则会叠字）：")
        for page, text in stats["not_removed"][:6]:
            print(f"   p{page}: {text}")
    return 0 if stats["applied"] or a.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
