"""群聊 @ 消息的正文清洗。

企微把「@助手 拖欠工资怎么办」原样推过来，若不剥掉 @ 前缀：
  1. 判断引擎的「@点名 → 沉默」规则会命中（那条规则是为「客户点名律师」设计的，
     客户点名助手恰恰是要它回答，语义相反）；
  2. @ 名字会混进模型上下文，影响分类与话术。
剥离只处理开头连续的 @昵称，句中提到的 @某某 保留（那是对话内容的一部分）。
"""

import re

# 企微 @ 后跟昵称，以空格或全角空格结束；昵称可能含中英文、数字、下划线、点
_LEADING_AT = re.compile(r"^\s*(?:@[^\s  ]+[\s  ]+)+")


def strip_mentions(text: str) -> tuple[str, bool]:
    """返回 (剥离 @ 前缀后的正文, 是否存在 @ 前缀)。"""
    if not text:
        return "", False
    cleaned = _LEADING_AT.sub("", text)
    return cleaned.strip(), cleaned != text
