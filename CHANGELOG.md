# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 与语义化版本。

## [0.1.0] - 2026-08-19

首个开源版本：**验证驱动闭环内核 + iOS 官方插件**。

### 内核（平台无关，零第三方依赖）
- 证据协议 `EvidenceArtifact`（observed / inferred，推断不许当观测）
- 能力契约 `Capability` + `Plugin` 协议（不支持能力返回 unsupported）
- flow 路由 + L1/L2/L3 自治分级 + `next_suggest` 主动引导，插件 flow 不覆盖核心
- 扩展 flow 自动扫描与加载；同等关键词命中时支持 `priority` 精确路由
- 四道关卡 `FourGate`（时间 / 范围 / 机制 / 反证，只认 observed 证据）
- 病例状态机 `Case`：建档 → 列原因 → 逐个证据排除 → `tick` 下一步检查 → `consult` 会诊 → `reroute` 重分诊 → 过四关收敛
- 9 个诊断方法专家 `ExpertRegistry`（只描述"怎么想"，零平台绑定）
- 独立验收 `AcceptanceStore`（验收包/外部 reviewer 回写/禁止自验）+ `IndependentReviewer` 规则参考 + 改动代价量化 `score_change`
- 错题本 `LessonBook` 召回与沉淀 + 通用工程种子
- 记账/外显 `Ledger`（`【iLoop】` 前缀协议 + 反循环闸门 3/6 轮）
- 提效看板 `Dashboard`（记账渲染成自包含 HTML）
- 事件源 + 通知接口 `channel`（oncall 通用抽取，飞书/Slack 是插件实现）
- 能力 Gate `CapabilityGate`（权限缺失即停，不伪装收口）
- 红线守卫 `redline`（危险命令拦截 + 禁止污染工程目录）
- 扩展机制 `extension`（二开硬边界，核心只读，命名空间防覆盖）
- 命令执行框架 `runner`（Xcode 自发现 + 红线拦截）
- 可恢复运行时 `Runtime` + `TaskStore`：Task/Case/Gate/Ledger/Evidence 原子落盘，`run/resume/tasks/round/case/accept/wrapup/dashboard` 串成闭环
- `wrapup` 改为不可绕过硬 Gate：步骤必须带证据/人确认，Capability Gate 必须有真实回读，Case resolved + 四关 + 全局复核 + 必需独立验收全部通过才可收口
- Evidence 增加 `outcome`，明确 observed failure 是硬事实但不能作为完成证据
- 信任边界 fail closed：普通 CLI 不可手工写 observed、用户确认、平台完成/取消和独立验收；受信宿主只能通过 Runtime/Store API 注入 verifier 回调，所有证明绑定 task/run/gate/target/flow/device、产物哈希与有效期
- 新增完整 diff 全局视角：公共定义、仓内调用方、共享边界、删除逻辑逐项复核；L2/L3 必须绑定工程根
- 独立验收拆成 `accept prepare/record/status`，主 Agent 禁止用 self/main reviewer 给自己盖章
- 新增 `next` 证据缺口路由、inputs manifest、Constitution、结构化 blocker、records 与可复用 UI Flow

### iOS 官方插件
- build / run / install / launch / screenshot / view_tree / logs / probe / crash / tap / swipe / type_text 真实现
- 编译、运行、安装和模拟器 UI 统一走公开 XcodeBuildMCP CLI；真机 UI 走 Appium 社区版 WebDriverAgent
- `run`/`launch` 启动 XcodeBuildMCP 动态日志，`logs` 只归档真实日志，不再拿设备信息冒充
- 动态日志改为绑定本次 `run` 的路径，禁止扫描全局最新日志串任务
- 固定 Appium WDA `v16.1.1` 及官方 commit/origin，新增 `ui_prepare/ui_status/ui_stop` 管理源码、XcodeBuildMCP runner 与 iproxy 生命周期
- 本机 Xcode 自动发现（不依赖全局 `xcode-select`），本地签名无私有服务
- 真机 crash 本地采集（`devicectl` 拉取）；模拟器 crash 扫 DiagnosticReports
- 真实模拟器 E2E 跑通 build/run/UI tree/screenshot/tap/logs；修复 snapshot 过期恢复、截图结构化成功判定和 runtime log 路径解析
- CommandRunner 改为跨平台进程树托管，超时/中断回收子进程，长期日志 helper 不再阻塞主命令返回
- direct `doctor/invoke` 证据统一进入项目数据目录；Xcode 环境发现下沉到 iOS adapter，并保留旧公开 API 兼容层

### 文档与工程
- 入口提示词 `AGENT_PROMPT.md` + 分片提示词 `prompts/`
- `SPEC.md`（四协议）/ `DESIGN.md`（设计）/ `EXTENDING.md`（二开）/ `docs/VDD.md`（方法论）
- README 与 DESIGN 按“程序员反馈循环 → 长任务问题 → 架构设计”重写；二次开发改为 Agent 执行协议
- 全局视角固定 Task 起始 Git commit，补齐行为配置文件、Objective-C selector、动态路由/DI、显式 evidence subjects 与受影响测试建议
- selftest 200 断言（内核 144 + iOS 插件 56），包含提交后空审、旧 Task 基线缺失、进程树泄漏、snapshot 误重绑、业务文本防误改、运行日志解析和宿主尾部错误等反向测试

### 已知缺口（诚实标注）
- 当前验收机未连接可用 iPhone，WDA 真机 E2E 仍待设备与签名环境；真机语义 elementRef 操作不与模拟器混用
- 本地生成式 fixture 已完成模拟器 E2E，仍需把该链路固化为公开 CI E2E
