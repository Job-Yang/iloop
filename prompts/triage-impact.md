# 影响面 · 崩溃巡检 · 升级契约（按需加载）

> 何时读：重构/回归影响面、崩溃归因、命中卡口要升级用户时。

## 主动排查与影响面
- 多问题输入拆成独立 task，各有目标、证据目录、结论状态。
- crash 归因先判真实 crash/进程退出，再看堆栈关键词、变更、入口、lessons。
- case 分层：`red` 必测 / `yellow` 抽样 / `green` 低优先 / `review` 需 Owner 拍板。
- 线上崩溃平台（Sentry/Crashlytics/自建）作为**证据源插件**接入——通过实现 `crash` 能力或 `EventSource` 接口挂进来，内核不绑定任何具体平台。平台给的自动修复/提交归因只作**候选**，默认不自动改代码，等用户确认再进 bugfix 并由 iLoop 编译、运行、验收。

## 归因结论的可信度标记（外显给用户时·强制统一）
排查/归因场景常给用户抛"这段代码是根因/这是高风险点"这类判断——每条结论必须带可信度标记，统一成中文 + `【iLoop】` 前缀，别把"我猜的"当"确定的"（软假设伪装成硬事实是长期记忆投毒的头号原因）。对应内核证据分级 observed/inferred：
- `【iLoop】🔍 取证 <结论> · [机验]` —— 机器测过（编译/探针/selftest/真跑复现），最硬，可直接信。对应 observed。
- `【iLoop】🔍 取证 <结论> · [证据]` —— 有截图/日志/源码行支撑，基本可信，存疑可复查。对应 observed。
- `【iLoop】🔍 取证 <结论> · [假设]` —— 只是 agent 读代码的判断、没验证过，读者不准当真值用、必须自己重验。对应 inferred。
- **禁止**用 `[Evidence]`/`[Hypothesis]` 等英文变体或自造标记；判不了来源的默认降级成 `[假设]`。

## 看板 / 提效面板（log 提效）
- 每轮通过 `run/resume caps=...` 自动记账，或显式调用 `round start/end`；收口执行 `wrapup` 刷看板。看板统计自动编译、取证、失败轮次和证据类型——这是用户感知 iLoop 价值最直接的入口，别省。
- trace 时间线落 `trace.jsonl`；会话诊断可喂 trace 客观定位"绕没绕、卡哪"。

## 升级契约（不傻干）
- 命中环境/签名/账号/权限/缺输入卡口，不反复试同一根因，必须产出结构化 blocker 并提问（证书失效、设备不可达、需切环境/登录/验证码/权限、缺必需 inputs、构建服务异常且非代码问题）。升级后等用户处理并记 `asked_human=true`。
- **升级前先确认不是 iLoop 能自愈的假卡口**：blocker 是"iLoop 尽力了仍需人"的最后一步，不是"撞到报错就甩用户"。典型假卡口——`xcode-select 指向 CommandLineTools` 导致 `devicectl/simctl not found`：只要本机装了完整 Xcode，内核 `discover_developer_dir` 会在 doctor/build/install/launch 时自动进程级指向完整 Xcode（不改全局、可逆）。**所以别为"CLT 缺 devicectl"升级——先走 CLI 命令（走了就自愈），真没装完整 Xcode 才是真卡口**。
- 反循环闸门（内核 `Ledger.should_stop`）：同根因失败 3 轮、总失败 6 轮触发停手，给证据 + 候选 + 推荐，不无限重试。
