"""Explicit image-byte consent boundary; only injected offline readers in DEV."""
from datetime import datetime,timezone
from io import BytesIO
from PIL import Image
from .contracts import ContractError,new_id
from .conversion import sha
from .content import exact
from .registry import now


class VisualReader:
    def __init__(self, runtime, provider, consents):
        self.runtime,self.provider,self.consents = runtime,provider,consents

    def read(self,original):
        if not self.provider.is_mock:
            raise ContractError('DEV visual reader refuses unapproved real model calls')
        consent = self.consents.get(original['asset_id'])
        if not consent: raise ContractError('image consent missing')
        exact(consent,('event_id','asset_id','sha256','model','expires_at'))
        expiry = datetime.fromisoformat(consent['expires_at'])
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) or not consent['event_id'] or (
            consent['asset_id'],consent['sha256'],consent['model']) != (original['asset_id'],original['sha256'],self.provider.model):
            raise ContractError('image consent expired or mismatched')
        data = self.runtime.checked(original['path']).read_bytes()
        if sha(data) != original['sha256']: raise ContractError('image changed after authorization')
        with Image.open(BytesIO(data)) as image:
            resolution = list(image.size)
            image.verify()
        response = self.provider.read(data,asset_id=original['asset_id'],sha256=sha(data),resolution=resolution)
        exact(response,('transcribed_text','interpretation','reading_status','unresolved'))
        if response['reading_status'] not in {'read','partially_read','failed'}: raise ContractError('invalid actual reading result')
        return dict(observation_id=new_id('observation'),image_asset_id=original['asset_id'],cloud_original_asset_id=original['asset_id'],
            source_locator=dict(extraction_id=original.get('extraction_id'),section=None,caption=original.get('caption'),page=None),
            read_asset_id=original['asset_id'],read_resolution=resolution,read_representation='original',
            **response,reader=self.provider.model,read_at=now(),consent_event_id=consent['event_id'],evidence_refs=[],validation_mode='offline_mock')


def render_image_notes(observations, *, human_labels=False):
    if not observations: return None
    paragraphs = []
    for index, item in enumerate(observations, 1):
        if item['reading_status'] not in {'read','partially_read'}:
            raise ContractError('unfinished reading has no finished image notes')
        title = f'图像记录 {index}' if human_labels else f"## 图像记录 {item['observation_id']}"
        paragraphs += [title,
                       '识读状态：'+('部分可读' if item['reading_status']=='partially_read' else '已识读'),
                       '识读文字：'+item['transcribed_text'], '整理解释：'+item['interpretation']]
        if item.get('validation_mode') == 'offline_mock':
            paragraphs.append('验证范围：离线合成识读。')
        elif item.get('validation_mode') == 'real_model_observation_pending_quality_review':
            paragraphs.append('验证范围：真实模型识读，质量待复核。')
        if item['unresolved']: paragraphs.append('未确认：'+'；'.join(item['unresolved']))
    return ('\n\n'.join(paragraphs)+'\n').encode()
