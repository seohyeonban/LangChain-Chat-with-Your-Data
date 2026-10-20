"""sample1.pdf(KCC 2단 양식)를 측정한 값으로 논문 docx를 만든다.
측정값: A4, 좌우 여백 약 1cm, 2단(단 너비 약 9cm, 간격 약 0.8cm), 굴림 10pt 본문 줄 간격 약 13.5pt,
제목 16pt, 영문 제목 14pt, '요 약' 10pt 가운데, 초록 9pt(좌우 안쪽 들여쓰기), 그림 캡션 '그림 1 ...' 가운데.
"""
import re, sys
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

FONT = '굴림'
doc = Document()

# ---------- 공통 스타일 ----------
st = doc.styles['Normal']
st.font.name = FONT; st.font.size = Pt(10)
st.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), FONT)
pf = st.paragraph_format
pf.space_before = pf.space_after = Pt(0)
pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY; pf.line_spacing = Pt(13.5)

def setfont(run, size=None, bold=None, italic=None, mono=False):
    name = 'Consolas' if mono else FONT
    run.font.name = name
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), FONT)
    if size: run.font.size = Pt(size)
    if bold is not None: run.bold = bold
    if italic is not None: run.italic = italic

def add_runs(p, text, size=None):
    """**굵게**, `코드`, *기울임* 마크다운을 run으로 변환"""
    for tok in re.split(r'(\*\*.+?\*\*|`.+?`)', text):
        if not tok: continue
        if tok.startswith('**'): setfont(p.add_run(tok[2:-2]), size, bold=True)
        elif tok.startswith('`'): setfont(p.add_run(tok[1:-1]), (size or 10) - 1, mono=True)
        else: setfont(p.add_run(tok), size)

def para(text='', align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=None, indent_first=True, left=0, right=0, before=0, after=0, keep=False):
    p = doc.add_paragraph()
    if align == WD_ALIGN_PARAGRAPH.JUSTIFY and any(len(m) > 22 for m in re.findall(r'`(.+?)`', text)):
        align = WD_ALIGN_PARAGRAPH.LEFT
    p.alignment = align
    f = p.paragraph_format
    f.space_before, f.space_after = Pt(before), Pt(after)
    if indent_first: f.first_line_indent = Pt(10)
    if left: f.left_indent = Pt(left)
    if right: f.right_indent = Pt(right)
    if keep: f.keep_with_next = True
    add_runs(p, text, size)
    return p

def heading(text):
    p = para(text, align=WD_ALIGN_PARAGRAPH.LEFT, indent_first=False, before=13.5, after=6.75, keep=True)
    return p

# ---------- 1쪽 상단(단일 단) ----------
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
sec.left_margin = sec.right_margin = Cm(1.0)
sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(2.5)

def title_block(ko, en, abstract):
    p = para(ko, align=WD_ALIGN_PARAGRAPH.CENTER, size=16, indent_first=False, before=14)
    for r in p.runs: r.bold = False
    para('', indent_first=False)
    p = para(en, align=WD_ALIGN_PARAGRAPH.CENTER, size=14, indent_first=False, before=18)
    for r in p.runs: setfont(r, 14)
    para('', indent_first=False, before=10)
    para('요   약', align=WD_ALIGN_PARAGRAPH.CENTER, indent_first=False, before=6, after=4, keep=True)
    para(abstract, size=9, left=60 - 28, right=71 - 28, after=10)

# ---------- 본문 마크다운 읽기 ----------
md = open('paper.md', encoding='utf-8').read()
body = md.split('---', 2)[2]
ko = re.search(r'^title: "(.+)"', md, re.M).group(1)
en = re.search(r'^subtitle: "(.+)"', md, re.M).group(1)

blocks = [b.strip('\n') for b in re.split(r'\n\s*\n', body.strip())]
i = 0
title_done = False
two_col = False

def start_two_columns():
    s = doc.add_section(WD_SECTION.CONTINUOUS)
    s.left_margin = s.right_margin = Cm(1.0)
    cols = s._sectPr.xpath('./w:cols')
    c = cols[0] if cols else OxmlElement('w:cols')
    c.set(qn('w:num'), '2'); c.set(qn('w:space'), str(int(0.8 * 567)))  # 0.8cm
    if not cols: s._sectPr.append(c)

def shade_border(tbl):
    b = OxmlElement('w:tblBorders')
    for e in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        x = OxmlElement(f'w:{e}'); x.set(qn('w:val'), 'single'); x.set(qn('w:sz'), '4'); x.set(qn('w:color'), '808080'); b.append(x)
    tbl._tbl.tblPr.append(b)

def add_table(lines):
    rows = [[c.strip() for c in l.strip().strip('|').split('|')] for l in lines if not re.match(r'^\|?\s*[-:| ]+\|?$', l)]
    if rows[0][0] == '구분':
        rows[0] = ['구분', '대상', '이동 확인', '미검증', '존재 확인', '분석 오류']
        widths = [1.5, 1.0, 1.6, 1.5, 1.7, 1.7]
    elif rows[0][0] == '단계':
        widths = [1.6, 5.2, 2.2]
    else:
        widths = [3.4, 2.1, 3.5]
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    shade_border(t)
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            c = t.cell(ri, ci)
            c.width = Cm(widths[ci])
            c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
            c.paragraphs[0].paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            add_runs(c.paragraphs[0], cell, 7.5)
            if ri == 0:
                for r in c.paragraphs[0].runs: r.bold = True
    para('', indent_first=False, size=4)

# ---- 표 1: 단계·입력·처리·출력·이유를 좁은 단에 맞게 3열로 재구성 ----
def table1_lines(lines):
    rows = [[c.strip() for c in l.strip().strip('|').split('|')] for l in lines if not re.match(r'^\|?\s*[-:| ]+\|?$', l)]
    out = ['| 단계 | 입력 → 처리 → 출력 | 분리 이유 |', '|---|---|---|']
    for r in rows[1:]:
        out.append(f'| {r[0]} | 입력: {r[1]} / 처리: {r[2]} / 출력: {r[3]} | {r[4]} |')
    return out

while i < len(blocks):
    b = blocks[i]; i += 1
    if b.startswith('## '):
        h = b[3:].strip()
        if h == '요 약':
            abstract = blocks[i].strip(); i += 1
            title_block(ko, en, abstract)
            start_two_columns(); two_col = True
            continue
        label = {'1. 서 론': '1. 서   론', '참고문헌': '참  고  문  헌'}.get(h, h)
        if h == '5. 논의 및 결론': label = '5. 결론 및 향후 연구'
        p = heading(label)
        if h == '참고문헌': p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        continue
    if b.startswith('![') :
        cap = re.match(r'!\[(.+?)\]\((.+?)\)', b)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        p.add_run().add_picture(cap.group(2), width=Cm(8.8))
        c = para(cap.group(1).replace('그림 1.', '그림 1'), align=WD_ALIGN_PARAGRAPH.CENTER, indent_first=False, size=9, after=6)
        continue
    if b.startswith('표 ') and not b.startswith('표 1은'):
        p = para(b.replace('표 1.', '표 1').replace('표 2.', '표 2').replace('표 3.', '표 3').replace('표 4.', '표 4'), align=WD_ALIGN_PARAGRAPH.CENTER, indent_first=False, size=9, before=4, after=3, keep=True)
        continue
    if b.startswith('|'):
        lines = b.split('\n')
        if '분리 이유' in lines[0]: lines = table1_lines(lines)
        add_table(lines)
        continue
    if re.match(r'^\[\d\]', b):
        p = para(b, align=WD_ALIGN_PARAGRAPH.LEFT, indent_first=False, size=9, left=18, after=3)
        p.paragraph_format.first_line_indent = Pt(-18)
        continue
    para(b.replace('\n', ' '))

doc.save('paper_kcc.docx')
print('saved')
