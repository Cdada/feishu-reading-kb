"""Finite F01 fixture flow. No credentials, network or scheduler on import.

This explicitly exercises bot-to-bot synthetic transport. It does not compose
the daily user-entry gateway or claim a model-generated reading guide.
"""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path

from .content import empty_record
from .contracts import ContractError, ENTRY_SCHEMA
from .conversion import CustodyConversion
from .f01 import sha
from .preview import render_preview
from .registry import Registry
from .runtime import Runtime, DEFAULTS
from .staging import Staging

TXT = '街道树荫影响步行路线。\n这是一份合成测试材料。\n'.encode()
MD = '# 合成验收材料\n\n街道树荫影响步行路线。\n这份文字只用于独立测试群的文件传输验证。\n'.encode()
NOTIFICATIONS = ['合成 TXT 已在独立 DEV 接管；转换因正文偏短需人工复核，未入库。',
                 '合成 Markdown 已在独立 DEV 接管并完成本地转换。']
PNG_SHA = '46c334467255ae9564c13280118776f5daf9d3985f7fb6bc183a8ed354a2fbf6'


def synthetic_preview(body, font_path):
    if body != MD:
        raise ContractError('preview input must equal frozen synthetic Markdown')
    record = empty_record('街道树荫与步行路线：合成验收样例', 'synthetic-test')
    return render_preview(record,
        '本页为合成文件传输验收样例，未使用模型生成导读。\n\n' + body.decode(),
        '覆盖范围：本次合成 Markdown 的全部文字。', font_path=font_path)


def fixtures(png, font_path):
    if len(png) != 2760 or sha(png) != PNG_SHA:
        raise ContractError('only the previously approved synthetic geometry PNG is allowed')
    result = {'fixture.txt': TXT, 'fixture.md': MD, 'geometry.png': png,
              'preview.pdf': synthetic_preview(MD, font_path)}
    if any(len(data) > 1024 * 1024 for data in result.values()):
        raise ContractError('synthetic fixture exceeds F01 size ceiling')
    return result


def convert_download(worktree, root, name, data, message):
    """Use the existing registry/staging/worker on verified downloaded bytes."""
    if (name, data) not in [('fixture.txt', TXT), ('fixture.md', MD)]:
        raise ContractError('conversion accepts only exact F01 synthetic inputs')
    runtime = Runtime(worktree, DEFAULTS, root)
    registry = Registry(runtime)
    row = registry.register(dict(schema_version=ENTRY_SCHEMA, intake_key='F01-synthetic',
        entry_key=name, received_filename=name,
        lineage={'message_id': message['message_id']},
        expected={'size_bytes': len(data)}))
    @contextmanager
    def source():
        with BytesIO(data) as stream:
            yield stream
    receipt = Staging(registry).ingest(row['entry_id'], source)
    if receipt['source_staging_status'] != 'accepted':
        raise ContractError('synthetic original was not accepted')
    conversion = CustodyConversion(registry)
    inspected = conversion.inspect(row['entry_id'])
    task = conversion.submit(inspected['inspection_id'], 'F01-' + name)
    conversion.worker.run_once()
    converted = conversion.service.ledger.get_task(task['task_id'])
    expected_status = 'needs_review' if name == 'fixture.txt' else 'ready_candidate'
    if converted['status'] != expected_status:
        raise ContractError('synthetic conversion differs from expected quality gate')
    mapping = json.loads(converted['quality_json'])['extraction']
    _, _, extraction = conversion.extraction(mapping['extraction_id'])
    body = (extraction / 'source.md').read_bytes()
    expected_body = b'# fixture\n\n' + TXT if name == 'fixture.txt' else MD
    if body != expected_body:
        raise ContractError('conversion changed the frozen synthetic body')
    return body, {'entry_id': row['entry_id'], 'mapping': mapping,
                  'source_sha256': sha(data), 'body_sha256': sha(body), 'status': converted['status']}


def completed_steps(client):
    """Read durable milestones, including after a later validation stops.

    These describe only completed local checks, never overall acceptance or
    permission to resume a consumed experiment.
    """
    return {kind: {name: value for name in names
                   if (value := client.value('verified-' + kind + ':' + name)) is not None}
            for kind, names in (
                ('downloads', ('fixture.txt', 'fixture.md', 'geometry.png', 'preview.pdf')),
                ('conversions', ('fixture.txt', 'fixture.md')))}


def run(client, worktree, root, frozen, font_path, *, reuse_known_txt=False):
    """Caller must authenticate once; every write batch rechecks membership."""
    client.establish()
    conversions = {}
    for index, name in enumerate(('fixture.txt', 'fixture.md', 'geometry.png')):
        client.verify('input-' + str(index))
        data = frozen[name]
        if reuse_known_txt and name == 'fixture.txt':
            if client.value('reused_known_message') != name:
                raise ContractError('validated known TXT receipt binding required')
            message = client.value('message:' + name)
        else:
            client.upload(name, data, image=name.endswith('.png'))
            message = client.send(name)
        downloaded = client.download(name)
        client.bind('verified-downloads:' + name, {'sha256': sha(downloaded), 'bytes': len(downloaded)})
        (client.root / ('download-' + name)).write_bytes(downloaded)
        if name != 'geometry.png':
            body, conversions[name] = convert_download(worktree, root, name, downloaded, message)
            client.bind('verified-conversions:' + name, conversions[name])
            client.send('notification-' + str(index), text=NOTIFICATIONS[index])
            if name == 'fixture.md':
                # Render again from the actual conversion, then require the
                # same frozen bytes that were reviewed before any upload.
                if synthetic_preview(body, font_path) != frozen['preview.pdf']:
                    raise ContractError('live conversion preview differs from frozen PDF')
    client.verify('preview')
    client.upload('preview.pdf', frozen['preview.pdf'])
    client.send('preview.pdf')
    downloaded = client.download('preview.pdf')
    client.bind('verified-downloads:preview.pdf', {'sha256': sha(downloaded), 'bytes': len(downloaded)})
    client.verify('final')
    client.list_sent()
    return {'conversions': conversions, 'transport_sha256': {k: sha(v) for k, v in frozen.items()},
            'real_user_entry': 'NOT_RUN', 'real_reading_guide': 'NOT_RUN',
            'unknown_recovery': 'NOT_RUN', 'end_to_end': 'NOT_RUN'}
