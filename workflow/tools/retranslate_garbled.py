r"""把「句子被拆乱」的段落整段重译，再换回 PDF。

为什么需要它（实测）：
    BabelDOC 把段落拆成内联片段分别翻译，模型偶尔会把 [7]、定理 5.1、JH 的顺序挪错，
    产出「JH [7， 的映射 X + H 定理 5。 1], ，」这种读不通的句子。
    逐字修不现实，但整段重译是可行的——只要能从源文完整拿到这一段。

配对办法：
    段落对里的译文带 {vN} 占位符，PDF 里那些位置已经换成了真符号，直接搜原文搜不到。
    所以取译文里「第一个占位符之前的那段字面」，用它去 PDF 块里定位。

用法：
    python tools/retranslate_garbled.py --pairs out\xxx\pairs.jsonl \
        --translated out\xxx\final2.pdf --out out\xxx\edits.garble.csv
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
from make_fixups import retranslate

# 拆乱的典型症状
GARBLE = re.compile(r"[，,]\s*[，,]|\[\d+\s*[,，]\s*的|。\s*\d|\b[A-Z]{2}\s*\[\d+,")
# 重译时要求：引用与公式不许挪位
KEEP = "\n特别注意：引用方括号 [n]、定理/公式编号、变量符号的位置必须与原文一致，不得挪动或调换；" \
       "不要输出连续标点（如「， ，」）；中文句子里的小数点用半角点。"


def key_of(dst: str) -> str:
    """取译文里第一个占位符之前的字面片段，用来在 PDF 里定位。"""
    head = re.split(r"\{v\d+\}|<style", dst)[0]
    head = re.sub(r"\s+", "", head)
    return head[:18]


def cjk_windows(text: str, size: int = 14, limit: int = 6) -> list[str]:
    """从块里切出若干「纯中文字面片段」，用来在段落对里反查。

    块里混着公式符号（占位符被还原后的样子），但中文字面部分和落盘译文完全一致，
    所以拿中文窗口去匹配，比拿整段开头去匹配稳。
    """
    runs = re.findall(r"[\u4e00-\u9fff，。、；：（）]{12,}", text)
    out = []
    for run in runs:
        for i in range(0, max(1, len(run) - size), max(1, size // 2)):
            out.append(run[i:i + size])
            if len(out) >= limit:
                return out
    return out


def find_pair(block: str, pairs: list[dict], used: set[int]):
    for win in cjk_windows(block):
        for i, pr in enumerate(pairs):
            if i in used:
                continue
            if win and win in pr["dst"]:
                return i, pr
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retranslate_garbled")
    p.add_argument("--pairs", required=True)
    p.add_argument("--translated", required=True)
    p.add_argument("--to", default="zh", choices=["zh", "en"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)

    pairs = []
    for line in Path(a.pairs).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        src, _ = clean(obj.get("src") or "")
        dst, _ = clean(obj.get("dst") or "")
        if len(src) > 40 and len(dst) > 20:
            pairs.append({"src": src, "dst": dst, "key": key_of(obj.get("dst") or "")})

    doc = pymupdf.open(Path(a.translated))
    edits = []
    used: set[int] = set()
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("blocks"):
            text = re.sub(r"[ \t]+", " ", (b[4] or "")).strip()
            if not text or not GARBLE.search(text):
                continue
            flat = re.sub(r"\s+", "", text)
            match = find_pair(flat, pairs, used)
            if not match:
                continue
            idx, pr = match
            used.add(idx)
            try:
                fresh = retranslate(pr["src"], a.to, None, KEEP)
            except Exception as e:
                print(f"! 重译失败 p{pno + 1}: {e}", file=sys.stderr)
                continue
            if re.search(r"[\u4e00-\u9fff]", fresh) or a.to == "en":
                edits.append({"page": pno + 1, "old": text, "new": fresh})
            if len(edits) >= a.limit:
                break
    doc.close()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["page", "old", "new"])
        w.writeheader()
        w.writerows(edits)

    print(f"重译 {len(edits)} 段 -> {out}")
    for e in edits[:4]:
        print(f"  p{e['page']} 新: {e['new'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
