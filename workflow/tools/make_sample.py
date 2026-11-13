"""生成一份"像真论文"的英文测试 PDF：标题/正文/表格线/图片/公式/页脚/参考文献。

用途：在没有真实样本、也不通外网时，验证解析→翻译→排版整条链路。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

BODY = (
    "Intracranial pressure (ICP) monitoring is essential for patients with severe traumatic "
    "brain injury. The sensor accuracy was evaluated at 37 degrees Celsius using a calibrated "
    "pressure chamber. Each device was tested three times at 0, 20, and 50 mmHg. The measured "
    "drift was 0.8 mmHg per hour, which is within the acceptable range of 1.5 mmHg. "
    "All procedures followed the institutional review board protocol and written consent was "
    "obtained from each subject before the experiment started."
)

TABLE = [
    ["Sensor", "Accuracy", "Drift"],
    ["ICP-100", "0.5 mmHg", "0.2 mmHg/h"],
    ["ICP-200", "0.9 mmHg", "0.8 mmHg/h"],
    ["ICP-300", "1.2 mmHg", "1.1 mmHg/h"],
]

REFS = [
    "[1] Smith JA, Lee K. Long-term ICP monitoring in neurocritical care. J Neurosurg. 2021;134(3):112-119.",
    "[2] Wang L, et al. Calibration drift of piezoresistive pressure sensors. Sensors. 2019;19(8):1802.",
]


def build(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    def text(x, y, s, size=10, font="helv", color=(0, 0, 0)):
        page.insert_text((x, y), s, fontsize=size, fontname=font, color=color)

    text(56, 80, "Precision Measurement of Intracranial Pressure Sensors", 17, "hebo")
    text(56, 104, "J. Smith, K. Lee — Department of Biomedical Engineering", 9, color=(0.3, 0.3, 0.3))

    text(56, 140, "2.1 Test Protocol", 13, "hebo")
    page.insert_textbox(pymupdf.Rect(56, 152, 539, 268), BODY, fontsize=10, fontname="helv", lineheight=1.45)

    text(56, 292, "Table 1: Sensor accuracy at 37 degrees Celsius", 9.5, "heit")
    x0, y0, cw, rh = 56, 302, 160, 22
    for i, row in enumerate(TABLE):
        for j, cell in enumerate(row):
            r = pymupdf.Rect(x0 + j * cw, y0 + i * rh, x0 + (j + 1) * cw, y0 + (i + 1) * rh)
            page.draw_rect(r, color=(0.55, 0.55, 0.55), width=0.6)
            page.insert_textbox(r + (4, 6, -4, 0), cell, fontsize=9,
                                fontname="hebo" if i == 0 else "helv")

    page.insert_textbox(pymupdf.Rect(56, 410, 539, 440), "P = ρ g h + ΔP", fontsize=11,
                        fontname="china-s", align=1)

    # 图：在一个临时页上画网格，再作为位图贴进来（模拟"图片不译、但要保留"）
    tmp = pymupdf.open()
    tp = tmp.new_page(width=220, height=120)
    for i in range(0, 220, 20):
        tp.draw_line((i, 8), (i, 112), color=(0.27, 0.47, 0.75), width=2)
    for i in range(0, 120, 20):
        tp.draw_line((8, i), (212, i), color=(0.27, 0.47, 0.75), width=2)
    pix = tp.get_pixmap(dpi=150)
    page.insert_image(pymupdf.Rect(170, 455, 420, 590), pixmap=pix)
    tmp.close()
    text(220, 610, "Figure 1: Chamber pressure profile", 9.5, "heit")

    text(56, 660, "https://example.org/icp-protocol", 8.5, color=(0.2, 0.35, 0.7))
    text(56, 800, "Page 1 of 2", 8.5, color=(0.4, 0.4, 0.4))

    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((56, 80), "References", fontsize=13, fontname="hebo")
    y = 104
    for ref in REFS:
        p2.insert_textbox(pymupdf.Rect(56, y, 539, y + 46), ref, fontsize=9, fontname="helv")
        y += 50
    p2.insert_text((56, 800), "Page 2 of 2", fontsize=8.5, fontname="helv", color=(0.4, 0.4, 0.4))

    doc.save(str(out))
    doc.close()
    return out


if __name__ == "__main__":
    p = build(Path(__file__).resolve().parents[1] / "samples" / "sample_en.pdf")
    print(p)
