"""Route local usage by default; keep independently sourced reports separate."""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from astrbot.api import logger
from astrbot.core.cron.events import CronMessageEvent
from .admin_info import owner_private
from .policy import TZ
from .peak_policy import automatic_peak_paused
from .token_usage_observer import install_provider_usage_observer
from .token_usage_store import TokenUsageStore
from .token_report_delivery import TokenReportDelivery
from .bill_display_policy import PROJECT_USAGE_LABEL, model_bill_result
from .official_token_delivery import OfficialTokenDelivery
from .official_token_query import (MODE, PERIODS, cached_period, historical_day, query_period,
                                   unsupported_period, unavailable)
from .cockpit_token_query import query_period as query_cockpit_period
from .cockpit_token_query import unsupported_period as unsupported_cockpit_period
from .cockpit_token_report import (MODE as COCKPIT_MODE, report_readiness as cockpit_report_readiness,
                                   CLASSIFICATION_BASIS)
from .cockpit_token_query import PERIODS as COCKPIT_PERIODS
from .local_feature_query import (MODE as FEATURE_MODE, PERIODS as FEATURE_PERIODS,
                                  query_period as query_feature_period, unsupported_period as unsupported_feature_period)
from .combined_token_report import (MODE as COMBINED_MODE, make_combined_report,
                                    render_combined_usage_image)

from .deepseek_reconcile_2h import maybe_reconcile_deepseek, reconciliation_status

ROOT = Path(__file__).resolve().parents[4]
OWNER = "0"
BOT_QQ = "0"
from .token_usage_intent import WINDOW_ROUTE_PATTERN, resolve_usage_intent
from .token_usage_diagnostics import record_usage_diagnostic, safe_reason


def parse_usage_window(text):
    return resolve_usage_intent(text)


class TokenAccounting(OfficialTokenDelivery, TokenReportDelivery):
    def init_token_usage(self):
        self.token_usage_store = TokenUsageStore(ROOT)
        self.token_usage_lock = asyncio.Lock()
        self._token_usage_bindings = {}
        self._token_usage_stopped = False
        self._token_usage_event_serial = 0
        self._token_usage_events = {}
        self._token_usage_recent = None

    def observe_token_usage_event(self, event):
        """Track owner turns, including unrelated ones, without persisting text."""
        if not owner_private(event) or getattr(event, "feiyu_internal_diagnostic", False) is True:
            return None
        try:
            identity = getattr(event, "get_message_id", lambda: "")()
            identity = identity or getattr(getattr(event, "message_obj", None), "message_id", "")
        except (AttributeError, TypeError, ValueError):
            identity = None
        if not identity or not isinstance(identity, (str, int)) or len(str(identity)) > 128:
            # An object address can be reused after GC. Without a transport ID,
            # invalidate context and advance the turn; never inherit or save it.
            self._token_usage_event_serial = getattr(self, "_token_usage_event_serial", 0) + 1
            self._token_usage_recent = None
            return None
        key = (str(event.unified_msg_origin), str(identity))
        events = getattr(self, "_token_usage_events", None)
        if not isinstance(events, dict):
            events = self._token_usage_events = {}
        if key not in events:
            self._token_usage_event_serial = getattr(self, "_token_usage_event_serial", 0) + 1
            events[key] = self._token_usage_event_serial
            if len(events) > 128:
                events.pop(next(iter(events)))
        return events[key]

    def _recent_token_usage(self, event):
        serial = self.observe_token_usage_event(event)
        recent = getattr(self, "_token_usage_recent", None)
        if (not isinstance(recent, dict) or serial != recent.get("serial", -2) + 1
                or time.monotonic() - recent.get("at", 0) > 600):
            return None
        return recent

    def _remember_token_usage(self, event, parsed, result):
        serial = self.observe_token_usage_event(event)
        if (serial is None or serial != getattr(self, "_token_usage_event_serial", None)
                or result.get("ok") is not True or parsed.get("period") == "status"):
            return
        summary = result.get("summary")
        if not isinstance(summary, dict):
            return
        start, end = summary.get("from"), summary.get("until")
        # Follow-ups use the actual frozen local interval. An official cached
        # period has no such endpoints, so no time is inherited from it.
        period = ({"ok": True, "period": "range", "start": start, "end": end}
                  if isinstance(start, str) and isinstance(end, str) else None)
        self._token_usage_recent = {"serial": serial, "at": time.monotonic(),
                                    "source": parsed["source"], "period": period,
                                    "feature": parsed.get("feature", ""),
                                    "purposes": list(parsed.get("purposes", []))}

    def restore_token_usage_observers(self):
        for binding in getattr(self, "_token_usage_bindings", {}).values():
            binding[1]()
        self._token_usage_bindings = {}

    def ensure_token_usage_observers(self):
        if getattr(self, "_token_usage_stopped", False):
            return False
        bindings = self._token_usage_bindings
        providers = self.context.get_all_providers()
        current = {id(provider): provider for provider in providers}
        for key in list(bindings):
            if key not in current:
                bindings.pop(key)[1]()
        for key, provider in current.items():
            old = bindings.get(key)
            if old and old[1].is_current():
                continue
            if old:
                old[1]()
            try:
                restore = install_provider_usage_observer(provider, self.token_usage_store.record)
            except (TypeError, ValueError, AttributeError):
                continue
            bindings[key] = (provider, restore)
        return bool(bindings)

    def token_usage_status(self, day=None):
        # Status does not launch a browser or conflate the official account
        # with the local QQBot record.
        result = {"ok": True, "report_mode": FEATURE_MODE, "status": "ready_for_query",
                  "source": "feiyu_local_feature_ledger",
                  "note": "普通查询默认分别展示肥鱼与项目用量的本机记录；自动日报仍为肥鱼昨日用量。"}
        return {**result, "provider_observers": len(self._token_usage_bindings),
                "daily_report_hour": 8, "daily_report_minute": 15,
                "report_scope": "feiyu_local_feature_ledger", "supported_periods": list(FEATURE_PERIODS),
                "report_destination": "owner_private", "report_owner": OWNER,
                "report_format": "image_with_text_fallback", "legacy_queues_paused": True,
                "query_sources": ["official", "cockpit", "feiyu", "local", "both"], "default_query_source": "both",
                "feiyu_supported_periods": list(FEATURE_PERIODS),
                "cockpit_supported_periods": list(COCKPIT_PERIODS), "cockpit_minimum_window_minutes": 1,
                "daily_receipts": self.state.get("token_usage_reports", {})}

    def token_usage_window_status(self, hours, *, now=None):
        return unsupported_period()

    def token_usage_frozen_result(self, window, *, now=None):
        return {**unsupported_period(), "status": "legacy_exact_window_paused"}

    async def official_token_usage_status(self, action="today", day="", *, refresh=True):
        now = datetime.now(TZ)
        if action in ("window", "hours", "minutes"):
            return unsupported_period()
        if action == "status":
            snapshots = [cached_period(period) for period in ("today", "yesterday", "last7")]
            available = [result for result in snapshots if result.get("ok")]
            return (max(available, key=lambda item: item["summary"]["observed_at"]) if available else
                    {"ok": True, "available": False, "report_mode": MODE, "status": "ready_for_query",
                     "source": "official_website", "queued": False,
                     "note": "可查看已缓存的官网周期数据。"})
        if action in ("week", "last_7_days"):
            action = "last7"
        if action == "day":
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return unsupported_period()
            try:
                target = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                return unsupported_period()
            if target == now.date():
                action = "today"
            elif target == now.date() - timedelta(days=1):
                action = "yesterday"
            else:
                return await asyncio.to_thread(historical_day, day)
        if action not in ("today", "yesterday", "last7"):
            return unsupported_period()
        return await query_period(action, refresh=refresh)

    async def query_token_usage(self, action="today", day="", *, source="both", refresh=True,
                                hours=0, minutes=0, start="", end="", feature=""):
        if source not in ("feiyu", "local") and feature:
            return {"ok": False, "queued": False, "status": "unsupported_feature_source",
                    "note": "功能分类只能查询肥鱼本机已记录的部分。"}
        if source == "official":
            if hours != 0 or minutes != 0 or start != "" or end != "":
                return unsupported_period()
            # Interactive reads never wait for or start an official scrape.
            # The periodic synchronizer can refresh the same cache separately.
            return await self.official_token_usage_status(action, day, refresh=False)
        if source == "both":
            if action == "status":
                if day or hours != 0 or minutes != 0 or start != "" or end != "" or feature:
                    return unsupported_feature_period()
                return {"ok": True, "report_mode": COMBINED_MODE, "status": "ready_for_query",
                        "source": "feiyu_and_cockpit_local_ledgers", "queued": False,
                        "supported_periods": list(FEATURE_PERIODS),
                        "note": "可分别查看肥鱼本机功能与项目用量中的项目用途。"}
            if action in ("week", "last_7_days"):
                action = "last7"
            # Capture the clock exactly once so both readers use identical
            # exclusive boundaries, even at midnight or during a rolling query.
            now = datetime.now(TZ).replace(microsecond=0)
            feiyu, cockpit = await asyncio.gather(
                asyncio.to_thread(query_feature_period, ROOT, action, day, now=now, hours=hours, minutes=minutes,
                                  start=start, end=end),
                asyncio.to_thread(query_cockpit_period, action, day, now=now, hours=hours, minutes=minutes,
                                  start=start, end=end),
            )
            return make_combined_report(feiyu, cockpit)
        if source in ("feiyu", "local"):
            if action == "status":
                if day or hours != 0 or minutes != 0 or start != "" or end != "" or feature:
                    return unsupported_feature_period()
                return {"ok": True, "report_mode": FEATURE_MODE, "status": "ready_for_query",
                        "deepseek_reconciliation": await asyncio.to_thread(reconciliation_status, ROOT),
                        "source": "feiyu_local_feature_ledger", "supported_periods": list(FEATURE_PERIODS),
                        "queued": False, "note": "可按功能查看肥鱼本机已记录部分，记录可能不完整。"}
            if action in ("week", "last_7_days"):
                action = "last7"
            return await asyncio.to_thread(query_feature_period, ROOT, action, day, hours=hours, minutes=minutes,
                                           start=start, end=end, feature=feature)
        if source != "cockpit":
            return {"ok": False, "queued": False, "status": "unsupported_source",
                    "note": "请指定肥鱼本机、项目用量、两者，或明确查询官网。"}
        if action == "status":
            if day or hours != 0 or minutes != 0 or start != "" or end != "":
                return unsupported_cockpit_period()
            return {"ok": True, "report_mode": COCKPIT_MODE,
                    "source": "cockpit_request_ledger", "status": "ready_for_query",
                    "supported_periods": list(COCKPIT_PERIODS), "minimum_window_minutes": 1,
                    "maximum_window_days": 30, "queued": False,
                    "note": "本机请求记录可按分钟、小时、日期或带日期的起止时刻查询。"}
        if action in ("week", "last_7_days"):
            action = "last7"
        return await asyncio.to_thread(query_cockpit_period, action, day, hours=hours, minutes=minutes, start=start, end=end)

    def _token_purpose_presentation(self, result, purposes):
        """Project validated Cockpit buckets to text without changing totals."""
        summary = result.get("summary") if isinstance(result, dict) else None
        if (result.get("report_mode") != COCKPIT_MODE or not cockpit_report_readiness(summary)["ready"]
                or not isinstance(summary.get("classification"), dict) or summary["classification"].get("valid") is not True
                or not purposes or not set(purposes) <= {"academic_poc", "feiyu_bot"}):
            return {"ok": False, "sent": False, "queued": False,
                    "note": "这些用途的本机记录暂时无法核实，请稍后再查。"}
        rows = {row.get("key"): row for row in summary.get("by_project", []) if isinstance(row, dict)}
        if any(key not in rows for key in purposes):
            return {"ok": False, "sent": False, "queued": False,
                    "note": "当前无法核实这些用途的分类，暂不猜测。"}
        lines = [f"{PROJECT_USAGE_LABEL} · 项目用途", f"时段：{summary['from']} 至 {summary['until']}（北京时间，结束时刻不含）"]
        if "coverage_incomplete" in summary.get("issues", []):
            retained = summary.get("retained_since")
            lines.append("本机记录起点：" + retained + "。" if retained else "本机记录不能覆盖整个查询时段。")
        for key in purposes:
            row = rows[key]
            totals, pricing = row["totals"], row["pricing"]
            amount = (f"已确认 {totals['total_tokens']:,} token" if totals["known_usage_calls"] or not totals["calls"]
                      else "用量待回传")
            line = (f"{row['label']}：{amount}，{totals['calls']:,} 次请求；"
                    f"{totals['unknown_usage_calls']:,} 次未回传完整用量")
            if pricing.get("valid") and pricing.get("priced_calls", 0):
                line += (f"；按官方单价折算已计价部分 ¥{pricing['known_cny']:,.6f}"
                         f"（{pricing['priced_calls']:,} 次已计价，{pricing['unpriced_calls']:,} 次未计价）")
            else:
                line += "；费用待核实"
            lines.append(line + "。")
        if summary.get("classification", {}).get("unclassified_calls", 0):
            lines.append("另有未归类请求，不能排除其中包含这些用途。")
        lines += [CLASSIFICATION_BASIS, "肥鱼相关是项目用途，与肥鱼本机功能用量分别统计。"]
        return {**result, "purpose_text_only": True, "selected_purposes": list(purposes),
                "text": "\n".join(lines)}

    def token_usage_report_image(self, result):
        if result.get("purpose_text_only") is True:
            return None
        if result.get("report_mode") == COMBINED_MODE:
            try:
                output = (ROOT / "data/token-usage/reports").resolve()
                path = Path(render_combined_usage_image(result, output_dir=output)).resolve()
                return path if path.parent == output and path.suffix.lower() == ".png" and path.is_file() else None
            except Exception as error:
                logger.warning("Combined local usage image unavailable (%s)", type(error).__name__)
                return None
        if result.get("report_mode") == FEATURE_MODE:
            try:
                from .feature_token_image import render_feature_usage_image
                output = (ROOT / "data/token-usage/reports").resolve()
                path = Path(render_feature_usage_image(result["summary"], output_dir=output)).resolve()
                return path if path.parent == output and path.suffix.lower() == ".png" and path.is_file() else None
            except Exception as error:
                logger.warning("Feature usage image unavailable (%s)", type(error).__name__)
                return None
        if result.get("report_mode") == COCKPIT_MODE:
            try:
                from .cockpit_token_report import render_cockpit_usage_image
                output = (ROOT / "data/token-usage/reports").resolve()
                path = Path(render_cockpit_usage_image(result["summary"], output_dir=output)).resolve()
                return path if path.parent == output and path.suffix.lower() == ".png" and path.is_file() else None
            except Exception as error:
                logger.warning("Local usage image unavailable (%s)", type(error).__name__)
                return None
        if result.get("ok") is not True or result.get("report_mode") != MODE or not isinstance(result.get("summary"), dict):
            return None
        try:
            from .official_token_image import render_official_usage_image
            output = (ROOT / "data/token-usage/reports").resolve()
            path = Path(render_official_usage_image(result["summary"], output_dir=output)).resolve()
            if path.parent != output or path.suffix.lower() != ".png" or not path.is_file():
                raise ValueError("invalid_usage_image")
            return path
        except Exception as error:
            logger.warning("Token usage image unavailable (%s)", type(error).__name__)
            return None

    async def send_token_usage_image(self, event, result):
        return await self.send_official_token_report(event, result)

    @staticmethod
    def _token_intent_error(parsed):
        reason = parsed.get("reason", "explicit_usage_request_required") if isinstance(parsed, dict) else "explicit_usage_request_required"
        notes = {
            "ambiguous_source": "来源不够明确：请说明是肥鱼本机、项目用量、两者，还是单独查看官网。",
            "context_unavailable": "上一份用量报告的范围已无法确定，请说明这次的来源和时间。",
            "ambiguous_period": "无法确定要查询的时段，请说明具体日期或起止范围。",
            "unsupported_period": "这段时间无法准确确定；可说今天、昨天、近7天、指定日期、最近1分钟至30天或带日期的起止时刻。",
            "invalid_usage_window": "无法确定这段时间，请说明准确的时长或起止时刻。",
            "unsupported_feature_filter": "这项筛选暂不支持，请说明是否查看全部明细。",
            "unverified_scope": "这句话中的查询条件无法全部识别，请说明要查的来源、时间和功能或用途。",
            "status_scope_unverified": "无法确认这是状态查询，请说明要检查的用量来源和状态。",
            "source_feature_unavailable": "肥鱼本机功能与项目用途是不同分类；请分别说明要查的来源和分类。",
            "explicit_usage_request_required": "请说明需要查询的用量与范围。",
            "owner_private_only": "用量查询仅供主人在私聊中使用。",
            "non_query_intent": "这条消息是讨论、否定或没有明确查询意图，未查询用量，也未发送报告。",
        }
        return {"ok": False, "sent": False, "queued": False, "reason": reason,
                "note": notes.get(reason, "查询范围无法确定，请补充说明。")}

    async def _token_query_failure(self, result, *, entrypoint, delivery=False):
        reason = safe_reason(result.get("reason") or result.get("status"),
                             "delivery_unconfirmed" if delivery else "query_unavailable")
        try:
            await asyncio.to_thread(record_usage_diagnostic, ROOT, entrypoint, reason)
        except Exception:
            # A diagnostic failure must not hide the actual query result.
            pass
        note = result.get("note")
        if not isinstance(note, str) or not note:
            notes = {
                "local_report_unavailable": "本机报告暂时无法生成，未确认发送成功。",
                "local_reports_unavailable": "本机报告暂时无法生成，未确认发送成功。",
                "report_intent_persist_failed": "报告发送准备记录未能保存，未继续发送。",
                "receipt_store_unavailable": "报告回执记录暂时不可用，未确认发送成功，不自动重发。",
                "qq_unavailable": "QQ 连接暂时不可用，未确认报告发送成功。",
                "qq_identity_unavailable": "暂时无法确认 QQ 发送身份，未继续发送报告。",
                "official_login_required": "官网登录状态已失效，暂时无法读取官网账单。",
                "official_data_unavailable": "官网账单数据暂时不可用。",
            }
            note = notes.get(reason, "报告未确认发送成功，不自动重发。" if delivery
                             else "本机用量暂时无法查询，请稍后重试。")
        return {**result, "reason": reason, "note": note,
                "next_action": "explain_reason_without_alternate_query",
                "instruction": "向主人说明note；不得改用执行或文件工具绕过查询，不扩大来源，不自行补算或重发报告。"}

    async def _execute_token_usage_intent(self, event, parsed, *, send_image, entrypoint="model"):
        if parsed.get("ok") is not True:
            return await self._token_query_failure(self._token_intent_error(parsed), entrypoint=entrypoint)
        result = await self.query_token_usage(parsed["period"], parsed.get("day", ""), source=parsed["source"],
            hours=parsed.get("hours", 0), minutes=parsed.get("minutes", 0),
            start=parsed.get("start", ""), end=parsed.get("end", ""), feature=parsed.get("feature", ""))
        if result.get("ok") is True and parsed.get("purposes"):
            result = self._token_purpose_presentation(result, parsed["purposes"])
        if result.get("ok") is not True:
            return await self._token_query_failure(result, entrypoint=entrypoint)
        if send_image and parsed["period"] != "status" and result.get("ok") and isinstance(result.get("summary"), dict):
            delivery = await self.send_official_token_report(event, result)
            if delivery.get("ok") is True and delivery.get("sent") is True and not delivery.get("duplicate"):
                self._remember_token_usage(event, parsed, result)
            if delivery.get("ok") is not True:
                return await self._token_query_failure(delivery, entrypoint=entrypoint, delivery=True)
            return delivery
        self._remember_token_usage(event, parsed, result)
        return result

    def prepare_token_usage_display(self, event):
        """Prevent streaming only for owner requests the usage parser can handle."""
        if not owner_private(event):
            return
        text = getattr(event, "get_message_str", lambda: getattr(event, "message_str", ""))()
        if not isinstance(text, str):
            return
        try:
            parsed = resolve_usage_intent(text, recent=self._recent_token_usage(event), via_model=True)
        except (AttributeError, TypeError, ValueError):
            return
        setter = getattr(event, "set_extra", None)
        if parsed is not None and callable(setter):
            setter("enable_streaming", False)

    async def token_usage_window_command(self, event, *, send_image=False):
        if not owner_private(event):
            return None
        text = getattr(event, "get_message_str", lambda: getattr(event, "message_str", ""))()
        parsed = resolve_usage_intent(text, recent=self._recent_token_usage(event))
        if parsed is None:
            return None
        result = await self._execute_token_usage_intent(event, parsed, send_image=send_image, entrypoint="direct")
        if result.get("ok") is not True:
            return result.get("note") or "查询范围无法确定。"
        return result

    async def token_usage_tool(self, event, action="today", day="", hours=0, send_image=False, source="both",
                               minutes=0, start="", end="", feature="", request_kind="query", unsupported_filter=""):
        if not owner_private(event):
            result = await self._token_query_failure(self._token_intent_error({"reason": "owner_private_only"}),
                                                    entrypoint="model")
            return json.dumps(model_bill_result(result), ensure_ascii=False)
        if isinstance(event, CronMessageEvent):
            # Scheduler metadata is not a human query. Only the existing job's
            # current, persisted occurrence may inspect background status.
            try:
                from .autonomy_budget import _verified_job

                job = await _verified_job(self.context, event, SimpleNamespace(contexts=[]))
                payload = getattr(job, "payload", None)
                meta = event.get_extra("cron_job", {})
                note = payload.get("note") if isinstance(payload, dict) else None
                verified = (
                    job is not None and getattr(job, "enabled", None) is True
                    and getattr(job, "persistent", None) is True
                    and getattr(job, "run_once", None) is False
                    and getattr(job, "cron_expression", None) == "*/15 * * * *"
                    and getattr(job, "timezone", None) == "Asia/Shanghai"
                    and isinstance(meta, dict) and isinstance(note, str) and bool(note)
                    and payload == event.get_extra("cron_payload", {})
                    and note == getattr(job, "description", None) == meta.get("description")
                    and note == meta.get("note") == event.get_message_str()
                    and meta.get("name") == getattr(job, "name", None)
                )
                paused = automatic_peak_paused() is not False
            except Exception:
                verified = False
            if not verified:
                return json.dumps(model_bill_result(self._token_intent_error(
                    {"reason": "status_scope_unverified"})), ensure_ascii=False)
            # A 15-minute wake-up must not become another daily/reconciliation
            # report sender or consume the human owner's follow-up context.
            try:
                result = ({"ok": True, "status": "automatic_peak_or_calendar_paused"} if paused else
                          await self.query_token_usage("status", source="feiyu", refresh=False))
                if not isinstance(result, dict):
                    raise TypeError("invalid_status_result")
            except Exception:
                result = {"ok": False, "status": "status_unavailable", "note": "后台用量状态暂时无法核实。"}
            return json.dumps(model_bill_result({
                **result, "sent": False, "queued": False, "automatic_status_only": True,
                "instruction": "这是后台状态核对；自动日报和两小时对账由独立监控负责。"
                               "不得据此发送Token账单、另建账单任务或重复执行对账。",
            }), ensure_ascii=False)
        text = getattr(event, "get_message_str", lambda: getattr(event, "message_str", ""))()
        parsed = resolve_usage_intent(text, recent=self._recent_token_usage(event), via_model=True,
                                      request_kind=request_kind, unsupported_filter=unsupported_filter)
        if not parsed:
            result = await self._token_query_failure(self._token_intent_error({"reason": "non_query_intent"}), entrypoint="model")
            return json.dumps(model_bill_result(result), ensure_ascii=False)
        # Mark only an actual resolved usage-tool turn; unrelated chat is untouched.
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("feiyu_token_report_display", True)
        # Model-proposed action/source/time/feature are descriptive hints only;
        # the original message and adjacent verified query determine the scope.
        result = await self._execute_token_usage_intent(event, parsed, send_image=send_image)
        return json.dumps(model_bill_result(result), ensure_ascii=False)

    async def maybe_send_token_usage_report(self, *, now=None, dry_run=False):
        now = now or datetime.now(TZ)
        if now.tzinfo is None:
            raise ValueError("timezone_required")
        now = now.astimezone(TZ)
        if automatic_peak_paused(now.timestamp()):
            return {"ok": True, "sent": False, "status": "peak_tariff"}
        activation = self.state.get("feiyu_local_daily_start_date_v1")
        if activation is None:
            first_date = now.date() if (now.hour, now.minute) < (8, 15) else now.date() + timedelta(days=1)
            activation = first_date.isoformat()
            if not dry_run:
                self.state["feiyu_local_daily_start_date_v1"] = activation
                self.save()
        try:
            if not isinstance(activation, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", activation):
                raise ValueError
            if now.date() < datetime.strptime(activation, "%Y-%m-%d").date():
                return {"ok": True, "sent": False, "status": "source_migration_hold"}
        except (TypeError, ValueError):
            return {"ok": False, "sent": False, "status": "source_configuration_required"}
        if (now.hour, now.minute) < (8, 15):
            return {"ok": True, "status": "not_due"}
        day = (now.date() - timedelta(days=1)).isoformat()
        identity = "feiyu_local_v1:" + day
        previous = self.state.get("token_usage_reports", {}).get(identity)
        if isinstance(previous, dict) and not (
            previous.get("status") == "blocked_before_send" and previous.get("peak_guard_v1") is True
        ):
            return {"ok": previous.get("status") == "sent", "status": previous.get("status"), "day": day, "duplicate": True}
        checks = self.state.get("feiyu_token_daily_checks_v1", {})
        check = checks.get(day, {}) if isinstance(checks, dict) else {}
        if not dry_run and (check.get("attempts", 0) >= 3 or check.get("next_check", 0) > now.timestamp()):
            return {"ok": True, "sent": False, "status": "local_update_waiting", "day": day}
        if not dry_run:
            self.state.setdefault("feiyu_token_daily_checks_v1", {})[day] = {
                "attempts": check.get("attempts", 0) + 1, "next_check": now.timestamp() + 3600}
            self.save()
        result = await asyncio.to_thread(query_feature_period, ROOT, "yesterday", now=now)
        if not result.get("ok"):
            return {**result, "day": day}
        if result.get("summary", {}).get("from", "")[:10] != day:
            return {"ok": False, "sent": False, "status": "local_report_interval_mismatch"}
        if automatic_peak_paused():
            return {"ok": True, "sent": False, "status": "peak_tariff", "day": day}
        return await self.send_official_token_report(None, result, source="daily", daily_day=identity, dry_run=dry_run)

    async def retry_pending_token_reports(self, *, now=None, limit=4):
        # Old exact-minute/combined/local queues remain byte-preserved. Source
        # changes cannot turn a historic or unknown operation into a new send.
        return {"ok": True, "checked": 0, "sent": 0, "status": "legacy_queues_paused"}

    async def token_usage_monitor(self):
        while not self._token_usage_stopped:
            try:
                self.ensure_group_provider_budget()
                self.ensure_token_usage_observers()
                if not automatic_peak_paused():
                    await maybe_reconcile_deepseek(self, ROOT)
                await self.maybe_send_token_usage_report()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Token usage monitor unavailable (%s)", type(error).__name__)
            await asyncio.sleep(30)
