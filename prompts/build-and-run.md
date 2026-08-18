# 构建 · 运行 · 取证（按需加载）

> 何时读：要 build/install/launch、模拟器或真机跑、抓运行态证据时。

## 编译与安装
- 编译只走 `python3 -m cli invoke build` 或可恢复的 `run/resume ... caps=build,run`。iOS 插件调用公开 XcodeBuildMCP CLI，禁止裸 `xcodebuild`。
- XcodeBuildMCP 的 Skill/MCP/CLI 是同一开源项目的不同入口；iLoop 的可交付依赖是 CLI 二进制。安装：`brew tap getsentry/xcodebuildmcp && brew install xcodebuildmcp`。
- 成功看 success markers（`BUILD SUCCEEDED`）+ 产物存在，**不只看 exit code**（VDD：exit 0 不等于成功）。
- 失败先分类：环境/签名/依赖/编译/链接/构建服务/测试/资源，别把环境问题误修成业务代码。
- **失败第一步查错题本**：分类后动手前先 `python3 -m cli lessons search "<报错关键词>"`。命中就按已有解法走（用当前日志复核，旧解法可能过期），别从零硬解。
- `Multiple commands produce`、签名、DerivedData 之类先查插件参数和工程配置，不改头文件/podspec/Info.plist 硬修。
- **Xcode 自发现**：内核 `discover_developer_dir` 会自动找到已装的 Xcode（不依赖全局 `xcode-select`），并注入 `DEVELOPER_DIR`。`doctor` 会如实报告依赖是否齐备。

## 单轮闭环
- 流程：`run 建 Task → 计划/改动 → resume caps=build,run,... → 读证据 → 修复 → 按目标取证 → accept/wrapup`。Runtime 自动保存 round、evidence 和 dashboard；手动轮次用 `round start/end`。
- 收口时按实自报这轮产出了哪些证据（`build` 编译通过 / `screenshot` 截图 / `log` 日志 / `crash` 崩溃报告 / `ui` 视图层级 / `acceptance` 独立验收），看板靠它统计"iLoop 给了多少证据"，是小场景价值的关键来源，别省。临时探针日志默认不提交，收口前清理。

## 运行态证据策略
- **目标导向选证据**，对应工具：数据/字段/已加日志→定向日志（`invoke logs`，不默认抓 UI/截图）；控件存在/隐藏/frame→UI 树（模拟器/真机 `invoke view_tree`）；最终表现→截图（`invoke screenshot`）；crash/进程退出→crash report/进程状态/启动日志。
- 证据不足只补最小缺口，不把轻量日志问题升级成全量重验证。**模拟器与真机结论分开，互不推断**。
- 检索取证用 Grep/Read，禁止拼长 `python3 -c`/heredoc 读全文件（易卡死）。大范围检索先缩小关键词。

## 真机 vs 模拟器（最佳实践）
- **默认优先模拟器**：本机自主、不依赖外接设备和签名、闭环最快。能用模拟器就别上真机。
- **必须真机**：直播/音视频/连麦、相机/麦克风/定位/蓝牙/传感器/Push 等硬件；签名/证书/权限；支付/登录态/风控/真实网络；性能/启动/内存量化。
- **判断法**：现象依赖硬件/签名/真实服务端策略吗？是→真机（`--real`），否→模拟器。两种设备结论互不推断，下结论写明在哪种设备。

## 模拟器工具链
- 快速闭环：优先 `resume <task_id> caps=run,view_tree,screenshot,logs ...`。`run` 是 XcodeBuildMCP 的 build-and-run，一次完成编译、安装、拉起并启动动态日志。
- 底层走 XcodeBuildMCP CLI：build/run/install/launch/snapshot-ui/tap/swipe/type/screenshot/probe。只验编译时使用 `build`，别跑重型 runtime。
- 模拟器异常不能推断真机；涉硬件/权限/音视频/系统账号/真实网络必须切真机。

## 真机执行（含真机 UI 自动化——VDD 一等能力）
- 标准链路：`resume <task_id> --real caps=run,... workspace=... scheme=... device_udid=...`。
- **执行栈全开源**：build/install/launch 走 XcodeBuildMCP device workflow；真机 UI（截图/点击/滑动/输入/UI 层级树）走 Appium 社区版 **WebDriverAgent**（内核 `plugins/ios_native/wda_client.py`），经 `iproxy` 转发到 127.0.0.1:8100。
- **签名**：使用工程在本机 Xcode 中已有的签名配置，无任何私有签名服务。
- 首装/首启遇登录/验证码/隐私/权限/证书信任等人工卡口，第一时间让用户处理并记 `asked_human=true`。
- **诚实缺口**：真机 crash 已通过 `devicectl` 拉取；WDA 生命周期仍需外部启动，真机不提供模拟器那套语义 elementRef。没有在线 WDA 时必须明确失败，别假装完整。

## UI 验证决策表（改了任何 UI，先查这张表用对工具"看到渲染结果"再核对——禁止只 grep 源码）
> 铁律：UI 改动的真值源是**渲染出来的画面**，不是源码里有没有那行 CSS/DOM/约束。改完必须"渲染→亲眼看/客观测→对照目标"再宣布完成。用错验证方式=没验证。

| 改的是什么 UI | 静态外观（布局/对齐/颜色/文案/控件在不在） | 动态（动画/转场/方向/卡顿/跟手） |
| --- | --- | --- |
| **iOS 模拟器** | `invoke view_tree`（查控件树/frame）+ `invoke screenshot` → Read 亲眼看 | 录屏（`xcrun simctl io booted recordVideo out.mov`）→ `video-reader` 逐帧 |
| **iOS 真机** | WDA `invoke view_tree`（UI 层级树）+ `invoke screenshot` → Read 看 | WDA MJPEG 录屏 → `video-reader` 逐帧；录屏扰动性能，性能测量窗口内禁用 |
| **PC Web / HTML 产物**（看板、报告） | headless 渲染成 PNG → Read 亲眼看；或起 browser 子 Agent 打开核对 | 录屏 → `video-reader` 逐帧；方向/位移别肉眼猜，用帧差追踪像素位移客观判 |

- **选静态还是动态**：只改外观→静态截图够；碰了动画/转场/滚动/手势→必须录屏逐帧，静态截图看不出方向和卡顿。
- **客观优先于肉眼**：方向、位移、跟手这类肉眼易错，用帧间像素差/几何量算，别"我看着像顺时针"。
- **没有对应工具时**：明确降级（起子 Agent 渲染 / 让用户截图录屏发来），**绝不退回"grep 到代码改了就算过"**（record-wrapup.md 红线）。

## 你自己截的图/录的屏落数据目录，别写工程目录
过程产物（截图/录屏/探针输出）一律进数据目录（内核 `EvidenceWriter` 默认落 evidence 目录），别图省事写 `./`（=用户工程根，会污染 git）。详见 record-wrapup.md 污染红线。
