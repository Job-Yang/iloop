# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 与语义化版本。

## [0.0.1] - 未发布

首个开源版本：**验证驱动闭环内核 + iOS 官方插件**。

### 内核（平台无关，零第三方依赖）
- 证据协议 `EvidenceArtifact`（observed / inferred，推断不许当观测）
- 能力契约 `Capability` + `Plugin` 协议（不支持能力返回 unsupported）
- flow 路由 + L1/L2/L3 自治分级 + `next_suggest` 主动引导，插件 flow 不覆盖核心
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

### iOS 官方插件
- build / install / launch / screenshot / view_tree / logs / probe / crash 真实现
- 模拟器走 `simctl` + `xcodebuild`，真机走 `devicectl` + Appium 社区版 WebDriverAgent
- 本机 Xcode 自动发现（不依赖全局 `xcode-select`），本地签名无私有服务
- 真机 crash 本地采集（`devicectl` 拉取）；模拟器 crash 扫 DiagnosticReports

### 文档与工程
- 入口提示词 `AGENT_PROMPT.md` + 分片提示词 `prompts/`
- `SPEC.md`（四协议）/ `DESIGN.md`（设计）/ `EXTENDING.md`（二开）/ `docs/VDD.md`（方法论）
- selftest 82 断言（内核 66 + iOS 插件 16），全绿才算完成

### 已知缺口（诚实标注）
- 真机 UI batch 目前只支持 tap 步骤
- iOS build/install 需真实工程环境端到端验证
