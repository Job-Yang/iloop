# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 与语义化版本。

## [0.0.1] - 未发布

首个开源版本：**验证驱动闭环内核 + iOS 官方插件**。

### 内核（平台无关，零第三方依赖）
- 证据协议 `EvidenceArtifact`（observed / inferred，推断不许当观测）
- 能力契约 `Capability` + `Plugin` 协议（不支持能力返回 unsupported）
- flow 路由 + L1/L2/L3 自治分级 + `next_suggest` 主动引导，插件 flow 不覆盖核心
- 扩展 flow 自动扫描与加载；同等关键词命中时支持 `priority` 精确路由
- 四道关卡 `FourGate`（时间 / 范围 / 机制 / 反证，只认 observed 证据）
- 病例状态机 `Case`：建档 → 列原因 → 逐个证据排除 → `tick` 下一步检查 → `consult` 会诊 → `reroute` 重分诊 → 过四关收敛
- 9 个诊断方法专家 `ExpertRegistry`（只描述"怎么想"，零平台绑定）
- 独立验收 `IndependentReviewer`（按风险触发 + 防踢皮球三约束）+ 改动代价量化 `score_change`
- 错题本 `LessonBook` 召回与沉淀 + 通用工程种子
- 记账/外显 `Ledger`（`【iLoop】` 前缀协议 + 反循环闸门 3/6 轮）
- 提效看板 `Dashboard`（记账渲染成自包含 HTML）
- 事件源 + 通知接口 `channel`（oncall 通用抽取，飞书/Slack 是插件实现）
- 能力 Gate `CapabilityGate`（权限缺失即停，不伪装收口）
- 红线守卫 `redline`（危险命令拦截 + 禁止污染工程目录）
- 扩展机制 `extension`（二开硬边界，核心只读，命名空间防覆盖）
- 命令执行框架 `runner`（Xcode 自发现 + 红线拦截）
- 可恢复运行时 `Runtime` + `TaskStore`：Task/Case/Gate/Ledger/Evidence 原子落盘，`run/resume/tasks/round/case/accept/wrapup/dashboard` 串成闭环

### iOS 官方插件
- build / run / install / launch / screenshot / view_tree / logs / probe / crash / tap / swipe / type_text 真实现
- 编译、运行、安装和模拟器 UI 统一走公开 XcodeBuildMCP CLI；真机 UI 走 Appium 社区版 WebDriverAgent
- `run`/`launch` 启动 XcodeBuildMCP 动态日志，`logs` 只归档真实日志，不再拿设备信息冒充
- 本机 Xcode 自动发现（不依赖全局 `xcode-select`），本地签名无私有服务
- 真机 crash 本地采集（`devicectl` 拉取）；模拟器 crash 扫 DiagnosticReports

### 文档与工程
- 入口提示词 `AGENT_PROMPT.md` + 分片提示词 `prompts/`
- `SPEC.md`（四协议）/ `DESIGN.md`（设计）/ `EXTENDING.md`（二开）/ `docs/VDD.md`（方法论）
- README 与 DESIGN 按“程序员反馈循环 → 长任务问题 → 架构设计”重写；二次开发改为 Agent 执行协议
- selftest 96 断言（内核 75 + iOS 插件 21），全绿才算完成

### 已知缺口（诚实标注）
- 真机 WDA 生命周期仍需外部启动，真机语义 elementRef 操作不与模拟器混用
- iOS build/run 仍需在一个公开样例工程上补持续 E2E
