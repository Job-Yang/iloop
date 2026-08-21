# iLoop 内核协议 SPEC

> 内核与插件之间唯一的契约。内部版和开源版共享这份 spec，各自实现。
> 内核只认这四种协议，不认识任何具体平台/语言。插件通过实现契约"插进"内核，而不是内核去"知道"插件。
>
> 原则（VDD）：**推断不许当观测；完成 = 结果被验过；谁做的不能由谁判。**

---

## 协议 1：证据 EvidenceArtifact

一切结论的地基。每条证据必须可复核、必须标清是"看到的"还是"推的"。

```json
{
  "id": "ev-<短哈希>",
  "capability": "build | logs | screenshot | view_tree | crash | probe | ...",
  "source": "<产出它的插件/工具 id，如 ios_native.wda>",
  "kind": "observed | inferred",
  "outcome": "success | failure | neutral | unknown",
  "created_at": "<ISO8601>",
  "path": "<可复核的产物路径，截图/日志/JSON>",
  "summary": "<一句话：这条证据说明了什么>",
  "for_hypothesis": "<关联的可能原因 id，可空>"
}
```

- `kind` 是红线字段：`observed` = 真跑真看到；`inferred` = 从源码/日志推出来的。**推断标成 observed 即造假。**
- `kind` 只回答“来源是否观测”，不回答“结果是否成功”。observed failure 仍是硬证据，但不能支持四关通过；只有 `kind=observed + outcome=success` 才能作为完成证据。
- observed 成功还必须满足主体与完整性：产物哈希未变化，并绑定 task/run/gate/target/flow/device/created_at。`trusted_producer` 只是声明；Plugin receipt、外部事实、Task 创建策略和 Capability requirements 都需由 Runtime 注入的宿主 attestation verifier 复验。默认 `host_cli` 将证明写入任务目录之外；低层 `cli` 不能手工创建 observed 或自行完成收口。
- `path` 必须指向能被别人重新打开/重跑的东西。嘴上说"验过了"不产出 artifact。

---

## 协议 2：能力契约 Capability Interface

插件对内核暴露的统一动作面。每个能力同名实现（不支持就 no-op 返回 `unsupported`），产出统一结果。

内核认识的能力集（首发）：
`doctor · build · run · install · launch · logs · view_tree · screenshot · crash · probe · counter_probe · tap · swipe · type_text · ui_prepare · ui_status · ui_stop`

统一结果 JSON：

```json
{
  "platform": "<adapter-id，如 ios_native>",
  "capability": "build",
  "status": "success | unsupported | error",
  "evidence_dir": "<本次产物目录>",
  "artifacts": ["<EvidenceArtifact.id ...>"],
  "summary": "<一句话结果>"
}
```

- `status=unsupported` 是合法返回，不是失败——让内核知道"这个平台没这能力"，而不是崩。
- 判成功看 success marker + artifact，不只看 exit code。
- `counter_probe` 必须执行一个明确变化的条件，并用机器断言验证差异；官方 iOS
  插件要求 `counter_condition` + `counter_expect=summary_contains:<text>` 或
  `artifact_contains:<text>`，不能只凭底层命令 exit 0 通过反证 Gate。

---

## 协议 3：flow schema

任务路由与自治分级。内核按 `when_keywords` 匹配任务，按 `autonomy` 决定放权。

```json
{
  "flow_id": "<命名空间.flow名，扩展 flow 必须带前缀防覆盖>",
  "name": "<中文名>",
  "autonomy": "L1 | L2 | L3",
  "when_keywords": ["<触发词>"],
  "priority": 0,
  "guidance": "<方向参考，不是必跑清单>",
  "required_docs": ["<开工前必读文档路径>"],
  "evidence_strategy": "<这类任务优先取什么证据：日志/UI/截图/crash/静态>",
  "escalate_when": "<什么情况停手升级用户>"
}
```

- `autonomy`：L1 只看不改 / L2 动手改(最小改动+编译+验证+可回滚) / L3 放手干(需授权+清单+验收标准)。
- `priority`：关键词命中数相同时的路由优先级。扩展可用更高值覆盖宽泛核心匹配，但不能覆盖核心 `flow_id`。
- 内置 flow 与插件 flow 由加载器合并；插件 flow 只增不覆盖内核。

---

## 协议 4：lesson schema

错题本。踩过的坑召回并前置到下一次。

```json
{
  "id": "lesson-<日期>-<slug>",
  "title": "<一句话坑>",
  "keywords": ["<召回用关键词>"],
  "symptom": "<现象>",
  "root_cause": "<根因>",
  "fix": "<解法>",
  "scope": "general | <插件id>",
  "created_at": "<ISO8601>"
}
```

- 只沉淀"非业务 + 高概率复现 + 解法非平凡"的坑。一次通过、纯业务 bug 不写。
- 开工前 `keywords` 召回；解决后满足条件才写。

---

## 内核不做什么（防过度设计）

- 不认识任何具体平台的 CLI / 域名 / 字段——那是插件的事。
- 不预建通用权限平台 / 账号中心 / 策略引擎——等第二个平台出现真实复用需求再抽象。
- 首发只把 iOS 一个官方插件跑穿这四协议，再谈第二个。
