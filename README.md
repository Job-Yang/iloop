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

## 仓库里已经有什么

开源仓同时交付 Agent 入口、平台无关内核和公开执行插件。下面的“验证口径”用来区分随仓可运行能力、需要用户环境完成的 smoke，以及留给扩展实现的接口。

| 能力 | 随仓交付 | 验证口径 |
|---|---|---|
| Agent 入口与工作流 | `AGENT_PROMPT.md`、按需加载的场景文档、flow 路由和 L1/L2/L3 放权等级 | Agent 先读目标和约束，再决定流程、工具与证据，不把 playbook 当固定命令清单 |
| VDD 运行时 | Task、Evidence、FourGate、版本化 Case、断点恢复和硬收口 | `observed` 与 `inferred` 分开记录；时间、范围、机制和反证没有闭合时不能完成任务 |
| 助手装配 | 动态 `CapabilitySpec`、`ActionSpec`、`AssistantRecipe`、Provider Registry 和 Deployment | Capability、Action、Provider 或部署缺失、歧义、漏报副作用时直接拒绝装配 |
| 安全执行 | 短期 `AuthorizationGrant`、PreToolUse Guard、签名 `TaskEnvelope`、防重放账本和 `WorkerReceipt` | 写工作区、启动进程和远端写入必须先过授权；历史消息不能继承成当前写权限 |
| 源码修复参考链 | `builtin.bugfix` Recipe 与 Git/GitHub Provider | 本地 Git worktree、快照、提交和远端分支使用真实仓库验证；draft PR 与 exact-commit CI 需要在使用者自己的 GitHub 登录态和测试仓做 live smoke |
| 生产就绪检查 | `AssistantSuite` 的 validate、compile、preflight、install、smoke 和 status | 配置存在只代表 declared；当前 Recipe、Provider、工具和新鲜 smoke 全部通过后才是 `production_ready` |
| 回归与验收 | GlobalReview R0-R3、target-bound UI screenshot、TimingEvent 和并行只读 AcceptanceBatch | 验证范围由完整影响图决定；项目规则只能升档，不能降低安全下限；并行验收仍回到同一 AcceptanceStore 收口 |
| 任务记忆与可见性 | Case、Ledger、LessonBook、UI Flow 和 Dashboard | 长任务可以跨会话恢复；工程坑按条件进入错题本；看板展示证据和过程，不拿动作数量冒充完成 |
| iOS 官方插件 | 模拟器与真机 build、install、launch、截图、UI 树、日志、Crash 和 UI 操作 | 模拟器闭环已端到端验证；真机实现使用 Appium WebDriverAgent，仍依赖使用者自己的签名、设备配对和 `iproxy` 环境 |

## 什么时候适合用

iLoop 适合已经在使用 Coding Agent，又希望它能把任务做到“有证据地完成”的个人和团队。下面这些情况最典型：

- Agent 会改代码，但编译、日志、UI、Crash 和回归仍靠人手工搬运；
- 任务一长就丢目标、丢现场，换会话后只能重新解释；
- 高风险改动需要独立验收，不能让执行者自己给自己盖章；
- 想把排查、Bug 修复、Oncall 或稳定性能力拆成可复用动作，再接入自己的平台。

iLoop 采用本机优先的执行方式。它没有内置托管控制面、企业账号中心或远端任务队列，也不会替团队预装所有监控、IM 和 CI 平台。

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

### VDD：验过才算数

Agent 的结论必须落到证据。收敛时检查四件事：

1. **时间对得上**：证据与现象来自同一次发生。
2. **范围对得上**：证据覆盖了实际受影响范围。
3. **机制说得通**：能解释为什么会发生。
4. **有反证**：换一个关键条件，现象应该消失或发生变化。

最终收口是一道硬门禁。任务步骤、运行证据、Case、四关、全局复核和必要的独立验收没有全部通过时，任务不能标记完成。

### 工作流与放权等级

iLoop 会根据任务选择 flow，并给出所需文档、取证方向、升级条件和下一步建议。flow 提供思考方向，Agent 仍要根据现场选择工具和验证方式。

- **L1 只看不改**：调查、取证、分析。
- **L2 动手改**：最小改动、可回滚、改后验证。
- **L3 放手干**：用户明确授权、有任务清单和验收标准后连续执行。

### 长期记忆按用途拆开

iLoop 把记忆拆成不同用途：

- `AGENT_PROMPT.md` 和 `prompts/`：稳定规则与场景方法。
- `workflow/flows.json` 和诊断专家：一类任务应该怎么思考。
- `Case`、`Ledger` 和证据文件：当前任务进行到了哪里。
- `LessonBook` 和 `seed_lessons/`：过去踩过什么坑、怎么避免。

每轮只加载命中的方法和最小证据缺口，避免把所有历史塞进上下文。

### 关键验收换一个视角

低风险改动由主 Agent 自核；公共逻辑或高风险链路触发独立验收。验收 Agent 只拿验收标准和证据，不拿执行过程，避免被原来的推理路径带偏。

### 全局复核对照固定目标

重大改动开始前，iLoop 会记录这次任务的核心目标、设计决策和不改边界。后续 Review 只做三种判断：符合目标就不改，偏离目标就改回，原目标没有覆盖就先补齐约定。这样全局复核有一把稳定的尺子，不会因为对话变长而忽松忽紧。

### 判断力、业务和平台分开

```text
稳定判断力：VDD / 工作流 / 病例 / 验收 / 错题本
业务领域层：业务材料 / 领域 flow / 专项脚本 / 角色
平台执行层：iOS / 监控 / 日志 / CI / IM / 其他工具
```

换业务只替换领域层，换平台只替换执行插件，主循环不用重写。

### 助手、平台和部署正交

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

## 安装和使用

准备好 Python 3.9+ 和一个能读取文件、执行本地命令的 Coding Agent。iOS 开发还需要 macOS 与 Xcode；模拟器和真机能力所需的公开工具，由 Agent 在实际用到时检查和安装。

入口协议、内核、插件和文档放在同一个仓库里，clone 一次就能拿到相互匹配的版本，不需要另外下载提示词。

把下面这句话发给你的 Agent：

> 请从 https://github.com/Job-Yang/iloop 安装 iLoop，读取仓库里的 AGENT_PROMPT.md，并接入我当前的工程。安装和环境检查由你完成，完成后告诉我可以直接交给你哪些任务。

安装完成后，直接用平时和同事沟通的方式描述任务：

> 帮我修复下单页崩溃，不能改公共 API。完成标准是编译通过，应用拉起后不再崩溃。
>
> 这个按钮偶尔不显示，帮我查清原因并修好。UI 改动要给我看最终截图。
>
> 基于 iLoop 做一个适合我们团队的代码排查助手。

任务怎么规划、调用哪些工具、如何保存进度、什么时候需要独立验收，都由 Agent 按 [AGENT_PROMPT.md](AGENT_PROMPT.md) 执行。普通用户不需要学习 iLoop 的 CLI；宿主接入、信任边界和底层协议分别见 [DESIGN.md](DESIGN.md) 与 [SPEC.md](SPEC.md)。

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
3. 创建隔离扩展。
4. 只修改扩展目录，不改 iLoop 核心。
5. 检查命名空间、依赖和越界修改。
6. 用一个真实任务确认新 flow 能被命中并跑通。

扩展作者真正需要关心的只有业务目标、输入、输出和验收口径。内核里的接口规范是 Agent 和插件之间的“插座标准”，不是普通用户的必修课。

---

## 能力边界

iLoop 核心负责反馈闭环、任务恢复、证据与验收、助手装配和本地执行契约。开源仓还附带 BugFix 参考 Recipe、Git/GitHub Provider 和 iOS 官方插件。

参考 BugFix Recipe 的远端写操作需要宿主签发短期授权。默认本地宿主只能提供同一系统用户内的完整性保护，不冒充独立身份系统；没有可信授权来源时会停在写操作之前。

团队自己的 Oncall、稳定性巡检、监控、IM 和企业 CI 通过扩展接入。仓库提供组装这些助手所需的协议和插槽，不把任何一家公司的平台实现写进核心。

跨节点任务可以使用签名信封和回执约束输入与结果，但远端队列、Worker 拉取、节点身份和密钥分发不在核心内。自动 merge、approve、发布和线上回滚也保留给人工或外部系统。

iOS 真机执行需要可用的 Apple Developer 签名、已配对设备和 `iproxy`。模拟器与真机使用不同的 UI 自动化路径，不能把一边的控件引用直接复用到另一边。Android、Web 和其他平台可以实现自己的 Provider；在官方 Provider 真正跑通之前，架构可扩展不等于已经支持。

MIT License。欢迎使用、修改和二次开发。
