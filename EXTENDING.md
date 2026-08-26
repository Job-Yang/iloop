# 基于 iLoop 做二次开发（Agent 执行协议）

> 这份文档首先写给 Agent，而不是要求使用者手工研究插件 SDK。
>
> 当用户说“基于 iLoop 做一个 XX Agent / 工作流 / 平台插件”时，Agent 按本文自主完成判断、创建、实现和验证。

## 目标

iLoop 是稳定的反馈闭环底座。业务二次开发只定义自己的领域逻辑：

- 这类任务什么时候命中；
- 应该先判断什么；
- 需要哪些业务输入；
- 调用哪个平台能力；
- 最终怎样验收。

计划、证据、病例、止损、独立验收、错题本和看板继续复用 iLoop 核心。

---

## 第一步：先判断归属，不要看到需求就改核心

只问一个问题：

> 这个能力是否要服务所有 iLoop 用户，并且必须成为公共底座的一部分？

- **是**：属于公共核心。走主仓贡献流程，修改前说明为什么它平台无关。
- **否**：只服务某个团队、业务或私有平台，走独立扩展。
- **不清楚**：先让用户点选“公共核心 / 业务扩展”，确认前不改文件。

不能因为当前目录就是 iLoop、用户可能是维护者，就默认改核心。

---

## 第二步：创建隔离扩展

扩展名使用 `<team>.<extension>`：

```bash
python3 -m host_cli extension-init team.oncall
```

默认生成：

```text
~/.iloop/extensions/team.oncall/
├── manifest.json
├── flows.json
├── capabilities.json      # 可选：扩展自己的 Driver Capability
├── actions.json           # 应用动作契约
├── recipes.json           # 助手动作组合
├── application.py         # 可选：应用动作 handler
└── plugin.py              # 可选：Driver Capability Provider
```

从这一刻起：

- **只修改这个扩展目录**；
- iLoop 核心目录整体只读；
- `flow_id` 必须以 `team.oncall.` 开头；
- 不复制或魔改 `AGENT_PROMPT.md`；
- 不往核心注册表手工加业务分支。

---

## 第三步：按需要选择扩展插口

Agent 根据目标自主选择最少的插口，不要求每个扩展都实现全部类型。

| 用户要做什么 | 该实现什么 | 接入方式 |
|---|---|---|
| 增加一类业务任务 | 业务 flow | 写 `flows.json` |
| 增加新的底层执行能力 | `CapabilitySpec` | 写 `capabilities.json` |
| 定义可组合的业务动作 | `ActionSpec` | 写 `actions.json` |
| 组合 Oncall/Bugfix/稳定性助手 | `AssistantRecipe` | 写 `recipes.json` |
| 实现动作编排逻辑 | Action handler | 在 `application.py` 导出 `create_action_handlers(config)` |
| 接日志、监控、CI、设备 | Provider | 实现 `Plugin` / Driver `Capability` |
| 多 Provider 能力重叠 | 显式绑定 | 在 manifest 的 `provider_bindings` 指定 capability → platform_id |
| 增加领域诊断方法 | 领域 flow | 把判断方法写入 `guidance` 与 `required_docs` |
| 有复杂批处理逻辑 | 扩展脚本 | 放在扩展目录，由 flow 引用 |

### flow 负责“怎么想”

flow 至少写清：

- `when_keywords`：什么任务命中；
- `priority`：与宽泛核心 flow 同时命中时，精确业务 flow 的优先级；
- `autonomy`：L1 / L2 / L3；
- `guidance`：先判断什么，不写机械清单；
- `required_docs`：开工前读哪些领域文档；
- `evidence_strategy`：优先取哪种证据；
- `escalate_when`：什么情况下必须停下问人；
- `next_suggest`：收口后主动建议什么。

### Action 和 Recipe 负责“做什么”

`capabilities.json` 中每个 `capability_id` 必须带扩展命名空间。它声明输入、输出、副作用、可替代工具组和支持的 Deployment；Provider 只负责实现，不得在加载时偷偷增加未声明能力。重复、未知和输出缺失都会 fail closed。

`actions.json` 中每个 `action_id` 必须带扩展命名空间，并声明输入、输出、风险、副作用、允许的助手和所需 Driver Capability。Action 的副作用声明不能比底层 Capability 更弱。Action 用 `lifecycle_stage` 声明 diagnosis / disposition / verification / observation 阶段；处置 Action 还要声明 `disposition_kind`（`code_change / isolation / human_handoff / observe`）。`recipes.json` 只保存版本、有序动作列表、入口和是否持续观察，不能写部署分支。

```json
{
  "capability_id": "team.oncall.incident_scan",
  "description": "Read incidents from the configured monitoring service",
  "inputs": {"project": "string"},
  "outputs": {"incidents": "array"},
  "side_effect": "read",
  "required_tools": [["sentry-cli", "custom-monitor-cli"]],
  "supported_deployments": ["team.oncall.local"]
}
```

需要执行应用逻辑时，在 manifest 的 `provides.application` 指向 Python 模块：

```python
def create_action_handlers(config):
    return {
        "team.oncall.collect": lambda payload: {"case_id": "..."},
    }
```

handler 必须是 callable，输出必须覆盖 ActionSpec 声明的字段。缺 Action、重复动作、风险不一致或 handler 非法都会拒绝装配。

### Provider 负责“怎么执行”

平台插件只负责接真实能力，例如 logs、metrics、build、screenshot、crash。它返回统一结果和证据，不负责决定病例是否收敛。

Capability Plugin 是宿主进程内执行的可信代码。安装第三方插件等价于允许它以
当前用户权限运行；只安装已审阅来源。需要隔离不可信插件时，由自定义宿主把
插件放进独立进程或容器，不能依赖 Python 模块边界提供安全沙箱。

需要运行时代码时，在 `manifest.json` 设置 `provides.plugin`，并导出 `create_plugin(config)`。返回对象必须满足 `Plugin` 协议。多个 Provider 可以同时装配；能力重叠时必须显式绑定，否则 fail closed。加载器会拒绝路径逃逸、缺文件、重复 `platform_id` 和不符合契约的返回值。

需要加入 `AssistantSuite` 的 Provider 还必须实现 `runtime_fingerprint()`，返回 64 位
SHA-256。摘要要同时覆盖实现版本和影响执行结果的配置，但不能泄露密钥原文。Provider
不提供有效摘要时仍可用于普通装配，Suite 会保持 `production_ready=false`，旧 smoke
不能替新实现或新配置背书。

如果能力不支持，返回 `unsupported`；不要假装成功。

有 `workspace_write`、`external_write` 或 `process` 副作用的 Action 必须由宿主提供短期 `AuthorizationGrant`。Grant 绑定 Task、Case、诊断 revision、policy 指纹和 Action；兼容层直接执行副作用 Capability 时必须由 `allowed_capabilities` 单独授权。扩展不能从用户文本自行推导授权，也不能在 Provider 内补默认放行。宿主还应把同一授权接到 PreToolUse Gate，阻止绕过 Runtime 的直接 shell 写操作；未知工具默认拒绝。

### 领域判断先放 flow

扩展自动发现会接入 flow、Action、Recipe、Action handler 和 Provider。领域判断仍应先写进 `guidance` / `required_docs`；`EventSource`、`Notifier` 和自定义专家属于手工宿主集成 API，尚不由扩展 manifest 自动加载。

---

## 第四步：校验边界

```bash
python3 -m host_cli extension-validate ~/.iloop/extensions/team.oncall
```

校验器会拒绝：

- 扩展名不符合 `<team>.<extension>`；
- flow 没带扩展命名空间；
- flow 与核心 ID 冲突；
- Action/Recipe 没有扩展命名空间；
- Capability 没有扩展命名空间、重复或 schema 不完整；
- Action 漏报底层 Capability 的副作用；
- Recipe 引用不存在的 Action 或风险声明冲突；
- manifest、flows、actions 或 recipes 格式非法。

校验通过只证明“扩展结构合法”，不证明业务真的能跑。
跨扩展装配时还会检查 Provider binding 指向的 Provider 是否存在、是否真的声明
对应能力；这一步依赖当前宿主已经加载的完整 Provider 集，不能由单包
`extension-validate` 独立证明。

---

## 第五步：用真实任务验证

`plan` 会自动扫描 `~/.iloop/extensions/`，合并校验通过的扩展 flow。

用一条真实业务请求验证：

```bash
python3 -m host_cli plan "处理一条真实 oncall 告警"
```

必须确认：

1. 命中了扩展 flow，而不是核心 fallback；
2. autonomy、required_docs、取证方向正确；
3. 实际跑一轮后能产出对应证据；
4. 失败时会按扩展定义升级，而不是无限重试；
5. 收口标准能被真实证据验证。

只生成了 `flows.json`，不能算二次开发完成。

---

## 示例：让 Agent 做一个智能 oncall 扩展

用户可以直接说：

> 基于 iLoop 做一个智能 oncall Agent。事件从我们的告警 webhook 进来，结论发到 Slack；默认只读诊断，任何写操作先问我。

Agent 应当：

1. 判断为业务扩展；
2. 创建 `team.oncall`；
3. 写 oncall flow；
4. 用 `ActionSpec` 定义接警、诊断、处置、验证和通知动作；
5. 用 `AssistantRecipe` 组合 Oncall 助手，不写部署分支；
6. 用 Provider 接 webhook、日志和 Slack，重叠能力显式绑定；
7. 复用版本化 `Case`、诊断专家、四道关卡和能力 Gate；
8. 校验扩展，并用一条测试告警跑通“事件 → 病例 → 证据 → 结论 → 通知”。

用户不需要手工理解内核协议。协议是 Agent 实现这些插口时使用的技术约束。

---

## 什么时候应该停止并升级核心维护者

扩展开发中遇到以下情况，不要继续在扩展里绕：

- 需要新增所有插件都会复用的公共能力；
- 现有 Capability 契约无法表达真实结果；
- 需要修改病例、四关、验收或证据协议；
- 发现扩展只能通过修改核心内部状态才能工作。

这说明需求可能属于公共核心，应停止并提出主仓改造，而不是在业务扩展里打补丁。
