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
python3 -m cli extension-init team.oncall
```

默认生成：

```text
~/.iloop/extensions/team.oncall/
├── manifest.json
└── flows.json
└── plugin.py              # 可选：manifest.provides.plugin 声明 Capability Plugin
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
| 接公司内部日志、监控、CI | 平台插件 | 实现 `Plugin` / `Capability` |
| 做 oncall 或事件驱动 Agent | 事件源 + 通知渠道 | 实现 `EventSource` / `Notifier` |
| 增加领域诊断方法 | 方法专家 | 参考 `kernel/experts.json` |
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

### 平台插件负责“怎么执行”

平台插件只负责接真实能力，例如 logs、metrics、build、screenshot、crash。它返回统一结果和证据，不负责决定病例是否收敛。

需要运行时代码时，在 `manifest.json` 设置 `"provides": {"plugin": "plugin.py"}`，并导出 `create_plugin(config)`。返回对象必须满足 `Plugin` 协议；调用时通过 `platform=<platform_id>` 选择。加载器会拒绝路径逃逸、缺文件和不符合契约的返回值。

如果能力不支持，返回 `unsupported`；不要假装成功。

### 方法专家负责“怎么判断”

专家只回答边界明确的问题，并通过 `wants_capabilities` 声明需要什么证据。它不绑定具体平台名称。

---

## 第四步：校验边界

```bash
python3 -m cli extension-validate ~/.iloop/extensions/team.oncall
```

校验器会拒绝：

- 扩展名不符合 `<team>.<extension>`；
- flow 没带扩展命名空间；
- flow 与核心 ID 冲突；
- manifest 或 flows 格式非法。

校验通过只证明“扩展结构合法”，不证明业务真的能跑。

---

## 第五步：用真实任务验证

`plan` 会自动扫描 `~/.iloop/extensions/`，合并校验通过的扩展 flow。

用一条真实业务请求验证：

```bash
python3 -m cli plan "处理一条真实 oncall 告警"
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
4. 实现 webhook `EventSource`；
5. 实现 Slack `Notifier`；
6. 复用 `Case`、诊断专家、四道关卡和能力 Gate；
7. 校验扩展；
8. 用一条测试告警跑通“事件 → 病例 → 证据 → 结论 → 通知”。

用户不需要手工理解内核协议。协议是 Agent 实现这些插口时使用的技术约束。

---

## 什么时候应该停止并升级核心维护者

扩展开发中遇到以下情况，不要继续在扩展里绕：

- 需要新增所有插件都会复用的公共能力；
- 现有 Capability 契约无法表达真实结果；
- 需要修改病例、四关、验收或证据协议；
- 发现扩展只能通过修改核心内部状态才能工作。

这说明需求可能属于公共核心，应停止并提出主仓改造，而不是在业务扩展里打补丁。
