# iLoop 设计文档（开源版）

> 本文讲 iLoop 开源版**实际是怎么设计的、为什么这么设计**。基于仓内真实代码，不是立项提案。方法论见 [docs/VDD.md](docs/VDD.md)，协议见 [SPEC.md](SPEC.md)。

---

## 1. 一句话定位

**iLoop = 一个平台无关的「验证驱动闭环内核」+ 一个能真跑的 iOS 官方插件。**

内核只认协议，不认识任何具体平台/语言。iOS 只是它的第一个插件，用来证明这套东西能落地。别人拿到内核，挂自己的插头就能做代码排查 Agent、oncall Agent。

---

## 2. 为什么这么设计：从 VDD 长出来的架构

iLoop 最值钱的不是代码，是一套**让 AI 不能自己骗自己、必须拿证据自证的制度**。这套制度跟语言、领域、公司无关——它是从"AI 会为了达标而钻空子"这个跨领域问题里长出来的。

每一条 VDD 原则，都对应一个具体的架构决定：

| VDD 原则 | 架构落地 |
|---|---|
| 完成 = 结果被验过 | `EvidenceArtifact` + 四关 `FourGate`，无证据不收敛 |
| 推断不许当观测 | `EvidenceKind.observed/inferred`，四关只认 observed |
| 谁做的不能由谁判 | `IndependentReviewer` + `agents/iloop-acceptance.md` 独立裁判 |
| 按代价定验证强度 | `assess_risk` 风险分级触发验收，不一刀切 |
| 任务是档案不是对话 | `Case` 病例状态机，跨轮次记住"凭什么信做对了" |
| 每步只推进一个最能分辨真假的检查 | 病例逐个 hypothesis 用证据排除 |
| 踩过的坑要召回 | `LessonBook` 错题本 + `seed_lessons/` 种子 |
| 别信间接指标 | flow 只做路由不做门槛，看板指标只当线索 |

---

## 3. 分层架构

```
┌─────────────────────────────────────────────────────────┐
│  AGENT_PROMPT.md（入口提示词）+ prompts/（分片）          │
│  ——Agent 读它就知道怎么用内核；协议与代码一体分发         │
├─────────────────────────────────────────────────────────┤
│  kernel/（平台无关内核，零第三方依赖）                    │
│  证据 · 能力契约 · flow · lesson · 四关 · 病例            │
│  记账/看板 · 独立验收 · 通道 · 能力Gate · 红线 · 扩展     │
│         只认 ↓ 四协议与接口，不认识任何具体平台           │
├───────────────────────────┬─────────────────────────────┤
│  官方插件（开源）          │  业务扩展包（用户自己写）    │
│  plugins/ios_native/       │  ~/.iloop/extensions/<t.e>/  │
│  simctl/devicectl/WDA      │  flows + 插件 + 事件源/通知  │
└───────────────────────────┴─────────────────────────────┘
```

- **内核对平台/语言零认知**。插件通过实现 `Capability` 契约插进来；扩展通过命名空间前缀隔离，核心整体只读。
- **提示词和代码一体分发**：一个 GitHub 仓 clone 到底，`git pull` 即更新，不搞远端拉取。

---

## 4. 内核只认四种协议（薄 kernel 的地基）

见 [SPEC.md](SPEC.md) 详解，一句话概括：

1. **EvidenceArtifact**：证据带 `source / kind(observed|inferred) / 可复核路径`。
2. **Capability Interface**：`doctor/build/install/launch/logs/view_tree/screenshot/crash/probe`，统一结果 JSON，不支持返回 unsupported。
3. **Flow schema**：`flow_id/name/autonomy(L1-L3)/when_keywords/guidance/required_docs/evidence_strategy/escalate_when/next_suggest`。
4. **Lesson schema**：错题本召回+沉淀结构。

内核不预建通用权限平台/账号中心——等第二个平台出现真实复用需求再抽象（VDD：别过度设计）。

---

## 5. 内核子系统一览（都有真代码 + selftest 覆盖）

| 模块 | 职责 |
|---|---|
| `evidence.py` | 证据 observed/inferred，推断不许当观测 |
| `capability.py` | 能力契约 + Plugin 协议，不支持能力返回 unsupported |
| `flow.py` | flow 路由 + L1/L2/L3 自治分级 + next_suggest，插件 flow 不覆盖核心 |
| `lesson.py` | 错题本召回与沉淀 |
| `gate.py` | 四道关卡：时间/范围/机制/反证，只认 observed 证据 |
| `experts.py` + `experts.json` | 9 个诊断方法专家 + coordinator，只描述"怎么想"，零平台绑定 |
| `case.py` | 病例状态机：建档→列原因→逐个证据排除→过四关收敛 |
| `ledger.py` | round 记账 + `【iLoop】` 外显协议 + 反循环闸门（同根因3轮/总6轮） |
| `dashboard.py` | 提效看板：把记账渲染成自包含 HTML |
| `acceptance.py` | 独立验收：按风险触发 + 防踢皮球三约束 |
| `channel.py` | 事件源 + 通知接口（oncall 通用抽取，飞书/Slack 是插件实现） |
| `gate_capability.py` | 能力 Gate：权限缺失即停，不伪装收口 |
| `redline.py` | 红线守卫：危险命令拦截 + 禁止污染工程目录 |
| `extension.py` | 扩展机制 + 二开硬边界（核心只读，命名空间防覆盖） |
| `runner.py` | 命令执行框架 + Xcode 自发现 + 红线拦截 |

---

## 6. iOS 官方插件：真机自动化是一等能力

VDD 主打"看真实结果"。真机自动化跑不起来，验证编程在真机这一环就是空的，只有模拟器等于只验了半个世界。所以 iOS 插件不只有 build：

- **执行**：`xcrun simctl`（模拟器）/ `xcrun devicectl`（真机）+ `xcodebuild`
- **真机 UI 全套**（截图/点击/滑动/输入/UI 层级树）：Appium 社区版 WebDriverAgent（`plugins/ios_native/wda_client.py`）+ `iproxy` + `ffmpeg`
- **签名**：本机 Xcode 账号（`Apple Development` + `-allowProvisioningUpdates`），无私有服务
- **Xcode 自发现**：`discover_developer_dir` 自动找到已装 Xcode 并注入 `DEVELOPER_DIR`，不依赖全局 `xcode-select`
- **诚实缺口**（写在 `KNOWN_GAPS`，不假装完整）：真机 crash 本地采集未实现；真机 UI batch 只支持 tap

模拟器链路更成熟，真机 UI 完整但有上述缺口。两条链路独立实现，按 `mode` 分叉。

---

## 7. oncall 的通用抽取（一套内核、多种 Agent）

把飞书剥掉，oncall 剩下的通用骨架就是内核本身：

```
事件源(EventSource) → 病例建档(Case) → 证据驱动诊断(experts + 四关) → 按代价处置 → 通知(Notifier)
```

飞书只占「事件源」和「通知渠道」两个接口的一种实现。开源版给 stdout/webhook 参考实现；企业内部 IM 挂对应实现；社区挂 GitHub Issues / PagerDuty / Sentry。`cli oncall-demo` 是这个骨架的最小演示——**抽出来的不是 oncall，是"事件驱动的诊断闭环"**。

---

## 8. 内核与插件：可插拔哲学

- iLoop 的价值在于**内核平台无关**，唯一契约是内核四协议 spec。任何具体平台（监控、告警、数据、IM、CI）都通过实现 `Capability` / `EventSource` / `Notifier` 接口作为**插件**接入，内核不认识它们。
- 私有平台适配、企业内部系统对接，都应做成独立插件或扩展包，不改核心。核心只维护协议和平台无关的能力。
- 判断某段逻辑该不该进核心：**"换一家公司、换一套工具链，这段逻辑还成立吗？"** 成立进核心，不成立做成插件/扩展。

---

## 9. 命名

- 仓库/品牌：**iLoop**（i + Loop 反馈闭环，本就与 iOS 无关）。
- 核心包：`kernel`。方法论旗号：**VDD**。
