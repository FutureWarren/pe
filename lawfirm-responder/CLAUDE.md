# CLAUDE.md — 律所微信群 AI 第一响应助手

本文件对在本目录（`lawfirm-responder/`）工作的 Claude Code 具有约束力。

## 项目定位

企业微信客户群里的 AI 第一响应屏障：客户消息在人工无法及时回复时，能答的直接答，
不能答的先安抚、承接、并通知人工。核心价值是**消灭冷场**，不是替代客服。
北极星指标：客户消息首次响应时长（中位数）。

## 合规护栏（不可违反）

以下规则硬编码于 `responder/compliance/`，**任何情况下不得绕过、弱化或有条件豁免**：

1. **渠道**：只对接企业微信官方 API。禁止引入任何个人微信协议桥接/逆向方案（封号 + PIPL 风险）。
2. **出口闸门**：所有 AI 生成文本（含影子模式草稿）必须经过 `compliance.guard.guard()`。
   禁止事项命中即丢弃原文、回退安全承接模板。
3. **禁止事项清单**（`compliance/forbidden.py`）：承诺结果、预测本案判决、提及任何费用金额、
   报价/改价、评价对方当事人或司法机关、催促客户做法律行为、冒充律师身份。
4. **免责句式**（`compliance/disclaimer.py`）：机制必须保持可用（verify 按开启态验证），
   但生产 `disclaimer_required` 默认关闭（业务决策 2026-07，暂不落地）。开启前句式须合伙人书面确认，
   AI 不得改写句式本身。
5. **人工优先**：律师近期发言过的群 AI 静默；紧急情形（拘留/传唤/开庭临近/情绪崩溃/投诉）
   只做一句安抚 + 强提醒承办律师，不展开回答。
6. **自指本案一律承接**：消息含「我的案子」等自指时，即使话题通用也不直接作答。

## 三分类判断规范

`responder/engine/rules.py`，分层命中即停，优先级：
非文本/空 → 沉默；@点名 → 沉默；紧急 → 承接(urgent)；费用 → 承接；案件特定 → 承接；
找人/催回复 → 承接；自指本案 → 承接；通用法律问题 → 直接回答；其余 → 沉默。
拿不准时倾向沉默——AI 是补位，不是抢答。沉默也必须写入判断日志。

## 命令

```bash
pip install -e ".[dev]"          # 安装
python -m pytest -q              # 单元测试（必须在本目录运行）
python scripts/verify.py         # 验证协议（见下）
responder-api                    # 启动服务（默认影子模式）
ruff check responder tests       # lint
```

## 验证协议

**功能「完成」的唯一标准是 `python scripts/verify.py` 对真实产物通过**：

- 测试集 ≥ 200 条脱敏消息（`tests/data/test_messages.jsonl`）
- 三分类准确率 ≥ 95%
- 生成回复对禁止事项清单零命中
- 直接回答类免责句式覆盖率 100%

**禁止无脚本输出的完成声明。** 修改判断规则或话术后必须重跑 verify + pytest。

## 自主循环边界

- 可无人值守：基础设施、测试、重构、日志/存储/控制台功能。
- **必须人工审核后合并**：话术模板（`reply/templates.py`）、prompt（`reply/prompts.py`）、
  语感规范（`docs/voice-guide.md`，模板与 prompt 的话术依据，三者须同步演进）、
  判断阈值（等待时长/接管时长/升级时长/复核置信度）、
  线索优先级权重与门槛（`engine/priority.py` 与 `docs/lead-routing.md` 须同步演进）、
  合规文本（`compliance/forbidden.py`、`compliance/disclaimer.py`）、测试集标注变更。

## LLM 层架构约定

- 双供应商：DeepSeek（默认，成本考虑）与 Anthropic 可切换（`engine/llm.py` 的 resolve()）；
  两条后端共用同一套 prompt 与净化/合规闸门，新增供应商必须走同一出口。

- system prompt 保持完全静态（不插时间/ID），一切易变上下文进 user 消息（`reply/prompts.py`）。
- 模型只能在「漏答方向」纠偏：仅复核规则判 default-silence 的边界样本；
  紧急/费用/案件特定/自指等高优先级规则命中不得交模型改判。
- 模型有示弱出口 `[[NEED_LAWYER]]`：答不稳一律转承接，禁止移除该机制。
- 所有模型输出必须过 `reply/sanitize.py`（形态）+ `compliance/guard.py`（语义）双重闸门。
- 任何 API 异常/拒答/解析失败必须静默降级到确定性路径，不允许让客户等待或看到报错。
- 配置好 API key 后跑 `python scripts/prompt_smoke.py` 人工审阅真实话术质量。

## 已定业务决策（2026-07，不要反向修改）

- 客户群已在企微，接入方案见 `docs/integration.md`（微信客服 / 机器人 / 会话存档 / 影子模式）。
  **已实测**：自建应用回调收不到群聊内容；微信客服是唯一免费且全自动的进线通道，为首选。
  客服会话复用群档案模型（`kf:{open_kfid}:{external_userid}`），首次进线自动建档。
- 试点：劳动仲裁/法律纠纷群；AI 处理追问时同话术不复读（`service.Pipeline._apply_followup_policy`，依据 `Store.count_recent_live` 的三级策略），转升级提醒。
- 分案体系（2026-07）：筛查后的线索按 `docs/lead-routing.md` 评分（P0/P1/P2）并自动派给
  具体律师（专长+负载）；律师个人令牌只看自己名下数据（`console/api.py` 服务端强制）。
  律师名册为空时整套派单回落旧链路——该回落行为是升级兼容承诺，不得移除。
- AI 身份：普通销售顾问，不明示 AI（全量留痕是该决策的合规兜底，留痕逻辑不得削弱）。
- 免责句式默认关闭（见上）。

## 待定决策（动工相关功能前先确认）

提醒升级链路参数、案管系统对接、数据留存时长/脱敏策略、智能机器人回调能力是否开通。
