"""Generic public interest prompts. Configure personal preferences locally."""

def interest_prompt(prompt):
    """Append a generic policy without embedding an operator's personal profile."""
    return str(prompt or "") + "\n请依据当前任务及本地明确配置的偏好处理；不推测或公开个人信息。"


def public_draft_prompt():
    """Return a generic public-drafting policy."""
    return "只根据已核验的公开来源拟稿。省略个人身份、私人偏好及内部记录；发布须由本地授权流程决定。"
