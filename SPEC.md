# iLoop 内核协议 SPEC

> 内核与扩展之间的稳定契约。内核不认识任何具体业务平台；扩展通过 Action、Recipe 和 Provider 插入，不让平台细节反向进入内核。
>
> 原则（VDD）：**推断不许当观测；完成 = 结果被验过；谁做的不能由谁判。**

---

## 协议 1：证据 EvidenceArtifact

一切结论的地基。每条证据必须可复核、必须标清是"看到的"还是"推的"。

```json
{
  "id": "ev-<短哈希>",
  "capability": "build | logs | screenshot | view_tree | crash | probe | ...",
  "source": "<产出它的插件/工具 id，如 ios_native.wda>",
  "kind": "observed | inferred",
  "outcome": "success | failure | neutral | unknown",
  "created_at": "<ISO8601>",
  "path": "<可复核的产物路径，截图/日志/JSON>",
  "summary": "<一句话：这条证据说明了什么>",
  "for_hypothesis": "<关联的可能原因 id，可空>"
}
```

- `kind` 是红线字段：`observed` = 真跑真看到；`inferred` = 从源码/日志推出来的。**推断标成 observed 即造假。**
- `kind` 只回答“来源是否观测”，不回答“结果是否成功”。observed failure 仍是硬证据，但不能支持四关通过；只有 `kind=observed + outcome=success` 才能作为完成证据。
- observed 成功还必须满足主体与完整性：产物哈希未变化，并绑定 task/run/gate/target/flow/device/created_at。`trusted_producer` 只是声明；Plugin receipt、外部事实、Task 创建策略和 Capability requirements 都需由 Runtime 注入的宿主 attestation verifier 复验。默认 `host_cli` 只提供同用户边界内的本地完整性证明；`independent_review`、`user_confirmation` 和任意 `evidence_subjects` 必须由进程外宿主提供，低层 `cli` 不能手工创建 observed 或自行完成收口。
- `path` 必须指向能被别人重新打开/重跑的东西。嘴上说"验过了"不产出 artifact。

---

## 协议 2：能力契约 Capability Interface

插件对内核暴露的统一动作面。内置枚举保留 build、logs、screenshot 等兼容能力；扩展通过 `capabilities.json` 注册 namespaced `CapabilitySpec`，不需要修改核心枚举。

```json
{
  "capability_id": "team.monitor.incident_scan",
  "description": "Read incidents from a monitoring service",
  "inputs": {"project": "string"},
  "outputs": {"incidents": "array"},
  "side_effect": "read",
  "required_tools": [["sentry-cli", "monitor-cli"]],
  "supported_deployments": ["team.monitor.local"]
}
```

内核认识的能力集（首发）：
`doctor · build · run · install · launch · logs · view_tree · screenshot · crash · probe · counter_probe · tap · swipe · type_text · ui_prepare · ui_status · ui_stop`

统一结果 JSON：

```json
{
  "platform": "<adapter-id，如 ios_native>",
  "capability": "build",
  "status": "success | unsupported | error",
  "evidence_dir": "<本次产物目录>",
  "artifacts": ["<EvidenceArtifact.id ...>"],
  "summary": "<一句话结果>"
}
```

- `status=unsupported` 是合法返回，不是失败——让内核知道"这个平台没这能力"，而不是崩。
- 判成功看 success marker + artifact，不只看 exit code。
- 未注册能力、重复能力、缺输入、Provider 漏输出、Deployment 不支持都 fail closed。
- 内置能力也必须声明真实副作用：build 属于 `workspace_write`，run/launch/ui_stop
  属于 `process`，install/tap/swipe/type_text/ui_prepare 属于
  `external_write`；其余只读采集能力属于 `read`。不能利用兼容枚举绕过授权。
- `counter_probe` 必须执行一个明确变化的条件，并用机器断言验证差异；官方 iOS
  插件要求 `counter_condition` + `counter_expect=summary_contains:<text>` 或
  `artifact_contains:<text>`，不能只凭底层命令 exit 0 通过反证 Gate。

---

## 协议 3：flow schema

任务路由与自治分级。内核按 `when_keywords` 匹配任务，按 `autonomy` 决定放权。

```json
{
  "flow_id": "<命名空间.flow名，扩展 flow 必须带前缀防覆盖>",
  "name": "<中文名>",
  "autonomy": "L1 | L2 | L3",
  "when_keywords": ["<触发词>"],
  "priority": 0,
  "guidance": "<方向参考，不是必跑清单>",
  "required_docs": ["<开工前必读文档路径>"],
  "evidence_strategy": "<这类任务优先取什么证据：日志/UI/截图/crash/静态>",
  "escalate_when": "<什么情况停手升级用户>"
}
```

- `autonomy`：L1 只看不改 / L2 动手改(最小改动+编译+验证+可回滚) / L3 放手干(需授权+清单+验收标准)。
- `priority`：关键词命中数相同时的路由优先级。扩展可用更高值覆盖宽泛核心匹配，但不能覆盖核心 `flow_id`。
- 内置 flow 与插件 flow 由加载器合并；插件 flow 只增不覆盖内核。

---

## 协议 4：lesson schema

错题本。踩过的坑召回并前置到下一次。

```json
{
  "id": "lesson-<日期>-<slug>",
  "title": "<一句话坑>",
  "keywords": ["<召回用关键词>"],
  "symptom": "<现象>",
  "root_cause": "<根因>",
  "fix": "<解法>",
  "scope": "general | <插件id>",
  "created_at": "<ISO8601>"
}
```

- 只沉淀"非业务 + 高概率复现 + 解法非平凡"的坑。一次通过、纯业务 bug 不写。
- 开工前 `keywords` 召回；解决后满足条件才写。

---

## 协议 5：ActionSpec 与 AssistantRecipe

Driver `Capability` 回答“平台会做什么”；`ActionSpec` 回答“助手要完成什么业务动作”。两者不得混进同一个枚举。

```json
{
  "action_id": "team.oncall.collect",
  "description": "收集告警证据",
  "risk": "low | medium | high",
  "side_effects": ["read"],
  "inputs": {"event_id": "string"},
  "outputs": {"case_id": "string"},
  "required_capabilities": ["logs", "probe"]
}
```

`AssistantRecipe` 保存版本、有序 Action 列表、入口和持续观察标志。Action 可声明 `lifecycle_stage`（diagnosis / disposition / verification / observation），Runtime 只能按阶段推进。Recipe 引用未知 Action、重复 Action、越权助手或风险冲突时必须 fail closed。Action 声明的副作用不得比底层 Capability 更弱。Recipe 不允许包含 deployment 分支。

---

## 协议 6：Provider Registry

一个 Runtime 可以注册多个 `Plugin` Provider，并按 Driver Capability 路由。规则：

- `platform_id` 全局唯一；
- Provider 返回的 `platform` 和 `capability` 必须与实际调用一致；
- 没有 Provider 时在装配阶段失败；
- 多个 Provider 声明同一能力时必须显式 `provider_bindings`，不能按加载顺序猜。
- 成功结果必须覆盖 `CapabilitySpec.outputs`；声明能力不等于运行时工具已经就绪。
- 要进入 Suite 的 `production_ready`，Provider 必须提供稳定的
  `runtime_fingerprint()`，覆盖实现版本和当前运行配置；缺失或非法摘要只能使用
  Provider，不能声称生产就绪。

---

## 协议 7：Versioned Resolve Case

Case 在原有假设与四关之外，正交记录：

- diagnosis：调查中 / 已冻结，并递增 `diagnosis_revision`；
- disposition：计划 / 执行 / 完成 / 失效；
- verification：待验证 / 通过 / 失败；
- observation：不需要 / 待观察 / 观察中 / 稳定 / 回归。

处置计划绑定确切 diagnosis revision。新增候选、反证冻结根因或观察到回归时必须重开诊断、清空旧四关并废止旧计划；旧非版本化 Case 读取时兼容迁移。

---

## 协议 8：Deployment 与执行信封

`DeploymentProfile` 只声明目标节点和可用 Provider，不复制 Recipe。`TaskEnvelope` 的签名覆盖 task、assistant、Deployment 指纹、target node、diagnosis revision、Git base commit、policy、输入、Recipe 指纹、有序 Action/Driver Capability 清单、交错 evidence plan 和 TTL。

Worker 必须在执行前验签并登记防重放。`WorkerReceipt` 绑定完整 Task digest；成功回执不得包含失败证据，并必须按顺序覆盖签名执行清单。Provider 没有可哈希产物时只能形成 `execution_record`，不能冒充 observed evidence。

核心只提供 local/in-process worker 契约。远端队列、transport、节点身份和运行账号由宿主或扩展实现。

---

## 协议 9：Authorization 与源码候选血缘

有 `workspace_write`、`external_write` 或 `process` 副作用的 Action，在调用 Provider 前必须验证 `AuthorizationGrant`。兼容层直接调用带副作用 Capability 时也必须单独授权，不能借低层接口绕开 Action Gate。Grant 包含：

- subject 与 human / automation 类型；
- 当前允许的 Action；
- 兼容层直接执行时当前允许的 Capability；
- task、case、diagnosis revision；
- host-attested policy digest；
- 签发时间、过期时间和来源 ID；
- 宿主签名。

Runtime 验证 Action 边界，`authorize_tool_use` 供宿主 PreToolUse Hook 验证 shell 和文件写入旁路。历史消息、CLI 参数、Action 输出和用户名称字符串都不能自行生成授权。

`HMACAuthorizationAuthority` 是本地宿主参考实现，只提供同一系统用户边界内的完整性，不提供独立身份。需要可信人员身份时，宿主必须把 `AuthorizationVerifier` 接到自己的审批或身份系统；默认本地宿主不能把自己签出的字符串冒充成人工授权。

源码候选使用四个 DTO 保持平台无关：

```text
ChangeSnapshot
  -> CandidateRevision
      -> ChangeRequestReceipt
          -> CICheckReceipt
```

四者必须绑定同一个 base commit、change digest、source branch 和 candidate commit。CI 结果必须从 candidate commit 的 checks API 读取并逐条核对 `head_sha`，不能把 PR 当前检查结果直接归到先前保存的 SHA。公开 Git Provider 只使用隔离 worktree 与 `iloop/` 分支，自动创建 draft change request；merge、approve、release 永远不是参考 Recipe 的自动动作。

---

## 协议 10：AssistantSuite 与生产就绪

`SuiteManifest` 可以包含任意数量的 Assistant 与 Deployment，不写死助手类型。生命周期固定为：

```text
validate -> compile -> preflight -> install -> smoke -> status
```

状态逐层区分 declared、handler installed、implementation ready、runtime ready、production ready。`production_ready` 必须同时满足：

- 当前 Recipe、Deployment、Provider 可装配，Provider 实现与配置摘要完整；
- Action handler 和运行工具就绪；
- 已安装指纹与当前编译指纹一致；
- 所有配置 smoke check 返回成功和可哈希产物；
- `SmokeReceipt` 签名有效、未过期、产物未变化。

smoke 只允许 read / none Capability，不能借体检执行写操作。

公开安装器把正式版本放在 `~/.iloop/iloop`，分别注册通用、Claude Code 和 Codex 入口。升级在候选目录完成 selftest 和 fresh-clone smoke 后原子切换；候选失败保留旧版本。

---

## 协议 11：验证范围、时间账本与并行验收

GlobalReview 根据完整 diff、公共定义、调用方、动态入口、行为配置和删除逻辑生成验证计划：

- R0：控件或资产，spot；
- R1：页面，spot；
- R2：模块，targeted；
- R3：全局，full。

未知归属默认 R2；UI 任务至少 R1，并要求 subjects 覆盖当前 target 的 screenshot evidence，人工接受风险也不能取消视觉点验。UI 语义既来自任务，也从界面源码、资源和样式路径推导。项目可以提供 scope rules，但规则只能提高范围；规范化规则和 UI 输入会持久化并参与 fingerprint，最终收口必须用同一输入重算。

Ledger 的 TimingEvent 分别记录 phase、Action、Capability、Provider、run、worker、开始结束时间和阻塞时间。Dashboard 从事件复算墙钟时间、工作时间、重试、最大并发和各层耗时；报告不把一轮总时间重复冒充成多个阶段的精确耗时。

`AcceptanceBatch` 必须从现有 `AcceptancePackage` 派生，把包内 criterion 按索引完整且不重复地拆成只读分片。每个分片由宿主证明 reviewer 身份，reviewer 不能是执行者且彼此唯一；错 package/token/fingerprint、重复或缺失分片、未逐条判定都拒绝。聚合结果仍写入 `AcceptanceStore`，由既有 `AcceptanceResult` 收口，不建立平行验收状态。

---

## 内核不做什么（防过度设计）

- 不认识任何具体平台的 CLI / 域名 / 字段——那是插件的事。
- 不预建账号中心或企业策略平台；核心只定义短期授权凭证和验证端口。
- 不在核心实现远端队列或企业平台 Adapter；先把本地 Recipe 主链和签名契约跑穿。
