"""Owner-confirmed relations, portable graph, and recoverable reciprocal links."""
from copy import deepcopy
import hashlib
import json
import os

from .contracts import stable_key
from .reading_bundle import frontmatter, link, md
from .reading_contract import sha
from .reading_publish import read_at
from .registry import encoded
from .safe_publish import open_root, root_identity
from .relation_catalog import RelationProblem, need, read_json_at

REFERENCE = 'P42-relations-v1'
DIRECTORY = '40_Projects_专项引用/资料关联'
GRAPH = DIRECTORY+'/关系记录.json'
INDEX = DIRECTORY+'/资料关联.md'
HEADING = '\n\n## 已确认的资料关联\n\n'
KINDS = {'interpretation': ('原文', '解读'), 'excerpt': ('原文', '摘要或摘录'),
         'revision': ('旧版', '修订版'), 'topic': ('主题相关资料', '主题相关资料')}


def pair_id(left, right):
    need(left != right, '不能将资料与自身关联。')
    return stable_key(REFERENCE, *sorted((left, right)))


def empty_graph():
    return dict(reference=REFERENCE, revision=0, materials={}, edges={}, history=[])


def graph_at(fd):
    raw = read_at(fd, GRAPH, limit=4*1024*1024)
    if raw is None:
        return empty_graph(), None
    graph, raw = read_json_at(fd, GRAPH, 4*1024*1024)
    need(set(graph) == {'reference','revision','materials','edges','history'} and graph['reference'] == REFERENCE,
         '关联记录格式异常，请先核查。')
    need(type(graph['revision']) is int and graph['revision'] >= 0 and isinstance(graph['materials'], dict)
         and isinstance(graph['edges'], dict) and isinstance(graph['history'], list)
         and len(graph['history']) == graph['revision'], '关联修订历史不完整。')
    for key, edge in graph['edges'].items():
        need(isinstance(edge,dict) and set(edge)=={'left','right','kind','active','revision','operation_id'}, '关联条目格式异常。')
        need(all(isinstance(edge[k],str) for k in ('left','right','kind','operation_id'))
             and key == pair_id(edge['left'], edge['right']) and edge['kind'] in KINDS
             and type(edge['active']) is bool and edge['left'] in graph['materials'] and edge['right'] in graph['materials'],
             '关联两端身份或类型异常。')
    return graph, raw


def related(graph, material_id):
    result = []
    for pid, edge in sorted(graph['edges'].items()):
        if not edge['active'] or material_id not in (edge['left'], edge['right']):
            continue
        other = edge['right'] if material_id == edge['left'] else edge['left']
        label = KINDS[edge['kind']][0 if material_id == edge['left'] else 1]
        if label == '原文' and graph['materials'][other].get('is_paper'):
            label = '论文原文'
        result.append(dict(reference=REFERENCE, pair_id=pid, material_id=other,
                           relation=label, path=graph['materials'][other]['group']+'/来源卡片.md'))
    return result


def section(graph, material_id, path):
    rows = related(graph, material_id)
    body = '以下关系均由本人确认；两份原件和独立导读分别保留。\n\n'
    body += '\n'.join('- '+r['relation']+'：'+link(path,r['path'],graph['materials'][r['material_id']]['title']) for r in rows)
    if not rows:
        body += '当前没有已确认关联。'
    body += '\n\n'+link(path, INDEX, '资料关联目录')
    return body


def index_bytes(graph):
    body = '# 资料关联\n\n仅显示本人已确认且未撤销的关系；主题相关不表示同一作品或不同版本。\n'
    for pid, edge in sorted(graph['edges'].items()):
        if not edge['active']:
            continue
        left, right = (graph['materials'][edge[k]] for k in ('left','right'))
        a = link(INDEX,left['group']+'/来源卡片.md',left['title'])
        b = link(INDEX,right['group']+'/来源卡片.md',right['title'])
        original_label = KINDS[edge['kind']][0]
        if original_label == '原文' and right.get('is_paper'):
            original_label = '论文原文'
        body += '\n- '+KINDS[edge['kind']][1]+' '+a+' ↔ '+original_label+' '+b+'\n'
    if not any(v['active'] for v in graph['edges'].values()):
        body += '\n当前没有已确认关联。\n'
    return md(dict(type='material_relation_index', reference=REFERENCE, revision=graph['revision']), body)


def build_bundle(profile, catalog, *, operation_id, left, right, kind, remove,
                 expected_edge, evidence, checked_access):
    """Prepare exact bytes only; publish_plan serializes and verifies the write.

    left is the interpretation/excerpt/newer revision; right is its original/
    older revision. That direction must come from an explicit owner decision.
    """
    checked_access(); profile.check()
    need(kind in KINDS and type(remove) is bool and isinstance(evidence, dict) and evidence,
         '关联必须有明确类型和本人选择依据。')
    first, second = catalog.get(left), catalog.get(right)
    pid = pair_id(left, right)
    fd = open_root(profile.root, root_identity(profile.root))
    try:
        before, graph_raw = graph_at(fd)
        need(not any(h['operation_id'] == operation_id for h in before['history']), '此关联决定已执行，请读取原结果。')
        old_edge = before['edges'].get(pid)
        need((sha(old_edge) if old_edge is not None else None) == expected_edge, '关联版本已变化，请使用最新选项卡。')
        need(not remove or old_edge and old_edge['active'], '没有可撤销的有效关联。')
        after = deepcopy(before)
        for item in (first, second):
            portable = {k:item[k] for k in ('material_id','group','title','source_sha256','is_paper')}
            previous = before['materials'].get(item['material_id'])
            need(previous is None or previous == portable, '关联资料的路径或身份已变化，请先核查。')
            after['materials'][item['material_id']] = portable
        after['revision'] += 1
        edge = dict(left=left, right=right, kind=kind, active=not remove,
                    revision=after['revision'], operation_id=operation_id)
        after['edges'][pid] = edge
        # Detailed platform receipts remain private; the portable graph carries
        # stable evidence hashes and user-visible choice, never local secrets.
        after['history'].append(dict(operation_id=operation_id, pair_id=pid,
            revision=after['revision'], before=old_edge, after=edge,
            evidence_sha256=sha(evidence), command=evidence['command']))
        files, expected, generated = {}, {}, []
        for item in (first, second):
            mid, group = item['material_id'], item['group']
            path = group+'/来源卡片.md'
            raw = read_at(fd, path, limit=1024*1024)
            need(raw is not None, '来源卡片缺失，无法建立双向链接。')
            meta, body = frontmatter(raw.decode())
            need(meta.get('material_id') == mid and meta.get('binding') == item['binding'], '来源卡片身份已变化。')
            if HEADING in body:
                need(body.count(HEADING) == 1, '来源卡片关联章节重复。')
                body, old_section = body.split(HEADING)
                need(old_section.strip() == section(before,mid,path).strip(), '来源卡片关联章节被修改，已保留改动。')
            else:
                need(not related(before, mid), '来源卡片关联章节缺失。')
            old_related = meta.get('related', [])
            need(isinstance(old_related, list), '来源卡片 related 必须为列表。')
            need([v for v in old_related if isinstance(v,dict) and v.get('reference')==REFERENCE]==related(before,mid),
                 '来源卡片关联元数据已修改，已保留改动。')
            meta['related'] = [v for v in old_related if not isinstance(v,dict) or v.get('reference') != REFERENCE]+related(after,mid)
            try:
                files[path] = md(meta, body.rstrip()+HEADING+section(after,mid,path))
            except (TypeError, ValueError) as error:
                # Handwritten YAML may contain dates or sets that the JSON-
                # compatible source-card renderer cannot preserve losslessly.
                raise RelationProblem('来源卡片元数据无法自动改写，已保留原文件，请先核查。') from error
            generated.append(path)
            expected[path] = hashlib.sha256(raw).hexdigest()
            metadata_path = group+'/metadata.json'
            metadata, original = read_json_at(fd, metadata_path)
            existing = metadata.get('related', [])
            need(isinstance(existing, list), '资料 related 必须为列表。')
            need([v for v in existing if isinstance(v,dict) and v.get('reference')==REFERENCE]==related(before,mid),
                 '资料关联元数据已修改，已保留改动。')
            metadata['related'] = [v for v in existing if not isinstance(v,dict) or v.get('reference') != REFERENCE]+related(after,mid)
            files[metadata_path] = (encoded(metadata)+'\n').encode()
            expected[metadata_path] = hashlib.sha256(original).hexdigest()
        old_index = read_at(fd, INDEX, limit=1024*1024)
        need(old_index == (index_bytes(before) if graph_raw else None), '关联目录有额外改动，已保留，请先核查。')
        files[INDEX] = index_bytes(after); generated.append(INDEX)
        files[GRAPH] = (encoded(after)+'\n').encode()
        need(all(len(raw) <= (4*1024*1024 if path==GRAPH else 1024*1024) for path,raw in files.items()),
             '关联文件超过本版大小范围，本次未写入，需先缩小范围。')
        for path, raw in ((GRAPH,graph_raw),(INDEX,old_index)):
            expected[path] = hashlib.sha256(raw).hexdigest() if raw is not None else None
        available = [v['group']+'/来源卡片.md' for v in after['materials'].values()]
        return dict(files=files, expected=expected, generated=generated, available_paths=available,
                    metadata_path=GRAPH, group=DIRECTORY, review_receipt=sha(evidence),
                    input_sha256=sha(dict(left=first,right=second,expected_edge=expected_edge)))
    finally:
        os.close(fd)
