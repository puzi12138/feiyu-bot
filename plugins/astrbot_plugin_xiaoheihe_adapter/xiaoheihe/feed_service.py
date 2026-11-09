from __future__ import annotations

from .feiyu_peak_bridge import automatic_peak_paused, automatic_scope, business_paused, feed_paused, feed_post_allowed, feed_resume_snapshot

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .api_client import RequestNotAuthorized, SendUncertainError, XiaoheiheApiClient
from . import feiyu_recovery
from . import feiyu_unseen_feed
from .feiyu_auto_reply_gate import authorize
from .models import Notification, NotificationType, RoutingTarget
from .repository import Repository
from .security import clean_untrusted_text, sanitize_reply_text
from .topic_service import fetch_topic_feed, normalize_topic_ids

SyntheticDispatch = Callable[[Notification, dict[str, Any]], Awaitable[None]]
ReviewedDelivery = Callable[[RoutingTarget, str], Awaitable[dict[str, Any]]]

SKIP_PATTERN = re.compile(r"(广告|推广|抽奖|开奖|出售|收购|交易|代购|引战|骂战|互喷)", re.I)
MAX_TOPIC_FETCH_CONCURRENCY = 4

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "all": (),
    "pc_game": ("pc游戏", "pc 游戏", "steam游戏", "steam", "单机游戏"),
    "mobile_game": ("手机游戏", "手游"),
    "console_game": ("主机游戏", "switch", "xbox", "playstation", "ps5", "ps4", "任天堂"),
    "community": ("盒友杂谈", "杂谈"),
    "daily": ("盒友日常", "日常", "生活"),
    "digital_tech": ("数码科技", "数码", "硬件", "外设", "科技"),
    "anime": ("动漫", "二次元", "漫画", "番剧", "同人"),
    "film_tv": ("影视", "电影", "电视剧", "综艺"),
    "esports": ("电竞", "赛事"),
    "guide": ("攻略", "心得", "教程"),
    "deals": ("特惠资讯", "优惠", "折扣", "史低"),
    "indie_game": ("独立游戏", "indie"),
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class FeedService:
    def __init__(
        self,
        profile_id: str,
        config: dict[str, Any],
        client: XiaoheiheApiClient,
        repository: Repository,
        synthetic_dispatch: SyntheticDispatch,
        reviewed_delivery: ReviewedDelivery,
        approval_lock: asyncio.Lock | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.config = config
        self.client = client
        self.repository = repository
        self.synthetic_dispatch = synthetic_dispatch
        self.reviewed_delivery = reviewed_delivery
        self._approval_lock = approval_lock or asyncio.Lock()

        feed_config = config["proactive_feed"]
        self._topic_ids = tuple(normalize_topic_ids(feed_config.get("topic_ids", [])))
        fallback_values = feed_config.get("fallback_sources", ["all"])
        legacy_source = str(feed_config.get("source", "")).strip()
        if legacy_source and fallback_values == ["all"]:
            fallback_values = [legacy_source]
        self._fallback_sources = tuple(
            str(value) for value in fallback_values if str(value).strip()
        ) or ("all",)
        self._fallback_aliases = tuple(
            dict.fromkeys(
                alias.casefold()
                for source in self._fallback_sources
                for alias in SECTION_ALIASES.get(source, ())
            )
        )
        self._keywords = tuple(
            str(item).casefold() for item in feed_config.get("keywords", []) if str(item).strip()
        )
        self._allowed_types = frozenset(
            str(value).casefold()
            for value in feed_config.get("allowed_post_types", [])
            if str(value).strip()
        )
        self._author_blacklist = frozenset(
            str(value) for value in config["permissions"].get("author_blacklist", [])
        )

    async def run_once(self) -> int:
        cycle = {
            "phase": "entry", "outcome": "returned", "fetched": 0, "scanned": 0,
            "generated": 0, "skipped_invalid_id": 0, "skipped_eligible": 0,
            "skipped_replied": 0, "skipped_existing_event": 0, "skipped_source_gate": 0,
            "stopped_reserve_quota": 0, "stopped_paused": 0, "stopped_run_limit": 0,
        }
        try:
            if feed_paused(self):
                cycle["phase"] = "paused"
                cycle["stopped_paused"] += 1
                return 0
            feed_config = self.config["proactive_feed"]
            if not feed_config.get("enabled", False):
                cycle["phase"] = "disabled"
                return 0
            cycle["phase"] = "counters"
            counters = await self.repository.today_counters(self.profile_id)
            daily_limit = int(feed_config["max_per_day"])
            if counters["proactive_count"] >= daily_limit:
                cycle["phase"] = "daily_quota"
                return 0

            cycle["phase"] = "recovery_lookup"
            recovery = await feiyu_recovery.next_notification(self, daily_limit)
            if recovery is not None:
                cycle["phase"] = "recovery"
                generated = 0
                batch = feiyu_recovery.batch_authorized(self.repository)
                for position in range(11 if batch else 1):
                    notification, metadata = recovery
                    if notification is None:
                        break
                    await self.synthetic_dispatch(notification, metadata)
                    generated += 1
                    cycle["generated"] = generated
                    if (not batch or position == 10 or feed_paused(self) or not await feiyu_recovery.batch_confirmed(
                            self.repository, metadata.get("_feiyu_recovery_event_id"))):
                        break
                    recovery = await feiyu_recovery.next_notification(self, daily_limit)
                    if recovery is None:
                        break
                return generated

            cycle["phase"] = "load_feed"
            feed_snapshot = feed_resume_snapshot(self)
            fetch_started_at = time.time()
            if feed_snapshot is None:
                cycle["stopped_paused"] += 1
                return 0
            posts = await self._load_feed_posts()
            cycle["fetched"] = len(posts)
            cycle["phase"] = "scan"
            generated = 0
            for post in posts:
                if feed_paused(self):
                    cycle["stopped_paused"] += 1
                    break
                if generated >= int(feed_config["max_per_run"]):
                    cycle["stopped_run_limit"] += 1
                    break
                cycle["scanned"] += 1
                post_id = str(post.get("link_id", post.get("post_id", post.get("id", ""))))
                if not post_id:
                    cycle["skipped_invalid_id"] += 1
                    continue
                author = post.get("author", post.get("user", {}))
                author = author if isinstance(author, dict) else {}
                author_uid = str(
                    author.get(
                        "uid",
                        author.get(
                            "heybox_id",
                            author.get(
                                "heyboxid",
                                author.get("user_id", author.get("userid", author.get("id", ""))),
                            ),
                        ),
                    )
                )
                if not self._eligible(post, author_uid):
                    cycle["skipped_eligible"] += 1
                    continue
                if await self.repository.has_replied_to_post(self.profile_id, post_id):
                    cycle["skipped_replied"] += 1
                    continue
                if await self.repository.proactive_event_id(self.profile_id, post_id) is not None:
                    cycle["skipped_existing_event"] += 1
                    continue
                if not feed_post_allowed(self, post, feed_snapshot):
                    cycle["skipped_source_gate"] += 1
                    continue
                if not await self.repository.reserve_proactive_request(self.profile_id, daily_limit):
                    cycle["stopped_reserve_quota"] += 1
                    break
                observed_at = time.time()
                post_created_at = _number(post.get("created_at"))
                notification = Notification(
                    profile_id=self.profile_id,
                    external_event_id=f"feed:{post_id}",
                    external_comment_id=f"feed:{post_id}",
                    notification_id=f"feed:{post_id}",
                    event_type=NotificationType.PROACTIVE_FEED,
                    sender_uid=author_uid,
                    sender_nickname=str(author.get("nickname", author.get("username", ""))),
                    post_id=post_id,
                    root_comment_id="",
                    parent_comment_id="",
                    content=clean_untrusted_text(
                        str(post.get("content", post.get("description", post.get("text", "")))),
                        max_chars=4000,
                    ),
                    created_at=post_created_at or observed_at,
                    observed_at=observed_at,
                    post_author_uid=author_uid,
                    explicit_wake=True,
                    image_urls=[
                        str(url) for url in post.get("image_urls", []) if isinstance(url, str) and url
                    ],
                    raw={"event_type": "proactive_feed", "post": post},
                )
                source_topic_id = str(post.get("source_topic_id", "")).strip()
                feed_origin = str(post.get("_proactive_feed_origin", ""))
                if source_topic_id:
                    source_label = f"真实分区 · {source_topic_id}"
                elif feed_origin == "recommendation_fallback":
                    source_label = f"回退推荐流 · {','.join(self._fallback_sources)}"
                else:
                    source_label = f"推荐流 · {','.join(self._fallback_sources)}"
                cycle["phase"] = "dispatch"
                with feiyu_unseen_feed.candidate_scope(self, notification, feed_snapshot, fetch_started_at):
                    await self.synthetic_dispatch(
                        notification,
                        {
                            "candidate_reason": f"{source_label} · 通过安全过滤器",
                            "post_title": str(post.get("title", "")),
                            "post_author_uid": author_uid,
                            "source_topic_id": source_topic_id,
                            "feed_origin": feed_origin,
                        },
                    )
                generated += 1
                cycle["generated"] = generated
                cycle["phase"] = "scan"
            cycle["phase"] = "complete"
            return generated
        except asyncio.CancelledError:
            cycle["outcome"] = "cancelled"
            raise
        except Exception:
            cycle["outcome"] = "failed"
            raise
        finally:
            # Fixed phases and aggregate counts only: no source, reply, error
            # text, user/profile/post identifier or credentials enter this log.
            # A diagnostic failure must never change the original result.
            try:
                import json
                from .runtime import get_runtime
                get_runtime().logging.emit(
                    "INFO", "FEIYU_FEED_CYCLE_V1 " + json.dumps(cycle, sort_keys=True, separators=(",", ":")),
                )
            except Exception:
                pass

    async def _load_recommendation_posts(self, *, fallback: bool) -> list[dict[str, Any]]:
        if feed_paused(self):
            return []
        page = await self.client.fetch_feed(offset=0)
        origin = "recommendation_fallback" if fallback else "recommendation"
        posts: list[dict[str, Any]] = []
        for post in page.items:
            if not isinstance(post, dict):
                continue
            item = dict(post)
            item["_proactive_feed_origin"] = origin
            posts.append(item)
        return posts

    async def _load_feed_posts(self) -> list[dict[str, Any]]:
        if not self._topic_ids:
            return await self._load_recommendation_posts(fallback=False)

        semaphore = asyncio.Semaphore(MAX_TOPIC_FETCH_CONCURRENCY)

        async def load(topic_id: str):
            async with semaphore:
                if feed_paused(self):
                    raise RequestNotAuthorized("peak_business_request_paused")
                return await fetch_topic_feed(self.client, topic_id)

        if automatic_peak_paused():
            # Never copy the current-task exception into gather's child tasks.
            results = []
            for topic_id in self._topic_ids:
                try:
                    results.append(await load(topic_id))
                except asyncio.CancelledError:
                    raise
                except RequestNotAuthorized:
                    raise
                except Exception as exc:
                    results.append(exc)
        else:
            results = await asyncio.gather(
                *(load(topic_id) for topic_id in self._topic_ids),
                return_exceptions=True,
            )
        successful_pages = 0
        deduplicated: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, (asyncio.CancelledError, RequestNotAuthorized)):
                raise result
            if isinstance(result, BaseException):
                continue
            successful_pages += 1
            for post in result.items:
                post_id = str(post.get("link_id", post.get("post_id", post.get("id", ""))))
                if not post_id:
                    continue
                existing = deduplicated.get(post_id)
                if existing is None:
                    deduplicated[post_id] = post
                    continue
                existing_topics = {
                    str(value)
                    for value in existing.get("matched_topic_ids", [])
                    if str(value).strip()
                }
                existing_topics.add(str(existing.get("source_topic_id", "")))
                existing_topics.add(str(post.get("source_topic_id", "")))
                existing["matched_topic_ids"] = sorted(value for value in existing_topics if value)

        if successful_pages == 0:
            return await self._load_recommendation_posts(fallback=True)

        return sorted(
            deduplicated.values(),
            key=lambda post: (
                _number(post.get("created_at")),
                _number(post.get("popularity_score")),
            ),
            reverse=True,
        )

    def _eligible(self, post: dict[str, Any], author_uid: str) -> bool:
        title = str(post.get("title", ""))
        body = str(post.get("content", post.get("description", post.get("text", ""))))
        text = f"{title}\n{body}".strip()
        if not text or len(clean_untrusted_text(text)) < 4:
            return False
        if SKIP_PATTERN.search(text):
            return False
        post_type = str(post.get("content_type", post.get("type", ""))).lower()
        if post_type in {"ad", "advertisement", "lottery", "trade"}:
            return False
        credentials = getattr(self.client, "credentials", None)
        self_uid = str(getattr(credentials, "uid", "") or "")
        if self_uid and author_uid == self_uid:
            return False
        if author_uid and author_uid in self._author_blacklist:
            return False
        if self._allowed_types and post_type not in self._allowed_types:
            return False

        feed_origin = str(post.get("_proactive_feed_origin", ""))
        if feed_origin.startswith("recommendation") and self._fallback_aliases:
            section_names = {
                str(value).strip().casefold()
                for value in post.get("section_names", [])
                if str(value).strip()
            }
            if not any(alias in name for alias in self._fallback_aliases for name in section_names):
                return False
        return not self._keywords or any(keyword in text.casefold() for keyword in self._keywords)

    async def approve(self, candidate_id: int, edited_text: str | None = None) -> str:
        async with self._approval_lock:
            candidate = await self.repository.feed_candidate(candidate_id)
            if not candidate:
                raise ValueError("候选不存在")
            if candidate["status"] not in {"pending", "approved"}:
                raise ValueError("候选已处理")
            existing_event_id = candidate.get("incoming_event_id")
            if existing_event_id is None:
                existing_event_id = await self.repository.proactive_event_id(self.profile_id, str(candidate["post_id"]))
            gate_route = RoutingTarget(profile_id=self.profile_id, post_id=str(candidate["post_id"]))
            if not await authorize(self.repository, existing_event_id, gate_route, phase="check"):
                # Old, unreserved and cross-day drafts remain in their queue.
                raise ValueError("自动评论候选未取得当日处理机会，保留待审记录")
            text = sanitize_reply_text(
                edited_text or candidate.get("edited_text") or candidate["generated_text"],
                int(self.config["reply"]["max_reply_chars"]),
                native_emojis=True,
            )
            if self.config["proactive_feed"].get("dry_run", True):
                changed = await self.repository.review_feed_candidate(
                    candidate_id,
                    "approved",
                    text,
                )
                if not changed:
                    raise ValueError("候选已处理")
                return "dry_run"
            claimed = await self.repository.claim_feed_candidate_for_send(
                candidate_id,
                text,
            )
            if claimed is None:
                raise ValueError("候选已由其他审核请求处理")
            event_id = claimed.get("incoming_event_id")
            if event_id is None:
                event_id = await self.repository.proactive_event_id(
                    self.profile_id,
                    str(claimed["post_id"]),
                )
            route = RoutingTarget(
                profile_id=self.profile_id,
                post_id=str(claimed["post_id"]),
                incoming_event_id=int(event_id) if event_id is not None else None,
            )
            try:
                result = await self.reviewed_delivery(route, text)
            except asyncio.CancelledError:
                await self.repository.finish_feed_candidate_send(
                    candidate_id,
                    "send_unknown",
                    text,
                )
                raise
            except SendUncertainError:
                await self.repository.finish_feed_candidate_send(
                    candidate_id,
                    "send_unknown",
                    text,
                )
                raise
            except Exception:
                await self.repository.finish_feed_candidate_send(
                    candidate_id,
                    "failed",
                    text,
                )
                raise
            if result.get("status") != "sent" or not result.get("external_comment_id"):
                if result.get("reason") in {"automatic_reply_gate", "previous_outgoing_preserved"} or result.get("status") == "retry_wait":
                    # Preserve the draft for review; a preserved prior outgoing
                    # may be unknown and is never treated as proof of absence.
                    await self.repository.db.execute(
                        "UPDATE feed_candidates SET status=? WHERE id=? AND status='sending'",
                        (candidate["status"], candidate_id),
                    )
                else:
                    await self.repository.finish_feed_candidate_send(candidate_id, "failed", text)
                raise ValueError("评论未确认发送，原候选未记为成功")
            comment_id = str(result["external_comment_id"])
            await self.repository.finish_feed_candidate_send(
                candidate_id,
                "sent",
                text,
                sent_comment_id=comment_id,
            )
            return comment_id

    async def reject(self, candidate_id: int) -> bool:
        return await self.repository.reject_feed_candidate(candidate_id)

    async def reject_expired(self, cutoff_timestamp: float) -> int:
        candidates = await self.repository.list_feed_candidates(status="pending", limit=500)
        count = 0
        for candidate in candidates:
            if float(candidate["created_at"]) < cutoff_timestamp:
                if await self.repository.review_feed_candidate(int(candidate["id"]), "expired"):
                    count += 1
        return count
