"""Exact Pilot PDF delivery through the existing bounded official CLI session.

The downloaded message resource, rather than an assumed equality of upload and
message keys, proves byte identity. This never approves or publishes a candidate.
"""
from .contracts import ContractError
from .f01 import PREFIX, encoded, identity, sha, validated_message_content
from .f05 import file_sha
from .staging import Staging

PREVIEW_LIMIT = 30 * 1000 * 1000


def deliver_preview(session, previews, pid):
    """One candidate, one upload/send, one independent byte verification.

    All operation names derive from the persisted publication identity. A killed
    unfinished request remains stopped; saved successful receipts can resume the
    local binding without creating another write request.
    """
    session.check()
    a=session.approval
    if (previews.scope.app_id,previews.scope.chat_id,previews.scope.user_id)!=(a['app_id'],a['chat_id'],a['account_id']):
        raise ContractError('preview scope differs from approved objects')
    if session.value('isolation_verified') is not True:
        raise ContractError('verify the fixed group before delivering a preview')
    row=previews.publisher._row(pid)
    if row['state']!='prepared':
        raise ContractError('only a prepared candidate can be delivered')
    entry=previews.registry.entry(row['entry_id'])
    original=previews.registry.runtime.checked(previews.registry.runtime.path(entry['relative_path']))
    if session.binding().get(str(original))!=entry['sha256']:
        raise ContractError('candidate original must be an explicitly frozen approved input')
    previews.stage(pid)
    with Staging(previews.registry).input_binding(row['entry_id'],'pilot_preview_delivery'):
        def candidate():
            session.check()
            with previews.registry.transaction() as db:
                previews._candidate(db,pid);previews._current(db,pid)
            current=previews.publisher._row(pid)
            if current['state']!='prepared':
                raise ContractError('candidate is no longer prepared')
            tree=previews.publisher._tree(current,pid+'.candidate')
            pdf=tree/'发布记录/已确认入库预览.pdf'
            if not 0<pdf.stat().st_size<=PREVIEW_LIMIT or file_sha(pdf)!=row['preview_sha256']:
                raise ContractError('exact bounded candidate PDF required')
            return pdf

        pdf=candidate()
        plan=dict(publication_id=pid,pdf_path=str(pdf),sha256=row['preview_sha256'],
                  size_bytes=pdf.stat().st_size,filename='preview-'+pid+'.pdf')
        session.bind('preview:'+pid,plan)
        frozen={str(pdf):plan['sha256']}
        prefix='preview:'+pid+':'

        def call(kind,path,**kwargs):
            candidate()
            value,key=session._call(kind,path,operation=prefix+kind,
                frozen_extra=frozen,preview=plan,**kwargs)
            candidate()
            receipt=session.root/(key+'.response.local')
            frozen[str(receipt)]=file_sha(receipt)
            return value,receipt

        try:
            upload,upload_receipt=call('upload',PREFIX+'/im/v1/files',mode='upload')
            upload_key=identity(upload.get('data',{}).get('file_key'),'file_')
            # The upload receipt itself is frozen in every following call.
            plan=dict(plan,upload_receipt=str(upload_receipt))
            body=encoded(dict(receive_id=a['chat_id'],msg_type='file',content=encoded(dict(file_key=upload_key)),
                uuid='preview-'+sha((a['app_id']+':'+a['chat_id']+':'+pid).encode())[:40]))
            sent,send_receipt=call('send',PREFIX+'/im/v1/messages?receive_id_type=chat_id',mode='send',body=body)
            item=sent.get('data')
            try:
                content=validated_message_content(item,app_id=a['app_id'],bot_id=a['bot_id'],
                    chat_id=a['chat_id'],kind='file',name=plan['filename'])
            except (ValueError,KeyError,TypeError) as error:
                raise ContractError('invalid preview message receipt') from error
            mid=item['message_id'];resource=content['file_key']
            plan=dict(plan,send_receipt=str(send_receipt))
            download=dict(message_id=mid,file_key=resource,filename=plan['filename'],sample_path=str(pdf),
                          sha256=plan['sha256'],size_bytes=plan['size_bytes'])
            checked,receipt=call('download',PREFIX+'/im/v1/messages/'+mid+'/resources/'+resource+'?type=file',download=download)
            candidate()
            if checked['sha256']!=plan['sha256']:
                raise ContractError('downloaded preview differs')
            previews.bind_delivery(pid,app_id=a['app_id'],chat_id=a['chat_id'],message_id=mid,
                pdf_sha256=plan['sha256'],receipt_ref=file_sha(receipt),outcome='sent',checked_access=session.binding)
            return dict(status='awaiting_owner_decision',publication_id=pid,message_id=mid,
                        pdf_sha256=plan['sha256'],byte_verification='passed',published=False)
        except Exception as error:
            session.append(dict(stopped=prefix,error_type=type(error).__name__))
            raise
