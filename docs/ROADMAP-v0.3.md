# iLoop 开源版 v0.3 Roadmap：从验证闭环内核到能力装配平台

> 状态：草案 · 目标版本 `0.3.x` · 起点 `0.2.3`
>
> 这份路线图不是新构想，而是 2026-08-22《开源版对照内部 2.0 全局架构复核》结论的落地排期。它只回答一件事：开源版从「单 Runtime 验证闭环内核」走到「用原子应用能力装配多个助手」，该按什么顺序走、每步做到哪算完成、什么留在门外。

## 为什么要做 v0.3

到 `0.2.3` 为止，开源版是一套能跑的验证闭环内核：Evidence、四关、Case、Task、Capability Gate、独立验收、全局复核、持久化 Runtime 都在，iOS 平台插件真实闭环。但它只能表达「一个内核 + 一个平台插件」，表达不了内部 2.0 已经跑通的那层——**用一组原子应用能力，声明式地装配出多个助手（Oncall / Bugfix / 稳定性），并让同一个助手独立于本地/远端部署**。

内部 2.0 把这层落成了可执行协议和本地契约回放，但它的生产接线还是渐进式的（Oncall 仍走旧网关、recipe 未驱动编排、远端只有签名协议没有 worker）。所以 v0.3 的原则是**迁移它的设计，不复制它的现状，也不搬它的业务枚举**。

## 已经完成的前置（不在 v0.3 范围内）

审计给的实施顺序第一步——「先补设计契约与逐条验收，关闭 review 过度/不足」——已经在补丁版做完：

- `0.2.2`：普通 L2 任务恢复后不再被静默升级为全局复核；核心风险关键词接进真实验收触发；独立验收逐条判定。
- `0.2.3`：`design_contract`（核心目标 / 核心设计决策 / 不改边界）在计划期冻结进 host-attested policy 并随任务恢复；`plan` 外显评审三裁决。

所以 v0.3 从审计实施顺序的第二步起步。

## 当前结构缺口（v0.3 要填的）

1. 没有 Assistant 聚合根，Task 不记录属于哪个助手。
2. Flow 不声明能力组合，核心 Flow 步骤硬编码在 Runtime，能力靠 CLI `caps=` 临时传入。
3. Runtime 只绑定一个 Plugin，没有按能力选择 Provider 的注册表。
4. 当前 `Capability` 是平台探针闭集，不是可扩展的应用动作契约。
5. Case 只有诊断假设和四关，没有版本化根因与处置/验证/观察生命周期。
6. 没有 Deployment、target node、Task Envelope、Worker Receipt。

## 里程碑

四个里程碑严格按依赖顺序推进，每个都能独立发一个 `0.3.x`、独立回归、独立回滚。不追求一次做完。

### M1 · 应用动作契约 + 助手 Recipe（`0.3.0`）

**要解决**：缺口 1、2、4。让「助手 = 一组应用能力的声明式组合」在开源版第一次成立。

**范围**
- 新增应用层 `ActionSpec`（或命名为 Domain Capability），描述一个应用动作的输入、副作用、风险档、允许的助手、输出。它与现有平台 `Capability` 分层——平台 `Capability`（build/run/log/UI/crash）保留为 Driver 能力，`ActionSpec` 是上层应用动作，两者不混进一个枚举。
- 新增 `AssistantRecipe`：声明一个助手由哪些 `ActionSpec` 组成、入口来源、是否需要持续观察。
- 新增装配校验：recipe 引用的每个 action 必须存在、风险档一致、无重复；校验失败 fail closed。
- Task 记录 `assistant_id`，与 recipe 绑定。

**不做**
- 不引入 Deployment / 远端（留 M4）。
- 不改现有平台 `Capability` 枚举和 iOS 插件行为。
- 不内置具体业务助手 recipe（Oncall/Bugfix 的具体配方走扩展或示例，不进核心）。

**验收**
- 用内存 Provider 装配出两个不同助手，二者能力集不同、各自独立执行、状态互不污染。
- recipe 引用不存在的 action、或风险档冲突时，装配被拒并给出可诊断原因。
- selftest 覆盖装配成功、装配 fail closed、Task↔assistant 绑定恢复。
- 既有 262 断言零回归。

### M2 · Provider 注册表（`0.3.1`）

**要解决**：缺口 3。让一个助手能同时组合多个平台 Provider（日志、CI、代码库、设备……），按能力选择实现。

**范围**
- 新增 capability-provider registry：Runtime 不再只绑一个 Plugin，而是按 action 所需能力路由到对应 Provider。
- Provider 按能力声明自己支持什么；缺能力返回 `unsupported`，不伪装成功。
- 扩展 manifest 支持声明「这些 Flow / action 由这些 Provider 支撑」，装配校验能检查组合完整性。

**不做**
- 不实现具体第三方 Provider（Sentry/Slack/CI 走公开扩展示例，不进核心）。
- 不改 Evidence / 四关 / 独立验收契约。

**验收**
- 一个助手同时用两个 Provider（如 iOS 执行 + 内存日志）跑通一条链，证据分别归属正确 Provider。
- 某能力无 Provider 时装配阶段就报缺，而不是运行到一半才失败。
- 重复 `platform_id` 仍 fail closed（沿用现有约束）。

### M3 · 版本化 Resolve Case 生命周期（`0.3.2`）

**要解决**：缺口 5。把 Case 从「诊断假设 + 四关」扩成正交生命周期，并冻结根因版本。

**范围**
- Case 正交表达 diagnosis / disposition / verification / observation 四段状态。
- 冻结 diagnosis revision：根因结论版本化，后续处置计划绑定确切 revision；根因翻新则旧处置计划自动失效。
- 处置路由：从冻结根因按可用能力路由到「代码建议 / 隔离修复 / 人工移交 / 观察」，无可信本地能力时保守转人工。

**不做**
- 不引入内部的 SQLite 影子层——开源版没有双存储迁移负担，新模型直接做唯一事实源。
- 不绑定任何具体平台的处置动作（只留通用路由骨架）。

**验收**
- 一条 Case 走完「诊断→冻结根因→处置路由→验证→观察」，各段状态经状态机校验、非裸写。
- 根因翻新后，基于旧 revision 的处置计划被拒。
- 旧的非版本化 Case 仍可读（向后兼容）。

### M4 · Deployment + Task Envelope + Worker Receipt（`0.3.3`）

**要解决**：缺口 6。让「助手定义」与「在哪执行」分离，先本地/进程内跑通，再谈远端。

**范围**
- `DeploymentProfile`：只描述节点特征和 Provider 可用性，不复制 AssistantRecipe。
- 通用 `TaskEnvelope` / `WorkerReceipt` DTO：绑定 assistant、deployment、target node、diagnosis revision、base commit、policy、TTL、签名；重放防护和证据契约进核心。
- 先做 local / in-process 执行路径，把契约跑通。

**不做**
- 不在核心实现远端队列 / transport / worker 拉取——那是公开扩展或后续版本的事，且必须等本地 Recipe 主链真实跑通后再扩。
- 不搬内部节点身份、发布平台、运行账号配置。

**验收**
- 同一助手定义在两个 DeploymentProfile 下装配出不同的 Provider 可用集，助手定义本身不变。
- Envelope 篡改、过期、跨节点、base commit 不符时验签失败。
- Worker Receipt 绑定完整 Task 指纹，成功回执必须携带类型化证据。
- 本地契约回放能跑通至少一个完整助手链（不止 `diagnosis.route` 一个动作）。

## 迁移边界（哪些进核心、哪些走扩展、哪些只留内部）

**进开源核心**
- 可扩展的 `ActionSpec` / `AssistantRecipe` / `DeploymentProfile` 及装配校验。
- 版本化 Resolve Case 生命周期。
- 通用 Task Envelope / Worker Receipt DTO、重放防护、证据契约。

**通过公开扩展提供**
- GitHub Issues、Sentry、Slack、CI runner 等 ingress / Provider 示例。
- 具体助手 recipe（通用 Oncall / Bugfix / 稳定性）。
- 远端 transport / worker 实现。

**只留内部，不进开源**
- 飞书、Slardar、TEA、Libra、Meego、Codebase 等平台 Adapter。
- 抖音仓库选择、白名单、分支、群聊状态位、公司授权策略。
- 内部节点身份、发布平台、运行账号配置。

## 风险与约束

- **不复制内部现状**：内部 recipe 目前三个助手能力集相同、recipe 不驱动编排、远端只有签名协议。开源版要的是它的设计骨架，不是它的半成品接线。
- **每个里程碑都要真跑通再进下一个**：M4 的远端执行必须等 M1–M3 的本地 Recipe 主链真实跑通，别先做远端队列。
- **不破坏已有硬约束**：宿主 attestation 分权、fingerprint 失效、四关、逐条验收、iOS 插件行为在 v0.3 全程保持不变；每个里程碑收口都跑全量 selftest + fresh-clone smoke + 独立验收。
- **设计契约先行**：每个里程碑本身就是一次大改动，开工前用 `design_contract` 钉死该里程碑的核心目标和不改边界，按三裁决评审，避免路线图自己跑偏。
