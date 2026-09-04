from responder.compliance import disclaimer, forbidden
from responder.compliance.guard import guard
from responder.models import Action

SAFE_FALLBACK = "您好，消息已收到。这个问题需要承办律师确认后答复您，我已提醒承办律师尽快回复。"


def test_forbidden_promise():
    assert forbidden.check("放心，这个案子肯定能赢")


def test_forbidden_quote_fee():
    assert forbidden.check("我们收费大概是5万元")
    assert forbidden.check("律师费3万起")


def test_forbidden_free_is_also_a_quote():
    # 抖音那套现成话术里满屏「免费」，照搬进来就等于让 AI 替所里许价。
    # 零也是一个价：客户后面拿这句话说事，跟报了个数字没有区别。
    assert forbidden.check("电话咨询免费，您方便留个电话吗")
    assert forbidden.check("我们会主动打来免费沟通")
    assert forbidden.check("先聊聊，不收费的")
    # 「费用由律师和您谈」是正确的回避说法，不该被这条误伤
    assert forbidden.check("费用这块得律师了解案情后跟您细说") == []


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


def test_guard_appends_disclaimer_when_required():
    result = guard("一般性说明内容。", Action.ANSWER, SAFE_FALLBACK, require_disclaimer=True)
    assert result.passed
    assert disclaimer.has_disclaimer(result.text)


def test_guard_no_disclaimer_by_default():
    # 业务决策：免责句式暂不落地，默认关闭
    result = guard("一般性说明内容。", Action.ANSWER, SAFE_FALLBACK)
    assert result.passed
    assert not disclaimer.has_disclaimer(result.text)


def test_guard_handoff_no_forced_disclaimer():
    result = guard("您好，已提醒律师尽快回复。", Action.HANDOFF, SAFE_FALLBACK)
    assert result.passed
    assert not disclaimer.has_disclaimer(result.text)
