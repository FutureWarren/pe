"""真人语感回归：模板与净化层不得出现 AI 腔特征（docs/voice-guide.md 的自动化红线）。"""

from responder.compliance import forbidden
from responder.models import Category, ClientStatus, GroupProfile
from responder.reply import templates
from responder.reply.sanitize import sanitize, split_messages

SIGNED = GroupProfile(
    group_id="g1", client_status=ClientStatus.SIGNED, case_type="劳动仲裁", lawyer_name="王"
)
PROSPECT = GroupProfile(
    group_id="g2", client_status=ClientStatus.PROSPECT, case_type="劳动仲裁", lawyer_name="李"
)

# AI 腔红线（出现即失败）
AI_TELLS = [
    "首先", "其次", "综上", "此外", "总而言之",
    "亲爱的", "希望能帮到您", "感谢您的咨询", "作为AI", "智能助理",
    "~", "～", "！！", "??",
]


def _all_template_texts() -> list[str]:
    texts = []
    for group in (SIGNED, PROSPECT):
        for category in Category:
            for seed in ("a", "b", "c", "d", "e"):
                texts.append(templates.build_handoff(category, group, seed=seed))
        texts.append(templates.safe_fallback(group))
        texts.append(templates.second_touch(group, urgent=False))
        texts.append(templates.second_touch(group, urgent=True))
        texts.append(templates.answer_without_llm(group))
        texts.append(templates.answer_scaffold(group, "一般四十五天内出结果。"))
    return texts


def test_no_ai_tells_in_any_template():
    for text in _all_template_texts():
        for tell in AI_TELLS:
            assert tell not in text, f"AI 腔特征 {tell!r} 出现在: {text}"


def test_no_forbidden_in_any_template():
    for text in _all_template_texts():
        assert forbidden.check(text) == [], text


def test_lines_are_wechat_length():
    # 每条（按换行拆分后）不超过 60 字——微信真人消息长度
    for text in _all_template_texts():
        for line in text.split("\n"):
            assert len(line) <= 60, f"过长（{len(line)}字）: {line}"


def test_at_most_one_ha_per_message():
    for text in _all_template_texts():
        assert text.count("哈") <= 1, text


def test_sanitize_strips_formal_connectors_and_tilde():
    out = sanitize("首先，要看合同。此外，还要看流水。结果如何～要看证据。")
    assert "首先" not in out and "此外" not in out and "～" not in out
    assert "要看合同" in out


# ---------------------------------------------------------------- 分条发送
def test_split_multiline():
    parts = split_messages("第一条内容比较长所以保留标点符号在结尾处呢好的。\n第二条短")
    assert len(parts) == 2
    assert parts[1] == "第二条短"


def test_split_short_chunk_drops_period():
    parts = split_messages("收到啦。\n关键是把证据留好，工资流水和考勤记录都要保存下来。")
    assert parts[0] == "收到啦"  # 短条去句号
    assert parts[1].endswith("。")  # 长条保留


def test_split_merges_beyond_max():
    parts = split_messages("一\n二\n三\n四\n五", max_parts=3)
    assert len(parts) == 3
    assert "四" in parts[2] and "五" in parts[2]


def test_split_single_line_passthrough():
    assert split_messages("就一条消息，不用拆分，长度也够长不去句号了对吧。") == [
        "就一条消息，不用拆分，长度也够长不去句号了对吧。"
    ]
