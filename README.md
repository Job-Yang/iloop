# iLoop

<p align="center">
  <img src="assets/iloop-mascot.png" alt="iLoop mascot: an agent that verifies work through feedback loops" width="960">
</p>

> 让 AI 像程序员一样，在真实反馈里一轮轮修正；说“完成”之前，先拿证据自证。

iLoop 是一层给研发 Agent 使用的**反馈闭环底座**。

人类程序员很少能一次把代码写对。我们会读项目规则、查过去踩过的坑，写完再编译、看日志、跑设备、抓 UI、看崩溃，然后根据结果继续修。真正让代码逐渐变对的，不只是“会写”，而是这套持续获得反馈、修正判断的工作循环。

Agent 缺的往往正是这套循环。它能生成代码，却未必能自己拿到一手反馈；任务一长，还会忘目标、凭感觉发挥、自己给自己验收，最后用一句“应该没问题”收尾。

iLoop 把这些缺失的部分接起来：

```text
目标与上下文
    ↓
选择工作流和放权等级
    ↓
修改代码
    ↓
编译 / 日志 / UI 树 / 截图 / Crash
    ↓
证据是否支持结论？
    ├─ 否 → 带着证据修正，再跑一轮
    └─ 是 → 独立验收 / 四关收敛 / 记录经验
```

这套做法叫 **VDD（Verification-Driven Development，面向验证的开发）**：完成的定义不是“代码写完了”，而是“结果被验过了”。

[完整设计思路](DESIGN.md) · [VDD 方法论](docs/VDD.md) · [二次开发](EXTENDING.md) · [版本变化](CHANGELOG.md)

---

## 它能干什么

iLoop 给现有 Agent 补上研发交付所需的工作循环：

- **按任务选工作流**：排查、修复、需求、重构、验收、环境问题走不同的路径，不让 Agent 一上来凭感觉乱做。
- **按风险决定放权**：L1 只看不改、L2 小步修改、L3 授权后连续执行。
- **用真实反馈纠偏**：验数据看日志，验控件看 UI 树，验最终表现看截图，验崩溃看 crash report。
- **把任务当持续档案**：记录现象、候选原因、证据和下一步检查，长任务换会话也能继续。
- **区分观测和推断**：真跑看到的是 `observed`，从源码推出来的是 `inferred`，两者不能混为一谈。
- **换一个 Agent 做关键验收**：高风险改动不让执行者自己盖章。
- **沉淀长期经验**：工程坑进入错题本，下次先召回，不从零再踩一次。
- **展示交付过程**：轮次、证据、止损和验收进入提效看板，而不是只留下聊天记录。
- **从整体复核改动**：收口读取完整 diff，检查公共定义、调用方、共享边界和删除逻辑，防止局部补丁都对、整体架构却持续变坏。
- **用原子动作装配助手**：把应用动作、平台能力和部署位置拆开，同一组动作可以组成不同助手，同一助手也可以部署到不同节点。

开源版包含平台无关内核和一个 iOS 官方插件，支持：

- 模拟器与真机的 build / install / launch
- 截图、UI 层级树、日志和 crash report
- 基于 Appium WebDriverAgent 的真机 UI 自动化
- 可复用 UI Flow（`verified` 由宿主证明的运行态证据写入），以及 Task/Case/Capability Gate/独立验收/全局复核的硬收口
- `ActionSpec` / `AssistantRecipe` / Provider Registry，以及版本化 Resolve Case
- 签名 `TaskEnvelope`、防重放账本和绑定完整任务指纹的 `WorkerReceipt`
- 本机 Xcode 自动发现，不依赖全局 `xcode-select`

---

## 和现有框架有什么不同

iLoop 不想替代 Spec Kit、Codex、Ralph、Superpowers 或各种 Harness 方法。它补的是这些系统下游经常缺失的一层：**真实执行后的反馈与验收**。

| 方向 | 主要解决什么 | iLoop 补什么 |
|---|---|---|
| Codex / 通用 Coding Agent | 规划、生成、修改、持续执行 | 不能只靠模型判断“已经达标”，要接入可复核的工程证据 |
| Spec Kit | 把需求拆成规范、计划和任务 | 规范完成后，继续在真实工程和设备里落地、验证、收口 |
| Ralph Loop | 让同一目标持续循环推进 | 不只“继续跑”，还要知道每轮该看什么反馈、何时能停 |
| Superpowers / Skills | 把方法和工具封装成可复用能力 | 把这些能力组织进同一条证据闭环，并约束验收边界 |
| Harness / Loop Engineering | 解释 Agent 为什么需要前馈、反馈和循环 | 把这些原则落成能在本机运行的 Agent 入口、内核和平台插件 |

所以 iLoop 更像一层**反馈底座**：它可以插到现有 Agent 或研发链路的某个节点，把“生成完就结束”变成“拿到真实结果后继续修正，验过才结束”。

---

## 核心设计

### 1. VDD：验过才算数

Agent 的结论必须落到证据。收敛时检查四件事：

1. **时间对得上**：证据与现象来自同一次发生。
2. **范围对得上**：证据覆盖了实际受影响范围。
3. **机制说得通**：能解释为什么会发生。
4. **有反证**：换一个关键条件，现象应该消失或发生变化。

`wrapup` 是收口门禁，不是总结命令。任务步骤、运行证据、Case、四关、全局复核和必要的独立验收没有全部通过时，任务不能标记完成。

### 2. 工作流与放权等级

`plan` 根据任务选择 flow，并给出所需文档、取证方向、升级条件和下一步建议。flow 不是教条清单，而是让 Agent 在开工前先确定“这类问题应该怎么想”。

- **L1 只看不改**：调查、取证、分析。
- **L2 动手改**：最小改动、可回滚、改后验证。
- **L3 放手干**：用户明确授权、有任务清单和验收标准后连续执行。

### 3. 长期记忆不是一个大知识库

iLoop 把记忆拆成不同用途：

- `AGENT_PROMPT.md` 和 `prompts/`：稳定规则与场景方法。
- `workflow/flows.json` 和诊断专家：一类任务应该怎么思考。
- `Case`、`Ledger` 和证据文件：当前任务进行到了哪里。
- `LessonBook` 和 `seed_lessons/`：过去踩过什么坑、怎么避免。

每轮只加载命中的方法和最小证据缺口，避免把所有历史塞进上下文。

### 4. 关键验收换一个视角

低风险改动由主 Agent 自核；公共逻辑或高风险链路触发独立验收。验收 Agent 只拿验收标准和证据，不拿执行过程，避免被原来的推理路径带偏。

### 5. 全局复核对照固定目标

重大改动开始前，iLoop 会记录这次任务的核心目标、设计决策和不改边界。后续 Review 只做三种判断：符合目标就不改，偏离目标就改回，原目标没有覆盖就先补齐约定。这样全局复核有一把稳定的尺子，不会因为对话变长而忽松忽紧。

### 6. 判断力、业务和平台分开

```text
稳定判断力：VDD / 工作流 / 病例 / 验收 / 错题本
业务领域层：业务材料 / 领域 flow / 专项脚本 / 角色
平台执行层：iOS / 监控 / 日志 / CI / IM / 其他工具
```

换业务只替换领域层，换平台只替换执行插件，主循环不用重写。

### 7. 助手、平台和部署正交

`ActionSpec` 描述一个应用动作需要什么输入、可能产生什么副作用、风险多高，以及依赖哪些底层 Driver Capability。`AssistantRecipe` 只声明动作组合，不写本地/远端分支；`ProviderRegistry` 再把 build、logs、screenshot 等 Driver Capability 路由到具体 Provider。

```text
AssistantRecipe
  -> ActionSpec
      -> Driver Capability
          -> Provider

DeploymentProfile
  -> target node + available providers
```

任务信封会签入 assistant、Recipe 指纹、有序动作清单、输入、部署、节点、诊断 revision、Git 基线、policy 和有效期。Worker 回执绑定完整任务摘要；成功回执必须逐项覆盖签名动作清单，Provider 没有真实产物时只能记为执行记录，不能冒充 observed evidence。

更完整的推导见 [DESIGN.md](DESIGN.md)。

---

## 快速开始

前置条件：

- Python 3.9+。内核只用标准库，不需要 `pip install`。
- 使用 iOS 插件需要 macOS + Xcode。
- iOS 编译、运行和模拟器 UI 自动化使用公开的 [XcodeBuildMCP](https://github.com/getsentry/XcodeBuildMCP) CLI。
- 真机 WDA UI 自动化额外需要 `iproxy`。

```bash
git clone https://github.com/Job-Yang/iloop.git
cd iloop

# 平台无关的内核自测（Linux/macOS 均可）
python3 -m host_cli selftest

# 看 iLoop 会怎么处理一个任务
python3 -m host_cli plan "帮我修复下单页崩溃"
```

需要执行 iOS build/run/UI 时，再在 macOS 安装公开执行底座：

```bash
brew tap getsentry/xcodebuildmcp
brew install xcodebuildmcp

# iOS 环境体检
python3 -m host_cli doctor

# 创建可恢复任务；能力结果、轮次和看板写入 ~/.iloop/data/
export ILOOP_PROJECT_ROOT=/path/to/your/app
python3 -m host_cli run "帮我修复下单页崩溃" \
  constraints="不改公共 API" \
  acceptance="编译通过;拉起无 crash"

# 中断或换会话后恢复
python3 -m host_cli tasks
python3 -m host_cli resume <task_id> caps=build,run,logs \
  workspace=App.xcworkspace scheme=App sim_udid=<simulator-id> \
  subjects=Sources/Feature.swift,Tests/FeatureTests.swift

# 查看证据缺口给出的下一动作；重构收口前逐项复核完整 diff
python3 -m host_cli next <task_id>
python3 -m host_cli global-review prepare <task_id> project_root=$ILOOP_PROJECT_ROOT

# 高影响改动：生成验收包；本地 review 只做 preflight
python3 -m host_cli accept prepare <task_id>
python3 -m host_cli accept review <task_id>
```

扩展助手可直接由 Recipe 驱动：

```bash
python3 -m host_cli run "处理这次故障" \
  assistant_id=team.oncall.agent event_id=evt-123

# 中断后继续剩余 Action
python3 -m host_cli resume <task_id> recipe=true

# 根因冻结后推进处置、验证与观察
python3 -m host_cli case disposition <task_id> reason="选择最安全可用动作"
python3 -m host_cli case advance <task_id> plan=plan-r1 status=executing
python3 -m host_cli case advance <task_id> plan=plan-r1 status=completed
python3 -m host_cli case verify <task_id> evidence=<evidence-id> passed=true
```

`host_cli` 是开源版默认入口。它把本地执行事实的完整性记录放在任务目录之外的
`~/.iloop/host-trust/`，防止仅编辑任务状态文件改变结论；它不是同一 OS 用户下的
独立身份边界。`independent_review`、`user_confirmation` 和
`evidence_subjects` 必须来自外部 Agent 宿主，默认本地账本拒绝签发和验证。
内置 `accept review` 只启动只读 preflight，永远不签发最终 `pass`。

`python3 -m cli` 保留为低层 fail-closed 调试入口：它可以规划和取证，但不能
自行给 Task policy、平台完成或独立验收签字，因此不能完成 `wrapup`。

宿主集成入口只有一个：

```python
runtime = Runtime(
    data_dir,
    registry,
    plugin,
    project_root=project_root,
    attestation_recorder=host.record,  # 只记录宿主实际观察到的事实
    attestation_verifier=host.verify,  # 受信状态必须保存在任务进程外
)
```

`host.record(kind, path, payload)` 与 `host.verify(kind, path, payload)` 必须按
事实类型分权；尤其不能向执行任务的 Agent 暴露 `independent_review`、
`user_confirmation` 或任意 `evidence_subjects` 的签发入口。外部验收结果通过
`runtime.record_external_acceptance(task, result_path)` 回写。把本地文件哈希
原样写回同一目录不构成宿主证明。

`project_root` 用于隔离不同工程的数据；也可固定设置 `ILOOP_PROJECT_ROOT`。任务、证据和看板默认写入 `~/.iloop/data/<project-id>/`，不污染业务仓。

然后把仓根的 [AGENT_PROMPT.md](AGENT_PROMPT.md) 作为项目规则或 Agent
入口加载到 Claude Code、Codex、Cursor 或自建宿主。默认调用
`python3 -m host_cli`；提示词、内核、宿主适配和插件一起随仓库分发。

---

## 二次开发交给 Agent

扩展机制首先是**给 Agent 看的开发协议**。

使用者不需要先理解 manifest、flow schema 或内核接口。可以直接对已经加载 iLoop 的 Agent 说：

> 基于 iLoop 做一个代码排查 Agent。
> 基于 iLoop 做一个智能 oncall Agent。
> 给我们团队增加一个发布回归工作流。

Agent 会按入口协议自动完成：

1. 判断这是公共核心能力还是业务扩展；归属不清先让用户选择。
2. 读取 [EXTENDING.md](EXTENDING.md)。
3. 执行 `extension-init` 创建隔离扩展。
4. 只修改扩展目录，不改 iLoop 核心。
5. 执行 `extension-validate` 检查命名空间和越界。
6. 用一个真实任务再次运行 `plan`，确认新 flow 能被命中。

扩展作者真正需要关心的只有业务目标、输入、输出和验收口径。内核里的接口规范是 Agent 和插件之间的“插座标准”，不是普通用户的必修课。

---

## 能力边界

iLoop 核心负责反馈闭环、任务恢复、证据与验收、助手装配和本地执行契约。具体的 Oncall、Bugfix、稳定性助手，以及 GitHub、Sentry、Slack、CI 等平台接入，由扩展提供。

跨节点任务可以使用签名信封和回执约束输入与结果，但远端队列、Worker 拉取、节点身份和密钥分发不在核心内。iOS 真机执行还需要可用的 Apple Developer 签名、已配对设备和 `iproxy`；模拟器与真机使用不同的 UI 自动化路径，不能把一边的控件引用直接复用到另一边。

MIT License。欢迎使用、修改和二次开发。
