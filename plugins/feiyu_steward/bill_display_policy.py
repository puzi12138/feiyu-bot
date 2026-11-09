"""Fixed names for user-visible usage reports; internal identifiers stay unchanged."""

import re

PROJECT_USAGE_LABEL = "项目用量"
PROJECT_LEDGER_LABEL = "项目账本"
PROJECT_SCOPE_LABEL = "本机已保留的项目请求记录（限定客户端；其他客户端及未记录请求不包含）"
BILL_DISPLAY_INSTRUCTION = (
    "账单展示规则：标题、明细、摘要、发送配文和账单回复均以“项目用量”或“项目账本”"
    "称呼项目来源，并与肥鱼本机用量分开；不在账单中使用来源品牌或客户端名称。"
    "保留实际时段、未回传次数、计价覆盖、差额和不可用原因；不改变来源或计算。"
    "主人明确询问实际来源时，在账单之外根据实际结果如实解释。"
)


# A source word beside letters, digits, underscore, dot or hyphen can be
# part of a model name or technical identifier; never rewrite that name.
_SOURCE_WORD = re.compile(r"(?<![A-Za-z0-9_.-])(?:cockpit|codex)(?![A-Za-z0-9_.-])", re.IGNORECASE)
_SOURCE_CODES = {
    "feiyu_and_cockpit_local_ledgers": "肥鱼本机与项目账本",
    "cockpit_request_ledger": PROJECT_LEDGER_LABEL,
    "cockpit_only_v1": "项目用量报告",
    "codex_client_all_upstream_accounts": "限定客户端全部上游账户",
    "cockpit_usage_unavailable": "项目用量不可用",
    "cockpit_source_unavailable": "项目账本不可用",
    "cockpit_usage_invalid": "项目用量数据无效",
    "codex_summary_unavailable": "项目账本摘要不可用",
    "codex_window_mismatch": "项目账本时段不一致",
    "codex_usage_invalid": "项目用量数据无效",
}
_SOURCE_CODE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:" + "|".join(_SOURCE_CODES) + r")(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)


def display_bill_text(value):
    """Normalize presentation names only; preserve model names and internal data."""
    if not isinstance(value, str):
        return value
    value = _SOURCE_CODE.sub(lambda match: _SOURCE_CODES[match.group(0).lower()], value)
    value = re.sub(r"(?<![A-Za-z0-9_.-])\.codex(?![A-Za-z0-9_.-])", "限定客户端", value, flags=re.IGNORECASE)
    return _SOURCE_WORD.sub(PROJECT_USAGE_LABEL, value)


def model_bill_result(value):
    """Copy a result for the model, normalize display fields and attach the rule."""
    def project(item):
        if isinstance(item, dict):
            return {key: display_bill_text(part) if key in {"text", "note", "instruction", "scope_label"}
                    and isinstance(part, str) else project(part) for key, part in item.items()}
        if isinstance(item, list):
            return [project(part) for part in item]
        return item
    result = project(value)
    if not isinstance(result, dict):
        return result
    previous = result.get("instruction")
    result["instruction"] = ((previous + " ") if isinstance(previous, str) and previous else "") + BILL_DISPLAY_INSTRUCTION
    return result


def display_bill_plain_chain(chain, plain_type):
    """Normalize contiguous text components, including a split source name."""
    if not isinstance(chain, list):
        return
    index = 0
    while index < len(chain):
        if not isinstance(chain[index], plain_type) or not isinstance(getattr(chain[index], "text", None), str):
            index += 1
            continue
        end = index + 1
        while (end < len(chain) and isinstance(chain[end], plain_type)
               and isinstance(getattr(chain[end], "text", None), str)):
            end += 1
        original = "".join(part.text for part in chain[index:end])
        visible = display_bill_text(original)
        if visible != original:
            chain[index].text = visible
            for part in chain[index + 1:end]:
                part.text = ""
        index = end


def display_bill_tool_messages(messages):
    """Copy marked tool text only; preserve media, validation and model names."""
    if not isinstance(messages, list):
        return messages
    output = []
    changed = False
    index = 0
    while index < len(messages):
        item = messages[index]
        kind = str(item.get("type", "")).lower() if isinstance(item, dict) else ""
        if kind == "file" and isinstance(item.get("text"), str):
            visible = display_bill_text(item["text"])
            output.append({**item, "text": visible} if visible != item["text"] else item)
            changed |= visible != item["text"]
            index += 1
            continue
        if (kind != "plain" or not isinstance(item.get("text"), str)
                or not item["text"].strip()):
            output.append(item)
            index += 1
            continue
        end = index + 1
        while end < len(messages):
            part = messages[end]
            if (not isinstance(part, dict) or str(part.get("type", "")).lower() != "plain"
                    or not isinstance(part.get("text"), str) or not part["text"].strip()):
                break
            end += 1
        run = messages[index:end]
        combined = "".join(part["text"].strip() for part in run)
        visible = display_bill_text(combined)
        if visible == combined:
            output.extend(run)
        else:
            rewritten = [display_bill_text(part["text"]) for part in run]
            if "".join(text.strip() for text in rewritten) == visible:
                output.extend({**part, "text": text} for part, text in zip(run, rewritten))
            else:
                # A source or model name crossed segments; merge just this text run.
                output.append({**run[0], "text": visible})
            changed = True
        index = end
    return output if changed else messages
