r"""跨领域批量试译：拿一批不同学科的文献跑同一个流程，收集失败类型。

目的不是把这些文档翻完，而是回答一个问题：
    同一套自动修复，在哲学、文学、热工、油气、arXiv 预印本上，分别会在哪里崩？

用法：
    python tools/batch_audit.py --out audit --pages 1-3 ^
        "C:\Users\QW\Downloads\A.pdf" "C:\Users\QW\Downloads\B.pdf"

产物：
    audit\<slug>\...            每篇的完整产物（qa.md / final.pdf / pairs.jsonl）
    audit\summary.md            汇总表：方向、耗时、必须修、建议看、失败类型
    audit\summary.json          机器读
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def detect_lang(pdf: Path) -> str:
    """复用流水线里的判定（看前 5 页，避免英文扉页把中文论文判成英文）。"""
    from translate import detect_source_lang

    return detect_source_lang(pdf)


def slug_of(pdf: Path) -> str:
    name = re.sub(r"[^\w\u4e00-\u9fff]+", "_", pdf.stem)
    return name[:40]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="batch_audit")
    p.add_argument("pdfs", nargs="+")
    p.add_argument("--out", default="audit")
    p.add_argument("--pages", default="1-3")
    p.add_argument("--glossary", default="glossary/generic.csv")
    a = p.parse_args(argv)

    out_root = ROOT / a.out
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []

    for i, raw in enumerate(a.pdfs, 1):
        pdf = Path(raw)
        if not pdf.exists():
            print(f"[{i}] 缺文件：{pdf}", file=sys.stderr)
            continue
        lang = detect_lang(pdf)
        to = "en" if lang == "zh" else "zh"
        slug = f"{i:02d}_{lang}_{slug_of(pdf)}"
        out = out_root / slug
        print(f"\n[{i}/{len(a.pdfs)}] {pdf.name[:40]} | {lang}->{to} | {a.pages}", file=sys.stderr)

        cmd = [sys.executable, str(TOOLS / "translate.py"), str(pdf),
               "--to", to, "--out", str(out), "--pages", a.pages]
        if (ROOT / a.glossary).exists():
            cmd += ["--glossary", str(ROOT / a.glossary)]
        t0 = time.time()
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        (out / "run.log").write_text((proc.stdout or "") + "\n" + (proc.stderr or ""),
                                     encoding="utf-8")

        qa, rep = {}, {}
        if (out / "qa.json").exists():
            qa = json.loads((out / "qa.json").read_text(encoding="utf-8"))
        if (out / "report.json").exists():
            rep = json.loads((out / "report.json").read_text(encoding="utf-8"))

        rows.append({
            "file": pdf.name,
            "slug": slug,
            "direction": f"{lang}->{to}",
            "exit": proc.returncode,
            "duration_s": round(time.time() - t0, 1),
            "must_fix": qa.get("must_fix"),
            "maybe": qa.get("maybe"),
            "by_kind": qa.get("by_kind") or {},
            "checked_pages": qa.get("checked_pages") or [],
            "fixups": (rep.get("outputs") or {}).get("fixups"),
            "sample_items": (qa.get("items") or [])[:6],
        })

    (out_root / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 跨领域试译汇总", "",
             "| # | 文献 | 方向 | 耗时 | 必须修 | 建议看 | 失败类型 |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        kinds = "、".join(f"{k}×{v}" for k, v in r["by_kind"].items())
        lines.append(f"| {r['slug'][:2]} | {r['file'][:26]} | {r['direction']} | "
                     f"{r['duration_s']}s | {r['must_fix']} | {r['maybe']} | {kinds or '-'} |")
    lines.append("")
    for r in rows:
        lines.append(f"## {r['file']}")
        lines.append(f"- 方向 {r['direction']}，检查页 {r['checked_pages']}，自动修复 {r['fixups']} 条")
        for it in r["sample_items"]:
            lines.append(f"  - [{it.get('level')}] {it.get('kind')}：{(it.get('detail') or '')[:70]}")
        lines.append("")
    (out_root / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n-> {out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
