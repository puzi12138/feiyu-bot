"""Persist background results without inventing user turns or stale writes."""

import json

from astrbot import logger
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.session_lock import session_lock_manager


async def persist_agent_history(
    conversation_manager: ConversationManager,
    *,
    event: AstrMessageEvent,
    req: ProviderRequest,
    summary_note: str,
) -> None:
    """Append a task result to the latest history under the normal session lock.

    The two callers are standalone cron/background tasks, outside the normal
    runner's session lock. Reusing that lock serializes this append with normal
    turns. Never write the request's launch-time history back over newer turns.
    """
    if not req or not req.conversation or not isinstance(summary_note, str) or not summary_note.strip():
        return
    origin = event.unified_msg_origin
    conversation_id = req.conversation.cid
    async with session_lock_manager.acquire_lock(origin):
        latest = await conversation_manager.get_conversation(
            origin, conversation_id, create_if_not_exists=False,
        )
        if latest is None:
            # A deleted or switched-away conversation is never recreated here.
            return
        if getattr(latest, "user_id", origin) != origin:
            logger.warning("Background history append skipped: conversation identity mismatch")
            return
        try:
            history = json.loads(latest.history or "[]") if isinstance(latest.history, str) else latest.history
            if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
                raise ValueError("invalid_history_shape")
        except (TypeError, ValueError):
            # Do not replace damaged/missing history with an empty task snapshot.
            logger.warning("Background history append skipped: invalid latest history")
            return
        history = list(history)
        history.append({
            "role": "assistant",
            "content": "[后台任务记录；不是用户发言]\n" + summary_note,
        })
        await conversation_manager.update_conversation(
            origin, conversation_id, history=history,
        )
