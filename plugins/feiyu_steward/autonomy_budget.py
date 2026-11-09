"""Reduce repeated scheduler metadata for one verified autonomous job."""

import copy
import functools
import json
import time
from datetime import datetime, timezone

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.astr_main_agent_resources import PROACTIVE_AGENT_CRON_WOKE_USER_PROMPT

from .policy import OWNER_UMO
from .reconnect import _verified_cron_note

JOB_ID = "d4e91900-f3ca-4b14-a7ac-b7fddab893ca"
MAX_STEPS = 24
BACKGROUND_LABEL = "[后台任务记录；不是用户发言]\n"
GUIDANCE = (
    "\n[调度执行提示]同一工具参数在没有新信息、结果无变化或失败时不要反复重试；"
    "任务完成或没有值得处理的新内容时及时结束。"
)


async def _verified_job(context, event, req):
    """Check the real scheduler occurrence before modifying its request."""
    if not isinstance(event, CronMessageEvent):
        return None
    if event.unified_msg_origin != OWNER_UMO or str(event.get_sender_id()) != OWNER_UMO.rsplit(":", 1)[-1]:
        return None
    meta = event.get_extra("cron_job", {})
    payload = event.get_extra("cron_payload", {})
    if (not isinstance(meta, dict) or not isinstance(payload, dict)
            or meta.get("id") != JOB_ID or meta.get("type") != "active_agent"
            or meta.get("session") != OWNER_UMO or payload.get("session") != OWNER_UMO
            or req is None or not isinstance(getattr(req, "contexts", None), list)):
        return None
    db = getattr(getattr(context, "cron_manager", None), "db", None)
    if not callable(getattr(db, "get_cron_job", None)):
        return None
    try:
        job = await db.get_cron_job(JOB_ID)
        started = getattr(job, "last_run_at", None)
        occurrence = datetime.fromisoformat(meta["run_started_at"])
        if not isinstance(started, datetime) or occurrence.tzinfo is None:
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        saved_payload = getattr(job, "payload", None)
        if (str(getattr(job, "job_id", "")) != JOB_ID
                or getattr(job, "job_type", None) != "active_agent"
                or getattr(job, "status", None) != "running"
                or not isinstance(saved_payload, dict)
                or saved_payload.get("session") != OWNER_UMO
                or abs((started - occurrence).total_seconds()) >= 0.001):
            return None
        return job
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return None


def _request_view(req, event, job):
    """Copy the request and remove only provable duplicate task descriptions."""
    meta = event.get_extra("cron_job", {})
    expected_prompt = PROACTIVE_AGENT_CRON_WOKE_USER_PROMPT.format(
        cron_job=json.dumps(meta, ensure_ascii=False)
    )
    name = getattr(job, "name", None) or JOB_ID
    description = getattr(job, "description", None)
    saved_payload = getattr(job, "payload", {})
    safe_metadata = (
        isinstance(name, str) and isinstance(description, str)
        and description and meta.get("name") == name
        and meta.get("description") == description
        and isinstance(saved_payload, dict)
        and saved_payload.get("note") == description
        and meta.get("note") == description
        and req.prompt == expected_prompt
    )

    view = copy.copy(req)
    view.conversation = copy.copy(req.conversation) if req.conversation else None
    view.contexts = copy.deepcopy(req.contexts)
    view.image_urls = list(req.image_urls or [])
    view.audio_urls = list(req.audio_urls or [])
    view.extra_user_content_parts = list(req.extra_user_content_parts)
    if req.func_tool is not None:
        view.func_tool = copy.copy(req.func_tool)
        view.func_tool.tools = list(req.func_tool.tools)

    if safe_metadata:
        compact_meta = dict(meta)
        del compact_meta["description"]
        view.prompt = PROACTIVE_AGENT_CRON_WOKE_USER_PROMPT.format(
            cron_job=json.dumps(compact_meta, ensure_ascii=False)
        )
        verified = []
        for index, entry in enumerate(view.contexts):
            if (isinstance(entry, dict) and set(entry) == {"role", "content"}
                    and entry.get("role") == "assistant"
                    and isinstance(entry.get("content"), str)
                    and entry["content"].startswith(BACKGROUND_LABEL)
                    and _verified_cron_note(
                        entry["content"][len(BACKGROUND_LABEL):], [job], OWNER_UMO, time.time()
                    )):
                verified.append(index)
        prefix = f"{BACKGROUND_LABEL}[CronJob] {name}: {description}  triggered at "
        short_prefix = f"{BACKGROUND_LABEL}[CronJob] {name}: [任务描述见本次调度信息]  triggered at "
        for index in verified:
            entry = view.contexts[index]
            entry["content"] = short_prefix + entry["content"][len(prefix):]

    view.system_prompt = (view.system_prompt or "") + GUIDANCE
    return view


def install_autonomy_budget(context):
    """Install a reversible builder wrapper without changing other sessions."""
    import astrbot.core.astr_main_agent as main_agent

    original = main_agent.build_main_agent
    state = {
        "active": True,
        "optimized_runs": 0,
        "last_history_chars_before": None,
        "last_history_chars_after": None,
        "last_prompt_chars_before": None,
        "last_prompt_chars_after": None,
    }

    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        event = kwargs.get("event")
        req = kwargs.get("req")
        if not state["active"] or kwargs.get("plugin_context") is not context:
            return await original(*args, **kwargs)
        try:
            job = await _verified_job(context, event, req)
            if not state["active"]:
                job = None
            view = _request_view(req, event, job) if job else None
            counts = None if view is None else {
                "last_history_chars_before": len(json.dumps(req.contexts, ensure_ascii=False)),
                "last_history_chars_after": len(json.dumps(view.contexts, ensure_ascii=False)),
                "last_prompt_chars_before": len(req.prompt or ""),
                "last_prompt_chars_after": len(view.prompt or ""),
            }
        except Exception:
            # A failed optimization must leave the original scheduler path intact.
            view = None
        if view is None or not state["active"]:
            return await original(*args, **kwargs)
        call_kwargs = dict(kwargs)
        call_kwargs["req"] = view
        result = await original(*args, **call_kwargs)
        if result and state["active"]:
            runner = result.agent_runner
            original_steps = runner.step_until_done

            async def limited_steps(max_step):
                budget = min(max_step, MAX_STEPS) if state["active"] else max_step
                async for response in original_steps(budget):
                    yield response

            runner.step_until_done = limited_steps
            state["optimized_runs"] += 1
            state.update(counts)
        return result

    def is_current():
        current = main_agent.build_main_agent
        seen = set()
        while current is not None and id(current) not in seen:
            if current is wrapped:
                return state["active"]
            seen.add(id(current))
            current = getattr(current, "__wrapped__", None)
        return False

    def restore():
        state["active"] = False
        if main_agent.build_main_agent is wrapped:
            main_agent.build_main_agent = original

    def status():
        return {
            "installed": is_current(),
            "optimized_runs": state["optimized_runs"],
            "last_history_chars_before": state["last_history_chars_before"],
            "last_history_chars_after": state["last_history_chars_after"],
            "last_prompt_chars_before": state["last_prompt_chars_before"],
            "last_prompt_chars_after": state["last_prompt_chars_after"],
            "step_limit": MAX_STEPS,
        }

    restore.is_current = is_current
    restore.status = status
    main_agent.build_main_agent = wrapped
    return restore
