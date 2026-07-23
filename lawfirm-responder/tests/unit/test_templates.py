from responder.compliance import forbidden
from responder.compliance.disclaimer import DISCLAIMER
from responder.models import Category, ClientStatus, GroupProfile
from responder.reply import templates

SIGNED = GroupProfile(
    group_id="g1", client_status=ClientStatus.SIGNED, case_type="刑事辩护", lawyer_name="王"
)
PROSPECT = GroupProfile(
    group_id="g2", client_status=ClientStatus.PROSPECT, case_type="婚姻家事", lawyer_name="李"
)


def test_all_handoff_templates_clean():
    for category in [Category.CASE_STATUS, Category.FEE, Category.URGENT, Category.CONTACT]:
        for group in (SIGNED, PROSPECT):
            text = templates.build_handoff(category, group)
            assert forbidden.check(text) == [], (category, text)


def test_lawyer_name_injected():
    text = templates.handoff_case_status(SIGNED)
    assert "王律师" in text


def test_fee_prospect_invites_meeting():
    assert "面谈" in templates.handoff_fee(PROSPECT)
    assert "面谈" not in templates.handoff_fee(SIGNED)


def test_answer_scaffold_with_disclaimer():
    text = templates.answer_scaffold(SIGNED, "一般性法律框架说明。", include_disclaimer=True)
    assert DISCLAIMER in text
    assert "王律师" in text


def test_answer_scaffold_default_no_disclaimer():
    text = templates.answer_scaffold(SIGNED, "一般性法律框架说明。")
    assert DISCLAIMER not in text


def test_answer_scaffold_prospect_invites_meeting():
    # 未成交群：销售顾问定位，first screening 后自然引导面谈
    text = templates.answer_scaffold(PROSPECT, "一般性法律框架说明。")
    assert "约个时间" in text


def test_fallback_without_lawyer_name():
    text = templates.safe_fallback(GroupProfile(group_id="g3"))
    assert "承办律师" in text
