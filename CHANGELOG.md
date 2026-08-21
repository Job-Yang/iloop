# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 与语义化版本。

## [0.2.1] - 2026-08-21

### [breaking-entry] 宿主证明分权
- 默认本地账本拒绝签发或验证独立验收、用户确认和任意 evidence subjects；
  高风险身份事实只能由进程外宿主提供。
- 修正宿主接入示例，明确 recorder/verifier 双向契约与外部验收回写 API。

### 修复
- UI Flow 转 Task 后持久化执行上下文，并从绑定的运行证据自动回填节点；
  验证失败不再污染已验证状态。
- 模拟器动作先解析真实 booted UUID；doctor 识别全局 CommandLineTools 对语义
  UI 的阻断；真机 crash 使用 `systemCrashLogs` domain。
- redline 按 argv 语义识别 Git 全局参数和 rm 等价选项，关闭常见绕过。
- 重复扩展 `platform_id` fail closed，插件加载失败返回可诊断来源。
- blocker 文件名改为纳秒时间加随机后缀；`plan` 不再提前输出完成态文案。
- 快速开始将平台无关内核与可选 iOS/Homebrew 安装拆开。

### 验证
- selftest 250 条断言：内核 175 + iOS 插件 75。
- fresh-clone managed-host smoke 通过。
- Xcode 26.3 `devicectl` 真实参数探针通过命令解析并进入设备查找阶段。

## [0.2.0] - 2026-08-21

### [breaking-entry] 受信宿主入口
- 新增 `python3 -m host_cli` 作为默认用户入口，证明账本独立存放于
  `~/.iloop/host-trust/`；低层 `cli` 保持 fail closed。
- 新增只读验收 preflight 子进程和 fresh-clone 旅程 CI；最终高风险 pass 仍只接受外部宿主证明。

### 修复
- Task policy 绑定 goal、constraints、acceptance 和稳定 step ID；证据 receipt
  绑定 created_at；Capability Gate 取消凭证重新核验 task/operation/user/reason。
- Task 与 UI Flow 使用随机唯一 ID；公共路径做 containment 校验；关键状态写入
  增加跨进程锁、原子替换和中断事务 journal。
- 扩展脚手架可立即校验；畸形扩展隔离、合并原子化，并校验内核版本约束。
- 真机 build 不再传非法 `--device-id`；真机 UI 动作写入响应证据；日志零命中
  不再成功；crash 按 bundle、run 和时间窗筛选；WDA prepare/stop 串行化。
- 单独“回归”路由到 L1 验证，L2/L3 缺 Git 工程根时创建前直接拒绝。

### 验证
- selftest 237 条断言：内核 166 + iOS 插件 71。
- fresh-clone managed-host 旅程覆盖低风险四关与最终 wrapup，并验证高风险验收 fail closed。

## [0.1.1] - 2026-08-19

### 修复
- 全局复核只接受本次 review 之后生成的证据，防止旧证据覆盖新 diff。
- 独立验收包绑定执行者身份，宿主必须证明 reviewer 与执行者不同。
- Task ID 拒绝路径分隔符，防止 CLI 写出工程数据目录。
- 损坏扩展不会阻断其他已验证插件的加载。
- WDA 超时和停止改为进程组回收；同秒重复能力调用使用独立证据目录。

### 文档
- README 增加 iLoop 吉祥物，并将真机验证状态更新为“设备已发现、签名账号待恢复”。
- 明确 UI Flow 的 `verified` 状态只能由宿主证明的运行态证据写入。

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
