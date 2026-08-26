# iLoop v0.4 Roadmap：让能力工厂真正跑起来

> 状态：M5-M7 已实现，进入发布复核。起点为 `0.3.0`。

`0.3` 建立了 Action、Recipe、Provider、Deployment、版本化 Case 和签名执行契约。它证明了助手可以被装配，但仍有四个缺口：

- 扩展能新增 Action，却不能新增 Driver Capability；
- Recipe 能装配，不代表写操作安全、源码候选可追溯；
- 配置里声明了助手，不代表当前环境真的能跑；
- 验证范围和耗时没有进入机器可复算的协议。

`0.4` 补齐这些生产执行能力。核心仍然不认识企业平台，不引入远端队列。

## 设计契约

### 目标

- 新 Driver Capability 不需要修改核心枚举；
- 有副作用 Action 未经当前授权，Provider 调用次数必须为零；
- 源码候选从 base commit 到 CI 始终绑定同一条血缘；
- production ready 只认当前配置的新鲜 live smoke；
- 验证范围由完整影响图决定，UI 点验不被提速策略取消；
- 时间和并行收益由结构化事件复算。

### 不改边界

- 不迁内部 IM、监控、研发协作、实验平台、专有宿主或业务仓策略；
- 不写死 Oncall、BugFix、稳定性三种助手；
- 不实现远端队列、Worker 拉取、企业节点身份；
- 不做没有真实样本支撑的编译/UI 并行调度。

## M5 · 可运行能力工厂

### 动态 Driver Capability

新增 `CapabilityId`、`CapabilitySpec` 和 `CapabilityCatalog`。内置枚举继续兼容，扩展可以在 `capabilities.json` 声明自己的输入、输出、副作用、工具和 Deployment。

装配与调用统一 fail closed：

- 未注册或重复能力；
- Provider 声明未知能力；
- 调用缺输入；
- 成功结果漏输出；
- Deployment 不支持；
- Action 漏报底层副作用。

### 副作用授权

`AuthorizationGrant` 绑定 subject、类型、允许的 Action 或兼容层 Capability、Task、Case、诊断 revision、policy 指纹、来源和有效期。Runtime 与 LocalRecipeWorker 都在 Provider 调用前验证；`authorize_tool_use` 对未知工具默认拒绝，并拦截直接 shell 和文件写入。

### 源码候选血缘

公开 Git/GitHub Provider 实现：

```text
base commit
  -> isolated worktree
  -> allowed paths + change digest
  -> candidate commit
  -> remote branch
  -> draft PR
  -> CI for the exact candidate commit
```

随仓提供 `builtin.bugfix` 参考 Recipe。自动 merge、approve 和发布不在配方内。

## M6 · 生产就绪与安装

### Assistant Suite

`SuiteManifest` 支持任意 Assistant 和 Deployment。生命周期固定为：

```text
validate -> compile -> preflight -> install -> smoke -> status
```

状态逐层区分 declared、handler installed、implementation ready、runtime ready 和 production ready。Provider 实现与当前配置必须提供稳定摘要并进入 Suite 指纹；SmokeReceipt 绑定 Suite 指纹、检查清单、证据产物和有效期。

### Agent 驱动安装

安装器固定使用 `~/.iloop/iloop`，注册通用、Claude Code 和 Codex 入口。升级在候选目录跑完 selftest 与 fresh-clone smoke 后原子切换；候选失败或宿主注册失败时恢复旧版本。

## M7 · 减少无效验证

### 验证范围

GlobalReview 复用现有定义、调用方、动态入口、配置和删除分析，不新增路径扫描旁路。每个影响项归入：

- R0：控件或资产，spot；
- R1：页面，spot；
- R2：模块，targeted；
- R3：全局，full。

未知归属默认 R2。UI 任务和可识别的界面源码/资源最低为 R1，并要求 subjects 覆盖当前 target 的 screenshot evidence；人工接受风险不能取消该门槛。scope rules 与 UI 输入会持久化，最终收口使用同一参数重算。

### 时间账本

TimingEvent 记录 phase、Action、Capability、Provider、run、worker、开始结束时间和阻塞时间。Dashboard 复算墙钟时间、工作时间、重试、并发和分层耗时。

### 并行验收

AcceptanceBatch 从现有 AcceptancePackage 按 criterion 拆成多个只读分片，宿主可并行调度。结果必须逐条判定、覆盖全部分片并由独立 reviewer 证明，聚合后继续写回既有 AcceptanceStore 与收口 Gate。

## 验收

- 动态扩展能完整声明 Capability、Action、Recipe、Provider；
- 未授权、过期、错 Task、错 Action、错 revision 在副作用发生前拒绝；
- 真实本地 Git fixture 跑通候选链，血缘篡改失败；
- Suite 旧回执、错配置和产物变化后不再 ready；
- 空 HOME 安装、更新和失败回滚可复现；
- GlobalReview 未知影响不降级，UI 日志不能冒充截图；
- 时间账本和并行验收结果可持久化、可复算；
- Python 3.9、3.11、3.12、fresh-clone 与公开 CI 全绿；
- 完整 diff 通过 VDD 三裁决和独立只读验收。
