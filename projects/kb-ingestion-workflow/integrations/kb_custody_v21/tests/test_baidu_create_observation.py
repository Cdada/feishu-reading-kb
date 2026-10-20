"""Static diagnostics preserve the existing acceptance and unknown rules."""
import json
import unittest

from kb_custody_v21.baidu_protocol import create_observation
from test_baidu_protocol import response
import test_b03


class CreateObservationTests(unittest.TestCase):
    def item(self):
        return dict(path='/apps/synthetic/test/fixture.txt', size=157,
                    isdir=0, fs_id=1234567, md5='a'*32)

    def test_states_do_not_expose_values_or_claim_hash_content_semantics(self):
        item=self.item()
        item.update(token='synthetic-token',uploadid='synthetic-upload',request_id='synthetic-request')
        result=create_observation(item,item['path'],157)
        self.assertEqual(result,dict(path='match',size='match',isdir='match',
            fs_id='valid_format',md5='hex32'))
        for value in (item['path'],str(item['fs_id']),item['md5'],'synthetic-token','synthetic-upload','synthetic-request'):
            self.assertNotIn(value,json.dumps(result))
        self.assertNotIn('accepted',result)

    def test_missing_types_mismatches_and_invalid_identifiers_are_distinct(self):
        self.assertEqual(create_observation({},self.item()['path'],157),
            dict.fromkeys(('path','size','isdir','fs_id','md5'),'absent'))
        for key,bad,state in (('path',None,'invalid_type'),('path','/other','mismatch'),
                             ('size','157','invalid_type'),('size',False,'invalid_type'),
                             ('size',158,'mismatch'),('isdir',False,'invalid_type'),
                             ('isdir',1,'mismatch'),('fs_id',1.0,'invalid'),
                             ('fs_id','0','invalid'),('fs_id',2**64,'invalid'),
                             ('md5',[],'invalid'),('md5','invalid-hash','invalid'),('md5',None,'null')):
            with self.subTest(field=key,value=bad):
                value=dict(self.item(),**{key:bad})
                self.assertEqual(create_observation(value,self.item()['path'],157)[key],state)

    def test_valid_format_is_not_identity_or_byte_verification(self):
        item=self.item(); item['fs_id']='7654321'; item['md5']='F'*32
        self.assertEqual(create_observation(item,item['path'],157)['fs_id'],'valid_format')
        self.assertEqual(create_observation(item,item['path'],157)['md5'],'hex32')

    def fixture(self):
        f=test_b03.B03Tests(); f.setUp(); self.addCleanup(f.doCleanups)
        return f

    def test_create_rejections_record_field_states_without_more_calls_or_restarts(self):
        for field,value,state in (('path','/apps/synthetic/other.txt','mismatch'),
                                  ('size','157','invalid_type'),('isdir',False,'invalid_type'),
                                  ('md5','invalid-hash','invalid')):
            with self.subTest(field=field):
                f=self.fixture(); item=json.loads(f.steps[8][1].body)
                if value is None: item.pop(field)
                else: item[field]=value
                f.steps[8]=('create',response(item))
                f.assert_stopped_attempt('response',9,0)
                journal=[json.loads(line) for line in (f.root/'b03-requests/journal.local.jsonl').read_bytes().splitlines()]
                receipt=next(e for e in journal if e.get('received')==9)
                self.assertEqual(receipt['api_errno'],0)
                self.assertEqual(receipt['create_observation'][field],state)
                self.assertEqual(journal[-1]['result']['error_type'],'RemoteFailure')
                self.assertEqual(journal[-1]['result']['error_stage'],'execute_archive')
                self.assertEqual(journal[-1]['result']['physical_attempts']['download'],0)

    def test_matching_observation_still_requires_full13_and_downloaded_sha(self):
        f=self.fixture(); result=f.run_experiment()
        self.assertEqual(result['status'],'OFFLINE_B03_VERIFIED')
        self.assertEqual(result['archive_result']['verification_level'],'content_sha256_verified')
        self.assertEqual(len(f.calls),13); f.assert_old_unchanged()
        journal=[json.loads(line) for line in (f.root/'b03-requests/journal.local.jsonl').read_bytes().splitlines()]
        receipt=next(e for e in journal if e.get('received')==9)
        self.assertEqual(receipt['create_observation'],dict(path='match',size='match',isdir='match',
            fs_id='valid_format',md5='hex32'))
