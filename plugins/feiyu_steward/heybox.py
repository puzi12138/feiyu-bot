"""Parse public share links and explicit owner comment requests."""

import html
import re
from urllib.parse import parse_qs, urlsplit


def heybox_post_url(value: str) -> str | None:
    """Normalize canonical and official share URLs without fetching redirects.

    Args:
        value: Public URL supplied by the owner or a platform tool.

    Returns:
        Canonical post URL, or None for unsupported hosts and paths.
    """
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        url = urlsplit(html.unescape(value.strip()))
        if (
            url.scheme != "https"
            or url.hostname
            not in {"example.invalid", "xiaoheihe.cn", "api.xiaoheihe.cn"}
            or url.port not in (None, 443)
            or url.username
            or url.password
        ):
            return None
        match = re.fullmatch(r"/app/bbs/link/([A-Za-z0-9_-]{1,64})/?", url.path)
        if match:
            identifier = match.group(1)
        elif (
            url.hostname == "api.xiaoheihe.cn"
            and url.path == "/v3/bbs/app/api/web/share"
        ):
            identifiers = parse_qs(url.query).get("link_id", [])
            if len(identifiers) != 1:
                return None
            identifier = identifiers[0]
        else:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            return None
        return "https://example.invalid/configure-endpoint" + identifier
    except ValueError:
        return None


def explicit_comment_request(value: str) -> bool:
    """Recognize direct commands while rejecting reports and quoted examples.

    Args:
        value: Original text from the verified owner event.

    Returns:
        Whether the owner explicitly requested a comment in this event.
    """
    return bool(
        re.search(
            r"^\s*/?(?:(?:帮我|替我|给我|你去|请)\s*)?(?:小黑盒\s*)?"
            r"(?:评论|留言|发(?:条|个|一条)?评论|写(?:条|个|一条)?评论)"
            r"(?=\s|https://|这|一下|该|：|:)",
            value,
        )
    )


def natural_comment_request(value: str, has_context: bool = False) -> bool:
    """Accept conversational owner intent, without treating reports as orders."""
    if not isinstance(value, str) or len(value) > 4096:
        return False
    text = value.strip()
    if re.search(
        r"^(?:bot说|机器人说|肥鱼说|他说|它说|复现|例如|比如|举例|转发|引用|测试用例|[‘“\"'])",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:不要|别|不用|先不|不许|暂不|取消|停止).{0,4}(?:评论|评|发|留言|说)|^(?:算了|取消|停止|先别|不要|别了|不用了|暂时不)",
        text,
    ):
        return False
    if re.search(
        r"为什么|怎么(?:会|又|还|不能)|无法|失败|不了|被拒|报错|卡在|权限|明确指令|能用了吗|没(?:发|评论)出去",
        text,
    ):
        return False
    if re.search(r"评论区|评论记录|评论内容", text) and not re.search(
        r"发|写|留|说两句|吐槽|评一下", text
    ):
        return False
    if re.search(
        r"^(?:你|帮我)?(?:看看|看下|看一下|读取|读一下|查看|查查|能看到|能看|看得到|有没有).*评论",
        text,
    ) and not re.search(r"再|然后|顺便|并且", text):
        return False
    if explicit_comment_request(text) or re.search(
        r"评论|锐评|点评|评一下|评一条|评两句|留(?:个)?言|说两句|整两句|回一句|回两句|吐槽(?:两句|一下)",
        text,
    ):
        return True
    return has_context and bool(
        re.fullmatch(
            r"(?:就这个|就这条|就它|你看着办|你看着来|你决定|随你|发吧|发出去|来一条|安排|搞起|继续|再试试|试试|现在呢|现在试试|再试一下|接着来)[吧呀啊呗了。！!~～\s]*",
            text,
        )
    )
