"""Version-bound technical review, distinct from the owner's PDF decision.

Only the known text-layer PDF review can gain candidate eligibility here.
Converter state and old evidence remain unchanged. This is a trusted local
reviewer interface, not an endpoint accepting arbitrary model/user JSON.
"""
import json
from pathlib import Path

from .content import exact, text_list
from .contracts import ContractError, new_id
from .conversion import CustodyConversion, sha
from .f05 import file_sha
from .registry import encoded, now
from .staging import Staging, IntegrityError


PDF_REVIEW = ('PDF text-layer extraction only: images, tables, layout and text '
              'completeness require review; no OCR was run.')
CHECKS = {'text_order', 'text_completeness', 'figures_tables', 'source_identity'}
EVENT = 'material_technical_review'


class MaterialReviews:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime
        self.conversion = CustodyConversion(registry)

    def binding(self, entry_id, extraction_id):
        entry = self.conversion._ready(entry_id)
        if self.registry.receipt(entry_id)['custody_health'] != 'healthy':
            raise ContractError('material original is not healthy')
        if file_sha(self.runtime.checked(self.runtime.path(entry['relative_path']))) != entry['sha256']:
            raise IntegrityError('material original identity changed')
        chain, seen = [], set()
        current = extraction_id
        while current:
            if current in seen or len(seen) >= 16:
                raise ContractError('invalid extraction ancestry')
            seen.add(current)
            row, mapping, root = self.conversion.extraction(current)
            if (mapping['source_asset_id'], mapping['source_sha256']) != (entry['asset_id'], entry['sha256']):
                raise ContractError('material review original mismatch')
            body = (root / 'source.md').read_bytes()
            if not body.strip():
                raise ContractError('empty extraction cannot be qualified')
            if mapping['extractor'] == 'pypdf':
                from .pdf_mapping import checked_pdf_map
                checked_pdf_map(dict(mapping, schema_version='local-pdf-text-map/v1'),
                                body.decode('utf-8'), entry['sha256'])
            inventory = json.loads(row['inventory_json'])
            self.conversion.registered_images(current)
            chain.append(dict(extraction_id=current, map_sha256=sha(encoded(mapping).encode()),
                              inventory_sha256=sha(encoded(inventory).encode())))
            current = row.get('parent_extraction_id')
        task = self.conversion.service.ledger.get_task(row['conversion_task_id'])
        if not task or task['status'] not in {'ready_candidate', 'needs_review'}:
            raise ContractError('review requires a terminal conversion candidate')
        quality = json.loads(task['quality_json'])
        if task['status'] == 'needs_review' and not (
                mapping['extractor'] == 'pypdf' and mapping['issues'] == [PDF_REVIEW]
                and quality.get('serious_issues') == 0 and quality.get('warnings') == 1
                and quality.get('manual_review_count') == 1):
            raise ContractError('unsupported or serious conversion defect requires repair/re-extraction')
        return dict(entry_id=entry_id, source_asset_id=entry['asset_id'], source_sha256=entry['sha256'],
                    extraction_id=extraction_id, chain=chain, task_id=task['task_id'],
                    task_status=task['status'], quality_sha256=sha(task['quality_json'].encode()))

    def _evidence(self, report):
        evidence = report['evidence']
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100:
            raise ContractError('bounded local review evidence required')
        ids = set()
        for item in evidence:
            exact(item, ('evidence_id', 'relative_path', 'sha256'))
            text_list(list(item.values()), 'evidence')
            if item['evidence_id'] in ids:
                raise ContractError('duplicate review evidence ID')
            ids.add(item['evidence_id'])
            relative = Path(item['relative_path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ContractError('review evidence must be inside the isolated runtime')
            path = self.runtime.checked(self.runtime.path(relative))
            if file_sha(path) != item['sha256']:
                raise ContractError('review evidence changed')
        return ids

    def _validate(self, report, mapping):
        exact(report, ('operator', 'checks', 'pages', 'issues', 'evidence'))
        text_list([report['operator']], 'review operator')
        ids = self._evidence(report)
        def common(item):
            text_list([item['note']], 'review note')
            text_list(item['evidence_refs'], 'review references')
            if not item['evidence_refs'] or not set(item['evidence_refs']) <= ids:
                raise ContractError('review requires current evidence references')
        checks = report['checks']
        if not isinstance(checks, list) or len(checks) != len(CHECKS):
            raise ContractError('all four technical checks required')
        for item in checks:
            exact(item, ('code', 'status', 'note', 'evidence_refs'))
            common(item)
            if item['status'] not in {'passed', 'pending', 'failed'}:
                raise ContractError('invalid technical check status')
        if {x['code'] for x in checks} != CHECKS:
            raise ContractError('all four technical checks required')
        pages = report['pages']
        expected = [p['page_index'] for p in mapping['page_mappings']]
        if not isinstance(pages, list):
            raise ContractError('page review list required')
        for item in pages:
            exact(item, ('page_index', 'status', 'note', 'evidence_refs'))
            common(item)
            if type(item['page_index']) is not int or item['status'] not in {'passed', 'pending', 'failed'}:
                raise ContractError('invalid physical page review')
        if [p['page_index'] for p in pages] != expected:
            raise ContractError('review must cover every mapped physical page once in order')
        issues = report['issues']
        if not isinstance(issues, list):
            raise ContractError('issue list required')
        issue_ids = set()
        for item in issues:
            exact(item, ('issue_id', 'category', 'status', 'note', 'evidence_refs'))
            common(item)
            text_list([item['issue_id']], 'issue ID')
            if item['issue_id'] in issue_ids:
                raise ContractError('duplicate review issue')
            issue_ids.add(item['issue_id'])
            allowed = {'content_review': {'resolved', 'pending'}, 'source_limitation': {'pending_user'},
                       'technical_failure': {'blocked'}, 'serious_content': {'blocked'}}
            if item['category'] not in allowed or item['status'] not in allowed[item['category']]:
                raise ContractError('technical failures cannot be accepted as material limitations')
        required = {'conversion-' + str(i) for i in range(len(mapping['issues']))}
        if not required <= issue_ids or any(x['issue_id'] in required and x['category'] != 'content_review' for x in issues):
            raise ContractError('all conversion review reasons must be addressed as technical review')
        qualified = (all(x['status'] == 'passed' for x in checks + pages)
                     and all(x['status'] in {'resolved', 'pending_user'} for x in issues))
        return 'qualified' if qualified else 'blocked'

    def latest(self, extraction_id, *, db=None):
        sql = 'SELECT payload_json FROM custody_events WHERE event_type=? AND aggregate_id=? ORDER BY rowid DESC LIMIT 1'
        rows = db.execute(sql, (EVENT, extraction_id)).fetchall() if db else self.registry.rows(sql, (EVENT, extraction_id))
        return json.loads(rows[0]['payload_json']) if rows else None

    def record(self, entry_id, extraction_id, report, *, previous_review_id=None):
        """Append an audited result; never update the converter's task status."""
        try:
            return self._record(entry_id, extraction_id, report, previous_review_id=previous_review_id)
        except IntegrityError:
            # Persist health after the failed review transaction rolled back.
            # Historical custody acceptance remains true.
            if self.registry.receipt(entry_id)['custody_health'] == 'healthy':
                self.registry.health(entry_id, 'suspect', 'original changed during material review')
            raise

    def _record(self, entry_id, extraction_id, report, *, previous_review_id=None):
        self.runtime.gate()
        with Staging(self.registry).input_binding(entry_id, 'material_review'):
            binding = self.binding(entry_id, extraction_id)
            _, mapping, _ = self.conversion.extraction(extraction_id)
            qualification = self._validate(report, mapping)
            identity = sha(encoded(dict(binding=binding, report=report, previous=previous_review_id)).encode())
            with self.registry.transaction() as db:
                latest = self.latest(extraction_id, db=db)
                if latest and latest['request_sha256'] == identity:
                    return self.checked(extraction_id, db=db)
                if (latest['review_id'] if latest else None) != previous_review_id:
                    raise ContractError('review revision must reference the current review')
                if self.binding(entry_id, extraction_id) != binding:
                    raise ContractError('material changed during review')
                self._evidence(report)
                saved = dict(schema_version='material-review/v1', review_id=new_id('review'), binding=binding,
                    report=report, qualification=qualification, owner_choice='pending', reviewed_at=now(),
                    previous_review_id=previous_review_id, request_sha256=identity)
                self.registry.event(db, EVENT, extraction_id, 'material-review:' + identity, saved)
            return saved

    def checked(self, extraction_id, *, db=None):
        saved = self.latest(extraction_id, db=db)
        if saved is None:
            return None
        if self.binding(saved['binding']['entry_id'], extraction_id) != saved['binding']:
            raise ContractError('material review binding changed; new review required')
        _, mapping, _ = self.conversion.extraction(extraction_id)
        if self._validate(saved['report'], mapping) != saved['qualification']:
            raise ContractError('material review qualification changed')
        return saved

    def require_qualified(self, extraction_id):
        saved = self.checked(extraction_id)
        if not saved or saved['qualification'] != 'qualified':
            raise ContractError('conversion quality review is unresolved; publication blocked')
        return saved

    @staticmethod
    def limitations(saved):
        return [i['note'] for i in saved['report']['issues'] if i['category'] == 'source_limitation'] if saved else []
