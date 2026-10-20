"""Passive local reading PDF. Only parsed Markdown formatting becomes PDF markup."""
from html import escape
from io import BytesIO
from pathlib import Path
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from .contracts import ContractError
from .conversion import sha
from .preview_markdown import PreviewCanvas, markdown_flowables


def render_preview(record, guide, coverage, *, font_path, limitations=(), image_notes=None):
    font_path = Path(font_path)
    # Apple's approved collection contains TC at index 0 and SC at index 1.
    # Choosing a face changes glyph shapes, never the source Unicode text.
    simplified_heiti = font_path.name == 'STHeiti Light.ttc'
    face_index = 1 if simplified_heiti else 0
    font_name = 'KB-' + sha(font_path.read_bytes())[:12] + '-' + str(face_index)
    if font_name not in pdfmetrics.getRegisteredFontNames():
        font = TTFont(font_name, str(font_path), subfontIndex=face_index)
        if simplified_heiti and font.face.name != b'STHeitiSC-Light':
            raise ContractError('preview requires the simplified Chinese STHeiti face')
        pdfmetrics.registerFont(font)
    strong_font = font_name + '-Strong'
    if strong_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(strong_font, str(font_path), subfontIndex=face_index))
    pdfmetrics.registerFontFamily(font_name, normal=font_name, bold=strong_font,
                                  italic=font_name, boldItalic=strong_font)
    glyphs = pdfmetrics.getFont(font_name).face.charToGlyph
    styles = dict(title=ParagraphStyle('title',fontName=font_name,fontSize=20,leading=29,spaceAfter=14),
        meta=ParagraphStyle('meta',fontName=font_name,fontSize=10,leading=16,textColor=colors.HexColor('#56606B'),spaceAfter=15),
        body=ParagraphStyle('body',fontName=font_name,fontSize=11,leading=19,wordWrap='CJK',spaceAfter=13,
                            allowWidows=0,allowOrphans=0),
        decision=ParagraphStyle('decision',fontName=font_name,fontSize=11,leading=19,spaceBefore=15,textColor=colors.HexColor('#214B5A')))
    story = []
    def check_text(text):
        if any(not ch.isspace() and ord(ch) not in glyphs for ch in text):
            raise ContractError('preview font cannot display every character')
    def add(text, kind='body'):
        check_text(text)
        story.append(Paragraph(escape(text).replace('\n','<br/>'), styles[kind]))
    add(record['work']['title'], 'title')
    authors = '、'.join(p['name'] for p in record['work']['authors']) or '作者未明'
    publication = record['work']['publication']
    add(authors + (' · ' + publication['name'] if publication and publication['name'] else ''), 'meta')
    if image_notes:
        add('正文导读的阅读范围', 'decision')
    add(coverage)
    story.extend(markdown_flowables(guide, styles['body'], strong_font, check_text))
    if image_notes:
        add('独立图像识读记录', 'decision')
        add(record['source_reading']['figures']['coverage_note'])
        story.extend(markdown_flowables(image_notes, styles['body'], strong_font, check_text))
    boundaries = [item['description'] for item in record['evidence_scope']['explicit_constraints']]
    if record['completeness']['relative_to_work'] != 'full':
        boundaries.append('当前材料与完整作品的覆盖关系尚需核对。')
    if boundaries: add(' '.join(boundaries))
    if limitations:
        add('当前材料的局限', 'decision')
        for limitation in limitations:
            add(limitation)
        add('回复“确认当前版本入库”表示接受以上局限，并批准这份预览对应的版本。'
            '也可回复“补充材料后再入库”或“暂不入库”。', 'decision')
    else:
        add('请决定：确认入库 / 补充材料后再入库 / 暂不入库。', 'decision')
    output = BytesIO()
    doc = SimpleDocTemplate(output,pagesize=(595.28,841.89),leftMargin=58,rightMargin=58,topMargin=54,bottomMargin=54,
                            title=record['work']['title'],author='', invariant=1)
    def footer(canvas, document):
        canvas.setFont(font_name,9)
        canvas.setFillColor(colors.HexColor('#69717A'))
        canvas.drawRightString(537,30,str(document.page))
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=PreviewCanvas)
    return output.getvalue()
