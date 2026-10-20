"""Explicit commands, bounded context and deterministic citation rendering."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from .contracts import ContractError

HELP = ('可以发送：\n问库：查文献 城市演变\n问库：查出处 “原文中的一句话”\n'
        '问库：综述 城市演变\n问库：工具 数据整理\n问库：比较 1,2 适用条件\n'
        '问库：追问 这一观点有什么限制\n问库：本周周报\n问库：新问题\n'
        '时间筛选：发表:2010-2020；周报：周报 2026-01-01 至 2026-01-07。'
        '\n只查询已完成整理的测试库；资料编号沿用最近一次结果，引用原文保留原字形。')
MODES = {'查文献':'search', '查出处':'source', '综述':'answer', '工具':'tools', '比较':'compare', '追问':'followup'}


def parse(text, context=None):
    context = context or {}
    if not isinstance(text, str) or len(text) > 2000:
        raise ContractError('问题请控制在 2000 字内。')
    text = re.sub(r'^问库\s*[:：]\s*', '', text.strip())
    if text in ('帮助', '菜单', '问库', ''):
        return dict(mode='help', question='', selected=[])
    if text in ('新问题', '重新开始'):
        return dict(mode='reset', question='', selected=[])
    if text.startswith(('本周周报', '上周周报', '周报')):
        return dict(mode='weekly', question=text, selected=[])
    mode = 'search' if re.search(r'哪些.*文献|相关.*文献|有哪些.*资料', text) else 'source' if re.search(r'哪[份篇个].*(?:文件|材料|文档)|出处', text) else 'answer'
    for prefix, value in MODES.items():
        if text.startswith(prefix):
            mode, text = value, text[len(prefix):].lstrip(' ：:')
            break
    if not text:
        raise ContractError('请在功能名称后写上要查的概念或问题。')
    selected = []
    if mode == 'compare':
        found = re.match(r'([1-9][0-9]?(?:\s*[,，、]\s*[1-9][0-9]?){1,3})\s+(.+)', text)
        if not found:
            raise ContractError('请先查文献，再输入“问库：比较 1,2 你的问题”，每次比较 2–4 份。')
        ids = context.get('materials', [])
        numbers = [int(v) for v in re.split(r'\s*[,，、]\s*', found[1])]
        if len(set(numbers)) != len(numbers) or any(n > len(ids) for n in numbers):
            raise ContractError('资料编号不在最近一次结果中，请先重新查文献。')
        selected, text = [ids[n-1] for n in numbers], found[2]
    if mode == 'followup':
        if not context.get('question'):
            raise ContractError('当前没有可追问的问题，请先发送一个完整问题。')
        selected = context.get('selected', [])
    dates = re.search(r'发表\s*[:：]\s*(\d{4})(?:\s*[-—至]\s*(\d{4}))?', text)
    year_from = year_to = None
    if dates:
        year_from, year_to = int(dates[1]), int(dates[2] or dates[1])
        if not 1000 <= year_from <= year_to <= 9999:
            raise ContractError('发表年份区间无效。')
        text = text[:dates.start()]+text[dates.end():]
    elif mode == 'followup':
        year_from, year_to = context.get('year_from'), context.get('year_to')
    if re.search(r'最新|现行|目前有效|现在有效', text) and not selected:
        return dict(mode='version', question=text, selected=[], year_from=year_from, year_to=year_to)
    # User question history is retrieval context, never factual evidence.
    query = (context.get('question', '')+' '+text if mode in ('followup', 'compare') else text)
    query = re.sub(r'请|帮我|目前|关于|有哪些|相关的|相关|文献|研究成果|相应的|阐述|知识库|记录了哪些|是什么|这个问题|怎么|如何', ' ', query)
    if mode == 'tools':
        query += ' 工具 软件'
    return dict(mode=mode, question=text.strip(), query=query.strip(), selected=selected,
                year_from=year_from, year_to=year_to,
                prior_question=context.get('question', '') if mode == 'followup' else '')


def citation(hit, number):
    kind = '论文原文' if hit['is_paper'] else '来源材料（未认定为论文原文）'
    if hit.get('document_role') in ('secondary_summary', 'abstract', 'notes'):
        kind = {'secondary_summary':'摘要/解读', 'abstract':'摘要', 'notes':'笔记'}[hit['document_role']]
    year = str(hit['publication_year']) if hit['publication_year'] else '发表时间未记录'
    where = ('“'+hit['heading']+'” · ' if hit['heading'] else '')+f"第 {hit['paragraph']} 段，字符 {hit['char_start']}–{hit['char_end']}"
    if hit.get('physical_pages'):
        where = 'PDF 物理页 '+','.join(map(str, hit['physical_pages']))+' · '+where
    return f"[{number}] {hit['title']}｜{kind}｜{year}\n{where}\n版本 {hit['body_sha256'][:12]}"


def render_search(hits, *, version=False):
    if not hits:
        return '当前检索范围内未找到可引用的原文。可以缩短关键词、换用同义词或扩大发表年份；这不代表库中一定没有相关资料。'
    result = ['找到以下原文依据（按检索相关性排序）：']
    seen = set()
    for hit in hits:
        if hit['material_id'] in seen:
            continue
        seen.add(hit['material_id'])
        result.append(citation(hit, len(seen))+'\n摘录：'+hit['text'][:360])
        if len(seen) == 8:
            break
    if version:
        result.append('现行或最新版本尚未核定。入库先后不能证明有效性；请指定篇名或先比较这些资料。')
    result.append('仅为检索摘录；尚未完成跨材料综述。可用“问库：比较 1,2 你的问题”选择资料。')
    return '\n\n'.join(result)


def weekly_window(text, now):
    now = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=now.weekday())
    end = now
    if text.startswith('上周'):
        end, start = start, start-timedelta(days=7)
    dates = re.search(r'(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})', text)
    if dates:
        try:
            start = datetime.fromisoformat(dates[1]).replace(tzinfo=now.tzinfo)
            end = datetime.fromisoformat(dates[2]).replace(tzinfo=now.tzinfo)+timedelta(days=1)
        except ValueError as error:
            raise ContractError('周报日期无效。') from error
        if not 0 < (end-start).days <= 93 or end > now+timedelta(days=1):
            raise ContractError('周报区间须为过去 1–93 天。')
        end = min(end, now)
    elif text not in ('本周周报', '上周周报', '周报'):
        raise ContractError('周报请用“本周周报”“上周周报”或“周报 YYYY-MM-DD 至 YYYY-MM-DD”。')
    return start, end


def render_weekly(materials, text, now):
    start, end = weekly_window(text, now)
    visible = [m for m in materials if m['brief_visibility']=='include' and start.timestamp() <= m['ingested_at'] < end.timestamp()]
    visible.sort(key=lambda m: (m['ingested_at'], m['material_id']))
    lines = [f"知识积累周报：{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（北京时间）",
             f'期间新增完成整理的可展示资料：{len(visible)} 份。']
    for i, m in enumerate(visible[:40], 1):
        lines.append(f"{i}. {m['title']}；关键词："+'、'.join(m['keywords'][:6]))
    if len(visible) > 40:
        lines.append('本次展示前 40 份，请缩小日期范围查看其余资料。')
    lines.append('统计依据为完成整理事件；不代表你已阅读，也不代表项目工作完成。隐私及禁止简报的资料不进入展示或统计。')
    return '\n\n'.join(lines)
