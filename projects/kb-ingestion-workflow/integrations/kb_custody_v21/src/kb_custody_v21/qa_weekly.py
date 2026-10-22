"""Deterministic weekly themes and bounded evidence for optional synthesis."""
from collections import defaultdict
import re

from .contracts import ContractError
from .qa_query import weekly_window, render_weekly

FIELDS=('material_id','title','keywords','body_sha256','binding','brief_visibility','ingested_at')


def plan(materials, text, now):
    start,end=weekly_window(text,now)
    visible=[m for m in materials if m['brief_visibility']=='include' and start.timestamp()<=m['ingested_at']<end.timestamp()]
    visible.sort(key=lambda m:(m['ingested_at'],m['material_id']))
    older={word for m in materials if m['brief_visibility']=='include' and m['ingested_at']<start.timestamp() for word in m['keywords']}
    topics=defaultdict(set)
    for m in visible:
        for word in m['keywords'][:6]:
            topics[word].add(m['material_id'])
    lines=[render_weekly(materials,text,now)]
    if topics:
        lines.append('本期主题（按已记录关键词汇总）：')
        for word,ids in sorted(topics.items(),key=lambda item:(-len(item[1]),item[0]))[:8]:
            fresh='；本期首次在可展示的库内记录中出现' if word not in older else ''
            lines.append(f'- {word}：{len(ids)} 份资料'+fresh)
        lines.append('关键词出现情况不等于研究突破或项目进展。可选择“周报解读”生成附原文引用的观点总结，将按现有额度调用一次模型。')
    return dict(text='\n\n'.join(lines),question=text,at=now,
                sources=[{key:m[key] for key in FIELDS} for m in visible])


def verify(snapshot, sources):
    # A privacy change between planning and delivery invalidates the report,
    # including titles/counts which may not be part of the model's evidence.
    for saved in snapshot['sources']:
        current=sources.get(saved['material_id'])
        if {key:current[key] for key in FIELDS} != saved:
            raise ContractError('周报资料或展示权限已变化，请重新生成。')


def evidence(snapshot, index):
    chosen=snapshot['sources'][:8]
    first=[];extra=[]
    for source in chosen:
        # Each source is explicitly selected. Queries never consult private
        # aliases or expand the report beyond its visible time window.
        query=' '.join(source['keywords'][:6])+' '+source['title']
        candidates=index.search(query,material_ids=[source['material_id']],limit=40,expand=False,body_only=True)
        # Keep short substantive statements and footnotes. Skip chunks made
        # entirely of Markdown headings, page numbers or punctuation; do not
        # alter source text/offsets used for exact quotation validation.
        hits=[h for h in candidates if not re.fullmatch(r'---[ \t]*\n.*?\n---[ \t]*',h['text'].strip(),re.S)
              and any(not re.match(r'^\s*#{1,6}(?:\s|$)',line) and re.search(r'[^\W\d_]',line)
                      for line in h['text'].splitlines())]
        # Prefer complete statements to bare titles/author lines. Keep relevant
        # unpunctuated notes available when no sentence evidence is present.
        hits.sort(key=lambda h:not re.search(r'[。！？.!?；;]',h['text']))
        first.extend(hits[:1]);extra.extend(hits[1:2])
    return (first+extra)[:12]


def scope(snapshot, hits):
    covered={h['material_id'] for h in hits}
    return (f"观点整理使用本期 {len(snapshot['sources'])} 份可展示资料中的 {len(covered)} 份、"
            f"{len(hits)} 个原文片段；未逐篇阅读全文。")
