r"""回译校验：拿「成对的源段/译文」做体检，让只懂大学英语的人也能判对错。

数据从哪来：
    BabelDOC 自己不做段落对的导出，所以由 tools/babeldoc_runner.py 挂钩子写成
    pairs.jsonl（每行 {src, dst}）。这比从 PDF 反推块靠谱得多——实测按块对齐会错位。

一份报告回答三件事：
    1. 哪些段「方向都反了」——英文页被翻成中文，交付级硬伤；
    2. 哪些段「源文本身就坏了」——叠字、乱码，模型看到的就是错的；
    3. 哪些段值得人看——长句/术语密集/含公式/数字多，按风险降序，只看前 N 段。

用法：
    python tools/backcheck.py --pairs out\pairs_p6\pairs.jsonl \
        --source ..\论文.pdf --to en --glossary glossary\physics.exotic-hadrons.csv --top 8

产物：
    review.md   人读：按风险降序，每段 原文 / 译文 / 回译 / 标记
    review.csv  同上，机器读
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

from run_babeldoc import api_key

MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com/v1/chat/completions"

CJK = re.compile(r"[\u4e00-\u9fff]")
NUM = re.compile(r"\d+(?:\.\d+)?")
DUP_CJK = re.compile(r"([\u4e00-\u9fff])\1{2,}")
STYLE_TAG = re.compile(r"<style[^>]*>|</style>")
PLACEHOLDER = re.compile(r"\{v\d+\}")
PUNCT_MAP = str.maketrans({"，": ",", "。": ".", "；": ";", "：": ":", "（": "(",
                           "）": ")", "、": ",", "　": " "})
PROMPT_NOISE = ("You are a professional", "You are an expert", "You are a professional,",
                "Translate the following", ";;")
NEG_ZH = ("不", "无", "未", "没有", "非", "否")


def norm_for_match(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").translate(PUNCT_MAP))


def clean(text: str) -> tuple[str, bool]:
    """去掉 BabelDOC 的富文本标签和占位符；返回 (清洗后文本, 是否含占位符)。

    钩子抓的是「占位符还没还原」的中间状态，所以像 −2.36 里的负号会变成 {v7}。
    这些段不能按数字比对（会一片假警报），单独标出来让人看图。
    """
    had_ph = bool(PLACEHOLDER.search(text))
    out = STYLE_TAG.sub("", text)
    out = PLACEHOLDER.sub("", out)
    return re.sub(r"\s+", " ", out).strip(), had_ph


def page_index(source: Path) -> list[tuple[int, str]]:
    doc = pymupdf.open(source)
    out = []
    for i, p in enumerate(doc):
        out.append((i + 1, norm_for_match(p.get_text())))
    doc.close()
    return out


def locate(src: str, pages: list[tuple[int, str]]) -> int | None:
    key = norm_for_match(src)[:24]
    if not key:
        return None
    for pno, text in pages:
        if key in text:
            return pno
    return None


def lang_of(text: str) -> str:
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return "en"
    if latin < cjk * 0.15:
        return "zh"
    return "mixed"


def risk(en: str, src: str, glossary: dict[str, str]) -> tuple[float, list[str]]:
    words = en.split()
    sents = [s for s in re.split(r"[.!?]\s", en) if s.strip()]
    avg = len(words) / max(1, len(sents))
    hits = sum(1 for t in glossary if t.lower() in en.lower())
    score, tags = 0.0, []
    if len(words) > 40:
        score += 2
        tags.append(f"长段{len(words)}词")
    if avg > 30:
        score += 2
        tags.append(f"长句均{avg:.0f}词")
    if hits >= 3:
        score += 1.5
        tags.append(f"术语密集{hits}")
    elif hits:
        score += 0.5
    if re.search(r"[=∑∫±≈≤≥]", en) or re.search(r"[=∑∫±≈≤≥]", src):
        score += 1
        tags.append("含公式")
    if len(NUM.findall(src)) >= 3:
        score += 0.5
        tags.append("数字多")
    return round(score, 2), tags


def hard_flags(src: str, dst: str, to: str, glossary: dict[str, str],
               had_placeholder: bool = False) -> list[str]:
    flags = []
    sl, dl = lang_of(src), lang_of(dst)
    if to == "en" and sl == "en" and dl == "zh":
        flags.append("方向反了：英文段被译成中文")
    if to == "zh" and sl == "zh" and dl == "en":
        flags.append("方向反了：中文段被译成英文")
    if DUP_CJK.search(src):
        flags.append("源文叠字（" + DUP_CJK.search(src).group(0)[:6] + "…）")
    # 占位符只做提示，不算硬伤：物理/数学段落几乎段段都有，算成硬伤会把真正的错淹掉
    if not had_placeholder:
        src_nums, dst_nums = set(NUM.findall(src)), set(NUM.findall(dst))
        if src_nums - dst_nums:
            flags.append("数字缺失:" + ",".join(sorted(src_nums - dst_nums)[:3]))
    for en_term, zh in glossary.items():
        if to == "en" and zh and zh in src:
            # 中文源提到某术语，英文译文里应出现对应英文
            if en_term.split()[0].lower() not in dst.lower():
                flags.append(f"术语未出现:{zh}")
    if len(dst.strip()) < max(3, len(src.strip()) * 0.15) and len(src) > 20:
        flags.append("译文异常短")
    return flags


def back_translate(items: list[str], to_zh: bool = True) -> list[str]:
    if not items:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
    prompt = ("把下面每段翻译成简体中文（原文是英文则回译，是中文则原样返回），"
              "忠实原句结构，不要润色，不要合并，不要解释。严格输出 JSON 数组，"
              "元素顺序与输入一致，只含译文字符串。\n\n" + numbered)
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
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    parsed = json.loads(m.group(0) if m else content)
    return [str(x) for x in parsed][: len(items)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backcheck")
    p.add_argument("--pairs", required=True, help="babeldoc_runner 落的 pairs.jsonl")
    p.add_argument("--source", default=None, help="源 PDF，用于定位页码（可选）")
    p.add_argument("--to", default="en", choices=["zh", "en"])
    p.add_argument("--glossary", default="glossary/physics.exotic-hadrons.csv")
    p.add_argument("--top", type=int, default=8, help="回译并展示多少段")
    p.add_argument("--out-dir", default=None)
    a = p.parse_args(argv)

    glossary: dict[str, str] = {}
    gp = Path(a.glossary)
    if gp.exists():
        with gp.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                en, zh = (row.get("en") or "").strip(), (row.get("zh") or "").strip()
                if en and zh:
                    glossary[en] = zh

    pairs = []
    for line in Path(a.pairs).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        src, dst = (obj.get("src") or "").strip(), (obj.get("dst") or "").strip()
        if len(src) < 6 or len(dst) < 2:
            continue
        if any(src.startswith(n) for n in PROMPT_NOISE):
            continue
        src_c, _ = clean(src)
        dst_c, had_ph = clean(dst)
        if len(src_c) < 6:
            continue
        pairs.append({"src": src_c, "dst": dst_c, "had_ph": had_ph})

    pages = page_index(Path(a.source)) if a.source and Path(a.source).exists() else []
    for it in pairs:
        it["page"] = locate(it["src"], pages) if pages else None
        it["flags"] = hard_flags(it["src"], it["dst"], a.to, glossary, it.get("had_ph", False))
        it["risk"], it["tags"] = risk(it["dst"], it["src"], glossary)
        if it.get("had_ph"):
            it["tags"].append("含公式占位符")
        # 硬伤直接排到最前
        it["sort"] = it["risk"] + (10 if it["flags"] else 0)
        it["back"] = ""

    pairs.sort(key=lambda x: -x["sort"])
    chosen = [x for x in pairs if not any(f.startswith("源文叠字") for f in x["flags"])][: a.top]
    print(f"段落对 {len(pairs)}，其中硬伤 {sum(1 for x in pairs if x['flags'])} 段；回译 {len(chosen)} 段…",
          file=sys.stderr)
    for start in range(0, len(chosen), 6):
        chunk = chosen[start:start + 6]
        try:
            backs = back_translate([c["dst"] for c in chunk])
        except Exception as e:
            print(f"! 回译失败：{e}", file=sys.stderr)
            backs = ["" for _ in chunk]
        for c, b in zip(chunk, backs):
            c["back"] = b
            if b and any(w in c["src"] for w in NEG_ZH) != any(w in b for w in NEG_ZH):
                c["flags"].append("否定词不对应")

    out_dir = Path(a.out_dir) if a.out_dir else Path(a.pairs).parent
    out_md, out_csv = out_dir / "review.md", out_dir / "review.csv"

    with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["page", "risk", "tags", "flags", "source", "translated", "back"])
        for it in pairs:
            w.writerow([it["page"] or "", it["risk"], ";".join(it["tags"]),
                        ";".join(it["flags"]), it["src"], it["dst"], it["back"]])

    lines = ["# 待看清单（按风险降序，前几段最值得看）", ""]
    hard = [x for x in pairs if x["flags"]][:10]
    if hard:
        lines += ["## 先说硬伤（不用判断英文，看标记即可）", ""]
        for it in hard:
            lines.append(f"- p{it['page'] or '?'} 「{'；'.join(it['flags'])}」")
            lines.append(f"  - 源：{it['src'][:90]}")
            lines.append(f"  - 译：{it['dst'][:90]}")
        lines.append("")

    lines += ["## 需要你判断的段落（读中文：原文 vs 回译，意思一样吗？）", ""]
    for it in chosen:
        lines.append(f"### p{it['page'] or '?'} 风险 {it['risk']}  {' '.join(it['tags'])}")
        lines.append(f"- **原文**：{it['src'][:200]}")
        lines.append(f"- **译文**：{it['dst'][:240]}")
        lines.append(f"- **回译**：{it['back'][:200] or '（未回译）'}")
        lines.append(f"- **机器标记**：{'、'.join(it['flags']) or '无明显偏差'}")
        lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"-> {out_md}\n-> {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
