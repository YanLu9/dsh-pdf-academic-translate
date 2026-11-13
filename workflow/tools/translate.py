r"""一条龙：导出术语表 → BabelDOC 出保版式 PDF → 译后体检。

两种输出模式：
    默认        给人看：进度打屏、结尾列产物
    --json      给机器看：stdout 只有 NDJSON 事件，人类日志全部走 stderr；
                结束时在输出目录写 report.json（插件/流水线读这个）

用法（在本目录下）：
    python tools/translate.py ..\paper.pdf --to zh
    python tools/translate.py ..\paper.pdf --to zh --json
    python tools/translate.py ..\paper.pdf --to en --glossary glossary/heat_island.csv --pages 1-20

产物在 out/<文件名>_<方向>/：
    *.mono.pdf            单语成品
    *.dual.pdf            双语对照（人工校对看这个）
    *.glossary.csv        BabelDOC 自动抽出的术语（本轮学习成果）
    qa.md                 体检报告（人读）
    qa.json               体检计数（机器读）
    report.json           本轮总账：页数、token、耗时、产物、体检计数
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


class Reporter:
    """默认写 stderr（人可以看）；--json 时同时把事件写成 NDJSON 到 stdout。"""

    def __init__(self, as_json: bool, t0: float):
        self.as_json = as_json
        self.t0 = t0

    def human(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def event(self, obj: dict) -> None:
        if self.as_json:
            print(json.dumps({"ts": round(time.time() - self.t0, 2), **obj}, ensure_ascii=False),
                  flush=True)


def run_stream(script: str, args: list[str], rep: Reporter) -> tuple[int, list[dict]]:
    """跑一个工具：它的 NDJSON 事件原样转发到我们的 stdout，其余文本进 stderr。"""
    cmd = [sys.executable, str(TOOLS / script), *args]
    rep.human(f">>> {script} {' '.join(args)}")
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdout is not None
    events: list[dict] = []
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                rep.human(line)
                continue
            events.append(obj)
            rep.event(obj)
        elif line:
            rep.human(line)
    return proc.wait(), events


def run_quiet(script: str, args: list[str], rep: Reporter) -> int:
    cmd = [sys.executable, str(TOOLS / script), *args]
    rep.human(f">>> {script} {' '.join(args)}")
    rc = subprocess.call(cmd, cwd=ROOT, stdout=sys.stderr, stderr=sys.stderr)
    return rc


def run_capture(script: str, args: list[str]) -> tuple[int, list[dict], str]:
    """跑一个工具并把它的事件收下来（不实时转发）。并行分片时用：多个进程同时往
    同一个 stdout 写 NDJSON 会互相插断，所以各自静默跑、跑完再报。"""
    cmd = [sys.executable, str(TOOLS / script), *args]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    events: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return proc.returncode, events, (proc.stdout or "") + (proc.stderr or "")


def detect_source_lang(pdf: Path, pages: int = 5) -> str:
    """按汉字占比判源语言（中文期刊常带英文摘要/英文扉页，多看几页更稳）。"""
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        text = "".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
        doc.close()
    except Exception:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk + latin == 0:
        return "en"
    return "zh" if cjk / (cjk + latin) > 0.25 else "en"


def reference_pages(pdf: Path, min_ratio: float = 0.15, min_entries: int = 3) -> list[int]:
    """找出参考文献所在的页（1 起）。

    两条判据，先严后松：
      1) 找到「参考文献 / References」标题行 → 该页及之后所有页都算（最常见）；
      2) 没有标题时，退化成「条目行占比高」的页。
    中文学术惯例是参考文献保留原语言；不跳过的话 DOI / arXiv 号会被截掉，引用直接不可验证。
    """
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
    except Exception:
        return []

    heading = re.compile(r"^\s*(参考文献|References|REFERENCES|Bibliography)\s*$", re.M)
    # 出版信息词汇：参考文献列表的强特征，正文里几乎不出现（附录、表格里也不出现）
    venue = re.compile(
        r"\b(Proceedings|Conference|Workshop|Journal|arXiv preprint|"
        r"Association for Computational|pp\.|vol\.|Press|Springer|ACM|ACL|"
        r"EMNLP|NAACL|NeurIPS|ICLR|CVPR|ICCV|ECCV)\b")
    pages = [(i + 1, page.get_text()) for i, page in enumerate(doc)]
    total = doc.page_count
    doc.close()

    def ref_like(text: str) -> bool:
        """这一页还像不像参考文献列表。

        只看「[n] 条目占比」会漏掉作者-年份式（BERT 那种没有编号的），
        而「找到标题就把后面全部当参考文献」又会把附录、表格页一起跳掉
        （实测 attention 的附录 13-15 页、ResNet 的附录 10-12 页整页没译）。
        """
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 8:
            return False
        entries = sum(1 for ln in lines if re.match(r"^\[\d{1,3}\]", ln))
        if entries >= min_entries:
            return True
        venues = sum(1 for ln in lines if venue.search(ln))
        if venues >= 4 and venues / len(lines) >= 0.05:
            return True
        return entries >= 1 and venues >= 3

    # 「References」在首页出现多半是目录/模板残留，不能因此把整篇都当参考文献
    for pno, text in pages:
        if pno < max(2, int(total * 0.3)):
            continue
        if heading.search(text):
            entries = sum(1 for ln in text.splitlines() if re.match(r"^\[\d{1,3}\]", ln.strip()))
            if entries >= 2 or ref_like(text):
                # 从标题页往后延，只延「还像参考文献」的页；遇到附录/表格就停
                out_pages = [pno]
                for p2, t2 in pages:
                    if p2 <= pno:
                        continue
                    if ref_like(t2):
                        out_pages.append(p2)
                    else:
                        break
                return out_pages

    out = []
    for pno, text in pages:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 8:
            continue
        entries = sum(1 for ln in lines if re.match(r"^\[\d{1,3}\]", ln))
        if entries >= min_entries and entries / len(lines) >= min_ratio:
            out.append(pno)
    return out


def toc_pages(pdf: Path, max_pages: int | None = None) -> list[int]:
    """找出目录页（1 起）。

    目录不是正常段落：它靠「引导点 + 页码」排版，被当正文翻过之后页码会和标题粘连
    （实测出现「83. 混合 p-自旋模型」这种）。中文学术惯例里目录也常保留原样，
    所以直接跳过、保持原样。

    判据：一行里同时出现标题文字、引导点（…… 或 ...）和结尾页码，且这种行占整页一半以上。
    """
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
    except Exception:
        return []

    # 引导点有两种写法：密集的「......」和中文排版常见的散点「. . . . .」
    dotted_run = re.compile(r"(?:\.\s*){4,}|…{2,}|·{3,}")
    heading = re.compile(r"^\s*(目录|目\s*录|CONTENTS|Table of Contents)\s*$", re.M | re.I)
    limit = max_pages if max_pages is not None else max(8, int(doc.page_count * 0.2))
    out = []
    for i, page in enumerate(doc):
        if i >= limit:
            break
        text = page.get_text()
        if not heading.search(text):
            continue
        runs = len(dotted_run.findall(text))
        if runs >= 3:
            out.append(i + 1)
    doc.close()
    return out


def to_ranges(nums: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3,5,7-8'"""
    nums = sorted(set(nums))
    if not nums:
        return ""
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = n
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)


def parse_ranges(spec: str) -> set[int]:
    """'1-3,5' -> {1,2,3,5}；也接受 '1,2,1-,-3' 这种 BabelDOC 写法里的常见形式。"""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo = int(a) if a else 1
            hi = int(b) if b else 10**6
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    return out


def compose_pdf(source: Path, chunks: list[tuple[int, int, Path]], out_pdf: Path) -> int:
    """把各分片翻好的页拼回一整份；没翻的页（比如参考文献）从原文取。

    chunks: [(起始页, 结束页, 该片的 mono.pdf)]，页号 1 起、含两端。
    """
    import pymupdf

    src = pymupdf.open(source)
    dst = pymupdf.open()
    # 关键：BabelDOC 带 --pages 时，输出的仍是「整份文档」，只有指定页被翻译。
    # 所以要按绝对页号取，而不是按片内偏移——按偏移取会把封面当成第 6 页。
    by_page: dict[int, tuple[Path, int]] = {}
    for start, end, mono in chunks:
        for p in range(start, end + 1):
            by_page[p] = (mono, p - 1)
    for p in range(1, src.page_count + 1):
        if p in by_page:
            path, idx = by_page[p]
            doc = pymupdf.open(path)
            dst.insert_pdf(doc, from_page=idx, to_page=idx)
            doc.close()
        else:
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
    total = dst.page_count
    dst.save(out_pdf, garbage=3, deflate=True)
    dst.close()
    src.close()
    return total


def chunk_fingerprint(common_args: list[str], to: str, source: Path) -> str:
    """这一片的输入指纹：页码由调用方拼进去，其它要素（术语表/模型/方向/源文件）都算进来。

    没有指纹会出两类隐性错误：
      · 换了术语表再跑，旧片被原样复用 → 新术语静默不生效；
      · 单页重译修好一页后整篇重跑，chunks 里的旧页把修好的覆盖回去。
    """
    import hashlib

    h = hashlib.sha1()
    h.update(to.encode())
    gloss = ""
    for i, arg in enumerate(common_args):
        if arg == "--glossary" and i + 1 < len(common_args):
            gloss = common_args[i + 1]
    if gloss and Path(gloss).exists():
        h.update(Path(gloss).read_bytes())
    h.update((os.environ.get("BABELDOC_MODEL") or "").encode())
    h.update(Path(source).name.encode())
    try:
        h.update(str(Path(source).stat().st_size).encode())
    except OSError:
        pass
    return h.hexdigest()[:16]


def run_chunked(pdf: Path, out: Path, pdf_pages: int, target_pages: list[int],
                chunk_size: int, common_args: list[str], rep: "Reporter",
                resume: bool, to: str = "zh", parallel: int = 1) -> tuple[bool, dict, list[Path]]:
    """分片翻译：每片单独跑 BabelDOC，失败只重这片；已完成的片可复用。

    parallel > 1 时并行跑多片（各自独立进程、独立输出目录）。
    并行只影响本机与 API 的并发，不改任何单片结果。
    """
    import pymupdf

    batches = [target_pages[i:i + chunk_size] for i in range(0, len(target_pages), chunk_size)]
    rep.human(f"分片翻译：{len(target_pages)} 页切成 {len(batches)} 片"
              + (f"，{parallel} 路并行" if parallel > 1 else ""))
    chunk_root = out / "chunks"
    chunk_root.mkdir(parents=True, exist_ok=True)
    fp_common = chunk_fingerprint(common_args, to, pdf)
    tokens: dict[str, str] = {}
    done_chunks: list[tuple[int, int, Path]] = []
    pairs_files: list[Path] = []

    def find_mono(cdir: Path) -> Path | None:
        hits = sorted(cdir.glob("*.mono.pdf"))
        return hits[0] if hits else None

    def cached_ok(cdir: Path, fp: str) -> bool:
        if not resume:
            return False
        mono0 = find_mono(cdir)
        stamp = cdir / "chunk.json"
        if mono0 is None or not stamp.exists():
            return False
        try:
            return json.loads(stamp.read_text(encoding="utf-8")).get("fp") == fp
        except Exception:
            return False

    def do_chunk(idx: int, spec: str, cdir: Path, fp: str) -> tuple[int, Path | None, dict, str]:
        """跑一片（带 3 次重试），返回 (idx, mono, tokens, log)。"""
        for attempt in (1, 2, 3):
            args = list(common_args) + ["--out", str(cdir), "--pages", spec]
            rc, events, log = run_capture("run_babeldoc.py", args)
            tk = {}
            for ev in events:
                if ev.get("event") == "babeldoc_done":
                    t = ev.get("tokens") or {}
                    if t.get("total_tokens"):
                        tk = t
            mono = find_mono(cdir)
            if rc == 0 and mono is not None:
                try:
                    (cdir / "chunk.json").write_text(
                        json.dumps({"fp": fp, "pages": spec}, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                return idx, mono, tk, log
            log_tail = (log or "").strip().splitlines()[-1:] or [""]
            rep.human(f"  片 {idx} 第 {attempt} 次失败：{log_tail[0][:120]}")
        return idx, None, {}, ""

    # 先分类：能复用的直接记账，其余进待跑队列
    plan: list[tuple[int, str, Path, str]] = []
    for idx, batch in enumerate(batches, 1):
        spec = to_ranges(batch)
        cdir = chunk_root / f"c{idx:02d}"
        fp = f"{fp_common}-{spec}"
        rep.event({"event": "chunk", "index": idx, "total": len(batches),
                   "pages": spec, "status": "start"})
        if cached_ok(cdir, fp):
            mono = find_mono(cdir)
            rep.human(f"  片 {idx}/{len(batches)} 复用已有结果（{spec}）")
            rep.event({"event": "chunk", "index": idx, "total": len(batches),
                       "pages": spec, "status": "cached"})
            done_chunks.append((batch[0], batch[-1], mono))
            pf = cdir / "pairs.jsonl"
            if pf.exists():
                pairs_files.append(pf)
        else:
            if (cdir / "chunk.json").exists():
                rep.human(f"  片 {idx}/{len(batches)} 输入变了（术语表/模型），重翻这一片")
            plan.append((idx, spec, cdir, fp))

    if parallel > 1 and len(plan) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        rep.human(f"  并行开跑：{len(plan)} 片，最多 {parallel} 路同时")
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(do_chunk, i, s, c, f): (i, s, c) for i, s, c, f in plan}
            results = []
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        results = [do_chunk(i, s, c, f) for i, s, c, f in plan]

    by_idx = {i: (mono, tk) for i, mono, tk, _ in results}
    for idx, spec, cdir, _fp in plan:
        mono, tk = by_idx.get(idx, (None, {}))
        if mono is None:
            rep.event({"event": "error", "stage": "translate",
                       "message": f"第 {idx} 片（{spec}）翻译失败"})
            return False, tokens, pairs_files
        if tk:
            # 累加而不是覆盖：之前每片覆盖一次，报告里只剩最后一片的用量，成本统计严重偏低
            for k in ("total_tokens", "prompt_tokens", "completion_tokens"):
                if tk.get(k):
                    tokens[k] = str(int(tokens.get(k) or 0) + int(tk[k]))
        rep.human(f"  片 {idx}/{len(batches)} 完成（{spec}）")
        rep.event({"event": "chunk", "index": idx, "total": len(batches),
                   "pages": spec, "status": "done"})
        batch = batches[idx - 1]
        done_chunks.append((batch[0], batch[-1], mono))
        pf = cdir / "pairs.jsonl"
        if pf.exists():
            pairs_files.append(pf)

    done_chunks.sort(key=lambda x: x[0])

    composed = out / "composed.mono.pdf"
    pages = compose_pdf(pdf, done_chunks, composed)
    rep.human(f"拼回整份：{pages} 页 -> {composed.name}")

    # 段落对合并，供后续修复/体检用
    merged = out / "pairs.jsonl"
    with merged.open("w", encoding="utf-8") as fh:
        for pf in pairs_files:
            for line in pf.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    fh.write(line + "\n")
    return True, tokens, [composed]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    as_json = "--json" in args
    t0 = time.time()
    rep = Reporter(as_json, t0)

    p = argparse.ArgumentParser(prog="translate")
    p.add_argument("pdf")
    p.add_argument("--to", default="zh", choices=["zh", "en"])
    p.add_argument("--glossary", default="", help="主术语表（en,zh,note）；留空则不注入术语")
    p.add_argument("--out", default=None)
    p.add_argument("--pages", default=None)
    p.add_argument("--no-auto-glossary", action="store_true")
    p.add_argument("--chunk-size", type=int, default=10,
                   help="大文档按多少页一片来翻（0=不分片）；分片后进度可见、失败只重一片")
    p.add_argument("--no-resume", action="store_true", help="忽略已完成的片，全部重翻")
    p.add_argument("--parallel", type=int, default=1,
                   help="同时跑几片（1=串行；2-3 明显更快，但会给 API 更大压力）")
    p.add_argument("--translate-refs", action="store_true",
                   help="连参考文献页一起翻（默认跳过，保留原文）")
    a = p.parse_args([x for x in args if x != "--json"])

    pdf = Path(a.pdf).expanduser().resolve()
    if not pdf.exists():
        rep.human(f"找不到 PDF：{pdf}")
        rep.event({"event": "error", "stage": "plan", "message": f"找不到 PDF：{pdf}"})
        return 2

    # 目标语言等于源语言时直接拦下：否则会跑一遍「英译英」，白花 token 还给你一份原样文档
    src_lang = detect_source_lang(pdf)
    if src_lang == a.to:
        msg = (f"目标语言（{a.to}）与源语言（{src_lang}）相同，没有翻译的必要；"
               f"要反方向请把 --to 改成 {'en' if a.to == 'zh' else 'zh'}")
        rep.human("! " + msg)
        rep.event({"event": "error", "stage": "plan", "message": msg,
                   "source_lang": src_lang})
        return 5

    tag = "en_zh" if a.to == "zh" else "zh_en"
    out_tag = tag + ("_p" + a.pages.replace(",", "-") if a.pages else "")
    out = Path(a.out).resolve() if a.out else ROOT / "out" / f"{pdf.stem}_{out_tag}"
    out.mkdir(parents=True, exist_ok=True)

    # 参考文献页、目录页默认不译：原样保留。
    # 参考文献译了会丢 DOI/arXiv 号；目录靠「引导点+页码」排版，译了页码会和标题粘连。
    skip_refs: list[int] = []
    skip_toc: list[int] = []
    pages_arg = a.pages
    if not a.translate_refs:
        skip_refs = reference_pages(pdf)
        skip_toc = toc_pages(pdf)
        protected = sorted(set(skip_refs) | set(skip_toc))
        if protected:
            import pymupdf as _pm
            total = _pm.open(pdf).page_count
            keep = set(range(1, total + 1)) - set(protected)
            if a.pages:
                keep &= parse_ranges(a.pages)
            pages_arg = to_ranges(sorted(keep))
            parts = []
            if skip_refs:
                parts.append(f"参考文献页 {skip_refs}")
            if skip_toc:
                parts.append(f"目录页 {skip_toc}")
            rep.human("、".join(parts) + f" 保留原文；本次翻译页码：{pages_arg}")
            rep.event({"event": "stage", "name": "refs", "status": "done",
                       "skipped_pages": protected, "skipped_refs": skip_refs,
                       "skipped_toc": skip_toc, "translate_pages": pages_arg})

    # ① 术语表（可以不给：没有词表也能跑，只是失去术语约束）
    glossary_src = None
    if a.glossary:
        cand = Path(a.glossary)
        cand = cand if cand.is_absolute() else (ROOT / cand)
        if cand.exists():
            glossary_src = cand.resolve()
        else:
            rep.human(f"! 术语表不存在，跳过：{cand}")

    rep.event({
        "event": "plan", "source": str(pdf), "target": a.to, "out": str(out),
        "pages": a.pages or "all",
        "glossary": str(glossary_src) if glossary_src else None,
    })

    babel_glossary: Path | None = None
    if glossary_src:
        rep.event({"event": "stage", "name": "glossary", "status": "start"})
        rc = run_quiet("glossary_export.py",
                       [str(glossary_src), "--out-dir", str(ROOT / "glossary" / "out")], rep)
        if rc != 0:
            rep.event({"event": "error", "stage": "glossary", "message": "术语表导出失败"})
            return 3
        babel_glossary = ROOT / "glossary" / "out" / f"babeldoc_{tag}.csv"
        rep.event({"event": "stage", "name": "glossary", "status": "done"})

    # ② 翻译（大文档分片：进度可见、失败只重一片、可续跑）
    rep.event({"event": "stage", "name": "translate", "status": "start"})
    common_args = [str(pdf), "--to", a.to, "--json"]
    if babel_glossary is not None:
        common_args += ["--glossary", str(babel_glossary)]
    if a.no_auto_glossary:
        common_args.append("--no-auto-glossary")

    import pymupdf as _pm
    total_pages = _pm.open(pdf).page_count
    target_pages = sorted(p for p in (parse_ranges(pages_arg) if pages_arg
                                      else range(1, total_pages + 1)) if 1 <= p <= total_pages)

    # 跑前预估：页数 × 实测系数（英→中 17s/页、约 2.5k token/页）
    est_min = max(1, round(len(target_pages) * 17 / 60))
    est_tok = len(target_pages) * 2500
    rep.human(f"预计：{len(target_pages)} 页 ≈ {est_min} 分钟 ≈ {est_tok / 10000:.1f} 万 token")
    rep.event({"event": "estimate", "pages": len(target_pages),
               "minutes": est_min, "tokens": est_tok})

    tokens: dict = {}
    rc = 0
    if a.chunk_size and len(target_pages) > a.chunk_size:
        ok, tokens, fresh = run_chunked(pdf, out, total_pages, target_pages,
                                        a.chunk_size, common_args, rep, not a.no_resume,
                                        to=a.to, parallel=a.parallel)
        rc = 0 if ok else 1
        duals: list[Path] = []
        gloss_csv = sorted(out.glob("*.glossary.csv"))
    else:
        t_args = list(common_args) + ["--out", str(out)]
        if pages_arg:
            t_args += ["--pages", pages_arg]
        rc, events = run_stream("run_babeldoc.py", t_args, rep)
        for ev in events:
            if ev.get("event") == "babeldoc_done":
                tokens = ev.get("tokens") or {}
        monos = sorted(out.glob("*.mono.pdf"))
        duals = sorted(out.glob("*.dual.pdf"))
        gloss_csv = sorted(out.glob("*.glossary.csv"))
        fresh = [f for f in monos if f.stat().st_mtime >= t0 - 1]
    rep.event({"event": "stage", "name": "translate", "status": "done", "code": rc})
    if rc != 0:
        rep.event({"event": "error", "stage": "translate", "message": f"BabelDOC 退出码 {rc}"})

    if rc != 0 or not fresh:
        msg = "BabelDOC 报错退出" if rc != 0 else "没有产出新的单语 PDF（目录里只有旧文件）"
        rep.human(f"翻译失败：{msg} -> {out}")
        rep.event({"event": "error", "stage": "translate", "message": msg})
        # 一片都没成功、目录里只剩空壳时清掉，免得用户对着空文件夹发愣；
        # 有分片成果就保留（下次能续跑）
        cleaned = False
        try:
            leftovers = [p for p in out.rglob("*") if p.is_file()]
            if not leftovers:
                import shutil as _sh
                _sh.rmtree(out, ignore_errors=True)
                cleaned = True
                rep.human("已清理空输出目录（下次重跑会重建）")
        except Exception:
            pass
        if not cleaned:
            _write_report(out, pdf, a, tag, tokens, None, t0, ok=False)
        return 4
    monos = fresh
    g_args = ["--glossary", str(glossary_src)] if glossary_src else []

    # ④ 自动修复：方向反转（英文页被翻成中文）、源文叠字导致的错译
    mono_final = monos[0]
    pairs = out / "pairs.jsonl"
    fix_count = 0
    if pairs.exists():
        rep.event({"event": "stage", "name": "fixups", "status": "start"})
        edits = out / "edits.csv"
        rc_fx = run_quiet("make_fixups.py", [
            "--pairs", str(pairs), "--source", str(pdf), "--to", a.to,
            *g_args, "--out", str(edits), "--retranslate",
        ], rep)
        if rc_fx == 0 and edits.exists() and len(edits.read_text(encoding="utf-8-sig").strip().splitlines()) > 1:
            fixed = out / f"{monos[0].stem.replace('.mono', '')}.fixed.pdf"
            rc_patch = run_quiet("patch_pdf.py", [str(monos[0]), str(edits), "-o", str(fixed)], rep)
            if rc_patch == 0 and fixed.exists():
                mono_final = fixed
                fix_count = len(edits.read_text(encoding="utf-8-sig").strip().splitlines()) - 1
        rep.event({"event": "stage", "name": "fixups", "status": "done",
                   "applied": fix_count, "output": str(mono_final)})

    # ③ 体检（对修复后的成品做）
    rep.event({"event": "stage", "name": "qa", "status": "start"})
    qa_json = out / "qa.json"
    rc_qa = run_quiet("qa_check.py", [
        "--source", str(pdf), "--translated", str(mono_final),
        *g_args, "--to", a.to,
        "-o", str(out / "qa.md"), "--json-summary", str(qa_json),
    ], rep)
    qa_summary = {}
    if qa_json.exists():
        qa_summary = json.loads(qa_json.read_text(encoding="utf-8"))
    rep.event({"event": "stage", "name": "qa", "status": "done", "code": rc_qa,
               "must_fix": qa_summary.get("must_fix"), "maybe": qa_summary.get("maybe")})

    # 第二轮：拿体检结果反查「残留未译」（标题这种太短的块第一轮抓不到）
    if qa_json.exists() and (qa_summary.get("must_fix") or qa_summary.get("maybe")):
        rep.event({"event": "stage", "name": "fixups2", "status": "start"})
        edits2 = out / "edits.qa.csv"
        # 只在本轮真正翻译过的页里找残留，否则会去动 201 页里没翻的部分
        page_arg: list[str] = []
        checked = qa_summary.get("checked_pages") or []
        if checked:
            page_arg = ["--pages", f"{min(checked)}-{max(checked)}"]
        rc2 = run_quiet("make_fixups.py", [
            "--pairs", str(pairs), "--source", str(pdf), "--to", a.to,
            *g_args, "--out", str(edits2),
            "--translated", str(mono_final), "--qa-json", str(qa_json), *page_arg,
        ], rep)
        rows2 = 0
        if edits2.exists():
            rows2 = max(0, len(edits2.read_text(encoding="utf-8-sig").strip().splitlines()) - 1)
        if rows2 > 200:
            rep.human(f"! 第二轮修订 {rows2} 条，超过安全上限，跳过自动修复（请人工看 qa.md）")
            rep.event({"event": "error", "stage": "fixups2",
                       "message": f"修订数异常（{rows2} 条），已跳过"})
        elif rc2 == 0 and rows2 > 0:
            round2 = out / "final2.pdf"
            if run_quiet("patch_pdf.py", [str(mono_final), str(edits2), "-o", str(round2)], rep) == 0 \
                    and round2.exists():
                mono_final = round2
                fix_count += len(edits2.read_text(encoding="utf-8-sig").strip().splitlines()) - 1
                run_quiet("qa_check.py", [
                    "--source", str(pdf), "--translated", str(mono_final),
                    *g_args, "--to", a.to,
                    "-o", str(out / "qa.md"), "--json-summary", str(qa_json),
                ], rep)
                qa_summary = json.loads(qa_json.read_text(encoding="utf-8"))
        rep.event({"event": "stage", "name": "fixups2", "status": "done",
                   "applied": fix_count, "must_fix": qa_summary.get("must_fix"),
                   "maybe": qa_summary.get("maybe")})

    # ⑤ 术语一致性（只报告，不自动改：统一措辞属于编辑决策）
    cons_json = out / "consistency.json"
    cons_summary: dict = {}
    if pairs.exists():
        rep.event({"event": "stage", "name": "consistency", "status": "start"})
        run_quiet("consistency.py", ["--pairs", str(pairs), "--top", "8",
                                     "--source", str(pdf),
                                     "--out", str(out / "consistency.md"),
                                     "--json-summary", str(cons_json)], rep)
        if cons_json.exists():
            cons_summary = json.loads(cons_json.read_text(encoding="utf-8"))
        rep.event({"event": "stage", "name": "consistency", "status": "done",
                   "terms": cons_summary.get("terms"),
                   "inconsistent": cons_summary.get("inconsistent")})

    # ⑥ 质量评分（只评估不改稿：成品 PDF 上做手术的风险大于收益，实测三次都被回滚拦下）
    rep.event({"event": "stage", "name": "score", "status": "start"})
    run_quiet("refine.py", ["--out-dir", str(out), "--report-only",
                            "--pdf", str(mono_final),
                            "--source", str(pdf), "--to", a.to], rep)
    rep.event({"event": "stage", "name": "score", "status": "done"})

    # 清掉上一轮遗留的派生成品：名字固定、本轮没重做就还留在目录里，
    # 用户点进去看到的是旧稿（实测有整本被打回英文的旧 final2.pdf）
    for stale in list(out.glob("final2.pdf")) + list(out.glob("*.fixed.pdf")):
        if stale == mono_final:
            continue
        try:
            if stale.stat().st_mtime < t0 - 1:
                stale.unlink()
        except OSError:
            pass

    report_path = _write_report(out, pdf, a, tag, tokens, qa_summary, t0, ok=True,
                                outputs={"mono": str(monos[0]),
                                         "final": str(mono_final),
                                         "fixups": fix_count,
                                         "dual": str(duals[0]) if duals else None,
                                         "glossary": str(gloss_csv[0]) if gloss_csv else None,
                                         "pairs": str(pairs) if pairs.exists() else None,
                                         "qa_md": str(out / "qa.md"),
                                         "consistency": str(out / "consistency.md"),
                                         "refine": str(out / "refine_report.md"),
                                         "qa_json": str(qa_json) if qa_json.exists() else None})

    rep.human(f"\n完成：{out}")
    for f in sorted(out.iterdir()):
        rep.human(f"  {f.name}  {round(f.stat().st_size / 1024)} KB")

    rep.event({
        "event": "done",
        "report": str(report_path),
        "outputs": {"final": str(mono_final),
                    "dual": str(duals[0]) if duals else None},
        "fixups": fix_count,
        "qa": {"must_fix": qa_summary.get("must_fix"), "maybe": qa_summary.get("maybe")},
        "consistency": {"terms": cons_summary.get("terms"),
                        "inconsistent": cons_summary.get("inconsistent")},
        "duration_s": round(time.time() - t0, 1),
    })
    return 0


def _write_report(out: Path, pdf: Path, a, tag: str, tokens: dict, qa: dict | None,
                  t0: float, ok: bool, outputs: dict | None = None) -> Path:
    import pymupdf  # 只在写报告时用，避免顶层依赖

    def pages_of(p: Path) -> int | None:
        try:
            d = pymupdf.open(p)
            n = d.page_count
            d.close()
            return n
        except Exception:
            return None

    report = {
        "ok": ok,
        "source": {"path": str(pdf), "pages": pages_of(pdf)},
        "target_lang": a.to,
        "direction": tag,
        "pages_requested": a.pages or "all",
        "glossary": str(a.glossary),
        "outputs": outputs or {},
        "tokens": tokens,
        "qa": qa or {},
        "duration_s": round(time.time() - t0, 1),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = out / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
