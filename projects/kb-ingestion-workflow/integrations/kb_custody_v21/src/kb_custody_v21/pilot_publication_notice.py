"""One explicitly approved notice for an already confirmed published version."""
import json
from pathlib import Path
from .contracts import ContractError,validate_id
from .f05 import file_sha
from .pilot_feishu import read_json
from .pilot_notice_workflow import PilotNoticeWorkflow
from .runtime import no_symlinks

LIMITS=dict(auth=4,identity=3,list=0,message=0,download=0,upload=0,send=1)

def deliver_publication_notice(session,intake,pid,expected):
    validate_id(pid,'publication')
    if session.limits!=LIMITS:
        raise ContractError('exact eight-request publication notice budget required')
    flow=PilotNoticeWorkflow(session,intake)
    if flow._publication_plan(pid)[1]!=expected:
        raise ContractError('notice differs from review copy')
    prior=intake.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_publication_notice' AND aggregate_id=?",(expected['notification_id'],))
    if any(json.loads(v['payload_json']).get('attempt') for v in prior):
        raise ContractError('notice already attempted; no repeat or unknown resend')
    session.verify_isolation()
    value=flow.publication(pid)
    if value['delivery_state']!='delivered':
        raise ContractError('notice delivery unknown; stop')
    return value

def publication_notice(session,intake,plan_path):
    path=no_symlinks(Path(plan_path).absolute())
    if session.binding().get(str(path))!=file_sha(path):
        raise ContractError('publication notice plan must be frozen')
    plan=read_json(path,65536)
    if set(plan)!={'reference','publication_id','notice'} or plan['reference']!='P11-publication-notice-v1':
        raise ContractError('one explicit publication notice plan required')
    return deliver_publication_notice(session,intake,plan['publication_id'],plan['notice'])
