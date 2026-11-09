"""Keep host administration in the verified owner's private conversation.

This boundary is independent of group participation and never sends a message.
It must run before command dispatch, before an LLM request, and on its final
result. The SnowLuma built-in status command is disabled separately because it
answers before AstrBot receives a message.
"""

import copy
import functools
import re
from contextlib import aclosing

OWNER = "0"
OWNER_UMO = f"snowluma-qq:FriendMessage:{OWNER}"
SAFE_CONNECTION_ERROR = "刚才连接没接上，这条没有完成，稍后再试试。"

_CONTROL = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_CQ_AT = re.compile(r"\[CQ:at,[^\]]*\]", re.IGNORECASE)
_EXACT_COMMAND = re.compile(
    r"^(?:[#/／](?:sl|status|version|runtime|uptime|dashboard|config|plugins?|"
    r"providers?|models?|admin|help|菜单|帮助|插件|插件列表|模型|模型列表|配置|"
    r"日志|后台|版本|状态)|(?:查看|查询|检查|显示)?(?:运行状态|系统状态|"
    r"后台配置|插件列表|模型配置|运行日志|运行时长)|状态)$", re.IGNORECASE)
_COMMAND_PREFIX = re.compile(
    r"^[#/／](?:sl|status|version|runtime|uptime|dashboard|config|plugins?|providers?|"
    r"models?|admin|help|菜单|帮助|插件|插件列表|模型|模型列表|配置|日志|后台|版本|状态|肥鱼管家|管家)"
    r"(?:\s|$)", re.IGNORECASE)
_TECH_SUBJECT = r"(?:SnowLuma|AstrBot|机器人|bot|肥鱼|你|系统|服务|后台|插件|模型)"
_TECH_FACT = (r"(?:软件版本|运行平台|运行时长|启动时间|运行时间|运行状态|系统状态|"
              r"后台状态|后台配置|日志|日志文件|配置文件|端口|进程|内存占用|CPU(?:占用|型号|使用率)|"
              r"API[ _-]?(?:Key|密钥|地址)|Cookie|密码|密钥|token|凭据|"
              r"插件列表|插件配置|模型配置|模型列表|运行版本|运行环境)")
_TECH_REQUEST = re.compile(
    r"(?:SnowLuma|AstrBot)\s*(?:状态|配置|版本|平台|日志)"
    + r"|" + _TECH_SUBJECT + r".{0,18}" + _TECH_FACT
    + r"|" + _TECH_FACT + r".{0,18}" + _TECH_SUBJECT
    + r"|(?:你|肥鱼|机器人|bot).{0,5}(?:的)?(?:版本|平台|配置)(?:是|为|号|啥|什么|多少|[：:?？]|$)"
    + r"|(?:你|肥鱼|机器人|bot).{0,5}(?:运行|开机|启动)(?:了|多长时间|多久)"
    + r"|(?:查看|查询|展示|发我|告诉我|读取|导出|修改|设置|打开|关闭|安装|卸载|启用|禁用)"
      r".{0,12}(?:后台|配置文件|运行日志|运行时长|插件列表|模型配置|API[ _-]?Key)"
    + r"|(?:列出|查看|查询|展示|看看).{0,6}(?:已安装|当前|所有|全部)?(?:的)?(?:插件|模型)"
    + r"|(?:插件|模型).{0,6}(?:有哪些|列表|配置|参数)"
    + r"|(?:你|肥鱼|机器人|bot).{0,8}(?:用|使用|接入|连接|跑|运行|装|安装|启用).{0,12}"
      r"(?:模型|插件|平台|接口|后端|API)"
    + r"|(?:你|肥鱼|机器人|bot).{0,6}(?:是|叫).{0,8}模型"
    + r"|(?:你|肥鱼|机器人|bot)(?:的)?(?:接口|API|内存|CPU|版本|平台|配置).{0,8}(?:是什么|多少|什么|怎样|怎么样|[?？]|$)"
    + r"|^(?:查询|检查|查看)(?:一下)?(?:CPU|内存|端口|进程|运行环境|后台配置)"
    + r"|^/?(?:肥鱼管家|管家)(?:\s|$)", re.IGNORECASE)
_CODE_DISCUSSION = re.compile(
    r"(?:解释|科普|讲讲|示例|举例|教程|什么意思|是什么|怎么处理|如何处理|"
    r"如何解决|怎么解决).{0,30}(?:APIConnectionError|Traceback|LLM|代码|异常)"
    r"|(?:APIConnectionError|Traceback|LLM).{0,20}(?:是什么|什么意思|怎么处理|"
    r"如何处理|如何解决|怎么解决|示例)", re.IGNORECASE)
_RAW_ERROR = re.compile(
    r"(?:^|\n)\s*(?:LLM\s*响应错误\s*[:：]|All chat models failed\s*[:：]|"
    r"APIConnectionError\s*[:：]|Traceback \(most recent call last\)\s*:)"
    r"|^\s*(?:发生错误|请求失败|模型错误|模型调用失败)\s*[:：].{0,100}"
    r"(?:APIConnectionError|All chat models failed)", re.IGNORECASE)
_DIRECT_RAW_ERROR = re.compile(
    r"(?:^|\n)\s*(?:LLM\s*响应错误\s*[:：]|All chat models failed\s*[:：]|"
    r"APIConnectionError\s*[:：]\s*(?:Connection error|connect(?:ion)? failed|"
    r"network error|request timed out))", re.IGNORECASE)
_HOST_OUTPUT = re.compile(
    r"SnowLuma\s*状态|(?:SnowLuma|AstrBot)\s*(?:版本|version)\s*[:：=]?\s*\d"
    r"|(?:^|\n)\s*(?:运行时长|启动时间|后台配置|模型配置|API\s*Key|"
    r"运行日志|插件列表|模型列表|服务端口|进程ID|进程 PID)\s*[:：]"
    r"|(?:^|\n)\s*平台\s*[:：]\s*(?:win32|linux|darwin|windows)[-\s]"
    r"|(?:^|\n)\s*(?:API|模型服务|后台|面板)(?:地址|URL)\s*[:：]\s*https?://"
    r"|(?:我|肥鱼|机器人)(?:现在|目前|当前)?(?:使用|用|接入|运行|安装|装了)(?:的)?(?:是)?"
    r".{0,30}(?:模型|插件|win32|linux|darwin|API接口)"
    r"|(?:我|肥鱼|机器人)(?:的)?(?:接口|API|后台|模型|插件|版本|运行平台)\s*(?:是|为|[:：])"
    r"|(?:运行时长|运行平台|后台配置|模型配置|内存占用|CPU使用率)\s*[:：]\s*\S"
    r"|[A-Z]:[\\/](?:QQBot|Users[\\/][^\s\\/]+[\\/]\.codex)[\\/]"
    r"|(?:^|\n)\s*(?:access_token|api_key|authorization|sessdata|bili_jct|p_skey)\s*[:=：]",
    re.IGNORECASE)
_ADMIN_TOOL = re.compile(
    r"(?:^|_)(?:admin|config|configure|configuration|dashboard|runtime|uptime|"
    r"status|version|diagnostic|diagnostics|log|logs|process|processes|env|"
    r"environment|credential|credentials|secret|secrets|settings|shell|terminal|"
    r"exec|execute|ipython|python|powershell|filesystem|file|grep|cua)(?:_|$)"
    r"|(?:^|_)(?:read|write|list|search|delete|edit|open)_files?(?:_|$)"
    r"|(?:^|_)(?:manage|list|get|install|uninstall|enable|disable)_(?:plugins?|providers?|models?)(?:_|$)",
    re.IGNORECASE)


def _extra(event, key, value):
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter(key, value)


def owner_private(event):
    """Authorize transport identity, with agreement of available raw fields."""
    try:
        if str(getattr(event, "unified_msg_origin", "")) != OWNER_UMO:
            return False
        if str(event.get_sender_id()) != OWNER:
            return False
        if str(getattr(event, "get_group_id", lambda: "")() or ""):
            return False
        message = getattr(event, "message_obj", None)
        if str(getattr(message, "group_id", "") or ""):
            return False
        sender = getattr(message, "sender", None)
        if sender is not None and str(getattr(sender, "user_id", "")) != OWNER:
            return False
        raw = getattr(message, "raw_message", None)
        if isinstance(raw, dict) and raw:
            if raw.get("message_type", "private") != "private" or raw.get("group_id"):
                return False
            if raw.get("user_id") is not None and str(raw["user_id"]) != OWNER:
                return False
            raw_sender = raw.get("sender")
            if (isinstance(raw_sender, dict) and raw_sender.get("user_id") is not None
                    and str(raw_sender["user_id"]) != OWNER):
                return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def admin_info_request(text):
    """Recognize actual host administration, not game or fictional status."""
    original = _CONTROL.sub("", _CQ_AT.sub("", str(text or ""))).strip()
    # The optional bot call name must not be confused with the trusted sender.
    text = re.sub(r"^(?:大肥鱼|肥鱼|bot)[,，:：\s]*", "", original, flags=re.IGNORECASE).strip()
    text = text.rstrip("。！!？? ")
    return bool(_EXACT_COMMAND.fullmatch(text) or _COMMAND_PREFIX.match(text)
                or _TECH_REQUEST.search(original))


def admin_info_tool_block(event, tool_name):
    """Use at tool execution as well as when constructing visible tool sets."""
    if owner_private(event):
        return ""
    return "owner_private_admin_only" if _ADMIN_TOOL.search(str(tool_name)) else ""


def admin_info_outbound_block(action, params):
    """Block direct status dumps at OneBot's final send boundary too.

    Ordinary quoted code errors are handled with event context by the result
    filter. This last boundary catches host information and the core's direct
    raw failure wrapper, which can bypass result decoration.
    """
    action = str(action).lower().lstrip("./_")
    if not action.startswith(("send", "handle_quick_operation")):
        return ""
    destination = params
    if action.startswith("handle_quick_operation") and isinstance(params.get("context"), dict):
        destination = params["context"]
    message_type = str(destination.get("message_type", destination.get("type", ""))).lower()
    private_action = action.startswith("send_private_") or message_type == "private"
    is_group = not private_action and (bool(destination.get("group_id"))
                                      or message_type == "group" or "group" in action)
    if not is_group and str(destination.get("user_id", "")) == OWNER:
        return ""

    def texts(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for item in value.values() for text in texts(item)]
        if isinstance(value, (list, tuple)):
            return [text for item in value for text in texts(item)]
        return []

    body = "\n".join(text for key in ("message", "messages", "operation")
                     for text in texts(params.get(key)))
    if _HOST_OUTPUT.search(body):
        return "owner_private_admin_only"
    if _DIRECT_RAW_ERROR.search(body):
        return "internal_error_output_suppressed"
    return ""


def install_admin_info_tool_guard(executor=None):
    """Guard the real execution entry, return an identity-safe restore callback."""
    if executor is None:
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        executor = FunctionToolExecutor
    descriptor = executor.__dict__.get("execute")
    if not isinstance(descriptor, classmethod):
        raise TypeError("admin_tool_executor_unsupported")
    original = descriptor.__get__(None, executor)

    @functools.wraps(original)
    async def guarded(cls, tool, run_context, **tool_args):
        event = getattr(getattr(run_context, "context", None), "event", None)
        reason = admin_info_tool_block(event, getattr(tool, "name", ""))
        if reason:
            from mcp.types import CallToolResult, TextContent

            yield CallToolResult(
                content=[TextContent(type="text", text='{"ok":false,"reason":"owner_private_admin_only"}')],
                isError=True,
            )
            return
        async with aclosing(original(tool, run_context, **tool_args)) as results:
            async for item in results:
                yield item

    installed = classmethod(guarded)
    executor.execute = installed

    def restore():
        if executor.__dict__.get("execute") is installed:
            executor.execute = descriptor

    return restore


class AdminInfoBoundary:
    """No state, IO or model calls; safe at the transport's first event gate."""

    def prepare_admin_info(self, event):
        """Return True after stopping a forbidden administrative command."""
        # AstrBot removes wake prefixes before the observer runs. Preserve
        # protection for /status etc. using the transport's original text too.
        texts = (getattr(event, "message_str", ""),
                 getattr(getattr(event, "message_obj", None), "message_str", ""))
        if owner_private(event) or not any(admin_info_request(text) for text in texts):
            return False
        _extra(event, "feiyu_admin_info_block", "owner_private_admin_only")
        event.stop_event()
        return True

    def apply_admin_info_context(self, event, req):
        """Remove host tools per request without changing the shared registry."""
        if owner_private(event):
            return
        req.system_prompt = (getattr(req, "system_prompt", "") or "") + (
            '请仅依据当前公开来源及本地明确配置的信息处理请求；不要推测个人身份或私人偏好。'
        )
        tools = getattr(req, "func_tool", None)
        items = getattr(tools, "tools", None)
        if isinstance(items, list):
            selected = [tool for tool in items
                        if not admin_info_tool_block(event, getattr(tool, "name", ""))]
            if len(selected) != len(items):
                if selected:
                    filtered = copy.copy(tools)
                    filtered.tools = selected
                    req.func_tool = filtered
                else:
                    req.func_tool = None

    def filter_admin_info_result(self, event):
        """Drop leaked host details and replace genuine raw error responses."""
        if owner_private(event):
            return False
        result = getattr(event, "get_result", lambda: None)()
        if result is None:
            return False
        chain = getattr(result, "chain", None)
        if not isinstance(chain, list):
            return False
        texts = []
        for component in chain:
            if isinstance(component, dict):
                data = component.get("data", {})
                if component.get("type") == "text" and isinstance(data, dict):
                    texts.append(str(data.get("text", "")))
            else:
                value = getattr(component, "text", None)
                if isinstance(value, str):
                    texts.append(value)
        text = "\n".join(texts)
        if _HOST_OUTPUT.search(text):
            result.chain = []
            _extra(event, "feiyu_admin_info_block", "admin_output_suppressed")
            event.stop_event()
            return True
        source = str(getattr(event, "message_str", "") or "")
        if _RAW_ERROR.search(text) and not _CODE_DISCUSSION.search(source):
            # Replacing the whole result also removes accidentally attached
            # diagnostic files or an image of a traceback from this failure.
            event.set_result(event.plain_result(SAFE_CONNECTION_ERROR))
            _extra(event, "feiyu_admin_error_redacted", True)
            return True
        return False
