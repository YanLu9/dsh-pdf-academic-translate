r"""按「专业方向」生成一份预设术语库（候选状态，需人工确认后再生效）。

为什么要有它：
    用户第一次用时没有词表；让他自己整理不现实。所以给一个方向名，让模型先列一版，
    落盘时统一标 status=candidate——机器生成的译名可能有错，不能当既定事实用。

用法：
    python tools/build_glossary.py --domain 粒子物理 --count 40
    python tools/build_glossary.py --domain "医疗器械" --out glossary/医疗器械.csv

产物：glossary/<方向>.csv，列 en,zh,note,source,status
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from run_babeldoc import api_key  # noqa: E402

BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"

PROMPT = """你是{domain}领域的资深译者。列出这个领域最常用、最容易译错的 {n} 个英文术语及其规范中文译名。

要求：
- 只输出 CSV 行，不要表头、不要解释、不要编号；
- 每行三列：英文术语,中文译名,一句说明（说明可留空），用英文逗号分隔；
- 英文用该领域文献里通行的写法；中文用国内期刊/教材的通行译法；
- 优先「一词一义」的专业术语，不要收录普通日常词；
- 如果某个词在该领域有公认的不同译法，在说明里注明。"""


def call_model(domain: str, n: int) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(domain=domain, n=n)}],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        BASE_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def parse_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.strip().strip("`")
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("en,"):
            continue  # 模型偶尔会加表头
        parts = [p.strip() for p in next(csv.reader([line]))]
        if len(parts) < 2:
            continue
        en, zh = parts[0], parts[1]
        note = parts[2] if len(parts) > 2 else ""
        if not en or not zh:
            continue
        if not re.search(r"[A-Za-z]", en) or not re.search(r"[\u4e00-\u9fff]", zh):
            continue
        rows.append([en, zh, note])
    return rows


def slug(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", name).strip("_") or "domain"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build_glossary")
    p.add_argument("--domain", required=True)
    p.add_argument("--count", type=int, default=40)
    p.add_argument("--out", default=None)
    p.add_argument("--json", action="store_true", help="输出机器可读结果")
    a = p.parse_args(argv)

    try:
        raw = call_model(a.domain, a.count)
    except Exception as e:
        msg = f"生成失败：{e}"
        print(msg, file=sys.stderr)
        if a.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        return 1

    rows = parse_rows(raw)
    if not rows:
        msg = "模型没有返回可解析的术语行"
        print(msg, file=sys.stderr)
        if a.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        return 1

    out = Path(a.out) if a.out else ROOT / "glossary" / f"{slug(a.domain)}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if out.exists():
        with out.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("en") or "").strip().lower()
                if key:
                    existing[key] = row

    added = 0
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["en", "zh", "note", "source", "status"])
        for row in existing.values():
            w.writerow([row.get("en", ""), row.get("zh", ""), row.get("note", ""),
                        row.get("source", ""), row.get("status", "candidate")])
        for en, zh, note in rows:
            if en.lower() in existing:
                continue
            w.writerow([en, zh, note, f"model:{a.domain}", "candidate"])
            added += 1

    total = len(existing) + added
    result = {"ok": True, "path": str(out), "added": added, "total": total,
              "domain": a.domain}
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"{out}  新增 {added} 条 / 共 {total} 条（status=candidate）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
