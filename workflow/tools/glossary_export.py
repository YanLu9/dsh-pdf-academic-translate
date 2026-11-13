"""把主术语表（en,zh,note）导出成 BabelDOC 认的 CSV（source,target,tgt_lng）。

主表只维护一份，双向翻译各导一份，避免两份表互相漂移。

用法：
    python tools/glossary_export.py glossary/heat_island.csv --out-dir glossary/out
产出：
    glossary/out/babeldoc_en_zh.csv   （英→中）
    glossary/out/babeldoc_zh_en.csv   （中→英）
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="glossary_export")
    p.add_argument("master", help="主表 CSV，列：en,zh[,note][,status]")
    p.add_argument("--out-dir", default="glossary/out")
    p.add_argument("--all", action="store_true",
                   help="连 candidate（机器生成、未人工确认）的词条一起导出；默认只导 confirmed")
    a = p.parse_args(argv)

    src = Path(a.master)
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    dropped = 0
    with src.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            en, zh = (row.get("en") or "").strip(), (row.get("zh") or "").strip()
            if not (en and zh):
                continue
            # 没有 status 列的老表当已确认处理；candidate 是机器给的译名，
            # 混进 BabelDOC 的术语约束里会把普通单词（up→上、top→顶）钉死，必须挡掉
            status = (row.get("status") or "").strip().lower()
            if not a.all and status not in ("", "confirmed"):
                dropped += 1
                continue
            rows.append({"en": en, "zh": zh})

    if not rows:
        print(f"! {src.name} 里没有已确认的词条（全是 candidate？），本次不注入术语约束"
              "；确认译名后在面板里勾选，或加 --all 强制全量导出")

    targets = (("babeldoc_en_zh.csv", "en", "zh", "zh"), ("babeldoc_zh_en.csv", "zh", "en", "en"))
    for name, frm, to, tgt_lng in targets:
        path = out_dir / name
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["source", "target", "tgt_lng"])
            for row in rows:
                w.writerow([row[frm], row[to], tgt_lng])
        print(f"{path}  {len(rows)} 条"
              + (f"（已挡掉 {dropped} 条 candidate）" if dropped else ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
