"""运行配置。所有阈值可通过环境变量按部署调整，敏感信息不入库。"""

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 把 .env 中的非 RESPONDER_ 前缀变量（DEEPSEEK_API_KEY / ANTHROPIC_API_KEY）载入环境
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESPONDER_", env_file=".env", extra="ignore"
    )

    # shadow: 只判断不发言，AI 草稿仅进入控制台; live: 自动发言
    mode: str = "shadow"

    wecom_corp_id: str = ""
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_corp_secret: str = ""
    wecom_agent_id: str = ""

    # 微信客服：独立 Secret（后台 → 客户与上下游 → 微信客服 → API）。
    # 该通道客户消息全量推送、无需 @ 触发，是新咨询首响的主通道。
    wecom_kf_secret: str = ""
    kf_enabled: bool = True

    # 智能机器人（群聊 @ 触发）：独立的 Token / EncodingAESKey，在后台创建机器人时获得。
    # 收：机器人回调；发：优先用回调随消息下发的会话 webhook（员工零配置），
    # 过期则回落该群人工配置的群机器人 webhook。
    wecom_bot_token: str = ""
    wecom_bot_aes_key: str = ""
    bot_enabled: bool = True
    # 会话 webhook 的可用窗口。企微侧为分钟级，这里取保守值：宁可回落到人工通道，
    # 也不要拿一个已过期的地址去发（发失败客户就等于没收到回复）。
    bot_webhook_ttl_seconds: int = 240
    # 群聊会话首次出现时自动建档，接收线索简报的默认对象（群里查不到「接待人」）
    bot_default_notify_userid: str = ""
    # 客服会话首次出现时自动建档，用于话术点名；留空则话术说「承办律师」
    kf_default_lawyer_name: str = ""
    kf_default_case_type: str = ""
    # 客户扫码进入客服会话即主动打招呼（不等他先开口）。
    # 空窗口是最大的流失点：客户点进来看到一片空白，很多人直接就退了。
    kf_welcome_on_enter: bool = True

    # ---- 抖音企业号私信（open.douyin.com 开发者后台申请，需蓝V认证企业号）
    # 业务依据：用户 first pass 多在抖音而非微信（微信要先扫码，多一个动作）。
    douyin_enabled: bool = True
    douyin_client_key: str = ""
    douyin_client_secret: str = ""
    # 回调签名校验的 Token 与消息加密 Key（在开发者后台配置回调地址时设定）。
    # aes_key 留空 = 平台推明文 JSON，不做解密。
    douyin_callback_token: str = ""
    douyin_encoding_aes_key: str = ""
    # 发送私信的接口地址。抖音文档站在部署环境不可达，故做成配置项：
    # 凭据到手后跑 scripts/douyin_smoke.py 校正，不必改代码重新部署。
    douyin_send_url: str = "https://open.douyin.com/im/send/msg/"
    # 平台硬限制，不是我们的策略，**不要往大了调**：
    # 用户发言后 24 小时内才允许回复；同一窗口内用户下次开口前最多 6 条。
    douyin_reply_window_seconds: int = 86400
    douyin_max_parts_per_window: int = 6
    # 分条发送在抖音要收敛：一条回复拆 3 条，两轮就打满配额，
    # 真正要紧的话（要电话、邀约到所）反而发不出去。
    douyin_split_max_parts: int = 2
    # 客户进入私信会话页即打招呼（平台要求 30 秒内响应，故走确定性模板不进模型）
    douyin_welcome_on_enter: bool = True
    # 抖音会话建档后线索简报的默认接收人（抖音侧没有「接待人」可查）
    douyin_default_notify_userid: str = ""

    # ---- 会话转接（见 docs/kf-handoff.md）
    # 强意愿线索直接把会话转给分到的律师，他在企微客服工作台接着聊，
    # 省掉「打电话」这个最容易断的环节。
    handoff_enabled: bool = True
    # 哪些优先级触发转接。紧急线索无论优先级一律转。
    # 别放宽到 P1/P2——一周 416 人进私信，全转过去律师什么也别干了。
    handoff_priorities: str = "P0"
    # 转接接口路径。企微文档站在部署环境不可达，故做成配置项：
    # 控制台自检探到正确路径后改这里，不必改代码重新部署。
    kf_trans_path: str = "kf/service_state/trans"
    kf_state_path: str = "kf/service_state/get"
    # 转接后律师迟迟不接手 → 把客户收回给 AI。转接引入的最坏情况是
    # 「客户被交给一个不看企微的律师」，那比 AI 一直陪着更糟，必须有这个兜底。
    handoff_reclaim_seconds: int = 1800

    # ---- 每日战报：管理员不必打开任何页面就知道昨天怎么样。
    # 控制台是给「在系统里干活的人」用的；所主任要的是一份推到眼前的摘要。
    # 让他每天主动去点开一个网页看数字，这件事不会持续超过一周。
    daily_digest_enabled: bool = True
    daily_digest_hour: int = 9  # 本地时间几点推（0-23）
    # 收件人留空则用 default_notify_userid
    daily_digest_userid: str = ""

    # 律所线下地址：邀约到所面谈的话术里用。留空则只约时间不报地址。
    office_address: str = "上海市松江区九峰路88号平高广场11楼"
    office_name: str = "上海松沪律师事务所"
    # 客户聊到第几条还没留联系方式时，主动开口要电话 + 邀约到所面谈。
    # 业务决策 2026-08：主动要电话是提高变现率的正当动作，阈值从 3 下调到 2 ——
    # 抖音漏斗显示开口的人里四成聊完就走，等到第 3 条往往已经错过。
    ask_contact_after_messages: int = 2
    # 承接类回复（「我帮您问下律师」）默认没有下一步，客户看完就没事干了。
    # 业务决策 2026-08：每条回复都要留一个下一步，哪怕只是轻轻推一句。
    handoff_next_step: bool = True
    # 挽留：会话静默这么久且仍未留联系方式 → 补发一条（一通对话只发一次）。
    # 对标抖音「自动挽留」（其官方数据：留资率 +7.4%）。0 或关闭则不发。
    winback_enabled: bool = True
    winback_idle_seconds: int = 1800

    # LLM 供应商：deepseek | anthropic | auto（auto = 谁的 key 在就用谁，deepseek 优先）
    # 业务决策 2026-07：默认 DeepSeek（成本考虑）；Anthropic 路径保留可随时切回
    llm_provider: str = "auto"
    deepseek_model: str = "deepseek-chat"
    claude_model: str = "claude-opus-4-8"

    # AI 在群内的身份定位：普通销售顾问角色，不明示 AI 身份（业务决策 2026-07）。
    # 全量消息留痕入库，可溯源。
    ai_persona_name: str = "顾问助理"

    # 免责句式开关：业务决策暂不落地，机制保留，合伙人审定句式后置 True
    disclaimer_required: bool = False

    # 回调异步处理：立即回 success（企微 5 秒超时红线），实际处理交后台工作线程。
    # 仅测试/本机联调可关。
    callback_async: bool = True
    # 后台线程定时事务间隔（秒）：补位等待到点复评、紧急提醒超时升级
    worker_poll_seconds: float = 10.0

    # ---- LLM 层（deepseek/anthropic 任一 key 未配置时自动降级为纯规则/模板路径）
    # 边界样本复核：规则判「沉默」但可能是漏掉的问题时，交给模型二次确认
    llm_refine_enabled: bool = True
    # 直接回答路径用模型生成一般性法律框架
    llm_answer_enabled: bool = True
    llm_timeout_seconds: float = 15.0
    llm_max_tokens_answer: int = 500
    # 注入模型的群聊上下文条数
    history_window: int = 10

    # ---- 长期记忆：律所自己的知识库（见 responder/memory.py）
    # 检索到的「本所口径」注入模型上下文，让 AI 答得像你们所而不是像一本教科书。
    # 只有人工审核过（approved）的条目会被引用——话术须人审是合规护栏。
    knowledge_enabled: bool = True
    # 注入几条。多了会挤占上下文、也更容易把不相关的口径带进来。
    knowledge_top_k: int = 3

    # ---- 线索简报：筛查完成后把咨询整理成交接单推给接待人
    lead_brief_enabled: bool = True
    # 全量推送（业务决策 2026-08，律所方：「我们有很多的客服，全部都得推给客服，
    # 不能躺死在对话里」）。系统只负责标好强弱，推不推由人手决定，不由系统替他们定。
    # 关掉则回到旧口径：只推有意向的（冷线索仅归档）。
    notify_all_leads: bool = True
    lead_history_window: int = 30  # 整理简报时回看的对话条数
    # 会话静默多久后为「有意向但未触发即时通知」的咨询补一份简报（秒）
    lead_idle_seconds: int = 900
    # 同一客户两次咨询的分隔阈值：超过此空档视为另一次咨询，不并入同一张交接单
    lead_session_gap_seconds: int = 7200

    # ---- 分案与优先级（规则定义与权重见 docs/lead-routing.md；阈值调整须人工确认）
    # P0 强意愿 / P1 有意愿 的分数门槛
    priority_p0_threshold: int = 60
    priority_p1_threshold: int = 30
    # P0 线索超过该时长仍未标记「已联系」→ 追加提醒并抄送第二责任人
    lead_sla_enabled: bool = True
    lead_p0_sla_seconds: int = 3600
    # P1 同样要有人管，只是时限宽得多。P1 是「有意愿但还没留电话」——
    # 它不该占用律师的即时注意力（那是 P0 的特权），但放着不管就是白丢：
    # 单子推出去之后没有任何机制会再提起它。0 = 关闭 P1 督办。
    lead_p1_sla_seconds: int = 86400
    # 紧急线索强推交接单的冷却时间：客户连发几条急消息，律师只该收一张单
    lead_force_cooldown_seconds: int = 600
    # 控制台对外基础地址（生成律师登录链接用）；留空时从请求 Host 推断
    public_base_url: str = ""
    # 群聊单条回复长度上限（字符），超出按句号截断
    answer_max_chars: int = 240

    # 分条发送：多句内容拆成 1~3 条微信消息，条间隔模拟打字节奏（真人感）
    split_messages: bool = True
    split_max_parts: int = 3
    split_delay_seconds: float = 1.5

    # AI 补位等待时长（秒）。[待定] 默认白天 2.5 分钟、夜间 1 分钟，可按群配置覆盖。
    wait_seconds_day: int = 150
    wait_seconds_night: int = 60
    # 一对一客服会话：AI 即第一响应人，等待无意义，默认即时响应
    kf_wait_seconds: int = 0
    night_start_hour: int = 21
    night_end_hour: int = 8

    # 律师群内发言后 AI 静默时长（秒）
    takeover_seconds: int = 1800

    # 紧急提醒升级时长（秒）
    escalation_seconds: int = 600
    # 群档案未配律师企微号时的兜底提醒接收人（话术已向客户承诺「已通知律师」，
    # 提醒必须真的送达；客服会话会自动取该客服账号的接待人，此项为最后兜底）
    default_notify_userid: str = ""

    db_path: str = "responder.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8020

    # ---- 远程升级：让运维/Claude 无需登录服务器即可拉取新版并重启。
    # 只允许拉取下方写死的仓库目录与分支，不接受任何外部输入，
    # 因此权限边界等同于「持有 admin_token 者可部署本仓库的新提交」。
    self_update_enabled: bool = True
    # 自动升级：服务器自己定时看远端分支有没有新提交，有就拉下来重启。
    # 存在的理由很实在——运维侧不一定够得着这台服务器（网络策略/没有 SSH），
    # 但服务器自己够得着 GitHub。关掉则回到「人工点按钮」。
    auto_update_enabled: bool = True
    auto_update_interval_seconds: int = 300
    # 客户刚说过话就不重启：重启会丢掉内存队列里没处理完的消息。
    # 升级晚五分钟没关系，客户的消息掉了有关系。
    auto_update_quiet_seconds: int = 120
    update_repo_dir: str = "/opt/pe"
    update_branch: str = "claude/law-firm-wechat-ai-responder-q3nttv"
    update_pip: str = "/opt/pe-venv/bin/pip"
    update_log: str = "/tmp/responder-update.log"

    # 控制台/ingest 访问令牌：公网部署必填（deploy.sh 自动生成）。
    # 为空时不鉴权（仅限本机开发）；企微回调路由不受此限（有签名校验）。
    admin_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def persist_setting(key: str, value: str, env_path: str = ".env") -> bool:
    """把一项配置写回 .env，使运行时改动（如切换运行模式）在重启后仍生效。

    .env 不存在（测试/临时环境）时静默跳过，返回 False。
    """
    p = Path(env_path)
    if not p.exists():
        return False
    lines = p.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = prefix + value
            replaced = True
            break
    if not replaced:
        lines.append(prefix + value)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
