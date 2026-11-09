"""Recognize only a current owner's explicit Heybox comment request.

The ordinary natural-language detector intentionally accepts short follow-ups
such as ``继续`` when a recent focus exists.  That is useful for conversation,
but it must not grant a manual-comment exemption from the automatic quota.
This module keeps that exemption narrow and stateless: it examines only the
verified current event and the original text supplied by the caller.
"""

import re

from .heybox import explicit_comment_request
from .peak_policy import manual_owner_event


_COMMENT = r"(?:评论(?:一下|一条|两句|一句)?|发(?:条|个|一条)?评论|写(?:条|个|一条)?评论|锐评(?:一下|两句|一句)?|点评(?:一下|两句|一句)?|吐槽(?:一下|两句|一句)?|留言|留(?:个)?言|留(?:一|两)?句|回复|回(?:一|两)?句|说(?:一|两)?句|评(?:一下|一条|两句|一句)?)"
_TARGET = r"(?:这个(?:帖子?|贴|条(?:帖子?)?|篇(?:帖子?)?)?|这(?:个)?(?:帖子?|贴|条(?:帖子?)?|篇(?:帖子?)?)|该(?:帖子?|贴|条(?:帖子?)?|篇(?:帖子?)?)|那个(?:帖子?|贴|条(?:帖子?)?|篇(?:帖子?)?)|那(?:个)?(?:帖子?|贴|条(?:帖子?)?|篇(?:帖子?)?))"
_TRAILING = r"(?:吧|呀|啊|呗)?[。！!，,；;：:]*(?:[？?])?"

# Prefixes that make a bare comment verb an instruction.  A request such as
# “去评论一下” is explicit even when its target is supplied by the caller's
# already-selected source URL.  Conversational follow-ups such as “继续” are
# deliberately absent.
_PREFIX_ACTION = re.compile(
    r"^(?:/?(?:帮我|替我|给我|请(?:帮我|帮忙|你|我)?|麻烦(?:帮我|帮忙)?|"
    r"你来|你去|去|帮忙|直接|现在|顺手|"
    r"(?:你)?能不能帮我|(?:你)?可不可以帮我|(?:你)?可以帮我))"
    r"(?:小黑盒)?" + _COMMENT + _TARGET + r"?" + _TRAILING + r"$"
)

# A target before the verb is another unambiguous natural form: “给这帖留
# 一句”.  Keep “话题” out of _TARGET so a discussion about a topic cannot
# silently become a publication request.
_TARGET_ACTION = re.compile(
    r"^(?:给|对)" + _TARGET + _COMMENT + _TRAILING + r"$"
)

# Without a request prefix, require a concrete demonstrative target.  This
# admits “评论一下这个” while rejecting a bare noun phrase such as “评论很
# 有意思”.
_COMMENT_TARGET = re.compile(
    r"^(?:小黑盒)?" + _COMMENT + _TARGET + _TRAILING + r"$"
)

# Restrict the established command parser's open-ended suffix.  An unrelated
# statement starting with “评论 这……” is not, by itself, a publication order.
_EXPLICIT_COMMAND = re.compile(
    r"^/?(?:(?:帮我|替我|给我|你去|请))?(?:小黑盒)?"
    r"(?:评论|留言|发(?:条|个|一条)?评论|写(?:条|个|一条)?评论)"
    r"(?:一下)?(?:" + _TARGET + r")?"
    r"(?:https://[^\s<>，。！？；、“”‘’]+)?" + _TRAILING + r"$"
)

_QUOTED_OR_REPORTED = re.compile(
    r"^(?:[\"'“‘「『《]|(?:bot说|机器人说|肥鱼说|他说|她说|它说|"
    r"转述|引用|例如|比如|举例|测试用例|复现)(?:[:：\s]|$))",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"(?:^(?:算了|取消|停止|先别|不要|别了|不用了?|暂时不|先不|不许|不想|"
    r"不需要|不打算)|"
    r"(?:不要|别|不用|先不|不许|暂不|取消|停止|不想|不需要|不打算|不喜欢|"
    r"讨厌|不再|不愿意|不能|不可以|不允许|无需|没有让|没让|拒绝).{0,12}"
    r"(?:评论|评|锐评|点评|留言|回复|回|说|吐槽)|"
    r"(?:评论|评|锐评|点评|留言|回复|回|说|吐槽).{0,12}(?:不要|别|不用|先不|"
    r"不许|暂不|取消|停止|不能|不可以|不允许|无需|拒绝))"
)
_QUESTION_OR_STATUS = re.compile(
    r"(?:评论|评|锐评|点评|留言|回复|回|吐槽)(?:区|记录|内容|状态|功能|能力|工具|"
    r"接口|权限|额度|次数|规则|流程|条件)|"
    r"(?:状态|记录|功能|能力|工具|接口|权限|额度|次数|规则|流程|条件).{0,12}"
    r"(?:评论|评|锐评|点评|留言|回复|回|吐槽)|"
    r"^(?:怎么|如何|为什么|能不能|可不可以|能否|是否|会不会|有没有).{0,20}"
    r"(?:评论|评|锐评|点评|留言|回复|回|吐槽)|"
    r"(?:评论|评|锐评|点评|留言|回复|回|吐槽).{0,20}"
    r"(?:怎么|如何|为什么|能不能|可不可以|能否|是否|会不会|能用吗|可以吗|支持吗|"
    r"行不行|好吗)$",
    re.IGNORECASE,
)
_QUESTION_END = re.compile(r"(?:吗|呢)$")


def _compact(value):
    """Return bounded text with whitespace removed, or ``None``."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    text = re.sub(r"\s+", "", value.strip())
    return text or None


def clearly_requested_manual_comment(event, original_text) -> bool:
    """Return whether this event explicitly asks for a Heybox comment.

    The identity check runs before text parsing, so cron-generated events can
    never acquire a manual exemption.  No conversation history or persistent
    state is consulted; an ambiguous follow-up therefore remains automatic.
    """
    try:
        if not manual_owner_event(event):
            return False
    except Exception:  # noqa: BLE001 - malformed event identity fails closed
        return False
    text = _compact(original_text)
    if text is None:
        return False
    if _QUOTED_OR_REPORTED.search(text):
        return False
    # A topic/status/capability discussion is not a publication instruction.
    if "话题" in text or "评论区" in text:
        return False
    if _NEGATED.search(text):
        return False
    direct = (
        _PREFIX_ACTION.fullmatch(text)
        or _TARGET_ACTION.fullmatch(text)
        or _COMMENT_TARGET.fullmatch(text)
    )
    if direct:
        return True
    if _QUESTION_END.search(text.rstrip("?？!！。")):
        return False
    # Preserve the established explicit path (including slash forms) only
    # after the stricter negative, quote, and topic checks above.
    try:
        if (_EXPLICIT_COMMAND.fullmatch(text) and explicit_comment_request(text)
                and not _QUESTION_OR_STATUS.search(text)):
            return True
    except Exception:  # noqa: BLE001 - parser failure must not grant access
        return False
    return False


__all__ = ["clearly_requested_manual_comment"]
