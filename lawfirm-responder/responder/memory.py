"""长期记忆：律所自己的知识库 + 客户跨会话档案。

## 两种记忆，别混在一起

- **组织记忆**（知识库）：律所的做法、口径、常见问答。跨客户、长期稳定。
  解决的是「答得像你们所」——现在模型答的是通用法律知识，对，但不是你们的。
- **客户记忆**（会话档案里的 `memory`）：这个人是谁、什么案子、聊到哪一步。
  解决的是老客户三周后回来，AI 还记得他。

## 为什么是检索，不是训练

一年 ~4700 通对话，这个量对微调模型远远不够（差两三个数量级），
但对检索绰绰有余。检索还有两个训练比不了的好处：
**改一条立刻生效**，以及**能说清这句话的依据是哪一条**——
后者对律所是硬要求，答错了要能追到出处。

## 为什么是暴力打分，不是向量库

条目数在几百到几千这个量级，全表算一遍中文二元组重合度是微秒级的事。
上向量库要额外的服务、额外的钱、额外的一处会挂的东西，换来的是
这个规模上察觉不到的速度差。**超过约 5000 条再说**，那时再换不迟。

## 命中不了就别塞

检索不到就什么都不注入。塞一条不相关的知识进上下文，比不塞更糟——
模型会努力把它用上，于是答非所问，而且答得理直气壮。
"""

import logging
import re

logger = logging.getLogger(__name__)

# 去掉标点与空白后再比对：客户打字带不带逗号是随机的，不该影响命中
_NOISE = re.compile(r"[\s，。！？、；：""''（）()【】\[\]…—~～,.!?;:'\"-]+")

# 命中门槛。调高 = 宁可不注入；调低 = 容易塞进不相关的条目。
# 0.12 是在「拖欠工资」这类两三字核心词能命中、而泛泛闲聊命中不了之间取的值。
MIN_SCORE = 0.12


def _norm(text: str) -> str:
    return _NOISE.sub("", (text or "")).lower()


def _bigrams(text: str) -> set[str]:
    """中文按二元组切。不用分词器：装 jieba 只为了这一处不值，
    而二元组对「拖欠工资」「劳动仲裁」这类固定搭配的召回已经足够好。"""
    t = _norm(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def score(query: str, entry_text: str) -> float:
    """Dice 相似度：两串二元组的重合比例，0~1。

    用 Dice 而不是「命中数」，是因为条目越长命中数天然越高——
    那会让一条啰嗦的知识永远排在最前面，跟它切不切题无关。
    """
    a, b = _bigrams(query), _bigrams(entry_text)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def search(entries: list[dict], query: str, limit: int = 3) -> list[dict]:
    """在已审核条目里找最相关的几条。命中不了返回空。

    比对的是「问题 + 标签」而不是答案：客户说的是问题，
    拿他的问题去撞答案的措辞，命中的往往是碰巧用词相似的无关条目。
    """
    scored = []
    for e in entries:
        s = score(query, f"{e.get('question', '')} {e.get('tags', '')}")
        if s >= MIN_SCORE:
            scored.append((s, e))
    scored.sort(key=lambda x: (-x[0], x[1].get("id", 0)))
    return [dict(e, _score=round(s, 3)) for s, e in scored[:limit]]


def format_for_prompt(hits: list[dict]) -> str:
    """拼成注入 prompt 的文本。空列表返回空串——调用方据此决定注不注入。"""
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(f"问：{h.get('question', '').strip()}")
        lines.append(f"本所口径：{h.get('answer', '').strip()}")
    return "\n".join(lines)
