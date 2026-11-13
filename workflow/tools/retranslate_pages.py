r"""单页重译：只重跑出问题的那几页，再拼回成品。

为什么需要它：
    一篇 46 页的稿子，某页翻坏了，现在只能整篇重跑（10 分钟 + 十几万 token）。
    分片之后每页都是独立翻的，所以「重译一页 + 换掉那一页」是安全的等价操作。

用法：
    python tools/retranslate_pages.py --out-dir out/night_46p --pages 5,12
    python tools/retranslate_pages.py --out-dir out/night_46p --pages 5 --no-qa

产物：<out>/final-merged.pdf（其余页逐字节沿用上一版）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def parse_pages(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            out.extend(range(int(a), int(b or a) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def pick_base(out_dir: Path) -> Path | None:
    # 用「原始成品」当底，而不是上一次的合并结果——否则反复重译会叠床架屋
    for name in ("final2.pdf", "composed.fixed.pdf", "composed.mono.pdf"):
        p = out_dir / name
        if p.exists():
            return p
    merged = sorted(out_dir.glob("final-merged*.pdf"))
    if merged:
        return merged[-1]
    monos = sorted(out_dir.glob("*.mono.pdf"))
    return monos[0] if monos else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retranslate_pages")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--pages", required=True, help="要重译的页，如 5 或 5,12 或 3-4")
    p.add_argument("--to", default=None, help="目标语言，默认读 report.json")
    p.add_argument("--no-qa", action="store_true", help="跳过重译后的体检")
    a = p.parse_args(argv)

    out_dir = Path(a.out_dir).resolve()
    if not out_dir.exists():
        raise SystemExit(f"找不到输出目录：{out_dir}")

    rep_path = out_dir / "report.json"
    report = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
    source = Path((report.get("source") or {}).get("path") or "")
    if not source.exists():
        raise SystemExit(f"找不到源 PDF：{source}")
    to = a.to or report.get("target_lang") or "zh"

    base = pick_base(out_dir)
    if base is None:
        raise SystemExit("找不到可用的成品 PDF")

    pages = parse_pages(a.pages)
    doc = pymupdf.open(base)
    total = doc.page_count
    doc.close()
    bad = [x for x in pages if not (1 <= x <= total)]
    if bad:
        raise SystemExit(f"页码越界：{bad}（文档共 {total} 页）")

    print(f"基线：{base.name}（{total} 页）｜源：{source.name}｜目标：{to}")
    redo_root = out_dir / "redo"
    redo = {}
    for pno in pages:
        cdir = redo_root / f"p{pno:03d}"
        cdir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(TOOLS / "run_babeldoc.py"), str(source),
               "--to", to, "--out", str(cdir), "--pages", str(pno)]
        print(f"  重译第 {pno} 页…")
        rc = subprocess.call(cmd, cwd=ROOT)
        monos = sorted(cdir.glob("*.mono.pdf"))
        if rc != 0 or not monos:
            print(f"  ! 第 {pno} 页重译失败（exit {rc}），保留原页", file=sys.stderr)
            continue
        redo[pno] = monos[0]

    if not redo:
        raise SystemExit("没有任何一页重译成功")

    merged = out_dir / "final-merged.pdf"
    if merged.exists():
        try:
            # 目标可能被 PDF 阅读器占用，删不掉就换个带时间戳的名字
            merged.unlink()
        except Exception:
            import time as _t
            merged = out_dir / f"final-merged-{_t.strftime('%H%M%S')}.pdf"

    base_doc = pymupdf.open(base)
    dst = pymupdf.open()
    for i in range(base_doc.page_count):
        pno = i + 1
        if pno in redo:
            rd = pymupdf.open(redo[pno])
            dst.insert_pdf(rd, from_page=i, to_page=i)
            rd.close()
        else:
            dst.insert_pdf(base_doc, from_page=i, to_page=i)
    dst.save(merged, garbage=3, deflate=True)
    dst.close()
    base_doc.close()
    (out_dir / "merge_result.json").write_text(
        json.dumps({"path": str(merged), "pages": list(redo)}, ensure_ascii=False),
        encoding="utf-8")
    print(f"换页完成：{len(redo)} 页 -> {merged}")

    if not a.no_qa:
        qa_json = out_dir / "qa_merged.json"
        subprocess.call([
            sys.executable, str(TOOLS / "qa_check.py"),
            "--source", str(source), "--translated", str(merged),
            "--to", to, "-o", str(out_dir / "qa_merged.md"),
            "--json-summary", str(qa_json),
        ], cwd=ROOT)
        if qa_json.exists():
            q = json.loads(qa_json.read_text(encoding="utf-8"))
            print(f"重译后体检：必须修 {q.get('must_fix')}，建议看 {q.get('maybe')}"
                  f" -> {out_dir / 'qa_merged.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
