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
| `responder/engine/` | 三层判断：要不要响应（开关/接管/等待时长）→ 三分类（确定性规则，`rules.py`）→ 可选 Claude 复核（`llm.py`） |
| `responder/compliance/` | 禁止事项清单（硬编码）、免责句式、出口闸门 |
| `responder/reply/` | 话术模板（按客户状态 × 问题类型）、Claude 生成一般性法律框架（可选） |
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
python -m pytest -q          # 单元测试（45 项）
python scripts/verify.py     # 验证协议：222 条脱敏测试集
```

验证协议断言：三分类准确率 ≥ 95%、禁止事项零命中、直接回答类免责句式覆盖率 100%。

## 分期状态

- [x] **Phase 0/1 代码基础**：判断引擎、合规护栏、影子模式管道、提醒队列、控制台 API、测试集与验证协议
- [ ] Phase 0 业务准备：企微群渠道确认、合伙人审定免责句式与禁止清单、真实历史群聊脱敏样本替换/扩充测试集
- [ ] Phase 1 影子模式试点（1–3 个群）：分类准确率 ≥95%、草稿采纳率 ≥70%、零合规违规
- [ ] Phase 2 自动回复上线：首周 100% 人工复核，周复盘误判
- [ ] Phase 3 扩展：全部客户群、案管系统状态查询打通、未成交群转化话术

## 开放问题（动工前需回答）

1. 现有客户群在个人微信还是企业微信？存量群如何迁移？（个人微信挂机器人为红线，不做）
2. 试点选哪 1–3 个群？（建议：已成交刑事 × 1、已成交民事 × 1、未成交咨询 × 1）
3. AI 在群内是否明示身份？账号昵称？（建议明示，如「智能助理」）
4. 提醒升级链路确认：当前默认第一责任人 10 分钟未处理升级第二责任人（`RESPONDER_ESCALATION_SECONDS`）
5. 律所是否有案件管理系统（决定二期「我的案子到哪一步了」能否直接回答）？
6. 群聊数据留存时长与脱敏要求（PIPL 最小化）？当前 SQLite 全量留存，上线前需定策略。
7. 客户群消息获取方式：会话存档拉取 or 群机器人回调？（`/ingest` 端点两者皆可对接）

## 技术栈

Python 3.10+ / FastAPI / SQLite / Claude API（`claude-opus-4-8`，可选）/ pycryptodome（企微 AES）。
控制台前端 [待定]（方案建议 React，Phase 1 用 API 直接复核足够）。
