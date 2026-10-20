"""Synthetic child-process lifecycle fixture; no platform/model transport."""
import json
import os
from pathlib import Path
import sys

PACKAGE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'src'),str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from fixtures_guide import SyntheticQuoteGuide
from kb_custody_v21.pilot_text import PilotTextSession
from kb_custody_v21.pilot_pipeline import PilotPipeline
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.pilot_resources import PilotResources, ResourceSample
from kb_custody_v21.registry import Registry, encoded
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE


def ready(registry):
    return PilotResources(registry,probe=lambda _:ResourceSample(60,'normal',20*1024**3))


def call(path, *, hook=lambda p:None):
    data=json.loads(Path(path).read_bytes())
    runtime=Runtime(WORKTREE,DEFAULTS,Path(data['runtime']))
    registry=Registry(runtime)
    intake=PilotIntake(registry,PilotScope(**data['scope']),max_attachments=5,max_pending=8)
    pipeline=PilotPipeline(intake,resources=ready(registry))
    class Wire:
        def exchange(self,packet,check):
            check()
            if data.get('wire_status') == 'blocked_before_turn':
                return dict(status='blocked_before_turn')
            with Path(data['marker']).open('x') as out: out.write('one synthetic generation')
            hook('text_submitted')
            return dict(status='received',text=encoded(SyntheticQuoteGuide().generate(json.loads(packet['request']))))
    source=lambda:data['source']
    def generate(*args,**kwargs):
        session=PilotTextSession(data['text_root'],data['text_approval_sha'],checked_source=source,wire=Wire())
        try:return session.generate_guide(*args,**kwargs)
        finally:session.close()
    return pipeline.prepare(**data['prepare'],text_root=data['text_root'],text_approval_sha=data['text_approval_sha'],
        checked_source=source,generate_guide=generate,hook=hook)


if __name__=='__main__':
    point=sys.argv[2]
    call(sys.argv[1],hook=lambda p:os._exit(77) if p==point else None)
