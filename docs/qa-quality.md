# 问答评分与回归

本工具评价固定证据下的回答质量。资料、问题、模型配置及评分版本均应固定；真实原文、题目和报告留在本地，公开测试只用合成材料。

## 评分与报告

`qa_quality.answer_packet()` 复用服务的原有回答提示词与输出约束，只有问题、约束、对话和证据进入回答请求。`judge_packet()` 另行构造匿名评分请求，提供参考要点，但不提供被测模型名称。实际服务仍通过 `qa_model` 导出原接口；提示词和引用验证已原样提取至 `qa_answer_contract`，不改变日常回答行为。

`assess()` 首先用服务的同一函数验证回答结构和逐字引文，再读取绑定题包、答案、评分规则和裁判版本的判断。五项权重为事实35、证据25、覆盖20、边界10、可用性10；各项按0–4档折算。每个必答点、禁止结论和主张必须得到有理由的判断，不能遗漏；裁判不确定时进入待复核。额外且有依据的正确内容不因与参考答案措辞不同被扣分。

85分只是初始门槛。关键错误、禁止结论、无依据主张、遗漏必答点和有证据却拒答均会阻止通过，不能被其他高分抵消。引用存在只证明文字匹配，不证明支持关系成立。

校准使用单独提供的参考判定：至少20个不同样本，包含正确、错误及关键错误，判定一致率至少90%，关键错误必须全部一致。合成参考只用于验证程序，不解锁真实质量放行；独立复核参考还必须匹配资料库、裁判和评分版本。调用方负责参考来源真实性，程序的哈希和标签不构成人工复核证明。

`make_report()` 保留每道计划题，缺测、错误、跳过、未知调用和待复核分别统计。完整支持题与边界题分开算平均分；未校准分数标为暂定，不计通过。报告按题型、可回答程度、测试分区汇总，同时记录缓存复用与模型次数。`markdown_report()` 输出简明中文摘要和逐题明细。

`compare_reports()` 只比较资料、题包、评分规则、裁判和校准记录一致的运行。提示词对照还要求模型、参数和代码版本一致。报告列出新失败、修复、关键题退步、总体及分组分数下降。首版采用严格门槛：所有计划题通过才能建议放行；即使如此，整体通过率下降超过3个百分点、分项平均分下降超过3分或切片下降超过5分仍须复核。它不自动部署、回退或启动服务。

`retrieval_metrics()` 另报前5/8来源覆盖及前12片段证据覆盖，允许一组等价证据。没有正向检索标注时返回未定义指标，不能以“相关结果非空”直接判错。这些检索指标不等于回答准确率。

## 验证

```sh
PYTHONPATH=projects/kb-ingestion-workflow/integrations/kb_custody_v21/src:projects/kb-ingestion-workflow/integrations/kb_custody_v21/tests python -m unittest test_qa_quality
```

合成用例包括引用伪造、错误引用含义、虚高评分、错误拒答、裁判缺项、未校准、版本不一致、保留缺测分母及等价来源。测试通过不表示真实知识库答题质量通过。

方法参考：[OpenAI Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)。任务化评分、裁判校准和分组回归在本地实现，不依赖云端Evals产品，也不会因为导入模块产生模型调用。

## 模型执行与累计额度

`qa_quality_runner.run_case()` 可通过调用方提供的回答与裁判适配器串联一题；不提供完整授权和适配器时返回 `skipped`。`EvaluationCalls` 在现有 `DailyStore` 的 `daily_models` 表中预留次数，再执行一次调用。回答、裁判、失败和不明结果使用同一累计上限，没有新的免费计数器。

评测授权必须绑定现有账本路径、身份、日常规则摘要、有效期、模型配置摘要、总调用上限、输入输出大小，并由调用方每次重新核验授权证据。它不能扩大日常累计上限；同一授权跨多次运行累计使用，不能改授权ID对应的范围。适配器只允许单次订阅通道调用，不得在内部重试；本工具不创建真实模型客户端或付费API入口。

超时先记 `unknown`，不自动重发；程序异常保留 `started` 意图以便检查。重启可复用已保存、重新验证的答案，再继续未开始的裁判步骤；不明或被拒绝步骤不会重发。新授权也不能让同一不明请求自动重试。报告区分新调用和缓存，token缺失明确标为未知，不能填零。实际返回的模型版本变化会阻止不适当的提示词比较。

`blind_pair()` 构造匿名A/B评审输入，映射表另行保存，并按种子改变顺序。它不调用裁判，也不把匿名比较自动视为可靠结论；需要校准和顺序反转检查。

## 更新与定期回归

`qa_regression.plan_regression()` 是不执行任务的规划函数。时间输入必须带时区，按北京时间计算最近的周日20:00及每月首个周日20:00；休眠漏跑只规划最近一次，不补一长串模型调用。

资料清单必须经核验，字段为 `corpus_id` 和 `sources`；每个来源包含 `id`、`canonical_id`、`body_sha256`、`metadata_sha256`、`critical`。同源资料以 `canonical_id` 去重统计。元数据摘要应覆盖来源身份、日期、版本及权限；文件修改时间不能替代正文变化。清单的构建者负责实际读取与核验，这不是无人值守的任意目录扫描器。

- 小更新：受影响题及指定核心题；新增来源找不到既有依赖时扩大到全量，避免漏掉新证据。
- 独立来源变化达到20份或5%、关键资料变化、系统配置变化：全量。
- 每周：全部适用题的离线回归。每月：最多10道固定核心题加轮换题，共最多20道候选语义测试。
- 正文、元数据或来源身份变更：相关固定证据题先标为需复核；重建审定版本前不能沿用旧答案评分。
- 保留集默认排除；显式要求才参与。未参与调试的保留题一旦用于修改提示词，应调整分区。

计划会列出候选语义调用上界，但 `automatic_model_execution=false`。离线任务完成只提供离线完成水位，不能将未执行的月度语义评测记作完成。

`RegressionStore` 在独立本地数据库去重任务、保存逐题结果，并用操作系统锁限制一个工作进程。中断后只补未保存的题；缺盘可延后重试，程序错误会保留原因并重新抛出，需要显式检查后重试。已保存的离线题失败不等于执行程序失败，报告分别保留。该数据库不管理或重置业务模型额度。

`notification()` 只产生本地状态变化事件，不发送飞书消息；相同状态不重复产生事件。实际定时唤醒、外部通知、模型适配器及真实飞书验收属于部署步骤，本模块不安装常驻服务或定时任务。

## 离线命令行

在仓库根目录设置模块路径，使用Python 3.12：

```sh
export PYTHONPATH=projects/kb-ingestion-workflow/integrations/kb_custody_v21/src
python -m kb_custody_v21.qa_quality_cli --help
```

以下路径均为调用方自己的本地评测文件。输出文件或目录必须不存在；程序不覆盖旧报告，不修改源资料，拒绝符号链接和非有限JSON数字。

```sh
# 准备回答请求，评分要点不会进入请求；只做字节上限检查，不代替真实模型token计量。
python -m kb_custody_v21.qa_quality_cli prepare --cases .dev-runtime/eval/cases.json --out .dev-runtime/eval/requests

# 导入已保存的答卷与评分判断；未提供校准文件时不会给语义通过结论。
python -m kb_custody_v21.qa_quality_cli score --cases .dev-runtime/eval/cases.json --records .dev-runtime/eval/records.json --run .dev-runtime/eval/run.json --out .dev-runtime/eval/report

# 比较两次运行。资料、评分版本等变化会显示不可直接比较。
python -m kb_custody_v21.qa_quality_cli compare --baseline .dev-runtime/eval/old/report.json --candidate .dev-runtime/eval/new/report.json --mode prompt --out .dev-runtime/eval/difference.json
```

`run.json` 必须包含：`run_id`、`corpus_id`、`corpus_sha256`、`code_commit`、`model`、`prompt_sha256`、`parameters_sha256`、`judge_fingerprint`、`evaluation_clock`。摘要使用 `qa_quality.digest()` 的规范JSON编码。裁判摘要应包括裁判模型、提示词及参数；回答提示词摘要对应实际传给 `answer_packet` 的指令。资料指纹和清单指纹不能混用。

`records.json` 是答卷记录数组，每项包含 `id`、`fixture_sha256`、`state`、`model_calls`、`cached`。完成项还含服务格式的 `answer`，以及 `bind_judgment()` 生成的 `judgment`；缺测可不提供该题记录。错误、跳过和不明项必须给 `reason`，不得冒充已完成答卷。`run_case()` 直接产出此格式。

规划命令 `plan` 接受 `--cases`、`--current`、可选 `--previous`、`--completed`、重复 `--core`，以及必须的 `--system-fingerprint`、`--now`、`--out`。默认禁用；`--enable-offline` 只让离线队列可执行，不安装定时任务。`--include-heldout` 必须显式指定。

`check --cases ... --plan ... --state-db ... --out ...` 执行冻结题包完整性检查并保留可恢复记录，不重新读取原件，也不判断答案质量。

`retrieve --cases ... --archive ... --selection ... --now ... --out ...` 只读指定ZIP中的已转换文本，在新输出目录建立独立索引，复用业务查询解析和检索。selection须有正文哈希；检索标注每题含 `id`、`question`、`expected_mode`、`evidence_groups`。每个证据组是可替代来源数组，元素包含 `material_id` 及可选的逐字 `quote`。未提供quote的组只核查来源，报告会单列，不能冒充片段语义支持。

`prepare` 有超限题、`score` 未全部通过、`compare` 不建议放行、`check` 未全部完成离线检查时，退出码为2，并仍保留报告。输入错误会明确失败。`plan`和`retrieve`是规划/诊断命令，退出码0只代表完成该命令；任何离线成功都不代表语义问答通过。

新增测试命令：

```sh
PYTHONPATH=projects/kb-ingestion-workflow/integrations/kb_custody_v21/src:projects/kb-ingestion-workflow/integrations/kb_custody_v21/tests python -m unittest test_qa_quality test_qa_quality_calls test_qa_regression test_qa_quality_cli
```
