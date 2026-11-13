r"""一句话翻译：给个 PDF，自动判方向，跑完整流水线。

存在的理由：用户在对话里甩一个 PDF 说「翻译一下」，不该还要他先判断中英、再想参数。
这个入口把「判方向 + 传参 + 跑」合成一步，脚本输出也保持简洁。

用法：
    python tools/translate_auto.py ..\paper.pdf
    python tools/translate_auto.py ..\paper.pdf --glossary glossary\粒子物理.csv --pages 1-10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from translate import detect_source_lang, main as translate_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="translate_auto")
    p.add_argument("pdf")
    p.add_argument("--glossary", default=None)
    p.add_argument("--pages", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)

    pdf = Path(a.pdf).expanduser().resolve()
    if not pdf.exists():
        print(f"找不到文件：{pdf}", file=sys.stderr)
        return 2

    src = detect_source_lang(pdf)
    to = "en" if src == "zh" else "zh"
    print(f"[auto] 源语言={src} → 目标={to}｜{pdf.name}")

    args = [str(pdf), "--to", to]
    if a.glossary:
        args += ["--glossary", a.glossary]
    if a.pages:
        args += ["--pages", a.pages]
    if a.out:
        args += ["--out", a.out]
    return translate_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
