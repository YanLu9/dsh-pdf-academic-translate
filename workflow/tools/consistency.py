r"""文档内术语一致性：不建词库，只问「同一个中文术语，在译文里是不是始终同一个英文说法」。

为什么这个检查值得做：
    专业读者最先崩的地方不是错译，是同一个概念一会儿叫 A 一会儿叫 B。
    而它不需要任何外部词库——用文档自己的段落对就能查出来。

做法：
    1. 从段落对里挑出高频中文术语候选（也可用 BabelDOC 自动抽的术语表当候选源）；
    2. 找出所有含该术语的源段落，连译文一起交给模型；
    3. 模型逐段回报「这个术语在这段译文里写成了什么」；
    4. 归一化后统计写法种数，>=2 种就是不一致。

用法：
    python tools/consistency.py --pairs audit\01_zh_xxx\pairs.jsonl --top 4
    python tools/consistency.py --dirs audit --top 3 --out audit\consistency.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from run_babeldoc import api_key

BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
CJK_TERM = re.compile(r"[\u4e00-\u9fff]{2,10}")


def load_pairs(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        src, dst = (o.get("src") or "").strip(), (o.get("dst") or "").strip()
        if len(src) >= 8 and len(dst) >= 8:
            out.append({"src": src, "dst": dst})
    return out


STOP_TERMS = {
    "基于", "式中", "以及", "其中", "分别", "可以", "进行", "通过", "对于", "由于",
    "因此", "但是", "并且", "我们", "本文", "研究", "方法", "结果", "问题", "方面",
    "强调", "模式", "提纲", "斯选集", "关于", "一种", "具有", "成为", "出现",
    "所示", "上述", "如下", "这些", "那些", "之间", "之后", "之前", "同时", "此外",
}


def candidate_terms(pairs: list[dict], glossary: Path | None, top: int) -> list[str]:
    """术语候选优先取 BabelDOC 从这份文档里抽出的术语（文档自身的学习结果），
    没有才退回 n-gram。n-gram 会把「循环的燃煤发电热力系」这种长片段也当术语，噪声太大。
    """
    counts: Counter = Counter()
    from_glossary: list[str] = []
    if glossary and glossary.exists():
        with glossary.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                src = (row.get("source") or "").strip()
                if 2 <= len(src) <= 12 and CJK_TERM.fullmatch(src) and src not in STOP_TERMS:
                    from_glossary.append(src)

    for p in pairs:
        for t in set(CJK_TERM.findall(p["src"])):
            counts[t] += 1

    ranked = [t for t in from_glossary if counts.get(t, 0) >= 2]
    ranked += [t for t, c in counts.most_common()
               if c >= 2 and t not in STOP_TERMS and t not in ranked
               and 2 <= len(t) <= 8]
    return ranked[:top]


def ask_variants(term: str, samples: list[dict]) -> list[str]:
    body_text = "\n\n".join(f"[{i + 1}] {s['dst'][:400]}" for i, s in enumerate(samples))
    prompt = (
        f"下面每段英文译文都来自同一个中文术语「{term}」所在的段落。"
        f"请逐段指出「{term}」在这个英文段落里被写成了什么（只给英文短语，不要解释；"
        f"如果该段没有对应译法，写 NONE）。严格输出 JSON 数组，元素与段落数一致。\n\n{body_text}"
    )
    body = json.dumps({"model": MODEL, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        BASE_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    try:
        got = json.loads(m.group(0) if m else content)
    except ValueError:
        return []
    return [str(x).strip() for x in got]


def norm(v: str) -> str:
    v = re.sub(r"[\s\-_]+", " ", v.lower()).strip(" .,;:")
    v = re.sub(r"^(the|a|an)\s+", "", v)
    v = re.sub(r"'s\b", "", v)          # Feuerbach's → Feuerbach
    v = re.sub(r"s\b", "", v)           # 粗糙单复数归一
    return v


def classify(variants: dict[str, list[str]]) -> str:
    """把「不同写法」分成三类，避免把长短形式当成错误。

    一致     归一化后只有一种
    长短形式 一种写法的词是另一种的子集（Toni Morrison / Morrison，全称与简称）
    不一致   词都对不上，是真正的前后不统一
    """
    keys = [k for k in variants if k]
    if len(keys) <= 1:
        return "一致"
    token_sets = [set(k.split()) for k in keys]
    small = min(token_sets, key=len)
    if all(small <= ts for ts in token_sets):
        return "长短形式"
    return "不一致"


def audit_doc(pairs: list[dict], glossary: Path | None, top: int) -> list[dict]:
    terms = candidate_terms(pairs, glossary, top)
    results = []
    for term in terms:
        hits = [p for p in pairs if term in p["src"]][:6]
        if len(hits) < 2:
            continue
        variants = ask_variants(term, hits)
        cleaned = [v for v in variants if v and v.upper() != "NONE"]
        # 作者名（Li Yiqiang、Zhang Yaqian）在中文文献首页成片出现，不是术语问题
        if cleaned and all(re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", v) for v in cleaned):
            continue
        groups: defaultdict[str, list[str]] = defaultdict(list)
        for v in cleaned:
            groups[norm(v)].append(v)
        results.append({
            "term": term,
            "paragraphs": len(hits),
            "variants": {k: sorted(set(v)) for k, v in groups.items() if k},
            "distinct": len(groups),
            "verdict": classify(groups),
        })
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="consistency")
    p.add_argument("--pairs", default=None)
    p.add_argument("--glossary", default=None)
    p.add_argument("--dirs", default=None, help="批量：遍历这个目录下每个子目录的 pairs.jsonl")
    p.add_argument("--source", default=None, help="源 PDF：用来定位术语在第几页")
    p.add_argument("--top", type=int, default=8, help="检查多少个候选术语（默认 8）")
    p.add_argument("--out", default=None)
    p.add_argument("--json-summary", default=None, help="写一份计数 JSON，供流水线读")
    a = p.parse_args(argv)

    docs: list[tuple[str, Path]] = []
    if a.dirs:
        for d in sorted(Path(a.dirs).iterdir()):
            cand = d / "pairs.jsonl"
            if cand.exists():
                docs.append((d.name, cand))
    elif a.pairs:
        docs.append((Path(a.pairs).parent.name, Path(a.pairs)))
    if not docs:
        raise SystemExit("没有可用的 pairs.jsonl")

    lines = ["# 文档内术语一致性", "",
             "| 文档 | 术语 | 页 | 段落数 | 结论 | 写法 |", "|---|---|---|---|---|---|"]
    all_rows = []

    # 术语出现在哪几页：拿源 PDF 逐页找，最多报 3 页
    page_texts: list[str] = []
    if a.source and Path(a.source).exists():
        try:
            import pymupdf
            doc = pymupdf.open(a.source)
            page_texts = [re.sub(r"\s+", "", p.get_text()) for p in doc]
            doc.close()
        except Exception:
            page_texts = []

    def pages_of(term: str) -> str:
        if not page_texts:
            return "-"
        hits = [str(i + 1) for i, t in enumerate(page_texts) if term in t]
        return ",".join(hits[:3]) if hits else "-"

    for name, path in docs:
        gloss = None
        if a.glossary:
            gloss = Path(a.glossary)
        else:
            found = list(path.parent.glob("*.glossary.csv"))
            gloss = found[0] if found else None
        pairs = load_pairs(path)
        if not pairs:
            continue
        print(f"[{name}] {len(pairs)} 段，候选术语…", file=sys.stderr)
        res = audit_doc(pairs, gloss, a.top)
        for r in res:
            spellings = " / ".join(v[0] for v in r["variants"].values())[:90]
            r["pages"] = pages_of(r["term"])
            lines.append(f"| {name[:24]} | {r['term']} | {r['pages']} | {r['paragraphs']} | "
                         f"{r['verdict']} | {spellings} |")
            all_rows.append({"doc": name, **r})

    out = Path(a.out) if a.out else Path("audit/consistency.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if a.json_summary:
        bad = [r for r in all_rows if r.get("verdict") == "不一致"]
        Path(a.json_summary).write_text(json.dumps({
            "terms": len(all_rows),
            "inconsistent": len(bad),
            "items": [{"doc": r["doc"], "term": r["term"], "pages": r.get("pages"),
                       "variants": [v[0] for v in r["variants"].values()]} for r in bad][:20],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n".join(lines[:40]))
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
