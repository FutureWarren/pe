# 律所微信群 AI 第一响应助手

企业微信客户群里的 AI 助理：保证客户的每一条问题在几分钟内得到有质量的回应——
能答的直接答，不能答的先安抚、承接、并通知人工跟进。核心价值是**消灭冷场**，不是替代客服。

> 合规护栏、三分类规范与验证协议见 [CLAUDE.md](CLAUDE.md)（对 Claude Code 有约束力）。

## 架构

```
[企微客户群消息] → 网关(gateway) → 判断引擎(engine) → 三种动作
                                                      ├─ ① 直接回答（通用法律知识）
                                                      ├─ ② 安抚 + 承接 + 通知人工
                                                      └─ ③ 保持沉默（也记日志）
                                        ↓
                          合规出口闸门(compliance.guard) —— 所有出口文本必经
                                        ↓
                  人工提醒通道(notify) —— 企微单聊推送承办律师，紧急超时升级第二责任人
                                        ↓
                  律师控制台 API(console) —— 待办队列 / 回复与沉默日志 / 群开关 / 话术反馈 / 看板
```

模块一览：

| 模块 | 职责 |
|---|---|
| `responder/engine/` | 三层判断：要不要响应（开关/接管/等待时长）→ 三分类（确定性规则 `rules.py` + 边界样本 Claude 复核 `llm.py`） |
| `responder/compliance/` | 禁止事项清单（硬编码）、免责句式、出口闸门 |
| `responder/reply/` | prompt 集中管理（`prompts.py`）、输出净化（`sanitize.py`）、话术模板与变体（按客户状态 × 问题类型 × 情绪/时段）、Claude 生成一般性法律框架 |
| `responder/gateway/` | 企微回调加解密（WXBizMsgCrypt）、回调路由、`/ingest` JSON 摄入、发送通道 |
| `responder/notify/` | 提醒分级、紧急超时升级链路 |
| `responder/store/` | SQLite：群档案、消息、判断日志（含沉默）、回复记录、提醒队列 |
| `responder/console/` | 控制台 API（前端界面待定） |

## 安装与运行

```bash
cd lawfirm-responder
python3 -m pip install -e ".[dev]"
cp .env.example .env   # 填写企微配置；ANTHROPIC_API_KEY 可选
responder-api          # 默认影子模式（只判断不发言）
```

- **影子模式（Phase 1，默认）**：`RESPONDER_MODE=shadow`。AI 草稿只入库进控制台，由人工决定是否采用。
- **自动回复（Phase 2）**：`RESPONDER_MODE=live`。需配置企微 corp_id / secret / agent_id。
- 未配置 `ANTHROPIC_API_KEY` 时判断走纯规则、直接回答类降级为确定性承接式回复，整条链路可离线运行与验证。

试点群档案通过控制台 API 维护：

```bash
curl -X PUT localhost:8020/console/groups/GROUP_ID -H 'content-type: application/json' -d '{
  "group_id": "GROUP_ID", "name": "张某刑事案服务群", "client_status": "signed",
  "case_type": "刑事辩护", "case_stage": "审查起诉", "lawyer_name": "王",
  "lawyer_userid": "wang", "backup_userid": "li"}'
```

## 验证（功能完成的唯一标准）

```bash
python -m pytest -q                 # 单元 + E2E 测试（81 项，含企微加密回调全链路与 mock LLM）
python scripts/verify.py            # 验证协议：228 条脱敏测试集
python scripts/e2e_replay.py        # 对运行中服务回放脱敏群聊（需先起 responder-api）
python scripts/prompt_smoke.py      # 配置 ANTHROPIC_API_KEY 后：真实调用冒烟，人工审话术
```

### AI 层能力一览

- **判断复核**：规则判「沉默」的边界样本（如「人事部又找我谈话了」这类无问号陈述）交 Claude 二次分类，
  置信度 ≥0.7 才采信，且只允许「漏答→响应」方向纠偏，不允许放宽合规层级
- **回答生成**：注入群背景（案件类型/客户状态/阶段）+ 最近 10 条群聊上下文 + 时段感知；
  模型答不稳输出示弱标记自动转承接
- **输出净化**：去 markdown/表情/问候残留、句界截断限长、AI 自曝语判废——保证微信群聊形态
- **共情开场**：深夜/焦虑情绪自适应开场白（确定性，非模型）
- **追问三级策略**：同类问题第 1 次正常话术（多变体防机器人感）→ 第 2 次二次安抚不复读 → 第 3 次群内静默仅升级提醒
- **全链路降级**：无 API key / 超时 / 拒答 / 解析失败，全部静默回落确定性模板，客户永远能得到回应

验证协议断言：三分类准确率 ≥ 95%、禁止事项零命中、直接回答类免责句式覆盖率 100%。

## 分期状态

- [x] **Phase 0/1 代码基础**：判断引擎、合规护栏、影子模式管道、提醒队列、控制台 API、测试集与验证协议
- [ ] Phase 0 业务准备：企微群渠道确认、合伙人审定免责句式与禁止清单、真实历史群聊脱敏样本替换/扩充测试集
- [ ] Phase 1 影子模式试点（1–3 个群）：分类准确率 ≥95%、草稿采纳率 ≥70%、零合规违规
- [ ] Phase 2 自动回复上线：首周 100% 人工复核，周复盘误判
- [ ] Phase 3 扩展：全部客户群、案管系统状态查询打通、未成交群转化话术

## 已定业务决策（2026-07）

1. **渠道**：客户群已在企业微信上，无迁移问题；接入方案（读/写三条通道 + 上线清单）见
   [docs/integration.md](docs/integration.md)。
2. **试点**：劳动仲裁/法律纠纷群。AI 回答基础问题并处理群内追问（追问去重：同话术不复读，
   转为升级提醒）。
3. **身份**：AI 以普通销售顾问身份出现，不明示 AI；定位为 first screening。
   全量消息留痕可溯源（合规兜底，见 integration.md 备注）。
4. **免责句式**：暂不落地。`disclaimer_required` 默认关闭，机制保留（验证协议按开启态验证机制可用）。

## 仍待确认

1. 提醒升级链路：当前默认第一责任人 10 分钟未处理升级第二责任人（`RESPONDER_ESCALATION_SECONDS`）
2. 律所是否有案件管理系统（决定二期「我的案子到哪一步了」能否直接回答）？
3. 群聊数据留存时长与脱敏要求（PIPL 最小化）？当前 SQLite 全量留存，上线前需定策略。
4. 企业是否已开通智能机器人「接收消息回调」能力（决定读通道走机器人还是会话存档）

## 技术栈

Python 3.10+ / FastAPI / SQLite / Claude API（`claude-opus-4-8`，可选）/ pycryptodome（企微 AES）。
控制台前端 [待定]（方案建议 React，Phase 1 用 API 直接复核足够）。
