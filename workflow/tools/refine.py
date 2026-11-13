r"""对已完成的译文做「扫描 → 修 → 复检」的迭代收敛。

设计上刻意保守——上一版吃过亏：patch_pdf 整块重写曾把整段后文抹掉，指标好看、质量更差。
所以这里只做**字符级、等长、单行块**的替换，并且每轮都验不变式，不达标就回滚：

    安全清单（只有这些会被自动改）
        · 数字之间的中文句号：定理 5。1 → 5.1
        · 连续标点：， ， → ，
        · 中文标点前的半角空格：文字 ， → 文字，
    只报告、不自动改
        · 版面重叠、页眉混入正文（要改排版，风险大）
        · 句子被拆乱（要重译 + 整段替换，会动版面）
        · 参考文献丢失（应该在翻译时跳过，事后改不动）

停止条件（任一满足即停）
    · 没有安全项可改
    · 本轮收益 < 上轮的 30%
    · 不变式校验失败（回滚后停）
    · 达到 --max-rounds

用法：
    python tools/refine.py --out-dir out/2610.36736v1_en_zh --max-rounds 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"

# 只改这些：字符级、不改变段落结构
SAFE_CHARS = set(
    "".join(chr(c) for c in range(0x20, 0x7F))          # ASCII 可打印
    + "，。、；：？！（）《》【】“”‘’…—·％"              # 中文标点
    + "°"
)

SAFE_RULES = [
    (re.compile(r"(?<=\d)。(?=\d)"), ".", "数字间中文句号"),
    (re.compile(r"([，,])\s*([，,])"), r"\1", "连续标点"),
    (re.compile(r"\s+([，。；：、])"), r"\1", "标点前空格"),
]


def is_plain(text: str) -> bool:
    """块里有没有「特殊字符」（数学符号、兼容区汉字等）。

    整块重写会把整段按新字体重排；一旦块里含 ⊗、∘、∑ 或兼容区汉字，
    新字体没有这些字形，就会渲染成方块——实测把第 4 页毁掉过一次。
    """
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            continue
        if ch in SAFE_CHARS or ch.isspace():
            continue
        return False
    return True


def fix_digit_dots(pdf: Path, out_pdf: Path) -> int:
    """把「5。1」里的全角句号换成半角点，只动那一个字符。

    为什么不用整块替换：块里常含 ⊗、∑ 这类字形，整块重排会被渲染成方块（踩过）。
    只抹掉中间那个「。」，再在同一基线上写回半角点，其余文字一个像素都不碰。
    """
    doc = pymupdf.open(pdf)
    fixed = 0
    for page in doc:
        text = page.get_text()
        for m in re.finditer(r"\d。\d", text):
            for rect in page.search_for(m.group(0)):
                # 三个字符按 0.5 / 1.0 / 0.5 字宽估：全角句号在中间那 50%
                x0 = rect.x0 + rect.width * 0.25
                x1 = rect.x0 + rect.width * 0.75
                sub = pymupdf.Rect(x0, rect.y0, x1, rect.y1)
                page.add_redact_annot(sub, fill=None)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
                size = max(6.0, rect.height * 0.78)
                page.insert_text((x0, rect.y1 - size * 0.22), ".",
                                 fontname="helv", fontsize=size, color=(0, 0, 0))
                fixed += 1
    doc.save(out_pdf, garbage=3, deflate=True)
    doc.close()
    return fixed


def page_stats(pdf: Path) -> dict:
    doc = pymupdf.open(pdf)
    text = "".join(p.get_text() for p in doc)
    formula = sum(text.count(c) for c in "∑∫√±≈≤≥∂∇")
    stats = {"pages": doc.page_count, "chars": len(text), "formula": formula}
    doc.close()
    return stats


def run_qa(pdf: Path, source: Path, to: str, out_json: Path) -> dict:
    subprocess.run(
        [sys.executable, str(TOOLS / "qa_check.py"),
         "--source", str(source), "--translated", str(pdf),
         "--to", to, "-o", str(out_json.with_suffix(".md")),
         "--json-summary", str(out_json)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))
    # 体检没跑起来时不能假装「0 分」——那会让质量闸门永远显示干净
    print(f"! 体检失败，无法评分（{out_json.name} 没生成）", file=sys.stderr)
    return {"error": "qa_failed"}


def build_edits(pdf: Path) -> list[dict]:
    """只挑单行块里、改完长度不变的那种，避免动版面。"""
    doc = pymupdf.open(pdf)
    edits = []
    for pno, page in enumerate(doc):
        for b in page.get_text("blocks"):
            raw = b[4] or ""
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            text = raw.strip()
            if len(text) < 8:
                continue
            new = text
            kinds = []
            for pattern, repl, name in SAFE_RULES:
                changed, n = pattern.subn(repl, new)
                if n:
                    new = changed
                    kinds.append(name)
            if new == text:
                continue
            # 块里含数学符号/兼容区汉字时不动：整块重排会渲染成方块（踩过）
            if not is_plain(text):
                continue
            # 多行块只在「等长替换」时动（比如 。→.）：长度不变，断行位置基本不变；
            # 删空格这类会改长度的，只对单行块做，否则会重排。
            if len(lines) > 1 and len(new) != len(text):
                continue
            edits.append({"page": pno + 1, "old": text, "new": new,
                          "why": "、".join(kinds)})
    doc.close()
    return edits


def apply_edits(pdf: Path, edits: list[dict], out_pdf: Path) -> tuple[int, str]:
    csv_path = out_pdf.with_suffix(".edits.csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["page", "old", "new"])
        w.writeheader()
        for e in edits:
            w.writerow({"page": e["page"], "old": e["old"], "new": e["new"]})
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "patch_pdf.py"),
         str(pdf), str(csv_path), "-o", str(out_pdf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"改好 (\d+) 处", out)
    return (int(m.group(1)) if m else 0), out.strip()


def invariants(before: dict, after: dict) -> list[str]:
    problems = []
    if after["pages"] != before["pages"]:
        problems.append(f"页数变化 {before['pages']} → {after['pages']}")
    if before["formula"] and after["formula"] < before["formula"] * 0.95:
        problems.append(f"公式符号减少 {before['formula']} → {after['formula']}")
    if abs(after["chars"] - before["chars"]) > max(30, before["chars"] * 0.03):
        problems.append(f"总字数变化过大 {before['chars']} → {after['chars']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="refine")
    p.add_argument("--out-dir", required=True, help="上一次翻译的输出目录")
    p.add_argument("--pdf", default=None, help="起始 PDF，默认取目录里的 final2/final/mono")
    p.add_argument("--source", default=None, help="源 PDF，默认从 report.json 读")
    p.add_argument("--to", default=None, help="目标语言，默认从 report.json 读")
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--min-gain", type=float, default=0.3, help="收益低于上轮这个比例就停")
    p.add_argument("--report-only", action="store_true",
                   help="只出质量评分和剩余问题，不改 PDF（推荐默认用这个）")
    a = p.parse_args(argv)

    out_dir = Path(a.out_dir)
    report = {}
    rp = out_dir / "report.json"
    if rp.exists():
        report = json.loads(rp.read_text(encoding="utf-8"))

    source = Path(a.source or (report.get("source") or {}).get("path") or "")
    to = a.to or report.get("target_lang") or "zh"
    if not source.exists():
        raise SystemExit(f"找不到源文件：{source}")

    pdf = Path(a.pdf) if a.pdf else None
    if pdf is None:
        for name in ("final2.pdf", "final.pdf", "fixed.pdf"):
            if (out_dir / name).exists():
                pdf = out_dir / name
                break
        if pdf is None:
            monos = sorted(out_dir.glob("*.mono.pdf"))
            pdf = monos[0] if monos else None
    if pdf is None or not pdf.exists():
        raise SystemExit("找不到可续修的 PDF")

    print(f"起点：{pdf.name} | 源：{source.name} | 方向 → {to}")
    rows = []
    score_prev = None
    cur = pdf
    rejected = None

    for rnd in range(1, a.max_rounds + 1):
        qa_json = out_dir / f"refine_qa_{rnd}.json"
        qa = run_qa(cur, source, to, qa_json)
        must, maybe = qa.get("must_fix", 0), qa.get("maybe", 0)
        score = must * 3 + maybe
        before = page_stats(cur)

        if a.report_only:
            rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                         "edits": 0, "applied": 0,
                         "status": "只评估，不改动"})
            print(f"第 {rnd} 轮：score={score}（必须修 {must}，建议看 {maybe}）")
            break

        # ① 字符级：小数点（安全，只动一个字符）
        dot_stage = out_dir / f"refine_{rnd}_dots.pdf"
        dots = fix_digit_dots(cur, dot_stage)
        base = dot_stage if dots else cur

        # ② 块级：只对「纯文本块」做等长/单行替换
        edits = build_edits(base)

        if not dots and not edits:
            rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                         "edits": 0, "applied": 0, "status": "没有安全项可改"})
            print(f"第 {rnd} 轮：score={score}，没有可安全修的项目，停。")
            break

        if edits:
            cand = out_dir / f"refine_{rnd}.pdf"
            applied, _log = apply_edits(base, edits, cand)
        else:
            cand, applied = base, 0
        if not cand.exists():
            rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                         "edits": len(edits), "applied": 0, "status": "替换失败"})
            print(f"第 {rnd} 轮：替换失败，停。")
            break

        after = page_stats(cand)
        problems = invariants(before, after)
        if problems:
            rejected = problems
            rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                         "edits": dots + len(edits), "applied": dots + applied,
                         "status": "不变式不过，已回滚：" + "；".join(problems)})
            print(f"第 {rnd} 轮：不变式不过（{problems}），回滚。")
            break

        # 修完再体检一次，确认分数确实下降且没有新增问题
        qa2 = run_qa(cand, source, to, out_dir / f"refine_qa_{rnd}_after.json")
        must2, maybe2 = qa2.get("must_fix", 0), qa2.get("maybe", 0)
        score2 = must2 * 3 + maybe2
        if score2 > score:
            cand.unlink(missing_ok=True)
            rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                         "edits": len(edits), "applied": applied,
                         "status": f"改完更差（{score} → {score2}），已回滚"})
            print(f"第 {rnd} 轮：改完分数反而升高（{score}→{score2}），回滚。")
            break

        rows.append({"round": rnd, "score": score, "must": must, "maybe": maybe,
                     "edits": len(edits), "applied": applied,
                     "status": f"接受（{score} → {score2}）", "score_after": score2})
        print(f"第 {rnd} 轮：改 {applied} 处，score {score} → {score2}")
        cur = cand

        gain = 0 if not score_prev else (score_prev - score2) / max(1, score_prev)
        if score2 == 0:
            print("  已清零，停。")
            break
        if score_prev is not None and gain < a.min_gain:
            print(f"  收益只有 {gain:.0%}（< {a.min_gain:.0%}），收益递减，停。")
            break
        score_prev = score2

    final = out_dir / "refined.pdf"
    shutil.copyfile(cur, final)

    # ---------- 判定：还值不值得改 ----------
    last = rows[-1] if rows else {}
    must = last.get("must", 0)
    maybe = last.get("maybe", 0)
    qa_items = qa.get("items") or []
    if not qa_items:
        for r in rows:
            if r.get("must"):
                qa_items = []
    cons = {}
    cp = out_dir / "consistency.json"
    if cp.exists():
        try:
            cons = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            cons = {}
    inconsistent = cons.get("inconsistent") or 0

    score = must * 3 + maybe + inconsistent * 2
    pages = page_stats(cur)["pages"]
    minutes = must * 2 + maybe * 1 + inconsistent * 2

    if score == 0:
        verdict = "可以交付，不必再改"
        why = "机器能查的项目已经清零"
    elif score <= 15:
        verdict = f"值得人工挑改，预计 {minutes} 分钟"
        why = (f"必须修 {must} 条、建议看 {maybe} 条、术语不一致 {inconsistent} 条；"
               f"都是局部问题，不动版式")
    else:
        verdict = "建议重译，不值得手改"
        why = (f"score={score}，问题密度过高（每页约 {score / max(1, pages):.1f} 分）；"
               f"重译 1 次约 {pages * 2500 / 10000:.1f} 万 token")

    lines = ["# 译后扫描与判定", "",
             f"**判定：{verdict}**", "",
             f"- 依据：{why}",
             f"- 扫描结果：必须修 {must} 条、建议看 {maybe} 条、术语不一致 {inconsistent} 条",
             f"- 文档：{pdf.name}（{pages} 页）",
             f"- 评分：score = {score}（必须修×3 + 建议看 + 不一致×2）", ""]

    if qa_items:
        lines += ["## 逐条清单", "", "| 页 | 级别 | 类型 | 说明 |", "|---|---|---|---|"]
        for it in qa_items[:20]:
            lines.append(f"| {it.get('page') or '-'} | {it.get('level')} | "
                         f"{it.get('kind')} | {str(it.get('detail') or '')[:60]} |")
        lines.append("")

    if cons.get("items"):
        lines += ["## 术语不一致", "", "| 术语 | 页 | 出现的写法 |", "|---|---|---|"]
        for it in cons["items"][:10]:
            lines.append(f"| {it.get('term')} | {it.get('pages') or '-'} | "
                         f"{' / '.join(it.get('variants') or [])[:60]} |")
        lines.append("")

    lines += ["## 为什么不自动改", "",
              "- **版面重叠 / 页眉混入正文**：要重排整页，风险大于收益",
              "- **句子被拆乱**：需整段重译再整块替换，会挤动后续段落",
              "- **编号/数值丢失**：得回源核对位置，机器猜不准",
              "- **术语不一致**：统一措辞是编辑决策，改错一处比不改更糟", ""]

    if rows:
        lines += ["## 本轮尝试", "", "| 轮次 | score | 必须修 | 建议看 | 候选 | 已改 | 结论 |",
                  "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['round']} | {r['score']} | {r['must']} | {r['maybe']} | "
                         f"{r['edits']} | {r['applied']} | {r['status']} |")
        lines.append("")
    if rejected:
        lines += ["## 被回滚的原因", ""] + [f"- {x}" for x in rejected]

    rp2 = out_dir / "refine_report.md"
    rp2.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n判定：{verdict}")
    print(f"-> {rp2}\n-> {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
