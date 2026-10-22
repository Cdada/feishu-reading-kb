"""Design evidence-bound test candidates for different local knowledge bases.

No model, network, corpus ingestion, answer generation or automatic gold labels.
Profile features must be established from the new corpus, never from old answers.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import re

from .contracts import ContractError

VERSION = 'question-blueprints-v1'
FEATURES = frozenset({'definitions', 'source_text', 'comparative_material', 'requirements',
    'relationships', 'dated_records', 'version_history', 'research_findings', 'methods_tools',
    'constraints', 'coverage_requirements', 'field_definitions', 'quantitative_records', 'plans'})
ROLES = frozenset({'original', 'converted_original', 'source_note', 'interpretation',
                  'personal_note', 'record', 'dataset', 'unknown'})


@dataclass(frozen=True)
class Blueprint:
    id: str
    label: str
    direction: str
    feature: str | None
    minimum_sources: int
    question: str
    paraphrase: str
    criteria: tuple[str, ...]
    capability: str = 'text'


BLUEPRINTS = (
    Blueprint('definition', '概念与边界', '定义、范围、适用条件', 'definitions', 1,
        '根据库中资料，{subject}如何定义，适用范围和不适用情况是什么？',
        '请解释{subject}的含义，并说明这个说法在哪些条件下成立、哪些情况下不适用。',
        ('概念定义有依据', '明确适用边界', '原文未说明的边界不自行补全')),
    Blueprint('inventory', '清单与覆盖', '资料范围、已有与缺失', None, 1,
        '当前知识库中，与{subject}有关的资料有哪些？请按材料性质分类并说明覆盖范围。',
        '如果我要了解{subject}，库里能找到哪些材料，各自属于什么类型，范围到哪里？',
        ('清单来自实际可访问资料', '原件与解读分开标识', '不声称覆盖库外全部资料')),
    Blueprint('provenance', '出处与依据', '原文位置、来源身份', 'source_text', 1,
        '关于{subject}，哪些表述能在原文中直接找到依据？请给出原文位置并区分归纳。',
        '请追溯{subject}相关说法的原文依据：哪里直接写了，哪里属于后来的解释？',
        ('引文及位置真实', '证据实际支持主张', '不把解读当成原文')),
    Blueprint('comparison', '异同与比较', '相同点、差异、比较口径', 'comparative_material', 2,
        '关于{subject}，《{first}》与《{second}》有哪些相同和不同之处？比较口径是否一致？',
        '请对照《{first}》和《{second}》说明{subject}：哪些一致、哪些不同，是否适合直接比较？',
        ('两份指定材料均被使用', '比较维度一致', '无法比较时解释原因')),
    Blueprint('alignment', '要求与对应', '规范、任务、成果的映射', 'requirements', 2,
        '关于{subject}，《{first}》与《{second}》中的要求如何对应？有哪些已对应项和缺项？',
        '请把《{first}》与《{second}》涉及{subject}的要求逐项对照，列出对应关系及尚缺部分。',
        ('对应关系有双侧依据', '不把相似名称视为相同要求', '缺项与不适用分开')),
    Blueprint('relationship', '关系与依赖', '职责、上下游、承接关系', 'relationships', 1,
        '资料如何描述{subject}中各对象的分工、依赖和承接关系？哪些关系尚未明确？',
        '围绕{subject}，谁负责什么、前后如何衔接，库中还有哪些关系没有讲清楚？',
        ('关系方向正确', '明确职责与推测分开', '不凭名称推断因果或依赖')),
    Blueprint('timeline', '历史与进展', '事件时间、实际完成与未来计划', 'dated_records', 1,
        '根据已有记录，{subject}如何随时间推进？请区分已发生事项、当时判断和后续计划。',
        '请按时间梳理{subject}，分别列出实际发生的事情、当时的看法和仍属计划的内容。',
        ('时间来自可信记录', '不把入库时间当事件时间', '不把计划表述为完成')),
    Blueprint('version', '版本与变化', '修订内容、替代关系、效力边界', 'version_history', 2,
        '关于{subject}，《{first}》与《{second}》之间发生了哪些变化？能否确认版本替代关系？',
        '请比较《{first}》和《{second}》对{subject}的表述变化，并说明是否有明确证据证明谁替代谁。',
        ('版本身份明确', '变化可定位', '不以入库先后判断现行或替代')),
    Blueprint('synthesis', '综合与总结', '关键观点、成果、差异与局限', 'research_findings', 1,
        '库中资料对{subject}有哪些主要发现或结论？请概括共同点、差异和证据限制。',
        '请总结资料对{subject}得出了什么：哪些结论一致、哪些仍有分歧，依据有什么局限？',
        ('保留重要结论与限制', '不重复计算同源证据', '明确归纳与原文陈述')),
    Blueprint('recommendation', '方法与建议', '工具、方法、选择条件', 'methods_tools', 1,
        '针对{subject}，库中记录了哪些方法或工具？各自的适用条件、限制和依据是什么？',
        '如果要处理{subject}，可以从库中选择哪些方法或工具，为什么适用，又有什么限制？',
        ('推荐来自实际记录', '条件与限制可核验', '不把未记录的能力当已验证功能')),
    Blueprint('feasibility', '可行性与约束', '条件、资源、风险、能做与不能做', 'constraints', 1,
        '根据库中条件与约束，{subject}哪些部分可行，哪些暂不能确定，还需要补充什么？',
        '就现有资料而言，{subject}能做到什么、受哪些条件限制，仍缺哪些判断依据？',
        ('结论有条件支撑', '未知与不可行分开', '不把建议写成已具备的条件')),
    Blueprint('gap_audit', '完整性与缺口', '遗漏、矛盾、待澄清事项', 'coverage_requirements', 2,
        '对照库中的要求与已有材料，{subject}有哪些遗漏、矛盾或需进一步澄清的问题？',
        '请核查{subject}是否覆盖了资料中的要求，并列出证据不足、互相冲突或尚未解释的部分。',
        ('检查范围明确', '缺口有对应要求', '无法核查不等于已确认缺失')),
    Blueprint('data_interpretation', '字段与口径', '指标含义、单位、粒度与来源', 'field_definitions', 1,
        '关于{subject}，资料中的字段或指标分别是什么意思，单位、时间和对象粒度如何界定？',
        '请解释{subject}涉及的数据口径：字段含义、计量单位、时间范围和统计对象分别是什么？',
        ('字段解释来自相应资料', '同名字段不自动视为同义', '单位和对象粒度准确')),
    Blueprint('calculation', '数量与核算', '统计范围、公式、重复与缺失', 'quantitative_records', 1,
        '库中数据能否支持核算{subject}的数量或差异？请先说明范围、口径、去重规则和可复核方法。',
        '要核对{subject}的数量或差异，现有数据是否足够？统计范围、计算方法和重复缺失应怎样处理？',
        ('有实际数据才给数值', '计算可复核', '不把摘录数字当作已重新核算'), 'tabular'),
    Blueprint('planning', '方案与组织', '目标、步骤、资源、约束与取舍', 'plans', 1,
        '依据已有资料，{subject}可以怎样组织目标、步骤和成果？请区分已有约定与新增建议。',
        '请围绕{subject}提出有依据的安排：目标、实施步骤、交付内容各是什么，哪些是建议？',
        ('回应目标及约束', '已有承诺与建议分开', '重要取舍有证据或明确假设')),
)
BY_ID = {b.id: b for b in BLUEPRINTS}


def _text(value, name, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError('invalid test profile text: ' + name)
    return value


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {'corpus_id', 'sources', 'subjects', 'capabilities'}:
        raise ContractError('complete explicit test profile required')
    _text(profile['corpus_id'], 'corpus_id', 100)
    if (not isinstance(profile['sources'], list) or not 1 <= len(profile['sources']) <= 5000
            or not isinstance(profile['subjects'], list) or not 1 <= len(profile['subjects']) <= 200
            or not isinstance(profile['capabilities'], list)
            or any(c not in ('text', 'tabular', 'spatial', 'vision') for c in profile['capabilities'])):
        raise ContractError('test profile size or capabilities invalid')
    sources = {}
    for s in profile['sources']:
        if not isinstance(s, dict) or set(s) != {'id', 'title', 'role', 'body_sha256'}:
            raise ContractError('explicit source identity required')
        key = _text(s['id'], 'source.id', 100)
        _text(s['title'], 'source.title')
        if (key in sources or not isinstance(s['role'], str) or s['role'] not in ROLES
                or not isinstance(s['body_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', s['body_sha256'])):
            raise ContractError('duplicate or invalid source identity')
        sources[key] = s
    subject_ids = set()
    for s in profile['subjects']:
        if not isinstance(s, dict) or set(s) != {'id', 'label', 'source_ids', 'feature_evidence'}:
            raise ContractError('explicit subject and evidence required')
        key = _text(s['id'], 'subject.id', 100)
        _text(s['label'], 'subject.label')
        ids, features = s['source_ids'], s['feature_evidence']
        if (key in subject_ids or not isinstance(ids, list) or not ids
                or any(not isinstance(i, str) or i not in sources for i in ids)
                or len(set(ids)) != len(ids) or not isinstance(features, dict)
                or not set(features) <= FEATURES):
            raise ContractError('invalid subject evidence')
        subject_ids.add(key)
        for feature, evidence in features.items():
            if (not isinstance(evidence, list) or not evidence
                    or any(not isinstance(i, str) or i not in ids for i in evidence)
                    or len(set(evidence)) != len(evidence)):
                raise ContractError('feature must reference actual subject sources: ' + feature)
    return sources


def design_candidates(profile, *, limit=100, paraphrases=False):
    """Instantiate question types only where the caller supplies feature evidence.

    Feature evidence is a reviewable design input, not proof of answerability.
    The output always requires source inspection and gold review before scoring.
    """
    sources = validate_profile(profile)
    if type(limit) is not int or not 1 <= limit <= 1000 or type(paraphrases) is not bool:
        raise ContractError('candidate limit must be 1–1000; paraphrases must be boolean')
    profile_sha = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    families, unavailable = [], []
    for subject in sorted(profile['subjects'], key=lambda s: s['id']):
        for b in BLUEPRINTS:
            ids = sorted(subject['source_ids'] if b.feature is None else subject['feature_evidence'].get(b.feature, []))
            if len(ids) < b.minimum_sources:
                unavailable.append(dict(subject_id=subject['id'], type=b.id, reason='feature_evidence_not_established'))
                continue
            group = hashlib.sha256(json.dumps([profile['corpus_id'], subject['id'], b.id]).encode()).hexdigest()[:20]
            slots = dict(subject=subject['label'], first=sources[ids[0]]['title'],
                         second=sources[ids[1]]['title'] if len(ids) > 1 else '')
            missing = b.capability not in profile['capabilities']
            shared = dict(parent_group=group, corpus_id=profile['corpus_id'], subject_id=subject['id'],
                type=b.id, direction=b.direction, evidence_candidates=ids,
                evidence_bindings={key: sources[key]['body_sha256'] for key in ids},
                source_roles={key: sources[key]['role'] for key in ids},
                criteria=list(b.criteria), gold_status='unreviewed', answerability=None, split=None,
                required_capability=b.capability,
                intended_check='capability_boundary' if missing else 'answer_quality',
                capability_gap=missing, profile_sha256=profile_sha)
            family = [dict(deepcopy(shared), id=group+'-base', question=b.question.format(**slots), variant='base', variant_of=None)]
            if paraphrases:
                family.append(dict(deepcopy(shared), id=group+'-paraphrase', question=b.paraphrase.format(**slots),
                                   variant='meaning_preserving_draft', variant_of=group+'-base'))
            families.append(family)
    # Keep base/variant families together. A small cap must not silently break
    # paired tests or make a generated paraphrase an independent coverage case.
    cases, omitted = [], []
    for family in families:
        if len(cases) + len(family) <= limit:
            cases.extend(family)
        else:
            omitted.append(family[0]['parent_group'])
    return dict(version=VERSION, profile_sha256=profile_sha, candidates=cases,
        available_families=len(families), generated_families=len({c['parent_group'] for c in cases}),
        omitted_by_limit=omitted, unavailable_types=unavailable, model_calls=0,
        gold_reviewed=0, limitation='候选题与特征关联已建立；尚未核验正文、参考答案或跨库回答质量。')
