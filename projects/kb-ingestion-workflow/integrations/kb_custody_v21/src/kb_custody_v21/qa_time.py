"""Explicit time meanings and bounded, zero-model clarification."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo
from .contracts import ContractError

CHOICES = {'按发表时间': 'publication', '按内容时间': 'content', '按入库时间': 'ingestion'}
LABELS = {'publication': '发表时间', 'content': '内容中提及的年份', 'ingestion': '完成入库时间'}
EXPLICIT = re.compile(r'(发表|内容|入库)\s*[:：]\s*(\d{4})(?:\s*[-—至到]\s*(\d{4}))?')
NATURAL = re.compile(r'(?<!\d)(\d{4})年(?:\s*[-—至到]\s*(\d{4})年?)?')

def extract(text, context, now):
    scope = None
    explicit = list(EXPLICIT.finditer(text))
    if len(explicit)>1:
        raise ContractError('一次查询请指定一个时间口径和区间。')
    found = explicit[0] if explicit else None
    natural = list(NATURAL.finditer(text))
    if found:
        scope = {'发表':'publication','内容':'content','入库':'ingestion'}[found[1]]
        a, z = int(found[2]), int(found[3] or found[2])
        text = text[:found.start()]+text[found.end():]
        text = NATURAL.sub('', text)
    elif natural:
        if len(natural) > 1:
            raise ContractError('请明确一个时间区间，例如“发表:2020-2025”。')
        found = natural[0]; a, z = int(found[1]), int(found[2] or found[1])
        text = text[:found.start()]+text[found.end():]
    else:
        found = re.search(r'(去年|今年|近[一二三四五六七八九十1-9]年|\d{2}世纪[0-9]0年代)', text)
        if not found:
            return text, {}
        year = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).year
        value = found[0]
        if '世纪' in value:
            century, decade = map(int,re.findall(r'\d+',value));a=(century-1)*100+decade;z=a+9
        elif value=='去年': a=z=year-1
        elif value=='今年': a=z=year
        else:
            number=value[1];count=int(number) if number.isdigit() else '一二三四五六七八九十'.index(number)+1
            a,z=year-count+1,year
        text=text[:found.start()]+text[found.end():]
    if not 1000 <= a <= z <= 9999:
        raise ContractError('年份区间无效。')
    if scope is None:
        if re.search(r'发表|出版|刊发',text): scope='publication'
        elif re.search(r'入库|归档',text): scope='ingestion'
        elif re.search(r'内容时间|文中提及',text): scope='content'
        else: scope=context.get('time_scope')
    return text, dict(year_from=a, year_to=z, time_scope=scope, needs_time=scope is None)

def choose(query, command):
    if command not in CHOICES or query.get('mode')!='clarify':
        raise ContractError('没有与该选项对应的时间澄清问题，请重新提问。')
    return dict(query,mode=query['original_mode'],time_scope=CHOICES[command],needs_time=False)

def prompt(query):
    return (f"你说的 {query['year_from']}–{query['year_to']} 年，是指资料发表、材料内容涉及，还是完成入库的时间？"
            '\n请点击下方选项。选择前不会调用模型。内容时间仅筛选正文提及的年份，不据此推定事件日期。')


def describe(text, query):
    if query.get('time_scope') == 'content':
        return '时间范围说明：以下仅为原文提及指定年份的片段，不代表已核定全部事件日期。\n\n'+text
    return text
