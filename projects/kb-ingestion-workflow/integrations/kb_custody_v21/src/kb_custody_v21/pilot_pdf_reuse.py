"""Explicit reuse of checked PDF evidence across independently accepted copies.

No old task is assigned to the new intake. A fresh local conversion must match
the original bytes, parser, page map and every native image locator and hash.
This adapter reads saved observations; it has no model or transport client.
"""
from copy import deepcopy
from pathlib import Path

from .contracts import ContractError
from .conversion import CustodyConversion, durable_new, sha
from .material_review import MaterialReviews
from .registry import encoded


class PdfEvidenceReuse:
    def __init__(self, registry, *, prior_extraction_id, prior_review_id,
                 target_entry_id, target_extraction_id, observations):
        self.registry, self.runtime = registry, registry.runtime
        self.conversion, self.reviews = CustodyConversion(registry), MaterialReviews(registry)
        self.prior, self.review_id = prior_extraction_id, prior_review_id
        self.entry, self.target = target_entry_id, target_extraction_id
        self.observations = deepcopy(observations)
        self.initial = self._check()

    def _check(self):
        prior = self.reviews.require_qualified(self.prior)
        target = self.reviews.binding(self.entry, self.target)
        if (prior['review_id'] != self.review_id or self.prior == self.target
                or prior['binding']['entry_id'] == self.entry
                or prior['binding']['source_sha256'] != target['source_sha256']):
            raise ContractError('exact qualified review and independently accepted identical PDF required')
        _, old_map, _ = self.conversion.extraction(self.prior)
        _, new_map, _ = self.conversion.extraction(self.target)
        if any(m['extractor'] != 'pypdf' or not m.get('pdf_image_inventory') for m in (old_map,new_map)):
            raise ContractError('both PDF expressions must include checked native image inventories')
        old_images = self.conversion.registered_images(self.prior)
        new_images = self.conversion.registered_images(self.target)
        def identity(image):
            return encoded({k:image[k] for k in ('source_page_index','object_path','sha256','size_bytes',
                'extraction_method','extraction_version','resolution','representation','role')})
        old = {identity(i):i for i in old_images}; new = {identity(i):i for i in new_images}
        if len(old)!=len(old_images) or len(new)!=len(new_images) or set(old)!=set(new):
            raise ContractError('PDF image bytes or physical object locators differ')
        ids = {old[k]['asset_id']:new[k]['asset_id'] for k in old}
        def canonical(mapping, rename):
            value = deepcopy(mapping)
            for key in ('extraction_id','source_asset_id','output_asset_id','extracted_at','parent_extraction_id'):
                value.pop(key,None)
            for item in value['image_mappings']:
                for key in ('image_asset_id','local_representation_asset_id'):
                    item[key] = rename.get(item[key],item[key])
            return value
        if canonical(old_map,ids)!=canonical(new_map,{}):
            raise ContractError('PDF text, parser, page mapping or image mapping differs')
        by_id={i['asset_id']:i for i in old_images}
        ancestors={r['extraction_id'] for r,_,_ in self.conversion.extraction_ancestry(self.prior)}
        if not isinstance(self.observations,list) or len(self.observations)>len(old_images):
            raise ContractError('bounded saved observations required')
        observed=[]
        for observation in self.observations:
            aid=observation.get('image_asset_id');image=by_id.get(aid)
            locator=observation.get('source_locator',{})
            if (not image or observation.get('reused_from')
                    or observation.get('cloud_original_asset_id')!=aid
                    or locator.get('extraction_id') not in ancestors
                    or locator.get('source_sha256',old_map['source_sha256'])!=old_map['source_sha256']
                    or locator.get('physical_page',image['source_page_index']+1)!=image['source_page_index']+1
                    or locator.get('pdf_object','/'.join(image['object_path']))!='/'.join(image['object_path'])
                    or observation.get('original_sha256',image['sha256'])!=image['sha256']
                    or observation.get('reading_status') not in {'read','partially_read'}
                    or observation.get('validation_mode') not in {'offline_mock','real_model_observation_pending_quality_review'}
                    or any(not isinstance(observation.get(k),str) or not observation[k]
                           for k in ('observation_id','reader','read_at','consent_event_id'))):
                raise ContractError('saved observation identity or completed provenance differs')
            observed.append(aid)
        if len(set(observed))!=len(observed):
            raise ContractError('duplicate saved image observation')
        return dict(prior_review=prior,target_binding=target,image_id_map=ids,images=new_images,
                    observations_sha256=sha(encoded(self.observations).encode()))

    def checked(self):
        value=self._check()
        if value!=self.initial:
            raise ContractError('PDF reuse source, review or target changed')
        return value

    def record_review(self, evidence_path):
        """Append a target-specific review with an explicit immutable reuse proof."""
        checked=self.checked();path=self.runtime.checked(Path(evidence_path))
        if not path.is_relative_to(self.runtime.root/'conversion-work'):
            raise ContractError('reuse evidence must be inside conversion-work')
        proof=dict(reference='pdf-evidence-reuse/v1',prior_extraction_id=self.prior,
            target_extraction_id=self.target,**checked,original_observations=self.observations,
            new_model_calls=0,owner_confirmation='still_required')
        path.parent.mkdir(parents=True,exist_ok=True)
        durable_new(path,encoded(proof).encode())
        report=deepcopy(checked['prior_review']['report'])
        eid='pdf-reuse-'+self.target
        report['operator']='verified identical PDF evidence reuse'
        report['evidence'].append(dict(evidence_id=eid,relative_path=self.runtime.relative(path),
                                       sha256=sha(encoded(proof).encode())))
        for item in report['checks']+report['pages']:
            item['note']='复用同字节 PDF 的既有复核；正文分页及原图位置、字节已比对。'+item['note']
            item['evidence_refs'].append(eid)
        self.checked()
        return self.reviews.record(self.entry,self.target,report)

    def read(self, original):
        """ImagePolicies reader adapter; preserve actual historical reading facts."""
        checked=self.checked();new={i['asset_id']:i for i in checked['images']}
        aid=original.get('asset_id')
        if aid not in new or any(original.get(k)!=new[aid][k] for k in ('path','sha256','size_bytes','extraction_id')):
            raise ContractError('reuse target image changed')
        old=next((k for k,v in checked['image_id_map'].items() if v==aid),None)
        matches=[o for o in self.observations if o['image_asset_id']==old]
        if len(matches)!=1:raise ContractError('image has no saved observation; no new reading permitted')
        saved=matches[0];result=deepcopy(saved)
        result.update(image_asset_id=aid,cloud_original_asset_id=aid,
            source_locator=dict(saved['source_locator'],extraction_id=new[aid]['extraction_id']),
            reused_from=dict(observation_id=saved['observation_id'],image_asset_id=old,
                extraction_id=self.prior,review_id=self.review_id,observation_sha256=sha(encoded(saved).encode()),
                original_source_locator=deepcopy(saved['source_locator']),new_model_call=False))
        return result
