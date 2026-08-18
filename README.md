# iLoop

> 让 AI 在说"完成"之前，先老老实实回答一句：**我凭什么这么说。**

iLoop 是一个**验证驱动（VDD）的研发 Agent 闭环内核**。它不替 AI 写代码，它管一件写代码管不了的事：**让 AI 不能自己骗自己——每个"完成"背后都得有一份别人能重跑、能复核的证据。**

内核平台无关。iOS 是它的第一个官方插件。你可以在同一个内核上挂自己的插头，做代码排查 Agent、智能 oncall Agent，或任何需要"证据驱动闭环"的 Agent。

- 方法论：[docs/VDD.md](docs/VDD.md)｜设计文档：[DESIGN.md](DESIGN.md)｜内核协议：[SPEC.md](SPEC.md)｜二次开发：[EXTENDING.md](EXTENDING.md)

---

## 它能干什么

给 AI 研发 Agent 装一套"刹车 + 制度"，覆盖需求 / 迭代 / Debug / 运行态排查 / 重构 / 崩溃巡检 / 环境体检：

- **VDD 验证闭环**：完成 = 结果被验过，不是代码写完。收敛必须过四道关卡（时间/范围/机制/反证）。
- **病例式诊断**：把任务当持续档案——建档、列可能原因、逐个用证据排除、过四关收敛，跨轮次跨会话。
- **证据分级**：observed（真跑真看到）vs inferred（推断），推断不许当观测。
- **独立验收**：高风险改动交给只负责挑错的独立角色，"谁做的不能由谁判"。
- **iOS 真机/模拟器自动化**：build / install / launch / 截图 / UI 层级树 / 日志，纯开源栈。
- **提效看板**：直观看到这一轮 iLoop 编译几次、取证几次、止损几次、验收几次。
- **错题本**：踩过的坑召回并前置，同一类坑不踩第二次。
- **可扩展**：任何业务能力通过扩展包接入，核心整体只读。

---

## 快速开始

**前置**：Python 3.9+（内核零第三方依赖，纯标准库）。用 iOS 插件另需 macOS + Xcode；真机 UI 另需 `ffmpeg`、`iproxy`（libimobiledevice）。

```bash
# 1. 拉仓（提示词 + 内核 + 插件都在这一个仓里，不用再拉别的）
git clone https://github.com/<your-org>/iloop.git
cd iloop

# 2. 自测，全绿说明环境就绪
python3 -m cli selftest

# 3. 让 flow 路由帮你判该怎么干
python3 -m cli plan "帮我修复下单页崩溃"

# 4. iOS 依赖体检（自动发现已装的 Xcode，不依赖全局 xcode-select）
python3 -m cli doctor
```

**在 Agent 宿主里用**：把仓根的 [AGENT_PROMPT.md](AGENT_PROMPT.md) 作为系统提示 / 项目规则加载到你的 Agent 宿主（Claude Code、Codex、Cursor、自建 Agent 皆可）。它是稳定入口，Agent 读了就知道怎么用这套内核；细则按需读 `prompts/` 分片。

> **分发就一个仓**：提示词和代码是一体的——提示词里写的 `python3 -m cli ...` 只有仓在本地才跑得通。`git clone` 拿到全部，`git pull` 就是更新，不存在"提示词拉不到仓"的问题。

---

## 命令速览

```
python3 -m cli plan "<任务>"        # flow 路由 + 自治分级（先跑这个）
python3 -m cli flows                # 列出已加载 flow
python3 -m cli experts "<任务>"     # 诊断方法专家路由
python3 -m cli doctor [--real]      # iOS 插件依赖体检
python3 -m cli invoke <cap> [--real] [k=v ...]   # 真调能力：build/install/launch/screenshot/view_tree/logs/probe
python3 -m cli oncall-demo          # 演示：同一内核驱动 oncall 诊断
python3 -m cli extension-init <team.ext>          # 创建业务扩展包（二开入口）
python3 -m cli extension-validate <dir>           # 校验扩展包
python3 -m cli selftest             # 改了内核/插件后必跑，全绿才算完成
```

示例：真跑一次模拟器截图（需已启动模拟器）
```bash
python3 -m cli invoke screenshot sim_udid=<booted-udid>
```

---

## 目录结构

```
iloop/
├── AGENT_PROMPT.md      # Agent 入口提示词（稳定入口）
├── SPEC.md              # 内核四协议契约
├── DESIGN.md            # 设计文档
├── EXTENDING.md         # 二次开发指南
├── cli.py               # 命令入口
├── selftest.py          # 内核自测（72 断言）
├── kernel/              # 平台无关内核（证据/能力/flow/lesson/四关/病例/记账/验收/通道/看板/红线/扩展）
├── plugins/ios_native/  # iOS 官方插件（含真机 WDA 自动化）
├── prompts/             # 分片提示词（按需加载）
├── workflow/flows.json  # 内置 flow 注册表
├── agents/              # 独立验收裁判角色
├── seed_lessons/        # 通用工程错题本种子
└── docs/VDD.md          # 方法论全文
```

---

## 扩展（做自己的 Agent）

iLoop 内核是原子能力。做代码排查 Agent、oncall Agent、领域诊断流程，都通过**扩展包**接入，核心整体只读：

```bash
python3 -m cli extension-init team.oncall
# 编辑 ~/.iloop/extensions/team.oncall/（flows.json / 插件 / 事件源 / 通知渠道）
python3 -m cli extension-validate ~/.iloop/extensions/team.oncall
```

详见 [EXTENDING.md](EXTENDING.md)。`python3 -m cli oncall-demo` 是"同一内核驱动一个 oncall Agent"的最小演示。

---

## 状态

早期（0.0.1）。首发范围：**内核 + iOS 官方插件（含真机自动化）**。

诚实边界：selftest 82 断言全绿（内核 66 + iOS 插件 16）、零第三方依赖；iOS 插件命令实现完整（含真机/模拟器 crash 采集），模拟器 screenshot/probe 已端到端真跑通，build/install 需真实工程环境验证；真机 UI batch 当前只支持 tap，见各模块 `KNOWN_GAPS`。oncall 参考插件紧随其后，坐实"一套内核、多种 Agent"。

## License

MIT（见 [LICENSE](LICENSE)）。开源前请与版权归属方最终确认。
