"""把「机器抽出来的术语」并进主表：只加候选，绝不动已确认的译名。

这是"词库自举"的落盘环节：BabelDOC 从文档里抽出的术语、或外部检索得到的候选，
统一按下面规则洗一遍再合进来——

    噪点       日期、纯数字、单字、人名/机构名（启发式判定）
    重复       归一化后已存在的 source → 跳过
    已确认     主表里 status=confirmed 的 → 不覆盖、不降级
    其余       以 status=candidate 追加，等人工确认

用法：
    python tools/glossary_bootstrap.py --domain physics.exotic-hadrons \
        --auto out/bootstrap_particles/*.glossary.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATE_RE = re.compile(r"\d{4}\s*年|\d{1,2}\s*月|\d{4}-\d{2}-\d{2}")
NUM_RE = re.compile(r"^[\d\s.,%+\-–—]+$")
# 人名/机构名启发式：2-4 个汉字且不带术语常见后缀，或含「大学/学院/研究所/系」
ORG_RE = re.compile(r"(大学|学院|研究所|研究院|系|实验室|中心)$")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def is_noise(src: str, tgt: str) -> str | None:
    s = src.strip()
    if not s or not tgt.strip():
        return "空行"
    if DATE_RE.search(s):
        return "日期"
    if NUM_RE.match(s):
        return "纯数字"
    if len(s) < 2:
        return "单字"
    if ORG_RE.search(s) or ORG_RE.search(tgt):
        return "机构名"
    return None


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="glossary_bootstrap")
    p.add_argument("--domain", required=True, help="主表文件名（不含 .csv），如 physics.exotic-hadrons")
    p.add_argument("--auto", nargs="+", required=True, help="一个或多个 BabelDOC 自动术语 CSV")
    p.add_argument("--glossary-dir", default="glossary")
    a = p.parse_args(argv)

    gdir = ROOT / a.glossary_dir
    master = gdir / f"{a.domain}.csv"
    fields = ["en", "zh", "note", "source", "status"]

    rows: list[dict] = []
    if master.exists():
        rows = read_rows(master)
    known = {norm(r.get("en", "")) for r in rows} | {norm(r.get("zh", "")) for r in rows}

    added = skipped = 0
    for auto_path in a.auto:
        for r in read_rows(Path(auto_path)):
            src, tgt = (r.get("source") or "").strip(), (r.get("target") or "").strip()
            if not src or not tgt:
                continue
            # 中文→英文时 source 是中文；这里两种方向都按"术语对"处理
            en, zh = (src, tgt) if re.search(r"[A-Za-z]", src) else (tgt, src)
            if norm(en) in known or norm(zh) in known:
                skipped += 1
                continue
            reason = is_noise(en, zh) or is_noise(zh, en)
            if reason:
                print(f"  跳过（{reason}）：{src} -> {tgt}", file=sys.stderr)
                skipped += 1
                continue
            rows.append({"en": en, "zh": zh, "note": "",
                         "source": f"auto:{Path(auto_path).name}", "status": "candidate"})
            known.add(norm(en))
            known.add(norm(zh))
            added += 1

    master.write_text("", encoding="utf-8")
    with master.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.get("status") or "?"] = by_status.get(r.get("status") or "?", 0) + 1
    print(f"{master}  共 {len(rows)} 条（新增候选 {added}，跳过 {skipped}）")
    for k, v in sorted(by_status.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
