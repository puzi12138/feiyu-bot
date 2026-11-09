"""Keep the assistant's execution diagnostics out of public content.

These are deliberately narrow operational phrases, not a ban on discussing
permissions, antivirus warnings, broken games or software errors in a post.
The same check is used before a write and before reusing an old public quote.
"""

import re

SKIP_PUBLIC_COMMENT = "__FEIYU_SKIP_PUBLIC_REPLY__"

_CONTROL = re.compile(
    r"\b(?:explicit_owner_comment_request_required|comment_intent_not_resolved|"
    r"owner_private_only|post_body_unavailable|heartbeat_state_unavailable|"
    r"sensitive_comment_content|comment_length_invalid|feiyu_comment_heybox)\b"
    r"|Traceback \(most recent call last\)|LLM\s*响应错误",
    re.IGNORECASE,
)
_HANDOFF = re.compile(
    r"(?:等(?:待)?|需要|请|得|让|交给|留给|只能由).{0,10}(?:号主|主人).{0,14}"
    r"(?:操作|登录|登陆|扫码|验证|确认|同意|授权|批准|解锁|处理|来发|亲自|手动)"
    r"|(?:号主|主人).{0,6}(?:手动|亲自).{0,8}(?:操作|登录|登陆|验证|发|处理)"
    r"|(?:我|肥鱼|机器人|本账号|当前账号).{0,12}(?:没有|缺少|缺乏|无|未获|被限制).{0,8}"
    r"(?:评论权限|发评权限|发布权限|号主授权|主人授权)",
)
_FAILURE = re.compile(
    r"(?:我(?:这边|这里|刚才|现在|目前|还|也|的)?|肥鱼|机器人|工具|接口|本次|这次|刚才)"
    r"[^。！？\n]{0,50}(?:评论|发评|发出去|发送|发布|登录|扫码|权限|授权)"
    r"[^。！？\n]{0,18}(?:失败|不了|不能|无法|没发出去|没能发|未成功|不够|不足|被拒|出错|报错|未就绪)"
    r"|(?:我(?:这边|这里|刚才|现在|目前)?|肥鱼|机器人|本次|这次|刚才)"
    r"[^。！？\n]{0,18}(?:无法|不能|没法|未能|没有权限|权限不足|权限不够)"
    r"[^。！？\n]{0,18}(?:发(?:送|表|出|评)|评论|操作|登录)"
    r"|(?:评论工具|发评工具|发评接口|评论接口)[^。！？\n]{0,40}"
    r"(?:失败|报错|被拒|没发|权限|不可用|不能|不让|调用|返回)"
    r"|(?:评论|这条评论)[^。！？\n]{0,8}(?:没发出去|未发出去|发送失败|未能发出)"
    r"[^。！？\n]{0,24}(?:权限|号主|主人|工具|接口|稍后|重试|登录)"
    r"|(?:这条评论|本条评论|本次评论|公开评论这边|评论这边|评论(?:我|我这边)?)"
    r"[^。！？\n]{0,10}(?:发不出去|没有发出去|没能发出去|没发出去|无法发出)"
)
_CURRENT_SEND_FAILURE = re.compile(r"这条[^。！？\n]{0,6}(?:没发出去|发不出去|没能发出去)")
_OPERATIONAL_CONTEXT = re.compile(
    r"主人|号主|评论工具|评论接口|发评|权限|授权|登录|扫码|只能.{0,6}私聊.{0,5}发"
)
_UNREAD_SOURCE = re.compile(
    r"(?:我|这边|本次|这次)[^。！？\n]{0,12}(?:没(?:有)?读到|没(?:有)?看到|无法读取|没拿到)"
    r"[^。！？\n]{0,6}(?:正文|原帖|帖子内容)"
    r"|(?:正文|原帖|帖子内容)[^。！？\n]{0,8}(?:读取失败|没读到|未加载|加载失败|无法读取)"
)
_COMMENT_OPERATION = re.compile(
    r"评论(?:工具|接口|权限|我|这边)?|发评|公开发布|公开发言|发布工具|发布接口|私聊.{0,5}发"
)


def internal_comment_reason(value):
    """Return a safe reason code, never the diagnostic text or credentials."""
    if not isinstance(value, str):
        return "empty_public_content"
    if SKIP_PUBLIC_COMMENT in value:
        return "not_interesting"
    if _CONTROL.search(value):
        return "internal_tool_diagnostic"
    # A game can legitimately need its account holder to operate the lobby;
    # mention of an owner is operational only with this comment's write path.
    comment_operation = bool(_COMMENT_OPERATION.search(value))
    if _HANDOFF.search(value) and comment_operation:
        return "internal_owner_handoff"
    if _FAILURE.search(value) and comment_operation:
        return "internal_execution_failure"
    if _CURRENT_SEND_FAILURE.search(value) and _OPERATIONAL_CONTEXT.search(value):
        return "internal_execution_failure"
    if _UNREAD_SOURCE.search(value):
        return "public_source_unavailable"
    return ""
