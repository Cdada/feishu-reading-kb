"""Reuse the prior F01 TXT and protocol wire fixtures; no service simulator."""
from datetime import datetime, timedelta, timezone
import hashlib
from kb_custody_v21.b01 import LIMITS, fixture
from kb_custody_v21.baidu_credentials import ApplicationCredentials
from kb_custody_v21.f01_flow import TXT
from test_baidu_protocol import response


def approval(registry, binding):
    now = datetime.now(timezone.utc)
    root = '/apps/synthetic/KB-V21-B01'
    return dict(reference='B01', approved=True, app_id='1234567', app_name='synthetic',
        synthetic_only=True, limits=LIMITS.copy(), max_calls=13, automatic_retries=0,
        redirects=0, unknown_lookup=0, token_storage='memory_only',
        first_account='operator_confirms_observed_uk', scopes=['basic','netdisk'],
        writes=['mkdir_once','upload_once'], fixture=fixture(), fixture_binding=binding,
        root=root, target=root+'/fixture.txt',
        runtime_relative=str(registry.runtime.root.relative_to(registry.runtime.worktree)),
        starts_at=(now-timedelta(minutes=1)).isoformat(), expires_at=(now+timedelta(minutes=60)).isoformat())


def steps(a, content=TXT):
    item = dict(fs_id=123456789, path=a['target'], size=len(content), isdir=0,
                md5=hashlib.md5(content).hexdigest())
    return [
        ('oauth_token', response(dict(access_token='synthetic-token', expires_in=1000,
                                     scope='basic netdisk', refresh_token='synthetic-refresh'))),
        ('uinfo', response(dict(errno=0, uk=1234567))),
        ('mkdir', response(dict(errno=0, fs_id=2345678, path=a['root'], isdir=1))),
        ('uinfo', response(dict(errno=0, uk=1234567))),
        ('quota', response(dict(errno=0, total=10**8, used=0))),
        ('list', response(dict(errno=0, list=[]))),
        ('precreate', response(dict(errno=0, return_type=1, uploadid='synthetic-upload', block_list=[0]))),
        ('slice', response(dict(errno=0, md5=item['md5']))),
        ('create', response(dict(errno=0, **item))),
        ('filemetas', response(dict(errno=0, list=[item]))),
        ('download_meta', response(dict(errno=0, list=[dict(item, dlink='https://d.pcs.baidu.com/file/synthetic?sign=synthetic-signed')]))),
        ('download', response(content)),
        ('filemetas', response(dict(errno=0, list=[item]))),
    ]


def callbacks():
    return dict(load_credentials=lambda app, name, check: ApplicationCredentials('synthetic-key', 'synthetic-secret'),
                obtain_code=lambda credentials, a: 'synthetic-code', confirm_account=lambda uk: uk)
