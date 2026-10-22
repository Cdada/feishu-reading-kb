# 从历史问答提炼可迁移的测试题型

历史问题用于发现用户需要哪些回答能力。新知识库的测试题必须从新资料的内容和证据关系出发，不能仅替换旧问题中的专有名称。

```mermaid
flowchart LR
    A[历史问题] --> B[题型、方向、证据结构]
    C[新库资料与来源] --> D[核定知识库特征]
    B --> E[选择适用题型]
    D --> E
    E --> F[生成候选题与问法变形]
    F --> G[核定可回答程度与参考证据]
    G --> H[提示词对照及端到端回归]
```

## 问题的四个维度

- **类型**：希望系统完成什么思考任务，例如比较、找出处、判断可行性。
- **方向**：关注什么，例如时间、职责、字段口径、适用条件。
- **证据结构**：单份原文、多份比较、时间记录、版本链或结构化数据。
- **边界与风险**：证据是否足够，能否确认现行性，是否需要计算或图像理解。

`qa_test_design.BLUEPRINTS` 提供 15 类模板：概念与边界、清单与覆盖、出处与依据、异同比较、要求对应、关系依赖、历史进展、版本变化、综合总结、方法建议、可行性、完整性缺口、字段口径、数量核算、方案组织。每类包含方向、适用特征、最低来源数、基础问法、同义问法和评分检查点。

## 根据新库特征生成候选题

`design_candidates(profile, limit=100, paraphrases=False)` 是纯本地函数，不读取磁盘、不调用模型、不联网，不接入日常服务。调用方先建立以下结构：

```json
{
  "corpus_id": "synthetic-orbit-v1",
  "capabilities": ["text"],
  "sources": [
    {"id":"a","title":"轨道测量实验","role":"original","body_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ],
  "subjects": [
    {"id":"orbit","label":"轨道测量","source_ids":["a"],
     "feature_evidence":{"definitions":["a"],"research_findings":["a"]}}
  ]
}
```

示意中的哈希不是实际证据。真实使用须读取正文计算摘要，并核对每项特征对应的来源。支持的特征见 `FEATURES`，未提供的特征不会从文件名或题目中猜出；比较和版本类题需要至少两份不同来源。角色标记由资料核验产生，不能因模型解释而自动赋予“官方”身份。

输出的每题包含：类型、方向、所需能力、候选证据及哈希、来源角色、评分要点、题目家族和变体关系。所有题均为 `gold_status=unreviewed`、`answerability=null`。绑定来源 ID 仅建立可复查关系，不证明这些来源足以回答，也不等于金标准已经核定。资料或特征无法确认时，缺口要显式报告。

基础题和同义问法共享 `parent_group`；切分开发集、回归集和保留集时必须整组划分。返回题量受上限限制时不拆开题目对，遗漏家族单独报告。生成同义句后仍需复核是否保留原意；改变时间、对象或比较范围属于另一类变化，需要重新核定期望答案，不能沿用旧答案。

数量核算类题若系统没有表格能力，会标记为 `capability_boundary`，用于检查能否诚实说明能力限制；不能把安全拒答记作成功完成核算。

## 三条评测线

1. **提示词对照**：固定问题、证据包、模型及参数，只改变提示词；匿名比较答案，报告胜/平/负及关键错误。不能把检索差异归因于提示词。
2. **问题变化**：同义改写测试稳定性，时间/范围变化测试敏感性，追问测试上下文边界。近重复题不得跨开发集与保留集。
3. **跨库与端到端回归**：将通用规则用于不同资料画像，逐库报告检索与回答成绩；新库未测试前，只能声称工具支持换库。合成异领域测试验证程序行为，不能证明真实跨库回答质量。

知识库画像目前由核验后的结构化输入提供，尚未实现从任意知识库自动识别特征、金标准标注或模型裁判。后续应先建立只读资料适配器，再校准特征识别；不在此函数里添加自动入库或联网能力。

真实历史题、资料画像、原文、哈希清单和报告只存本地忽略目录；公开仓库使用独立合成示例。使用现有累计账本，新评测预算不得绕过或重置。

评测方法参考 [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)：按真实任务构建题目，结合自动检查与人工校准，并留意模型裁判的位置和篇幅偏差。本模块不依赖云端评测服务。


## 只读资料适配与独立索引

`qa_benchmark_corpus.BenchmarkCorpus` 可以只读使用 ZIP 中明确选中的 `.md` / `.txt` 转换成果。它与飞书入库提供器分开，不伪造确认记录、不自动批准来源、不解压原件。可传给既有 `QAIndex.sync()`，输出目录应选独立、被 Git 忽略的评测目录，不能复用日常问答索引。

```python
from pathlib import Path
from kb_custody_v21.qa_benchmark_corpus import BenchmarkCorpus
from kb_custody_v21.qa_index import QAIndex

selection = [{
    "id": "sample-a", "path": "sample/experiment.md",
    "title": "轨道测量实验", "role": "record", "canonical_id": "experiment-a",
}]
with BenchmarkCorpus("sample.zip", selection) as sources:
    # 先读取、核对正文，保存 body_sha256；复跑时在 selection 中填入该值。
    source = sources.get("sample-a")
    index = QAIndex(Path(".dev-runtime/qa-eval/index.sqlite3").resolve())
    index.sync(sources)
```

读取器显式使用 UTF-8 文件名编码，检查路径冲突、符号链接、重复选择、文件及总量上限；忽略 macOS 元数据。正文必须已是有效 UTF-8，不在此处重新 OCR。提供正文哈希时，不一致即失败。来源角色与 canonical_id（同源资料分组标识）由调用方核验，不能因文件夹名或相同标题就确认来源权威、版本替代或派生关系；相同正文哈希只证明文字一致。

输出标记 `benchmark_only=true`，日期与完整性保持未知；不会用 ZIP 修改时间补成发表日期。`body_sha256` 绑定解码后的文字，`source_sha256` 绑定 ZIP 条目原始字节，两者不等于整份原始 PDF 的哈希。证据校准应保存具体范围、原文位置、必答要点和禁止结论；对转换文字的未检出结果，不可扩张为全部原件或库外资料从未出现。

本地合成验证命令（仓库根目录）：

```sh
PYTHONPATH=projects/kb-ingestion-workflow/integrations/kb_custody_v21/src:projects/kb-ingestion-workflow/integrations/kb_custody_v21/tests python -m unittest test_qa_test_design test_qa_benchmark_corpus test_qa_evaluation
```


## 变体与测试分组

`qa_test_sets.make_variant()` 保留题目家族与候选证据，只复制明确字段。改变时间、材料范围、来源身份或对话条件后，不继承旧标准答案、可回答标签或测试成绩；同义改写也必须复核。输出始终是待审候选题，`proposed_expected_behavior` 只表示出题者建议检查的行为。

`partition_cases(cases, seed=...)` 在划分开发、回归、保留集之前，按题目家族、完全相同正文哈希，以及调用方提供的 `source_lineage`（已核定的同源关系）合并组。同源关系必须来自核查，不能只因标题相同就指定。同一组中任何题已经用于调试（`seen_in_development=true`），整组留在开发集。建题者阅读资料、建立标签本身不等于已将该题用于调试；应按实际用途记录。

默认60/20/20是目标比例。只有一个相互关联的大组时，不会强拆成三个集合制造独立性；报告实际数量、无候选证据题和未核定金标准题。候选检索来源也可能误连成大组，因此这一步只是分组提案，正式冻结前应先核准实际证据及派生关系。近重复但未登记同源关系的材料仍可能泄漏，需要额外复核。

这个模块不执行问答、不评分，也不会把 `gold_status=reviewed` 标签当作它已经验证过事实。语义评测前仍需独立核对证据、必答点和禁止结论；已用于修复的保留题应转入回归范围，并补充新的保留题。


## Freeze the evidence supplied to a question

`qa_test_fixtures.compile_fixture` produces two separate objects: `model_input`
(question, explicit constraints, conversation and authorized source spans) and
`expected` (private reference criteria). Reference claims never enter the model
input. The caller supplies loaded source records; this function does not open
files, grant access, retrieve documents or run a model.

Each source span is bound to the complete source body's SHA-256. A changed
source requires review. Input, rubric and complete fixture have separate hashes,
so a rubric edit can be distinguished from an evidence change. Context character
counts are reported for later budgeting; no silent truncation is performed.

Answerability is explicitly relative to the supplied evidence. A partial answer
in this test does not mean that the complete knowledge base lacks the answer.
Use a separate retrieval/coverage audit for that claim. Run fixed-context tests
to compare answer instructions, then end-to-end tests to assess the combined
retrieval and answer system. Correctly handling missing evidence is scored
separately from actually resolving the user's business question.

Treat document text as untrusted evidence, including embedded instructions.
Permission tests use synthetic inaccessible documents; do not load private
forbidden content into the candidate model merely to test whether it reveals it.
Fixture validation checks identities, hashes and spans, not semantic quality.
