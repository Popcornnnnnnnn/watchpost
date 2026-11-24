// Watchpost Showcase Interactivity: 2-state theme toggle, accessible tabs,
// live simulator state machine, clipboard copy + live region, bilingual EN/zh-Hans,
// and progressive scroll-reveal fallback.

const I18N = {
  en: {
    'meta.title': 'Watchpost — Headless Local Bridge for Your Coding Agent',
    'nav.skip': 'Skip to main content',
    'nav.simulator': 'Simulator',
    'nav.guardrails': 'Guardrails',
    'nav.platforms': 'Platforms',
    'nav.quickstart': 'Quickstart',
    'nav.studio': 'Studio',
    'controls.langLabel': '中文',
    'controls.toLight': 'Switch to light theme',
    'controls.toDark': 'Switch to dark theme',
    'hero.eyebrow': 'HEADLESS LOCAL AGENT BRIDGE · APACHE-2.0',
    'hero.title': 'Stand watch over noisy chats. Never auto-send a word.',
    'hero.sub':
      'Watchpost passively watches selected team chats, runs your local coding agent with automatic backup failover, and pushes crisp diagnoses plus 1-tap copy draft replies straight to your phone—waiting for your "go" before touching code.',
    'hero.ctaPrimary': 'Try Live Simulator',
    'hero.ctaSecondary': 'Setup in 60s',
    'hero.ctaRepo': 'GitHub Repo',
    'trust.t1Title': 'Draft-First Safety',
    'trust.t1Sub': 'Zero auto-replies to outsiders',
    'trust.t2Title': '10 Native Platforms',
    'trust.t2Sub': 'Chats + phone push built in',
    'trust.t3Title': '100% Local State',
    'trust.t3Sub': 'SQLite WAL + Whisper ASR',
    'trust.t4Title': 'Primary → Backup Agent',
    'trust.t4Sub': 'Antigravity, Codex, Claude, Gemini',
    'flow.k1': '01 · Inbound Work Channels (Read-Only)',
    'flow.v1': 'Feishu · Slack · Discord · Telegram · DingTalk · WeCom · WhatsApp SDK — debounced bursts + Bucket 1/2 reply-chain context.',
    'flow.k2': '02 · Local Coding Agent + Guardrails',
    'flow.v2': 'Antigravity (agentapi + Stop Hook) with auto-failover to Codex / Claude Code / Gemini CLI, cross-channel memory & Quiet Hours queue.',
    'flow.k3': '03 · Phone Entrance + "go" Gate',
    'flow.v3': 'iMessage · Telegram · Slack/Discord DM · WeCom · ntfy · Bark · Pushover — concise bubbles + 1-tap copy draft. Reply "go" to execute fixes.',
    'sim.heading': 'Interactive Bridge Simulator',
    'sim.sub': 'Switch between real scenarios and test how Watchpost separates noisy group triage, overnight Quiet Hours, sibling-channel recall, and Personal Direct Chat.',
    'sim.tab1': 'Incident Triage',
    'sim.tab2': 'Quiet Hours',
    'sim.tab3': 'Cross-Channel Memory',
    'sim.tab4': 'Personal Direct Chat',
    'sim.p1LeftTitle': 'Slack · #ops-alerts (diagnose_only)',
    'sim.p1LeftBadge': '25s Burst · No Read Leak',
    'sim.p1Msg1': 'Staging checkout webhook just started timing out with 502s.',
    'sim.p1Msg2': '@owner can you check worker/db.py? Looks like pool exhaustion.',
    'sim.p1StateInit': 'Watchpost finished read-only diagnosis (Mode 4 Bug) and paused at the "go" approval gate. Outsiders see nothing until you copy or approve.',
    'sim.p1RightTitle': 'Owner Phone · iMessage / Slack DM',
    'sim.p1RightBadgeInit': 'Awaiting "go"',
    'sim.p1RightBadgeApproved': 'Approved → Executed',
    'sim.p1RightBadgeDismissed': 'Dismissed',
    'sim.p1Bubble1': '[Slack · #ops-alerts] worker/db.py opens an unclosed session when retries throw on HTTP 502 (verified locally, 3 unit checks pass). Say "go" to apply the patch or "skip" to dismiss.',
    'sim.draftLabel': '1-Tap Copy Draft (Bark / UI)',
    'sim.copyBtn': 'Copy Draft',
    'sim.copiedBtn': 'Copied!',
    'sim.p1Draft': 'Looking at worker/db.py now — found the session leak on 502 retries. Preparing a safe fix and will share diff results shortly.',
    'sim.btnGo': 'Reply "go"',
    'sim.btnSkip': 'Reply "skip"',
    'sim.btnReset': 'Reset',
    'sim.p1ReplyGo': 'Owner → go · Patch applied cleanly (pytest 12/12 passed). Ready for you to copy final confirmation when desired.',
    'sim.p1ReplySkip': 'Owner → skip · Pending "go" gate dismissed cleanly; workspace untouched.',
    'sim.p2LeftTitle': 'Feishu & Discord · 02:15 Local Time',
    'sim.p2LeftBadge': 'Quiet Hours · 23:00–08:00',
    'sim.p2LeftMutedBadge': 'Muted · 2h Override',
    'sim.p2Msg1': '[routine] Staging nightly benchmark took 4% longer after dependency bump.',
    'sim.p2Msg2': 'URGENT P0: Payment callback worker down in eu-west!',
    'sim.p2StateInit': 'Routine alerts are silently diagnosed locally and queued into kv_state for the 08:00 morning briefing—while urgent keywords break through immediately.',
    'sim.p2RightTitle': 'Owner Phone · Morning Briefing',
    'sim.p2RightBadge': 'auto_brief_on_end: true',
    'sim.p2Bubble1': '02:14 queued silently (diagnosed cleanly, zero wake-up ping). 04:02 matched urgent_keywords ["P0"] and punched through Quiet Hours immediately.',
    'sim.btnBrief': 'Send /brief',
    'sim.btnMute': 'Send /mute 2h',
    'sim.p2ReplyBrief': 'Morning Briefing (1 item flushed): Staging benchmark +4% traced to new JSON parser allocation; zero code changes made.',
    'sim.p2ReplyMute': 'Quiet Hours override enabled for 120 minutes. Routine alerts will queue silently until resumed or /brief.',
    'sim.p3LeftTitle': 'Two Sibling Channels · Shared Workspace',
    'sim.p3LeftBadge': 'cross_channel_window_s: 7200',
    'sim.p3Msg1': 'Enterprise tenant hits HTTP 429 on /v1/export since 15:00.',
    'sim.p3Msg2': '@owner Anyone seeing rate-limiter drops on export jobs?',
    'sim.p3StateInit': 'Watchpost automatically injects the sibling WeCom finding (Redis burst cap change in PR #48) into the Discord prompt—strictly excluding Personal Direct Chat history.',
    'sim.p3RightTitle': 'Owner Phone · Linked Recall',
    'sim.p3RightBadge': 'Zero Privacy Bleed',
    'sim.p3Bubble1': '[Discord · #backend-dev] Matches the 15:10 WeCom report in the same repo: export bursts hit the tightened Redis limit (120→40 req/min). Same root cause; draft ready.',
    'sim.p3Draft': 'Yes — support flagged the same 429s earlier. It comes from the Redis export cap change (120→40/min). Rolling back the limiter config or whitelisting batch workers fixes both.',
    'sim.p4LeftTitle': 'Owner → Phone Entrance (Plain Message)',
    'sim.p4LeftBadge': 'thread_originator_guid Guard',
    'sim.p4Msg1': 'Check why test_queue_retry is flaky in this repo today.',
    'sim.p4StateInit': 'Plain un-swiped iMessages always route to your Personal Direct Agent session—never hijacked by macOS automatic reply_to_guid chains, and never leaked to work channels.',
    'sim.p4RightTitle': 'Direct Coding Agent Reply',
    'sim.p4RightBadge': 'Natural Tone · /new /reset',
    'sim.p4Bubble1': 'Checked test_queue_retry: it uses wall-clock sleep(0.05) instead of a monotonic fake clock under CI load. Swapping to an event gate fixes it cleanly—reply "apply" if you want me to edit and re-run pytest.',
    'guards.heading': 'Four Core Engineering Guardrails',
    'guards.sub': 'Built so an autonomous coding agent stays genuinely helpful in noisy real-world work chats without ever embarrassing you or breaking production silently.',
    'guards.c1Tag': 'SAFE EXECUTION',
    'guards.c1Title': '4-Mode Triage & "go" Approval Gate',
    'guards.c1Desc': 'Classifies incoming messages into Q&A, Advice, FYI, or Bug under per-channel autonomy (assistant_only, diagnose_only, auto_fix). High-impact edits pause until you reply go, or skip to dismiss cleanly.',
    'guards.c2Tag': 'NOISE CONTROL',
    'guards.c2Title': 'Quiet Hours & Morning Briefing',
    'guards.c2Desc': 'Suppress pings overnight (such as 23:00–08:00) while diagnosing quietly into SQLite kv_state, then flush one morning digest at 08:00. Configurable urgent keywords still break through on real emergencies.',
    'guards.c3Tag': 'CONTEXT RECALL',
    'guards.c3Title': 'Cross-Channel Memory + Isolation',
    'guards.c3Desc': 'Feeds recent incident summaries from sibling channels in the same repository (for example Support in WeCom + Dev in Slack) while keeping owner_direct personal sessions strictly sealed off.',
    'guards.c4Tag': 'RESILIENCE',
    'guards.c4Title': 'Hot Reload & Backup Agent Failover',
    'guards.c4Desc': 'Reload channels, personas, and Quiet Hours in milliseconds with POST /api/reload or SIGHUP, recover active threads cleanly after crashes, and automatically fail over from Antigravity to Codex or Claude Code.',
    'plat.heading': 'Complete Platform Matrix',
    'plat.sub': 'Zero-dependency Python adapters over official APIs and OS protocols, paired with a stdio JSON SDK (watchpost.sdk) for custom or copyleft external bridges.',
    'plat.fAll': 'All Integrations',
    'plat.fIn': 'Work Chat Sources',
    'plat.fOut': 'Owner Entrances',
    'plat.fAgent': 'Agent Backends',
    'plat.caption': 'Supported inbound chat adapters, owner push/steering entrances, and local coding agent backends in Watchpost v0.4',
    'plat.colName': 'Integration',
    'plat.colRole': 'Direction',
    'plat.colProto': 'Transport & Highlights',
    'plat.colStatus': 'Status',
    'plat.rIn': 'Work Chat',
    'plat.rOut': 'Owner Phone',
    'plat.rBoth': 'Work Chat + Owner DM',
    'plat.rAgent': 'Coding Agents',
    'plat.sBuilt': 'Built-in',
    'plat.sSdk': 'Plugin SDK',
    'plat.dFeishu': 'Read-only REST (tenant, user, lark-cli), <at> mentions, image/audio/file download, zero read receipts',
    'plat.dSlack': 'Web API history + thread replies, <@user> & <!channel>, private file download, DM conversations.open + thread go',
    'plat.dDiscord': 'REST v10 guild channels & attachments, message_reference reply chains, DM steering',
    'plat.dTelegram': 'Shared single-stream TelegramUpdateHub (zero stolen updates), @username mentions, photo/voice download, /brief /mute',
    'plat.dDing': 'Signed HMAC-SHA256 webhook (POST /api/webhooks/dingtalk), atUsers trigger, OAuth2 robot file download',
    'plat.dWecom': 'Signed SHA1 GET/POST /api/webhooks/wecom (plaintext mode or upstream AES proxy), XXE-safe XML/JSON, media/get, message/send',
    'plat.dWa': 'Executable stdio SDK template (examples/whatsapp_plugin_template.py) keeping copyleft/desktop bridges outside Apache-2.0 core',
    'plat.dImsg': 'chat.db swipe-reply thread_originator_guid, plain message → Personal Direct Chat, photos & Whisper voice',
    'plat.dNtfy': 'UTF-8 JSON publish + optional reply_topic NDJSON polling for #N / go / skip / /brief',
    'plat.dBark': 'Clean APNs alert with copy / autoCopy populated strictly from the standalone draft reply bubble',
    'plat.dPushover': 'Multi-device delivery via Pushover REST API with automatic 1024-char chunking',
    'plat.dAgent': 'AntigravityBackend (agentapi + Stop Hook) and CLI runners with FallbackBackend automatic failover',
    'qs.heading': 'Quickstart, SDK & Local Sandbox',
    'qs.sub': 'Start with one guided wizard command, test your plugin with automated conformance checks, or simulate colleagues and phone replies offline in your browser.',
    'qs.tab1': 'Interactive Setup',
    'qs.tab2': 'Plugin SDK',
    'qs.tab3': 'Local Sandbox UI',
    'qs.tab4': 'Webhook & Control API',
    'qs.copy': 'Copy',
    'footer.brand': 'Watchpost v0.4 · Apache-2.0',
    'footer.note': ' — Runs locally on your machine. Copyleft/desktop bridges stay separate out-of-process plugins via watchpost.sdk.',
    'footer.home': 'popcornnn.xyz',
    'footer.repo': 'GitHub',
    'footer.sim': 'Simulator',
    'footer.docs': 'Quickstart',
  },
  'zh-Hans': {
    'meta.title': 'Watchpost — 本地无头 Coding Agent 聊天桥接器',
    'nav.skip': '跳到正文',
    'nav.simulator': '交互模拟器',
    'nav.guardrails': '核心护栏',
    'nav.platforms': '平台矩阵',
    'nav.quickstart': '快速开始',
    'nav.studio': '主页',
    'controls.langLabel': 'EN',
    'controls.toLight': '切换到浅色模式',
    'controls.toDark': '切换到深色模式',
    'hero.eyebrow': '本地无头 AGENT 桥接器 · APACHE-2.0',
    'hero.title': '替你盯住嘈杂群聊，绝不替你乱发一句话。',
    'hero.sub':
      'Watchpost 在本机静默监听指定工作群聊，调起你的 Coding Agent（支持主备自动切换）完成排查，把精简总结和“点一下即可复制”的回复草稿推到手机上——未获你的 "go" 批准绝不擅自改代码或对外回复。',
    'hero.ctaPrimary': '体验交互模拟器',
    'hero.ctaSecondary': '60 秒快速上手',
    'hero.ctaRepo': 'GitHub 仓库',
    'trust.t1Title': '草稿先行安全闸门',
    'trust.t1Sub': '绝不自动向外部群发消息',
    'trust.t2Title': '10 个原生平台',
    'trust.t2Sub': '工作群源 + 手机推送全内置',
    'trust.t3Title': '100% 本地状态存储',
    'trust.t3Sub': 'SQLite WAL + 本地 Whisper 语音转写',
    'trust.t4Title': '主备 Agent 自动容灾',
    'trust.t4Sub': 'Antigravity / Codex / Claude / Gemini',
    'flow.k1': '01 · 只读工作消息源',
    'flow.v1': '飞书 · Slack · Discord · Telegram · 钉钉 · 企业微信 · WhatsApp SDK —— 防抖合批 + Bucket 1/2 引用链上下文。',
    'flow.k2': '02 · 本机 Agent 排查 + 护栏',
    'flow.v2': 'Antigravity（agentapi + Stop Hook）主后端失效自动切换 Codex / Claude / Gemini；跨群事件记忆与夜间免打扰队列。',
    'flow.k3': '03 · 手机入口 + "go" 二次确认',
    'flow.v3': 'iMessage · Telegram · Slack/Discord 私聊 · 企微 · ntfy · Bark · Pushover —— 短气泡推送 + 一键复制草稿，回复 "go" 执行修复。',
    'sim.heading': '交互式桥接模拟器',
    'sim.sub': '点击切换真实场景，现场体验 Watchpost 如何处理四态分诊、夜间免打扰早报、同库跨群记忆以及个人直聊隔离。',
    'sim.tab1': '故障分诊与 "go" 闸门',
    'sim.tab2': '夜间免打扰 + 早报',
    'sim.tab3': '跨群关联记忆',
    'sim.tab4': '个人直聊隔离',
    'sim.p1LeftTitle': 'Slack · #ops-alerts（diagnose_only 只读排查）',
    'sim.p1LeftBadge': '25s 连发合批 · 零已读泄露',
    'sim.p1Msg1': '预发结算回调刚才突然大量 502 超时。',
    'sim.p1Msg2': '@owner 能帮忙看下 worker/db.py 吗？像是连接池打满了。',
    'sim.p1StateInit': 'Watchpost 已在本地完成只读排查（Mode 4 故障），并停在 "go" 审批闸门前。你复制发送或批准前，群里没有任何回复。',
    'sim.p1RightTitle': '主人手机 · iMessage / Slack 私聊',
    'sim.p1RightBadgeInit': '等待回复 "go"',
    'sim.p1RightBadgeApproved': '已批准 · 已执行',
    'sim.p1RightBadgeDismissed': '已忽略关闭',
    'sim.p1Bubble1': '【Slack · #ops-alerts】定位到 worker/db.py 在 HTTP 502 重试抛错时未关闭 session（本地 3 条单测复现通过）。回复 "go" 应用补丁，或回复 "skip" 忽略。',
    'sim.draftLabel': '一键复制对外草稿（支持 Bark / UI）',
    'sim.copyBtn': '复制草稿',
    'sim.copiedBtn': '已复制',
    'sim.p1Draft': '正在看 worker/db.py，定位到 502 重试时有 session 未释放，已准备好修复方案，验证后发大家同步。',
    'sim.btnGo': '回复 "go"',
    'sim.btnSkip': '回复 "skip"',
    'sim.btnReset': '重置演示',
    'sim.p1ReplyGo': '主人 → go · 补丁已应用并通过 pytest（12/12），最终确认草稿已准备好。',
    'sim.p1ReplySkip': '主人 → skip · 已取消本次 "go" 等待，未修改工作区代码。',
    'sim.p2LeftTitle': '飞书 & Discord · 凌晨 02:15',
    'sim.p2LeftBadge': '免打扰 · 23:00–08:00',
    'sim.p2LeftMutedBadge': '临时静音 2 小时',
    'sim.p2Msg1': '[常规] 升级依赖后预发夜间压测耗时增加约 4%。',
    'sim.p2Msg2': '紧急 P0：eu-west 支付回调 worker 挂了！',
    'sim.p2StateInit': '常规告警在夜间静默完成本地诊断并写入 SQLite 早报队列，08:00 统一推送；命中紧急关键词时立刻穿透直达手机。',
    'sim.p2RightTitle': '主人手机 · 晨间简报',
    'sim.p2RightBadge': 'auto_brief_on_end: true',
    'sim.p2Bubble1': '02:14 常规事件已静默排查并入队（零夜里弹窗打扰）。04:02 命中紧急关键词 ["P0"]，已破例实时推送。',
    'sim.btnBrief': '发送 /brief',
    'sim.btnMute': '发送 /mute 2h',
    'sim.p2ReplyBrief': '晨间简报（共汇总 1 条）：夜间压测 +4% 来自 JSON 解析内存分配变化，无需回滚代码。',
    'sim.p2ReplyMute': '已开启 120 分钟临时静音，常规消息将先静默诊断并暂存简报队列。',
    'sim.p3LeftTitle': '同仓库双群组 · 共用同一工作区',
    'sim.p3LeftBadge': 'cross_channel_window_s: 7200',
    'sim.p3Msg1': '企业微信客户支持群 · 15:10：大客户调用 /v1/export 自 15:00 起频繁 429。',
    'sim.p3Msg2': '@owner 有人看到导出接口最近限流报错吗？',
    'sim.p3StateInit': 'Watchpost 自动将 2 小时内同仓库（企微支持群 PR #48 限流变更）结论注入当前 Discord 排查上下文，且严格排除个人直聊隐私。',
    'sim.p3RightTitle': '主人手机 · 同源故障召回',
    'sim.p3RightBadge': '个人直聊零泄露',
    'sim.p3Bubble1': '【Discord · #backend-dev】与 15:10 企微支持群反馈一致：导出并发触发收紧后的 Redis 限流（120→40 次/分），已生成统一答复草稿。',
    'sim.p3Draft': '是同个原因，刚才支持群也反馈了 429。主要是 Redis 导出限流刚从 120 降到了 40/分，调回阈值或加批处理白名单就行。',
    'sim.p4LeftTitle': '主人 → 手机入口（直接发普通消息）',
    'sim.p4LeftBadge': 'thread_originator_guid 精准区分',
    'sim.p4Msg1': '帮我查下今天这个仓库里 test_queue_retry 为什么偶发失败。',
    'sim.p4StateInit': '未经右滑引用的普通消息一律进入你的「个人直聊」独立会话——不会被 macOS 自动生成的 reply_to_guid 链误绑进工作群。',
    'sim.p4RightTitle': '私人 Agent 直聊回复',
    'sim.p4RightBadge': '自然语气 · /new /reset',
    'sim.p4Bubble1': '看了 test_queue_retry：它依赖了真实 sleep(0.05)，CI 负载高时容易超时。改成事件同步门控就能稳定通过；如果要我现在改并重跑测试，回复“执行”即可。',
    'guards.heading': '四大工程安全护栏',
    'guards.sub': '确保后台 Coding Agent 在复杂工作群里既能帮你提前干活，又绝不会擅自乱发消息或悄悄改坏线上配置。',
    'guards.c1Tag': '安全执行',
    'guards.c1Title': '4 态智能分诊与 "go" 审批闸门',
    'guards.c1Desc': '将群消息自动归类为问答、建议、通知或故障，按频道粒度配置只答、只查或可改权限。高风险变更一律等待你回复 go 才动手，回 skip 直接忽略。',
    'guards.c2Tag': '静音降噪',
    'guards.c2Title': '夜间免打扰 + 08:00 晨间早报',
    'guards.c2Desc': '在设定时段（如 23:00–08:00）静默运行后台诊断并存入 SQLite kv_state，早上结束时段统一发送一份简洁汇总；命中 P0/停机等紧急关键词照常瞬间穿刺告警。',
    'guards.c3Tag': '上下文记忆',
    'guards.c3Title': '同库跨群关联记忆 + 隐私隔离',
    'guards.c3Desc': '自动为同工作区兄弟群（如企微客服群与 Slack 开发群）共享近期故障摘要避免重复排查，同时严格禁止个人直聊记录流入任何团队群Prompt。',
    'guards.c4Tag': '高可用架构',
    'guards.c4Title': '零停机热重载与主备 Agent 容灾',
    'guards.c4Desc': '通过 POST /api/reload 或 SIGHUP 毫秒级热更新频道、人设和免打扰配置；崩溃重启自动恢复中断会话；Antigravity 配额耗尽或超时无缝切往 Codex / Claude Code。',
    'plat.heading': '全平台适配矩阵',
    'plat.sub': '零新增重依赖，基于各平台官方 API 与操作系统协议原生实现，同时提供标准化 watchpost.sdk 让自定义或 Copyleft 桥接运行在独立进程之外。',
    'plat.fAll': '全部集成',
    'plat.fIn': '工作群源（入站）',
    'plat.fOut': '主人手机入口',
    'plat.fAgent': 'Agent 后端',
    'plat.caption': 'Watchpost v0.4 支持的工作消息源、主人触达/控制入口与本地 Coding Agent 后端',
    'plat.colName': '平台 / 组件',
    'plat.colRole': '方向',
    'plat.colProto': '传输协议与关键能力',
    'plat.colStatus': '支持状态',
    'plat.rIn': '工作消息源',
    'plat.rOut': '主人推送/控台',
    'plat.rBoth': '工作源 + 主人私聊',
    'plat.rAgent': 'Coding Agent',
    'plat.sBuilt': '原生内置',
    'plat.sSdk': 'SDK 模板',
    'plat.dFeishu': '只读 REST API（支持 tenant/user/lark-cli），精准 <at> 识别与音视频/图片下载，零已读泄露',
    'plat.dSlack': '官方 Web API 历史与 Thread 回复同步、<@user>/<!channel>、私有文件下载、DM conversations.open + Thread 回复控台',
    'plat.dDiscord': 'REST v10 频道发现与 Snowflake 排序、附件下载、message_reference 引用链、DM 私聊指令',
    'plat.dTelegram': '单一 TelegramUpdateHub 共享拉流（杜绝双端抢消息）、@username 提醒、语音/图片下载、/brief /mute',
    'plat.dDing': 'HMAC-SHA256 验签 Webhook（POST /api/webhooks/dingtalk）、atUsers 触发、OAuth2 机器人媒体下载',
    'plat.dWecom': 'SHA1 验签 GET/POST /api/webhooks/wecom（明文回调或前置 AES 解密代理）、防 XXE XML/JSON 解析、media/get 与应用推送',
    'plat.dWa': '可执行 examples/whatsapp_plugin_template.py，通过 stdio JSON 保持与 Apache-2.0 核心干净隔离',
    'plat.dImsg': '读取 chat.db 并按 thread_originator_guid 严格区分右滑回帖与个人直聊，支持图片与 Whisper 语音',
    'plat.dNtfy': 'JSON UTF-8 推送 + 可选 reply_topic NDJSON 双向订阅（支持 #N / go / skip / /brief）',
    'plat.dBark': '轻量 iOS APNs 弹窗，将纯回复草稿自动注入 Bark copy / autoCopy 实现锁屏一键复制',
    'plat.dPushover': 'Pushover REST 跨端推送，自动按 1024 字符安全分段送达',
    'plat.dAgent': 'AntigravityBackend（agentapi + Stop Hook）及主流 CLI 后端，支持 FallbackBackend 自动兜底',
    'qs.heading': '快速配置、插件 SDK 与离线沙盒',
    'qs.sub': '一行向导命令生成安全占位符配置，用自动化一致性检查验证外部插件，或直接在浏览器里离线模拟整个团队对话流程。',
    'qs.tab1': '交互式安装向导',
    'qs.tab2': 'Plugin SDK 与检查器',
    'qs.tab3': '本地浏览器沙盒',
    'qs.tab4': 'Webhook 与热重载 API',
    'qs.copy': '复制',
    'footer.brand': 'Watchpost v0.4 · Apache-2.0',
    'footer.note': ' — 100% 本地守护进程。第三方桌面或 Copyleft 桥接通过 watchpost.sdk 运行在独立外置进程中。',
    'footer.home': 'popcornnn.xyz',
    'footer.repo': 'GitHub',
    'footer.sim': '模拟器',
    'footer.docs': '快速开始',
  },
};

function announce(msg) {
  const region = document.getElementById('live-status');
  if (!region) return;
  region.textContent = '';
  window.setTimeout(() => {
    region.textContent = msg;
  }, 20);
}

// 1. 2-state Dark/Light Mode Toggle following modern-web-guidance `dark-mode-toggle`
function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (!btn || !meta) return;

  const systemDarkMQ = window.matchMedia('(prefers-color-scheme: dark)');

  function currentRenderedTheme() {
    if (meta.content === 'dark' || meta.content === 'light') {
      return meta.content;
    }
    return systemDarkMQ.matches ? 'dark' : 'light';
  }

  function syncButtonState() {
    btn.setAttribute('aria-pressed', currentRenderedTheme() === 'dark' ? 'true' : 'false');
  }

  btn.addEventListener('click', () => {
    const sys = systemDarkMQ.matches ? 'dark' : 'light';
    const next = currentRenderedTheme() === 'dark' ? 'light' : 'dark';
    try {
      if (next !== sys) {
        localStorage.setItem('watchpost.theme', next);
        meta.content = next;
      } else {
        localStorage.removeItem('watchpost.theme');
        meta.content = 'light dark';
      }
    } catch {
      meta.content = next;
    }
    syncButtonState();
    announce(next === 'dark' ? 'Dark theme enabled' : 'Light theme enabled');
  });

  systemDarkMQ.addEventListener('change', syncButtonState);

  window.addEventListener('storage', (ev) => {
    if (ev.key !== 'watchpost.theme') return;
    meta.content = ev.newValue === 'light' || ev.newValue === 'dark' ? ev.newValue : 'light dark';
    syncButtonState();
  });

  syncButtonState();
}

// 2. Bilingual EN / zh-Hans Toggle
let activeLang = 'en';

function applyLanguage(lang) {
  const next = lang === 'zh-Hans' ? 'zh-Hans' : 'en';
  activeLang = next;
  document.documentElement.lang = next;
  const dict = I18N[next];
  if (dict['meta.title']) {
    document.title = dict['meta.title'];
  }
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (key && Object.hasOwn(dict, key)) {
      el.textContent = dict[key];
    }
  });
  const langBtn = document.getElementById('lang-toggle');
  if (langBtn) {
    langBtn.setAttribute('aria-pressed', next === 'zh-Hans' ? 'true' : 'false');
  }
}

function initLanguageToggle() {
  const btn = document.getElementById('lang-toggle');
  applyLanguage(document.documentElement.lang);
  if (!btn) return;
  btn.addEventListener('click', () => {
    const target = activeLang === 'zh-Hans' ? 'en' : 'zh-Hans';
    try {
      localStorage.setItem('watchpost.lang', target);
    } catch {}
    applyLanguage(target);
    announce(target === 'zh-Hans' ? '已切换为中文' : 'Switched to English');
  });
}

// 3. Accessible Tabs (`focusgroup` + roving tabindex + hidden="until-found" + beforematch)
function initTabs() {
  document.querySelectorAll('[data-tabs-widget]').forEach((widget) => {
    const tabs = Array.from(widget.querySelectorAll('[role="tab"]'));
    const panels = Array.from(widget.querySelectorAll(':scope > [role="tabpanel"]'));

    function selectTab(targetTab, { focusTab = false } = {}) {
      if (!targetTab) return;
      tabs.forEach((tab) => {
        const isSel = tab === targetTab;
        tab.setAttribute('aria-selected', isSel ? 'true' : 'false');
        tab.tabIndex = isSel ? 0 : -1;
        tab.toggleAttribute('focusgroupstart', isSel);
      });
      panels.forEach((panel) => {
        const match = panel.getAttribute('aria-labelledby') === targetTab.id;
        if (match) {
          panel.removeAttribute('hidden');
          panel.tabIndex = 0;
        } else {
          panel.setAttribute('hidden', 'until-found');
          panel.removeAttribute('tabindex');
        }
      });
      if (typeof targetTab.scrollIntoView === 'function') {
        try {
          targetTab.scrollIntoView({ block: 'nearest', inline: 'nearest', container: 'nearest' });
        } catch {
          targetTab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        }
      }
      if (focusTab) {
        targetTab.focus();
      }
    }

    tabs.forEach((tab, idx) => {
      tab.addEventListener('click', () => selectTab(tab));
      tab.addEventListener('keydown', (ev) => {
        let nextIdx = -1;
        if (ev.key === 'ArrowRight') nextIdx = (idx + 1) % tabs.length;
        else if (ev.key === 'ArrowLeft') nextIdx = (idx - 1 + tabs.length) % tabs.length;
        else if (ev.key === 'Home') nextIdx = 0;
        else if (ev.key === 'End') nextIdx = tabs.length - 1;
        if (nextIdx >= 0) {
          ev.preventDefault();
          selectTab(tabs[nextIdx], { focusTab: true });
        }
      });
    });

    panels.forEach((panel) => {
      panel.addEventListener('beforematch', () => {
        const ownerTab = tabs.find((t) => t.id === panel.getAttribute('aria-labelledby'));
        if (ownerTab) selectTab(ownerTab);
      });
    });
  });
}

// 4. Interactive Bridge Simulator Actions
function initSimulator() {
  document.querySelectorAll('[data-sim-act]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const act = btn.getAttribute('data-sim-act');
      const panel = btn.getAttribute('data-panel');
      const dict = I18N[activeLang] || I18N.en;
      const replyEl = document.querySelector(`[data-sim-reply="${panel}"]`);
      const badgeEl = document.querySelector(`[data-sim-badge="${panel}"]`);
      const stateEl = document.querySelector(`[data-sim-state="${panel}"]`);

      if (panel === 'triage' && replyEl && badgeEl && stateEl) {
        if (act === 'approve') {
          replyEl.setAttribute('data-i18n', 'sim.p1ReplyGo');
          replyEl.textContent = dict['sim.p1ReplyGo'];
          replyEl.hidden = false;
          badgeEl.setAttribute('data-i18n', 'sim.p1RightBadgeApproved');
          badgeEl.textContent = dict['sim.p1RightBadgeApproved'];
          badgeEl.className = 'pane-meta is-ok';
          stateEl.setAttribute('data-i18n', 'sim.p1ReplyGo');
          stateEl.textContent = dict['sim.p1ReplyGo'];
          announce(dict['sim.p1RightBadgeApproved']);
        } else if (act === 'dismiss') {
          replyEl.setAttribute('data-i18n', 'sim.p1ReplySkip');
          replyEl.textContent = dict['sim.p1ReplySkip'];
          replyEl.hidden = false;
          badgeEl.setAttribute('data-i18n', 'sim.p1RightBadgeDismissed');
          badgeEl.textContent = dict['sim.p1RightBadgeDismissed'];
          badgeEl.className = 'pane-meta';
          stateEl.setAttribute('data-i18n', 'sim.p1ReplySkip');
          stateEl.textContent = dict['sim.p1ReplySkip'];
          announce(dict['sim.p1RightBadgeDismissed']);
        } else {
          replyEl.removeAttribute('data-i18n');
          replyEl.hidden = true;
          badgeEl.setAttribute('data-i18n', 'sim.p1RightBadgeInit');
          badgeEl.textContent = dict['sim.p1RightBadgeInit'];
          badgeEl.className = 'pane-meta is-warn';
          stateEl.setAttribute('data-i18n', 'sim.p1StateInit');
          stateEl.textContent = dict['sim.p1StateInit'];
        }
      }

      if (panel === 'quiet' && replyEl && badgeEl) {
        if (act === 'brief') {
          replyEl.setAttribute('data-i18n', 'sim.p2ReplyBrief');
          replyEl.textContent = dict['sim.p2ReplyBrief'];
          replyEl.hidden = false;
          announce(dict['sim.p2ReplyBrief']);
        } else if (act === 'mute') {
          replyEl.setAttribute('data-i18n', 'sim.p2ReplyMute');
          replyEl.textContent = dict['sim.p2ReplyMute'];
          replyEl.hidden = false;
          badgeEl.setAttribute('data-i18n', 'sim.p2LeftMutedBadge');
          badgeEl.textContent = dict['sim.p2LeftMutedBadge'];
          announce(dict['sim.p2ReplyMute']);
        } else {
          replyEl.removeAttribute('data-i18n');
          replyEl.hidden = true;
          badgeEl.setAttribute('data-i18n', 'sim.p2LeftBadge');
          badgeEl.textContent = dict['sim.p2LeftBadge'];
        }
      }
    });
  });
}

// 5. Matrix Filter Toolbar
function initMatrixFilter() {
  const bar = document.getElementById('matrix-filters');
  const rows = Array.from(document.querySelectorAll('#matrix-body tr'));
  if (!bar || !rows.length) return;
  const buttons = Array.from(bar.querySelectorAll('button[data-filter]'));

  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const filter = btn.getAttribute('data-filter') || 'all';
      buttons.forEach((b) => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
      rows.forEach((row) => {
        const cats = (row.getAttribute('data-cat') || '').split(/\s+/);
        row.hidden = filter !== 'all' && !cats.includes(filter);
      });
    });
  });
}

// 6. Disambiguated Clipboard Copy Buttons
function initCopyButtons() {
  document.querySelectorAll('[data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const targetId = btn.getAttribute('data-copy-target');
      const target = targetId ? document.getElementById(targetId) : null;
      if (!target) return;
      const text = target.textContent || '';
      const dict = I18N[activeLang] || I18N.en;
      const origKey = btn.getAttribute('data-i18n') || 'qs.copy';
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = dict['sim.copiedBtn'];
        announce(dict['sim.copiedBtn']);
        window.setTimeout(() => {
          btn.textContent = (I18N[activeLang] || I18N.en)[origKey];
        }, 1600);
      } catch {
        announce('Clipboard permission unavailable');
      }
    });
  });
}

// 7. Scroll-driven reveal fallback only when view() timeline unsupported
function initScrollRevealFallback() {
  const nativeSupported =
    typeof CSS !== 'undefined' &&
    typeof CSS.supports === 'function' &&
    CSS.supports('(animation-timeline: view()) and (animation-range: entry)');
  if (nativeSupported) return;
  if (!window.matchMedia('(prefers-reduced-motion: no-preference)').matches) return;

  const blocks = document.querySelectorAll('.reveal-block');
  if (!blocks.length || typeof IntersectionObserver === 'undefined') return;

  blocks.forEach((el) => el.classList.add('reveal-fallback'));
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      }
    },
    { threshold: 0.12 }
  );
  blocks.forEach((el) => observer.observe(el));
}

document.addEventListener('DOMContentLoaded', () => {
  initThemeToggle();
  initLanguageToggle();
  initTabs();
  initSimulator();
  initMatrixFilter();
  initCopyButtons();
  initScrollRevealFallback();
});
