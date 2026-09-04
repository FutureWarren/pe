from responder.reply.sanitize import clamp, is_unusable, sanitize


def test_strips_markdown():
    raw = "## 说明\n**重点**：按劳动合同法规定，一般有两种情况。\n- 第一种\n- 第二种\n`code`"
    out = sanitize(raw)
    assert "#" not in out and "*" not in out and "`" not in out
    assert "- " not in out
    assert "重点" in out


def test_strips_greeting_and_emoji():
    out = sanitize("您好！按规定一般三个月内出结果😊")
    assert not out.startswith("您好")
    assert "😊" not in out


def test_collapses_blank_lines():
    out = sanitize("第一句。\n\n\n第二句。")
    assert "\n\n" not in out


def test_clamp_cuts_at_sentence_boundary():
    text = "第一句话说完了。第二句话也说完了。" + "很长的没有标点的尾巴" * 30
    out = clamp(text, 40)
    assert out.endswith("。")
    assert len(out) <= 40


def test_clamp_hard_cut_without_boundary():
    out = clamp("没有标点" * 100, 50)
    assert len(out) == 50


def test_ai_self_reference_unusable():
    assert is_unusable("作为AI，我无法给出建议")
    assert is_unusable("我是一个大模型")
    assert is_unusable("我只是个程序，帮不了您")


def test_meta_refusal_unusable():
    assert is_unusable("抱歉，我不能提供法律建议")
    assert is_unusable("")


def test_normal_text_usable():
    assert not is_unusable("按劳动合同法的规定，一般可以主张经济补偿。")
