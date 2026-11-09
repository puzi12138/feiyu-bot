"""Recover a missed, evidence-backed recommendation through the native queue.

This is an authenticated local maintenance operation, not an LLM write tool.
No caller-supplied comment or private conversation enters the public draft.
"""

# External plugin failures are reported by type without raw response details.
# ruff: noqa: BLE001

import importlib
import json
import math
import re
from datetime import datetime

from .availability import refresh_availability
from .bili_conversation import owner_mention_block
from .bili_journal_gate import bvid_for_committed_note, journaled_bvid
from .peak_policy import automatic_peak_paused, peak_active
from .policy import PUBLIC_COMMENT_SENSITIVE, TZ, interest_driven


_PRIVATE_REASON = re.compile(
    r"主人|私聊(?:里|中|记录)|我们(?:以前|之前|上次)|"
    r"你(?:上次|之前|曾经|告诉我|肯定|一定|喜欢)|"
    r"内部(?:任务|记录|状态)|系统提示|(?:API|访问|认证)[ _-]?(?:key|token|密钥)",
    re.IGNORECASE,
)
_GENERIC_REASON = re.compile(
    r"(?:看到|看了|发现)?(?:这个|这段|这部)?视频[，,。!！\s]*"
    r"(?:挺好看|很有趣|挺有意思|不错|想分享给你|想推荐给你|值得一看)[，,。!！\s]*"
)


def public_recommendation_reason(value):
    """Validate a reason from this source-only review, never a private note.

    The caller supplies the current review's reason before it is merged into a
    private journal. Historical watch reasons may contain owner memories and
    are deliberately not used as a fallback. No additional model call is made.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 800:
        return ""
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        return ""
    text = re.sub(r"\s+", " ", value).strip(' "“”\'')
    if (not 8 <= len(text) <= 260 or not any("\u4e00" <= char <= "\u9fff" for char in text)
            or re.search(r"https?://|www\.|[@＠]|<[^>]+>|```", text, re.IGNORECASE)
            or PUBLIC_COMMENT_SENSITIVE.search(text) or _PRIVATE_REASON.search(text)
            or _GENERIC_REASON.fullmatch(text)):
        return ""
    return text


def _share_generation(steward):
    """Read the exact current incident generation for one automatic share."""
    try:
        hooks = steward._incident_hooks
        features = ("bili_journal", "bili_share")
        if hooks._blocked(*features):
            return None
        return hooks._resume_snapshot(features)
    except Exception:
        return None


def _automatic_bili_allowed():
    """Keep autonomous recommendations inside the verified daytime window."""
    try:
        from .bili_journal_review import journal_generation_allowed

        return (8 <= datetime.now(TZ).hour < 22 and automatic_peak_paused() is False
                and journal_generation_allowed())
    except Exception:
        return False


def _native_modules(bili):
    package = bili.__class__.__module__.rsplit(".", 1)[0]
    return (importlib.import_module(package + ".core.config"),
            importlib.import_module(package + ".core.content_protocol"))


def _evaluation(row, interest=False):
    """Recover only fields already validated by the native evaluation schema.

    Older native watch logs omit recommend_owner itself. Their strict parser
    requires a nonempty reason exactly when recommend_owner is true, so that
    persisted reason is the compatibility marker; no new rating is invented.
    """
    source = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else row
    score = source.get("score")
    reason = source.get("recommend_reason")
    if (isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score) or not (0 if interest else 7) <= score <= 10
            or not isinstance(reason, str) or not reason.strip()
            or len(reason) > 4096 or source.get("recommend_owner", True) is not True):
        return None
    return {"score": score, "recommend_owner": True, "recommend_reason": reason.strip()}


def _recommended_state(bili, watch, ledger, now, interest=False):
    """Read the native ledger without triggering migration writes in previews."""
    records = {str(key): value for key, value in ledger.items() if isinstance(value, dict)}
    for row in watch:
        if (isinstance(row, dict) and row.get("bvid")
                and any(bili._is_owner_recommend_action(action) for action in row.get("actions", []) or [])):
            records.setdefault(str(row["bvid"]), {
                "time": row.get("recommended_at") or row.get("time", ""),
                "title": row.get("title", "")})
    today = now.date().isoformat()
    count = sum(str(row.get("time", "")).startswith(today) for row in records.values())
    try:
        gap = max(1800, int(bili.config.get("RECOMMEND_OWNER_MIN_GAP_MINUTES", 120)) * 60)
    except (TypeError, ValueError):
        gap = 7200
    for row in records.values():
        try:
            stamp = datetime.strptime(str(row["time"]), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            if now.timestamp() - stamp.timestamp() < gap:
                return records, count, "native_recommendation_cooldown"
        except (ValueError, TypeError, KeyError):
            continue
    return records, count, ""


class BiliRecommendationRecovery:
    async def retry_bili_recommendation(self, bvid, dry_run=True, *, new_journal=None,
                                        public_reason=None):
        """Recover a current-day recommendation, or one newly journaled video."""
        from .bili_cron_guard import _CRON_SHARE_SCOPE, cron_dispatch_allowed

        cron_scope = _CRON_SHARE_SCOPE.get()
        bvid = str(bvid or "").strip()
        base = {"bvid": bvid, "recommended": False, "dry_run": bool(dry_run)}

        def result(status, **fields):
            return {**base, "status": status, "ok": status in {"preview", "sent", "already_recommended"}, **fields}

        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            return result("invalid_bvid")
        if peak_active():
            return result("peak_tariff")
        if new_journal is not None and (
                not await cron_dispatch_allowed(cron_scope) or not _automatic_bili_allowed()):
            return result("automatic_daytime_unavailable")
        generation = _share_generation(self) if new_journal is not None else None
        if new_journal is not None and generation is None:
            return result("incident_generation_unavailable")
        if not journaled_bvid(bvid):
            return result("journal_required")
        bili = self.plugin("astrbot_plugin_bilibili_ai_bot")
        if not bili:
            return result("plugin_unavailable")
        async with self.lock:
            if new_journal is not None and (
                    not await cron_dispatch_allowed(cron_scope) or not _automatic_bili_allowed()):
                return result("automatic_daytime_unavailable")
            now = datetime.now(TZ)
            if refresh_availability(self.state, now):
                self.save()
            block = owner_mention_block(self.state, now)
            if block:
                return result(block)
            try:
                config, protocol = _native_modules(bili)
                watch = bili._load_json(config.WATCH_LOG_FILE, [])
                ledger = bili._load_json(config.OWNER_RECOMMEND_LOG_FILE, {})
                if not isinstance(watch, list) or not isinstance(ledger, dict):
                    return result("invalid_native_logs")
                matching = [row for row in watch if isinstance(row, dict) and row.get("bvid") == bvid]
                if not matching:
                    return result("not_watched_today")
                row = matching[-1]
                watched = datetime.strptime(str(row.get("time", "")), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                new_note_matches = (
                    isinstance(new_journal, tuple) and len(new_journal) == 2
                    and bvid_for_committed_note(*new_journal) == bvid
                )
                share_reason = public_recommendation_reason(public_reason) if new_note_matches else ""
                if new_journal is not None and not share_reason:
                    return result("public_recommendation_reason_unavailable")
                if watched > now or (watched.date() != now.date() and not new_note_matches):
                    return result("not_watched_today")
                basis = row.get("analysis_basis")
                if (basis not in {"video_media", "subtitles"}
                        or row.get("evaluation_status") == "failed"
                        or not str(row.get("content_summary", "")).strip()):
                    return result("insufficient_watch_evidence")
                evaluation = _evaluation(row, interest=interest_driven(self.state))
                if evaluation is None:
                    return result("not_recommendable")
                records, count, native_block = _recommended_state(
                    bili, watch, ledger, now, interest=interest_driven(self.state))
                if bvid in records or bvid in getattr(bili, "_owner_recommended_bvids", set()):
                    return result("already_recommended", recommended=True)
                if native_block:
                    return result(native_block)
                if not bili._can_recommend_owner(evaluation, evaluation["score"], count):
                    return result("native_recommendation_gate")
                if bili._owner_recommend_delivery() not in {"comment", "both", "all"}:
                    return result("comment_delivery_disabled")
                if new_journal is not None and (
                        not await cron_dispatch_allowed(cron_scope) or not _automatic_bili_allowed()):
                    return result("automatic_daytime_unavailable")
                if new_journal is not None and _share_generation(self) != generation:
                    return result("incident_generation_changed")
                data, _ = await bili._http_get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
                view = data.get("data") if isinstance(data, dict) else None
                if (data.get("code") != 0 or not isinstance(view, dict) or view.get("bvid") != bvid
                        or isinstance(view.get("aid"), bool) or not str(view.get("aid", "")).isdigit()
                        or int(view["aid"]) <= 0 or not str(view.get("title", "")).strip()
                        or view.get("state", 0) != 0):
                    return result("video_unavailable")
                aid = int(view["aid"])
                existing = self.state.get("bili_owner_comments", {}).get(str(aid))
                if existing:
                    if existing.get("status") == "sent" and existing.get("rpid"):
                        return result("already_recommended", recommended=True, aid=aid, rpid=existing["rpid"])
                    return result("previous_comment_receipt_unresolved", aid=aid)
                guard = getattr(self, "_bili_mention_guard", None)
                if not guard or guard[0] is not bili or bili._send_comment is not guard[2]:
                    return result("mention_guard_unavailable")
                # This reason came from the current source-only review before
                # private journal content was formed. Do not fall back to a
                # saved watch reason or copy any private journal field.
                text = share_reason if new_journal is not None else "看到这个视频，想分享给你。"
                comment = "@一团普子 " + text
                preview = {"aid": aid, "title": str(view["title"]), "score": evaluation["score"],
                           "analysis_basis": basis, "evaluation_basis": "validated_watch_reason",
                           "comment": comment}
                if dry_run:
                    return result("preview", **preview)
                # Recheck before the native queue dispatches.
                if owner_mention_block(self.state, datetime.now(TZ)):
                    return result("availability_changed")
                if not bili._can_recommend_owner(evaluation, evaluation["score"], count):
                    return result("native_recommendation_gate")
                if peak_active():
                    return result("peak_tariff")
                if new_journal is not None and (
                        not await cron_dispatch_allowed(cron_scope) or not _automatic_bili_allowed()):
                    return result("automatic_daytime_unavailable")
                if new_journal is not None and _share_generation(self) != generation:
                    return result("incident_generation_changed")
                if not journaled_bvid(bvid):
                    return result("journal_required")

                async def guarded_send():
                    if new_journal is not None and not await cron_dispatch_allowed(cron_scope):
                        return False
                    if (peak_active() or not journaled_bvid(bvid)
                            or (new_journal is not None and (
                                _share_generation(self) != generation
                                or bvid_for_committed_note(*new_journal) != bvid
                                or public_recommendation_reason(share_reason) != share_reason))
                            or (new_journal is not None and not _automatic_bili_allowed())):
                        return False
                    latest_watch = bili._load_json(config.WATCH_LOG_FILE, [])
                    latest_ledger = bili._load_json(config.OWNER_RECOMMEND_LOG_FILE, {})
                    if not isinstance(latest_watch, list) or not isinstance(latest_ledger, dict):
                        return False
                    latest_records, latest_count, cooldown = _recommended_state(
                        bili, latest_watch, latest_ledger, datetime.now(TZ),
                        interest=interest_driven(self.state))
                    if (cooldown or bvid in latest_records
                            or not bili._can_recommend_owner(evaluation, evaluation["score"], latest_count)
                            or owner_mention_block(self.state, datetime.now(TZ))):
                        return False
                    return await bili._send_comment(aid, comment)

                outcome = await bili._execute_proactive_action(
                    "proactive_owner_comment:" + bvid, "proactive_owner_recommend", bvid,
                    guarded_send)
                receipt = self.state.get("bili_owner_comments", {}).get(str(aid), {})
                if not (outcome.success and receipt.get("status") == "sent" and receipt.get("rpid")):
                    reason = str(getattr(outcome, "reason", ""))
                    if not re.fullmatch(r"[a-z_]+(?::[a-z0-9_]+){0,2}", reason):
                        reason = "native_action_not_confirmed"
                    return result("unknown" if receipt.get("status") in {"pending", "unknown"}
                                  or getattr(outcome, "state", "") == "unknown" else "not_sent",
                                  aid=aid, action_state=getattr(outcome, "state", ""),
                                  reason=reason)
                # Merge fresh files so concurrent native watches are not lost.
                sent_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
                fresh_ledger = bili._load_json(config.OWNER_RECOMMEND_LOG_FILE, {})
                fresh_ledger.update(records)
                fresh_ledger[bvid] = {"time": sent_at, "title": str(view["title"])}
                bili._save_json(config.OWNER_RECOMMEND_LOG_FILE, fresh_ledger)
                fresh_watch = bili._load_json(config.WATCH_LOG_FILE, [])
                for item in fresh_watch:
                    if isinstance(item, dict) and item.get("bvid") == bvid and item.get("time") == row["time"]:
                        actions = item.setdefault("actions", [])
                        if "📢评论区推荐给主人" not in actions:
                            actions.append("📢评论区推荐给主人")
                        item["recommended_at"] = sent_at
                bili._save_json(config.WATCH_LOG_FILE, fresh_watch)
                bili._owner_recommended_bvids = set(getattr(bili, "_owner_recommended_bvids", set())) | {bvid}
                bili._owner_last_recommended_at = sent_at
                return result("sent", recommended=True, rpid=receipt["rpid"], **preview)
            except Exception as error:
                # Exceptions can contain cookies, signed URLs or raw responses.
                receipt = self.state.get("bili_owner_comments", {}).get(str(locals().get("aid", "")), {})
                if receipt.get("status") == "sent" and receipt.get("rpid"):
                    return result("sent", recommended=True, aid=aid, rpid=receipt["rpid"],
                                  metadata_synced=False, error=type(error).__name__)
                if receipt.get("status") in {"pending", "unknown"}:
                    return result("unknown", aid=aid, error=type(error).__name__)
                return result("failed", error=type(error).__name__)
