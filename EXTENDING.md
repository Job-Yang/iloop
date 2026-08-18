# 扩展 iLoop（二次开发）

iLoop 内核是原子能力。任何业务定制——代码排查 Agent、智能 oncall Agent、领域诊断流程——都通过**扩展包**接入，而不是改内核。

## 硬边界

- **核心整体只读**。你唯一该改的是自己的扩展目录 `~/.iloop/extensions/<team.ext>/`。
- **flow_id 必须带命名空间前缀** `<team>.<ext>.`，防止覆盖核心 flow。
- 扩展需要新增公共内核能力、平台 adapter 时，走内核 MR，不在扩展里改核心。

## 三步接入

```bash
# 1. 创建扩展包骨架
python3 -m cli extension-init team.oncall

# 2. 编辑 ~/.iloop/extensions/team.oncall/
#    - flows.json：你的业务 flow（flow_id 以 team.oncall. 为前缀）
#    - manifest.json：声明版本、内核依赖、是否带 plugin
#    - （可选）实现 Plugin 契约的插件，暴露 Capability 能力

# 3. 校验（检查越界、命名空间冲突、manifest 合法性）
python3 -m cli extension-validate ~/.iloop/extensions/team.oncall
```

## 扩展能提供什么

| 类型 | 怎么做 | 内核如何合并 |
|---|---|---|
| 业务 flow | 写 `flows.json`，flow_id 带前缀 | `FlowRegistry` 合并，只增不覆盖核心 |
| 平台插件 | 实现 `kernel.Plugin` 协议，暴露 `Capability` 能力 | 通过能力契约插进内核 |
| 事件源/通知 | 实现 `EventSource` / `Notifier` 接口 | oncall 类 Agent 挂自己的事件源和通知渠道 |
| 诊断专家 | 参考 `kernel/experts.json` 结构补方法专家 | 只描述"怎么想"，用 `wants_capabilities` 声明证据需求 |

## 举例：基于内核做一个智能 oncall Agent

内核已经把 oncall 的通用骨架抽好了（`kernel/channel.py`）：

```
事件源(EventSource) → 病例建档(Case) → 证据驱动诊断(experts+四关) → 通知(Notifier)
```

你只需要：
1. 写一个 `EventSource` 实现（从你的告警平台/工单系统拉事件）。
2. 写一个 `Notifier` 实现（发到你的 IM：Slack / 企业微信 / webhook）。
3. 复用内核的病例状态机 + 四关 Gate + 诊断专家做诊断。

`python3 -m cli oncall-demo` 就是这个骨架的最小演示（用 stdout 通知）。

## 校验器会拦什么

`extension-validate` 会报 error 并拒绝：
- 扩展名不是 `<team>.<ext>` 形式
- flow_id 没带命名空间前缀
- flow_id 与核心 flow 冲突（劫持核心）
- flows.json / manifest.json 格式非法
