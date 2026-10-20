"""N10 bounded official-CLI reads, sharing its physical request ledger.

This is a test-window adapter, not an unmetered background listener. A complete
history page is evidence of that query interval only, never permanent backfill.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
from urllib.parse import urlencode

from .contracts import ContractError
from .f01 import Experiment, PREFIX, identity
from .f02 import ISOLATION, administration
from .f05 import file_sha
from .runtime import no_symlinks
from .s6 import instant

LIMITS = dict(auth=10, identity=6, list=12, message=6, download=6, upload=4, send=6)
FILE_LIMIT = 32 * 1024 * 1024


def read_json(path, maximum=1024*1024):
    with no_symlinks(Path(path)).open('rb') as stream:
        raw = stream.read(maximum+1)
    if len(raw)>maximum:
        raise ContractError('bounded JSON exceeds limit')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ContractError('invalid bounded JSON') from error


class PilotReadSession(Experiment):
    reference, limits = 'N10', LIMITS
    root_name = 'n10-requests'

    def approved_limits(self, approval):
        if approval.get('limits') != LIMITS:
            raise ContractError('exact N10 category limits required')
        return dict(LIMITS)

    def __init__(self, root, approval_sha, *, runner, checked_source, clock=None):
        self.root = no_symlinks(Path(root).absolute())
        if self.root.name!=self.root_name or '.dev-runtime' not in self.root.parts:
            raise ContractError('fixed private Pilot session location required')
        self.session = self.root/'approval.local.json'
        self.approval_sha = approval_sha
        if file_sha(self.session)!=approval_sha:
            raise ContractError('approved N10 binding changed')
        self.approval = read_json(self.session,65536)
        a = self.approval
        self.limits = self.approved_limits(a)
        if (a.get('reference')!=self.reference or a.get('approved') is not True
                or a.get('root')!=str(self.root)
                or a.get('automatic_retries')!=0 or a.get('redirects')!=0
                or a.get('publish_scope')!='only_bound_account'
                or not a.get('cli_profile')):
            raise ContractError('exact N10 objects, budget and no-retry approval required')
        for name,prefix in [('app_id','cli_'),('chat_id','oc_'),('bot_id','ou_'),('account_id','ou_')]:
            identity(a.get(name),prefix)
        if not 0<(instant(a['expires_at'])-instant(a['starts_at'])).total_seconds()<=36000:
            raise ContractError('one approved ten-hour window required')
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.runner,self.checked_source=runner,checked_source
        self.token=None
        self.path=self.root/'journal.local.jsonl'
        self.lock=no_symlinks(self.root/'execution.lock').open('a+')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.events=[]
        try:
            if self.path.exists():
                self.events=[json.loads(line) for line in no_symlinks(self.path).read_text().splitlines()]
                if not self.events or self.events[0]!={'approval':a}:
                    raise ContractError('N10 journal identity changed; never reset')
            else:
                self.append({'approval':a})
            self.check()
        except Exception:
            self.close()
            raise

    def binding(self):
        def window():
            if not instant(self.approval['starts_at'])<=self.clock()<instant(self.approval['expires_at']):
                raise ContractError('outside approved N10 window')
        window()
        if file_sha(self.session)!=self.approval_sha:
            raise ContractError('N10 approval changed')
        frozen=self.checked_source()
        if not isinstance(frozen,dict) or not frozen:
            raise ContractError('complete checked source binding required')
        for path,digest in frozen.items():
            if file_sha(Path(path))!=digest:
                raise ContractError('frozen source or predecessor evidence changed')
        window()  # Source and evidence hashing may cross the deadline.
        self.frozen=dict(frozen,**{str(self.session):self.approval_sha})
        return self.frozen

    def check(self):
        super().check()
        self.binding()
        self.http_events()

    def request(self,*args,**kwargs):
        raise ContractError('N10 permits only the bounded official CLI route')

    def http_events(self):
        path=self.root/'http.local.jsonl'
        if not path.exists():
            return []
        raw=no_symlinks(path).read_bytes()
        if len(raw)>65536:
            raise ContractError('N10 HTTP journal exceeds limit')
        try:
            events=[json.loads(line) for line in raw.splitlines()]
        except (ValueError,UnicodeError) as error:
            raise ContractError('invalid N10 HTTP journal') from error
        starts,ends={},{}
        for e in events:
            k,kind=e.get('key'),e.get('kind')
            if not isinstance(k,str) or kind not in LIMITS:
                raise ContractError('invalid physical request category')
            if e.get('type')=='intent' and k not in starts:
                starts[k]=kind
            elif e.get('type')=='complete' and k not in ends and starts.get(k)==kind and e.get('http_status')==200:
                ends[k]=kind
            else:
                raise ContractError('physical request stopped or duplicate')
        if starts!=ends or len(events)>200:
            raise ContractError('unfinished physical request; no retry')
        limits=self.limits
        extension=self.root/'extension.local.json'
        if str(extension) in self.frozen:
            if self.reference!='N10':
                raise ContractError('this session has no automatic budget extension')
            value=read_json(extension,16384)
            if (file_sha(extension)!=self.frozen[str(extension)] or value.get('reference')!='N10'
                    or value.get('maximum_total')!=100 or not value.get('reason','').strip()
                    or value.get('approval_sha256')!=self.approval_sha
                    or type(value.get('prior_attempts')) is not int
                    or not 1<=value['prior_attempts']<=min(50,len(starts))):
                raise ContractError('invalid budget extension evidence')
            limits={k:2*v for k,v in LIMITS.items()}
        if any(list(starts.values()).count(k)>v for k,v in limits.items()):
            raise ContractError('active physical category budget exceeded')
        return events

    def _receipt(self,key,kind,download=None):
        rows=[e for e in self.http_events() if e['type']=='complete' and e['key']==key+':'+kind]
        receipt=self.root/(key+'.response.local')
        if len(rows)!=1 or rows[0].get('response_sha256')!=file_sha(receipt):
            raise ContractError('hash-bound successful physical receipt required')
        if kind=='download':
            if download and download.get('reference')=='P19-inbox-download-v1':
                from .pilot_inbox import inbox_policy
                policy=inbox_policy(self)
                if download['max_asset_bytes']!=policy['max_asset_bytes'] or not 0<receipt.stat().st_size<=download['max_asset_bytes']:
                    raise ContractError('received inbox file exceeds the approved limit')
                return dict(path=str(receipt),sha256=rows[0]['response_sha256'],size_bytes=receipt.stat().st_size)
            if (not download or receipt.stat().st_size!=download['size_bytes']
                    or rows[0]['response_sha256']!=download['sha256']):
                raise ContractError('download differs from the frozen approved sample')
            return dict(path=str(receipt),sha256=download['sha256'],size_bytes=download['size_bytes'])
        value=read_json(receipt)
        if type(value.get('code')) is not int or value['code']!=0:
            raise ContractError('successful API status required')
        return value

    def _call(self,kind,path,*,operation,mode='read',frozen_extra=None,download=None,preview=None,notice=None,body=''):
        self.check()
        if notice is not None and self.reference!='P11':
            raise ContractError('notices require an independently approved P11 session')
        if any(file_sha(Path(p))!=h for p,h in (frozen_extra or {}).items()):
            raise ContractError('origin receipt changed before CLI access')
        prior=[e for e in self.events if e.get('operation')==operation]
        if prior:
            if (len(prior)!=1 or (prior[0]['kind'],prior[0]['path'],prior[0]['mode'])!=(kind,path,mode)
                    or prior[0].get('download')!=download or prior[0].get('preview')!=preview
                    or prior[0].get('notice')!=notice
                    or prior[0].get('body','')!=body):
                raise ContractError('stable operation changed')
            key=prior[0]['intent']
            return ({} if mode=='profile' else self._receipt(key,kind,download)),key
        sequence=1+sum('intent' in e for e in self.events)
        key=f'p{sequence:03d}-{kind}'
        frozen=self.binding()
        frozen.update(frozen_extra or {})
        c=dict(reference=self.reference,mode=mode,root=str(self.root),profile=self.approval['cli_profile'],
            app_id=self.approval['app_id'],kind=kind,key=key,path=path,session=str(self.session),
            starts_at=self.approval['starts_at'],expires_at=self.approval['expires_at'],frozen=frozen)
        if download is not None:c['download']=download
        if preview is not None:c['preview']=preview
        if notice is not None:c['notice']=notice
        if body:c['body']=body
        self.append(dict(intent=key,kind=kind,path=path,mode=mode,operation=operation,at=self.clock().isoformat(),
                         **({'preview':preview,'body':body} if preview is not None else {}),
                         **({'notice':notice,'body':body} if notice is not None else {}),
                         **({'download':download} if download is not None else {})))
        try:
            result=self.runner(c)
            if result.get('ok') is not True or result.get('mode')!=mode:
                raise ContractError('official CLI did not complete')
            value={}
            if mode!='profile':
                value=self._receipt(key,kind,download)
            self.binding()
            self.append(dict(complete=key,response_file=key+'.response.local' if mode!='profile' else None))
            return value,key
        except Exception as error:
            self.append(dict(stopped=key,error_type=type(error).__name__))
            raise

    def verify_isolation(self):
        self.check()
        if self.value('isolation_verified') is True:
            return
        try:
            self._call('identity',PREFIX+'/bot/v3/info',mode='profile',operation='profile')
            bot,_=self._call('identity',PREFIX+'/bot/v3/info',operation='bot')
            if bot.get('bot',{}).get('open_id')!=self.approval['bot_id']:
                raise ContractError('bound application bot differs')
            path=PREFIX+'/im/v1/chats/'+self.approval['chat_id']
            response,_=self._call('identity',path,operation='chat')
            info=response.get('data',{})
            if (info.get('chat_mode')!='group' or info.get('chat_type')!='private'
                    or info.get('external') is not False or str(info.get('user_count'))!='1'
                    or str(info.get('bot_count'))!='1' or administration(info)!=self.approval['administration']
                    or any(info.get(k)!=v for k,v in ISOLATION.items())):
                raise ContractError('fixed private group isolation differs')
            response,_=self._call('identity',path+'/members?member_id_type=open_id&page_size=100',operation='members')
            members=response.get('data',{});items=members.get('items',[])
            if (members.get('has_more') is not False or members.get('trigger_security_conf_limit') is True
                    or len(items)!=1 or items[0].get('member_id')!=self.approval['account_id']
                    or items[0].get('member_id_type')!='open_id'):
                raise ContractError('complete sole authorized member required')
            self.binding()
            self.bind('isolation_verified',True)
        except Exception as error:
            self.append(dict(stopped='isolation',error_type=type(error).__name__))
            raise

    def scan(self,scan_id,*,start,end,max_pages=1):
        """Persist each page. An explicit incomplete result never advances past it."""
        self.check()
        identity(scan_id,'scan_')
        if self.value('isolation_verified') is not True:
            raise ContractError('verify the fixed group before listing messages')
        lower=instant(self.approval['starts_at'])-timedelta(hours=24)
        if (type(max_pages) is not int or not 1<=max_pages<=12
                or not lower<=start<end<=min(self.clock(),instant(self.approval['expires_at']))):
            raise ContractError('bounded history interval and page count required')
        bound=dict(start=int(start.timestamp()),end=int(end.timestamp()),max_pages=max_pages)
        self.bind(scan_id+':interval',bound)
        old=self.value(scan_id+':result')
        if old is not None:
            if any(file_sha(Path(p))!=h for p,h in old['receipts'].items()):
                raise ContractError('saved scan evidence changed')
            return old
        # A process death after a completed page reuses the saved response. The
        # unfinished command path remains stopped by check(), never repeated.
        pages=[]
        token=''; seen=set(); selected=[]; hashes={}
        try:
            for index in range(max_pages):
                params=dict(container_id_type='chat',container_id=self.approval['chat_id'],
                    start_time=str(bound['start']),end_time=str(bound['end']),page_size='50',sort_type='ByCreateTimeAsc')
                if token:params['page_token']=token
                response,key=self._call('list',PREFIX+'/im/v1/messages?'+urlencode(sorted(params.items())),
                    operation=scan_id+':page:'+str(index))
                pages.append(key)
                receipt=self.root/(key+'.response.local');hashes[str(receipt)]=file_sha(receipt)
                data=response.get('data',{});items=data.get('items')
                if not isinstance(items,list) or len(items)>50 or type(data.get('has_more')) is not bool:
                    raise ContractError('incomplete history page contract')
                for item in items:
                    if not isinstance(item,dict) or item.get('chat_id')!=self.approval['chat_id']:
                        raise ContractError('history returned another group')
                    identity(item.get('message_id'),'om_')
                    sender=item.get('sender',{})
                    if (sender.get('id')==self.approval['account_id'] and sender.get('sender_type')=='user'
                            and sender.get('id_type')=='open_id'):
                        selected.append(item['message_id'])
                more=data['has_more']; next_token=data.get('page_token','')
                if not more:break
                if not isinstance(next_token,str) or not next_token or len(next_token)>4096 or next_token in seen:
                    raise ContractError('history pagination cursor missing or repeated')
                seen.add(next_token);token=next_token
            result=dict(status='COMPLETE_INTERVAL' if not more else 'INCOMPLETE_PAGE_LIMIT',
                interval=bound,pages=pages,message_ids=list(dict.fromkeys(selected)),receipts=hashes,
                next_page_token=token if more else None,permanent_backfill_verified=False)
            self.binding();self.bind(scan_id+':result',result)
            return result
        except Exception as error:
            self.append(dict(stopped=scan_id,error_type=type(error).__name__))
            raise

    def message(self,scan_id,message_id):
        self.check()
        result=self.value(scan_id+':result')
        if not result or message_id not in result['message_ids']:
            raise ContractError('exact message must originate in a verified fixed-group scan')
        response,key=self._call('message',PREFIX+'/im/v1/messages/'+identity(message_id,'om_'),
            frozen_extra=result['receipts'],operation=scan_id+':message:'+message_id)
        items=response.get('data',{}).get('items',[])
        sender=items[0].get('sender',{}) if len(items)==1 else {}
        if (len(items)!=1 or items[0].get('message_id')!=message_id or items[0].get('chat_id')!=self.approval['chat_id']
                or any(sender.get(k)!=v for k,v in {'id':self.approval['account_id'],'sender_type':'user','id_type':'open_id'}.items())):
            self.append(dict(stopped=key,error_type='MessageIdentityMismatch'))
            raise ContractError('exact message identity differs')
        return items[0]

    def receive_file(self,scan_id,message_id,intake,*,sample_path):
        """One approved file message -> exact downloaded bytes -> existing custody.

        The local sample must be in the execution's frozen inputs, not simply a
        caller-selected filename. This N10 route does not expand posts/folders or
        manufacture image filenames. Conversion remains a separate serial step.
        """
        self.check()
        a=self.approval
        if (intake.scope.app_id,intake.scope.chat_id,intake.scope.user_id)!=(a['app_id'],a['chat_id'],a['account_id']):
            raise ContractError('intake runtime differs from approved platform scope')
        sample=no_symlinks(Path(sample_path).absolute())
        frozen=self.binding()
        if (str(sample) not in frozen or not sample.is_file()
                or sample.suffix.lower() not in {'.md','.txt','.pdf','.webarchive'}
                or not 0<sample.stat().st_size<=min(FILE_LIMIT,intake.runtime.config['max_asset_bytes'])):
            raise ContractError('a bounded, frozen approved file sample is required')
        m=self.message(scan_id,message_id)
        if m.get('msg_type')!='file' or m.get('deleted') is not False:
            raise ContractError('only an available file message is eligible')
        raw=m.get('body',{}).get('content')
        if not isinstance(raw,str) or len(raw.encode())>65536:
            raise ContractError('bounded raw file-message JSON required')
        try:
            content=json.loads(raw)
        except (ValueError,UnicodeError) as error:
            raise ContractError('invalid raw file-message JSON') from error
        if not isinstance(content,dict) or content.get('file_name')!=sample.name:
            raise ContractError('message filename differs from the approved sample')
        key=identity(content.get('file_key'),'file_')
        expected=dict(message_id=message_id,file_key=key,filename=sample.name,sample_path=str(sample),
                      sha256=frozen[str(sample)],size_bytes=sample.stat().st_size)
        operation=scan_id+':message:'+message_id
        origin=[e for e in self.events if e.get('operation')==operation]
        if len(origin)!=1:
            raise ContractError('one exact-message receipt required')
        receipt=self.root/(origin[0]['intent']+'.response.local')
        origins=dict(self.value(scan_id+':result')['receipts'],**{str(receipt):file_sha(receipt)})
        envelope=dict(appId=a['app_id'],chatId=a['chat_id'],senderId=a['account_id'],chatType='group',
            messageId=message_id,resources=[dict(type='file',fileKey=key,fileName=sample.name,
                                               sizeBytes=expected['size_bytes'],sha256=expected['sha256'])])

        @contextmanager
        def source():
            result,_=self._call('download',PREFIX+'/im/v1/messages/'+message_id+'/resources/'+key+'?type=file',
                operation='download:'+message_id+':'+key,download=expected,frozen_extra=origins)
            path=no_symlinks(Path(result['path']))
            with path.open('rb') as stream:
                yield stream
            # A successful download is not sufficient when bound evidence changed
            # during the local custody copy. The original acceptance stays atomic.
            self.binding()
            if any(file_sha(Path(p))!=h for p,h in origins.items()) or file_sha(path)!=expected['sha256']:
                raise ContractError('download or message evidence changed during custody copy')

        result=intake.receive(envelope,0,source)
        row=intake.registry.entry(result['entry_id'])
        if (row['sha256'],row['size_bytes'])!=(expected['sha256'],expected['size_bytes']):
            intake.registry.health(result['entry_id'],'suspect','Pilot approved sample differs')
            raise ContractError('accepted original differs from frozen sample; preserve acceptance')
        self.binding()
        return dict(receipt=result,status=intake.status(result['entry_id']),
                    historical_completeness=self.value(scan_id+':result')['status'],
                    permanent_backfill_verified=False)
