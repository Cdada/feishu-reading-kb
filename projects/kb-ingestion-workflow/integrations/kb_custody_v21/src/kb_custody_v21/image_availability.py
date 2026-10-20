"""Source-bound owner choices for unavailable Markdown images.

Continuing permits a text-led guide, never publication. Existing retained image
corruption is still fatal; a missing-image choice cannot waive custody checks.
"""
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .conversion import sha
from .embedded_markdown import REASONS
from .f05 import file_sha
from .pilot_decision import observed_reply
from .registry import encoded

COMMANDS = {'重新发文件': 'resend', '按缺图版本继续': 'continue_without_missing'}


def availability(conversion, extraction_id):
    row, mapping, _ = conversion.extraction_ancestry(extraction_id)[-1]
    report = mapping.get('image_availability')
    if report is None or not report['missing_count']:
        return None
    if (report['source_sha256'] != mapping['source_sha256'] or
            report['normalized_sha256'] != mapping['output_sha256']):
        raise ContractError('image availability differs from the registered source')
    binding = dict(extraction_id=mapping['extraction_id'], source_asset_id=mapping['source_asset_id'],
        source_sha256=mapping['source_sha256'], report_sha256=sha(encoded(report).encode()))
    key = 'image-availability-choice:' + mapping['extraction_id']
    saved = conversion.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (key,))
    decision = json.loads(saved[0]['payload_json']) if saved else None
    if decision:
        if decision['binding'] != binding or decision['choice'] not in COMMANDS.values():
            raise ContractError('image choice belongs to different source bytes')
        for path, digest in decision['evidence']['receipts'].items():
            if file_sha(Path(path)) != digest:
                raise ContractError('image choice receipt changed')
    return dict(binding=binding, report=report, decision=decision, key=key)


def require_image_choice(conversion, extraction_id):
    value = availability(conversion, extraction_id)
    if value and (not value['decision'] or value['decision']['choice'] != 'continue_without_missing'):
        raise ContractError('missing images require owner choice before guide generation')
    return value


def warning_text(filename, value):
    report = value['report']
    missing = [item for item in report['occurrences'] if item['status'] == 'unavailable']
    details = '；'.join(f"图 {item['number']}：{REASONS[item['reason']]}" for item in missing[:8])
    if len(missing) > 8:
        details += f'；另有 {len(missing)-8} 处，完整清单已保存'
    return (f'材料“{filename}”生成导读前检查：{report["missing_count"]} 处图片缺失或无法读取，'
            f'{report["available_count"]} 处图片可保留。{details}。'
            '当前尚未调用正文模型。请直接回复本条消息“重新发文件”或“按缺图版本继续”。'
            '选择继续会保留已有图片、标记缺图位置；导读完成后仍需另行确认入库。'
            '选择重新发文件会停止处理当前版本，请随后发送含完整图片的新文件。')


def apply_image_reply(session, conversion, value, notice_id, scan_id, observed):
    """Only one verified direct owner reply to the exact report can unblock it."""
    session.check()
    if session.value('isolation_verified') is not True:
        raise ContractError('fixed group isolation required for image choice')
    saved_notice = session.value('notice:' + notice_id + ':verified')
    if not saved_notice:
        raise ContractError('image warning must have a verified delivery')
    binding = value['binding']
    current = availability(conversion, binding['extraction_id'])
    if current is None or current['binding'] != binding:
        raise ContractError('image report changed before owner choice')
    entries = conversion.registry.rows('SELECT entry_id FROM intake_entries WHERE asset_id=?',
                                      (binding['source_asset_id'],))
    if not entries:
        raise ContractError('image choice requires an accepted original')
    entry = conversion.registry.entry(entries[0]['entry_id'])
    original = conversion.runtime.checked(conversion.runtime.path(entry['relative_path']))
    if session.binding().get(str(original)) != binding['source_sha256']:
        raise ContractError('image choice original must be a frozen approved input')
    notice_plan_path = session.root / (notice_id + '.notice.local.json')
    notice_plan = json.loads(notice_plan_path.read_bytes())
    if notice_plan.get('payload') != warning_text(entry['received_filename'], current):
        raise ContractError('image warning does not describe this exact material')
    message_id = observed['message_id']
    item = session.message(scan_id, message_id)
    parent, command, version = observed_reply(item, session.approval['bot_id'])
    if (version != observed_reply(observed, session.approval['bot_id'])[2]
            or parent != saved_notice['message_id'] or command not in COMMANDS):
        raise ContractError('image choice must match the exact warning and observed owner reply')
    frozen = dict(session.value(scan_id + ':result')['receipts'])
    matches = [m for p in frozen for m in json.loads(Path(p).read_bytes()).get('data', {}).get('items', [])
               if m.get('message_id') == message_id]
    if matches != [observed]:
        raise ContractError('image choice requires one frozen physical list item')
    events = [e for e in session.events if e.get('operation') == scan_id + ':message:' + message_id]
    if len(events) != 1 or events[0]['kind'] != 'message':
        raise ContractError('image reply detail provenance required')
    event = events[0]
    raw = session._receipt(event['intent'], 'message', event.get('download'))
    if raw.get('data', {}).get('items') != [item]:
        raise ContractError('image reply differs from its physical receipt')
    detail_path = session.root / (event['intent'] + '.response.local')
    frozen[str(detail_path)] = file_sha(detail_path)
    notice_path = session.root / saved_notice['response_file']
    frozen[str(notice_path)] = saved_notice['response_sha256']
    frozen[str(notice_plan_path)] = file_sha(notice_plan_path)
    source = session.binding()
    evidence = dict(reference='P30-image-owner-choice-v1', approval_sha256=session.approval_sha,
        notice_id=notice_id, reply_to=parent, message_id=message_id,
        observed_version_sha256=sha(encoded(version).encode()), receipts=frozen)
    decision = dict(binding=binding, choice=COMMANDS[command], evidence=evidence)
    def checked():
        session.check()
        if session.binding() != source or any(file_sha(Path(p)) != h for p, h in frozen.items()):
            raise ContractError('image choice source or receipt changed')
    checked()
    with conversion.registry.transaction() as db:
        existing = db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (current['key'],)).fetchone()
        if existing and json.loads(existing['payload_json']) != decision:
            raise ContractError('image choice already recorded; conflicting decisions cannot overwrite it')
        checked()
        if not existing:
            conversion.registry.event(db, 'owner_image_availability_choice', binding['source_asset_id'], current['key'], decision)
    return decision


def image_choice_notice_id(service, key):
    return stable_key(service.prefix, 'image-choice:' + key)
