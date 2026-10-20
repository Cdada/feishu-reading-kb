"""Passive Markdown layout; only generated formatting reaches ReportLab."""
from html import escape

from markdown_it import MarkdownIt
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfgen.textobject import PDFTextObject
from reportlab.platypus import Paragraph, Spacer, XPreformatted

from .contracts import ContractError


class _PreviewText(PDFTextObject):
    """Embolden the approved CJK font without substituting missing glyphs.

    PDF fill-and-stroke keeps the original metrics, so wrapping and selectable
    text stay intact. Both font setters are used by pinned ReportLab 4.4.9.
    """
    def setFillColor(self, color, alpha=None):
        super().setFillColor(color, alpha=alpha)
        self.setStrokeColor(color, alpha=alpha)

    def _weight(self, name):
        self.setTextRenderMode(2 if name.endswith('-Strong') else 0)

    def _setFont(self, name, size):
        super()._setFont(name, size)
        self._weight(name)

    def setFont(self, name, size, leading=None):
        super().setFont(name, size, leading)
        self._weight(name)


class PreviewCanvas(Canvas):
    def beginText(self, x=0, y=0, direction=None):
        self.setLineWidth(0.28)
        text = _PreviewText(self, x, y, direction=direction)
        text.setStrokeColor(self._fillColorObj)
        return text


def _inline(tokens):
    parts, links = [], []
    for token in tokens:
        kind = token.type
        if kind in {'text', 'html_inline', 'code_inline'}:
            value = escape(token.content)
            parts.append('<span backColor="#F0F2F4">'+value+'</span>' if kind=='code_inline' else value)
        elif kind == 'strong_open': parts.append('<b>')
        elif kind == 'strong_close': parts.append('</b>')
        elif kind in {'softbreak', 'hardbreak'}: parts.append('<br/>')
        elif kind == 'link_open': links.append(token.attrGet('href') or '')
        elif kind == 'link_close':
            parts.append(' ('+escape(links.pop())+')')
        elif kind == 'image':
            parts.append(escape('['+token.content+'] ('+(token.attrGet('src') or '')+')'))
        elif kind in {'em_open', 'em_close'}:
            # Italic-only syntax remains visible until a compatible italic
            # CJK face is explicitly bound; never claim an absent font style.
            parts.append(escape(token.markup))
        else:
            raise ContractError('unsupported preview inline token: '+kind)
    return ''.join(parts)


def markdown_flowables(source, base, strong_font, check_text):
    """Render common guide structure; HTML, links and images stay passive."""
    check_text(source)
    tokens = MarkdownIt('commonmark', {'html': False}).parse(source)
    story, lists = [], []
    quote_depth, prefix, heading = 0, '', None
    for token in tokens:
        kind = token.type
        if kind in {'bullet_list_open', 'ordered_list_open'}:
            lists.append(dict(ordered=kind=='ordered_list_open',number=int(token.attrGet('start') or 1)))
        elif kind in {'bullet_list_close', 'ordered_list_close'}:
            lists.pop()
        elif kind == 'list_item_open':
            item = lists[-1]
            prefix = (str(item['number'])+'. ') if item['ordered'] else '- '
            item['number'] += 1
        elif kind == 'list_item_close': prefix = ''
        elif kind == 'blockquote_open': quote_depth += 1
        elif kind == 'blockquote_close': quote_depth -= 1
        elif kind == 'heading_open': heading = int(token.tag[1:])
        elif kind == 'heading_close': heading = None
        elif kind == 'inline':
            for child in token.children or []:
                check_text(child.content)
            indent = len(lists)*15 + quote_depth*14
            options = dict(leftIndent=indent,spaceAfter=8 if lists else base.spaceAfter)
            if heading:
                size = {1:17,2:15,3:13}.get(heading,12)
                options.update(fontName=strong_font,fontSize=size,leading=size*1.5,
                               spaceBefore=10,spaceAfter=8,keepWithNext=True)
            style = ParagraphStyle('markdown',parent=base,**options)
            story.append(Paragraph(escape(prefix)+_inline(token.children or []),style))
            prefix = ''
        elif kind in {'fence','code_block'}:
            style = ParagraphStyle('code',parent=base,fontSize=10,leading=16,
                                   leftIndent=12,backColor='#F0F2F4')
            story.append(XPreformatted(escape(token.content.rstrip('\n')),style))
        elif kind == 'hr': story.append(Spacer(1,12))
        elif kind in {'paragraph_open','paragraph_close'}: pass
        else:
            raise ContractError('unsupported preview block token: '+kind)
    return story
