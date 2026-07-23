from responder.compliance import disclaimer, forbidden
from responder.compliance.guard import guard
from responder.models import Action

SAFE_FALLBACK = "您好，消息已收到。这个问题需要承办律师确认后答复您，我已提醒承办律师尽快回复。"


def test_forbidden_promise():
    assert forbidden.check("放心，这个案子肯定能赢")


def test_forbidden_quote_fee():
    assert forbidden.check("我们收费大概是5万元")
    assert forbidden.check("律师费3万起")


def test_forbidden_predict_case():
    assert forbidden.check("您的案子大概率会判缓刑")


def test_forbidden_attack_court():
    assert forbidden.check("这个法官就是乱判")


def test_forbidden_push_act():
    assert forbidden.check("你应该认罪认罚，赶紧签了")


def test_clean_text_passes():
    text = "根据刑法相关规定，量刑会综合考虑金额、情节与悔罪表现等因素。"
    assert forbidden.check(text) == []


def test_guard_blocks_and_falls_back():
    result = guard("这个案子稳赢，放心", Action.ANSWER, SAFE_FALLBACK)
    assert not result.passed
    assert result.text == SAFE_FALLBACK
    assert result.violations


def test_guard_appends_disclaimer_on_answer():
    result = guard("一般性说明内容。", Action.ANSWER, SAFE_FALLBACK)
    assert result.passed
    assert disclaimer.has_disclaimer(result.text)


def test_guard_handoff_no_forced_disclaimer():
    result = guard("您好，已提醒律师尽快回复。", Action.HANDOFF, SAFE_FALLBACK)
    assert result.passed
    assert not disclaimer.has_disclaimer(result.text)
