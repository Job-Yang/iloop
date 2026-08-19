# 能力 Gate 与人机接力（按需加载）

> 何时读：某个能力需要外部授权/凭据（发通知、拉取告警、访问受限数据）却缺失时。

## 原则：权限缺失即停，不伪装收口
内核 `CapabilityGate`（`kernel/gate_capability.py`）只认 required operation 的状态与动作，**不认识任何具体平台**：
- 状态：`blocked` / `ready` / `completed` / `cancelled`
- 动作：`require` / `complete` / `close`

收口/验收前检查两件事：**宿主是否复验了 requirements 清单**，以及**是否仍有未完成的 required operation**。任一不满足都不许收口。

## 平台授权是插件的事，不进内核
具体某个平台（企业 IM、告警平台、受限数据源）怎么授权——OAuth、Device Flow、Token——都是**插件实现**，通过 `Notifier` / `EventSource` / 自定义能力接口挂进来。内核不写死任何平台的 scope、appId、域名。

典型接力流程：
1. 插件发现缺权限 → `gate.require("<op_id>", "<为什么需要>")`，状态置 blocked。
2. 外显 `【iLoop】⛔ 阻塞 需要 <op> 授权：<原因>`，把授权链接/步骤给用户，等用户处理。
3. 平台调用成功且回读证据落盘 → 受信 Adapter 调 `CapabilityGate.complete(..., verify_attestation=...)`；receipt 必须绑定 task_id + operation_id。普通 CLI 默认拒绝完成。
4. 用户明确放弃 → 受信宿主调 `CapabilityGate.close(..., verify_attestation=...)` 写入用户证明。普通 CLI 无权取消，不能用 `confirmed=true` 或环境变量冒充。
5. 收口前宿主先复验 `capability_requirements`，再由 `gate.can_wrapup()` 检查每条 receipt；两层全清才放行。

## 不伪装、不降级欺骗
- 权限没到位就停并升级，**不要用低可信数据伪装成已接入**（VDD：不许把没验到的当验过）。
- 若某能力永久不可用，插件应诚实返回 `unsupported`，而不是假装成功。
