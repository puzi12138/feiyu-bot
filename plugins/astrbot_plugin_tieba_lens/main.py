"""贴吧拾光：链接识别、任务调度、QQ 输出与 AstrBot 插件面板。"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import io
import json
import re
import shlex
import time
import uuid
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from .compat import error_response, json_response, request, stream_response
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
import astrbot.api.message_components as Comp

from .client import TiebaClient, sort_document
from .browser import LocalRenderer
from .delivery import is_confirmed_upload_failure, is_upload_failure, send_with_retry
from .fonts import FontStore
from .links import extract_targets, parse_url
from .media import download_originals
from .miniapp import resolve_target
from .renderer import CardRenderer
from .render_service import RenderManager, RendererUnavailable
from .settings import ENUMS, GROUP_KEYS, effective_settings, public_settings, validate_patch
from .storage import Store, cache_key

PLUGIN_NAME = "astrbot_plugin_tieba_lens"
VERSION = "1.2.2"
JOB_ID = re.compile(r"[0-9a-f]{24}\Z")
ACTIVE = {"queued", "fetching", "rendering", "sending"}
PARSE_KEYS = GROUP_KEYS | {"image_width", "page_height", "max_pages"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summary(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {"files", "original_files", "options", "document"}}


def markdown(document: dict) -> str:
    lines = [f"# {document.get('title', '贴吧内容')}", "", f"原帖：{document.get('url', '')}", ""]
    if document.get("description"):
        lines.extend([document["description"], ""])

    def add_post(post: dict, nested: bool = False):
        author = post.get("author", {})
        name = author.get("name", "匿名用户")
        level = author.get("level")
        label = f" · Lv.{level}" if level is not None else ""
        floor = post.get("floor")
        if isinstance(floor, int) and not isinstance(floor, bool) and floor > 0:
            location = f"展示序号 {floor}" if post.get("floor_inferred") else f"{floor}楼"
        else:
            location = "楼中楼" if nested else "回复"
        lines.extend([f"{'####' if nested else '###'} {location} · {name}{label}",
                      str(post.get("time", "")), ""])
        for seg in post.get("segments", []):
            typ = seg.get("type")
            if typ in {"image", "emoji"} and seg.get("url"):
                lines.append(f"![{seg.get('alt', typ)}]({seg['url']})")
            elif typ in {"link", "video"} and seg.get("url"):
                lines.append(f"[{seg.get('text') or seg.get('alt') or typ}]({seg['url']})")
            else:
                lines.append(str(seg.get("text", seg.get("alt", ""))))
        lines.append("")
        for comment in post.get("comments", []):
            add_post(comment, True)
    for post in document.get("posts", []):
        add_post(post)
    for thread in document.get("threads", []):
        lines.extend([f"## [{thread.get('title', '')}]({thread.get('url', '')})",
                      str(thread.get("excerpt", "")), ""])
    for warning in document.get("warnings", []):
        lines.append(f"> {warning}")
    return "\n".join(lines)


class TiebaLens(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_root = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = Store(self.data_root)
        self.local_renderer = LocalRenderer(self.data_root)
        self.render_manager = RenderManager(self.local_renderer, self._service_render_html, self.settings, self._render_endpoint)
        self.renderer = CardRenderer(self._render_html, self.data_root)
        self.fonts = FontStore(self.data_root)
        self.font_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.jobs: dict[str, dict] = {}
        self.clients: dict[str, dict] = {}
        self.locks: dict[str, dict] = {}
        self.recent: OrderedDict[str, float] = OrderedDict()
        self.cooldowns: OrderedDict[str, float] = OrderedDict()
        self.pending_links: set[str] = set()
        self.settings_lock = asyncio.Lock()
        self.submit_lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self.clients_lock = asyncio.Lock()
        self.artifact_lock = asyncio.Lock()
        # Fetch jobs may overlap; image decoding, browser capture and PNG
        # encoding share one slot to bound peak memory across both backends.
        self.render_lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(4)
        self.in_flight = 0
        self.cleanup_task = None
        self.stopping = False
        self._register_web()

    async def initialize(self):
        await asyncio.to_thread(self.store.recover)
        await self._restore_delivery_guards()
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[贴吧拾光] v%s 已启动，中文控制台及自动解析已就绪", VERSION)

    async def _restore_delivery_guards(self):
        wall, monotonic = time.time(), time.monotonic()
        for job in await asyncio.to_thread(self.store.list_jobs, 1000):
            origin, url = job.get("delivery_origin"), job.get("dedup_url")
            expiry = job.get("dedup_until", 0)
            if (origin and url and isinstance(expiry, (int, float)) and expiry > wall
                    and (job.get("delivery_status") in {"sent", "unknown"} or job.get("sent_messages", 0) > 0)):
                duration = job.get("dedup_seconds", self.settings()["dedup_seconds"])
                self.recent[str(origin) + "|" + str(url)] = monotonic - max(0, duration - min(3600, expiry - wall))

    async def terminate(self):
        self.stopping = True
        if self.cleanup_task:
            self.cleanup_task.cancel()
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.cleanup_task:
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        await asyncio.gather(*(entry["client"].close() for entry in self.clients.values()), return_exceptions=True)
        self.clients.clear()
        await self.render_manager.close()

    async def _render_html(self, template: str, data: dict, options: dict) -> str:
        return await self.render_manager.render(template, data, options)

    async def _service_render_html(self, template: str, data: dict, options: dict) -> str:
        return await self.html_render(template, data, return_url=False, options=options)

    @staticmethod
    def _render_endpoint() -> str:
        try:
            from astrbot.core import astrbot_config
            return str(astrbot_config.get("t2i_endpoint") or "").strip()
        except (ImportError, AttributeError):
            return ""

    def settings(self, group_id: str = "") -> dict:
        return effective_settings(self.config, group_id)

    def _register_web(self):
        routes = [
            ("state", self.web_state, ["GET"]),
            ("settings", self.web_settings, ["POST"]),
            ("parse", self.web_parse, ["POST"]),
            ("job", self.web_job, ["GET"]),
            ("retry", self.web_retry, ["POST"]),
            ("preview", self.web_preview, ["GET"]),
            ("export", self.web_export, ["GET"]),
            ("cache/clear", self.web_clear_cache, ["POST"]),
            ("history/clear", self.web_clear_history, ["POST"]),
            ("diagnostics", self.web_diagnostics, ["GET"]),
            ("font", self.web_font, ["GET", "POST"]),
            ("font/reset", self.web_font_reset, ["POST"]),
            ("renderer", self.web_renderer, ["GET"]),
            ("renderer/prepare", self.web_renderer_prepare, ["POST"]),
        ]
        for route, handler, methods in routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, "贴吧拾光管理接口")

    async def _cleanup_loop(self):
        while True:
            try:
                settings = self.settings()
                async with self.submit_lock, self.artifact_lock:
                    await asyncio.to_thread(self.store.cleanup, settings["retention_hours"],
                                            settings["max_cache_mb"], set(self.tasks))
                self._trim_memory()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("[贴吧拾光] 缓存清理失败", exc_info=True)
            await asyncio.sleep(600)

    def _trim_memory(self):
        t = time.monotonic()
        for mapping in (self.recent, self.cooldowns):
            while mapping and (len(mapping) > 5000 or next(iter(mapping.values())) < t - 3600):
                mapping.popitem(last=False)
        for jid in list(self.jobs):
            if len(self.jobs) <= 60:
                break
            if jid not in self.tasks:
                self.jobs.pop(jid)

    async def _save(self, job: dict):
        await asyncio.to_thread(self.store.save_job, job)

    async def _get_job(self, job_id: str) -> dict | None:
        if not JOB_ID.fullmatch(str(job_id)):
            return None
        if job_id in self.jobs and (job_id in self.tasks or self.jobs[job_id].get("persistence_error")):
            return self.jobs[job_id]
        return await asyncio.to_thread(self.store.get_job, job_id)

    async def _client(self, settings: dict) -> tuple[str, TiebaClient]:
        async with self.clients_lock:
            return await self._client_locked(settings)

    async def _client_locked(self, settings: dict) -> tuple[str, TiebaClient]:
        network = {k: settings[k] for k in ("cookie", "request_timeout", "request_delay", "retries")}
        key = hashlib.sha256(json.dumps(network, sort_keys=True).encode()).hexdigest()
        if key not in self.clients:
            for old in list(self.clients):
                if self.clients[old]["users"] == 0:
                    await self.clients.pop(old)["client"].close()
            self.clients[key] = {"client": TiebaClient(settings), "users": 0}
        self.clients[key]["users"] += 1
        return key, self.clients[key]["client"]

    async def submit(self, target: dict, options: dict, *, event=None, input_text: str = "") -> dict:
        async with self.submit_lock:
            return await self._submit(target, options, event=event, input_text=input_text)

    async def _submit(self, target: dict, options: dict, *, event=None, input_text: str = "") -> dict:
        if self.stopping:
            raise ValueError("插件正在重载，请稍后重试")
        if len(self.tasks) >= 16:
            raise ValueError("任务队列已满，请稍后再试")
        jid = uuid.uuid4().hex[:24]
        job = {"id": jid, "status": "queued", "stage": "等待处理", "progress": 0,
               "delivery_origin": str(event.unified_msg_origin) if event else "", "dedup_url": target["url"],
               "dedup_seconds": options["dedup_seconds"], "dedup_until": 0,
               "input": input_text or target["url"], "title": "正在读取贴吧内容", "kind": target.get("kind", "thread"),
               "started_at": now(), "finished_at": None, "error": None, "warnings": [],
               "pages": 0, "original_count": 0, "source_url": target["url"], "cache_hit": False,
               "duration_ms": 0, "options": {k: v for k, v in options.items()
                                             if k not in {"cookie", "blacklist", "group_overrides"}}}
        self.jobs[jid] = job
        await self._save(job)
        await self._increment_stat("total")
        task = asyncio.create_task(self._run(job, target, copy.deepcopy(options), event))
        self.tasks[jid] = task
        task.add_done_callback(lambda _: self.tasks.pop(jid, None))
        return job

    async def _run(self, job: dict, target: dict, settings: dict, event):
        started = time.monotonic()
        dedup_url = target["url"]
        client_key = None
        last_reaction = ""
        admitted = False

        async def progress(stage: str, value: int):
            job.update(stage=stage, progress=min(98, max(1, int(value))))
            await self._save(job)

        try:
            if event:
                last_reaction = settings["reaction_start"]
                await self._react(event, settings, last_reaction, True)
            async with self.semaphore:
                # 可动态修改并发，不更换正在使用的信号量。
                while self.in_flight >= self.settings()["max_concurrent"]:
                    await asyncio.sleep(0.15)
                self.in_flight += 1
                admitted = True
                if target.get("kind") == "miniapp":
                    job["status"] = "fetching"
                    await progress("还原贴吧小程序地址", 5)
                    target = await resolve_target(target, event)
                    job.update(kind=target["kind"], source_url=target["url"])
                key = cache_key(target, settings)
                entry = self.locks.setdefault(key, {"lock": asyncio.Lock(), "users": 0})
                entry["users"] += 1
                try:
                    async with entry["lock"]:
                        job["status"] = "fetching"
                        await progress("读取帖子和回复", 10)
                        try:
                            document = await asyncio.to_thread(self.store.get_cache, key) if settings["cache_minutes"] else None
                        except Exception as exc:
                            logger.warning("[贴吧拾光] 内容缓存暂不可读，将重新读取：%s", type(exc).__name__)
                            document = None
                        if document:
                            job["cache_hit"] = True
                            await self._increment_stat("cache_hits")
                        else:
                            client_key, client = await self._client(settings)
                            document = await asyncio.wait_for(client.parse(target, settings, progress), timeout=240)
                            try:
                                await asyncio.to_thread(self.store.put_cache, key, document, settings["cache_minutes"])
                            except Exception as exc:
                                logger.warning("[贴吧拾光] 内容缓存暂未写入，继续生成卡片：%s", type(exc).__name__)
                finally:
                    entry["users"] -= 1
                    if entry["users"] == 0:
                        self.locks.pop(key, None)
                # Old cache entries can predate local like sorting. Keep each
                # job's export and render order identical without mutating cache.
                document = copy.deepcopy(document)
                sort_document(document, settings)
                job.update(title=document.get("title", "贴吧内容"), kind=document.get("kind", "thread"),
                           source_url=document.get("url", target["url"]), status="rendering", document=document)
                await progress("等待图文排版", 75)
                async with self.render_lock:
                    await progress("排版图文卡片", 78)
                    files = await asyncio.wait_for(self.renderer.render(document, settings, job["id"]), timeout=420)
                if not files:
                    raise RuntimeError("渲染服务没有返回图片")
                validated = []
                for file in files:
                    path = Path(file).resolve()
                    if not path.is_relative_to(self.store.artifacts) or not path.is_file():
                        raise RuntimeError("渲染输出不在插件数据目录")
                    validated.append(str(path))
                job.update(files=validated, pages=len(validated), document=document,
                           warnings=list(document.get("warnings", [])))
                if settings["forward_original_images"]:
                    await progress("保存正文原图", 90)
                    originals, media_warnings = await download_originals(document, settings,
                        self.store.artifacts / job["id"])
                    job.update(original_files=originals, original_count=len(originals))
                    document.setdefault("warnings", []).extend(w for w in media_warnings if w not in document["warnings"])
                    job["warnings"] = list(document["warnings"])
                job["result_ready"] = True
                if event:
                    job.update(status="sending", delivery_status="sending", sent_messages=0)
                    await progress("发送卡片", 96)
                    await self._send(event, job, settings)
                    job["delivery_status"] = "sent"
                job.update(status="done", stage="解析完成", progress=100, finished_at=now())
                await self._increment_stat("success")
                if event:
                    if last_reaction:
                        await self._react(event, settings, last_reaction, False)
                    await self._react(event, settings, settings["reaction_done"], True)
                    last_reaction = ""
        except asyncio.CancelledError:
            if job.get("delivery_status") == "sent":
                # Shutdown may interrupt the optional completion reaction
                # after QQ has already acknowledged every message.
                raise
            delivery = job.get("status") == "sending"
            job.update(status="delivery_failed" if delivery else "failed", stage="发送已中断" if delivery else "任务已中断",
                       error="插件已停止或重载；发送结果可能未知，请核对聊天记录。" if delivery else "插件已停止或重载，可重试。", finished_at=now())
            if delivery:
                job["delivery_status"] = "unknown"
            raise
        except Exception as exc:
            message = self._safe_error(exc, settings)
            delivery = job.get("status") == "sending"
            if delivery and isinstance(exc, TimeoutError):
                message = "等待 QQ 发送响应超时，发送结果未知，请先核对聊天记录。"
            if delivery:
                job["delivery_status"] = "failed" if is_confirmed_upload_failure(exc) else "unknown"
            job.update(status="delivery_failed" if delivery else "failed", stage="卡片已生成，发送失败" if delivery else ("渲染环境未就绪" if isinstance(exc, RendererUnavailable) else "解析失败"),
                       error=message, finished_at=now(), progress=100 if delivery else job["progress"])
            await self._increment_stat("failed")
            logger.warning("[贴吧拾光] 任务 %s 失败：%s", job["id"], message)
            if event:
                if not delivery or (is_confirmed_upload_failure(exc) and not job.get("sent_messages")):
                    self.recent.pop(str(event.unified_msg_origin) + "|" + dedup_url, None)
                if last_reaction:
                    await self._react(event, settings, last_reaction, False)
                await self._react(event, settings, settings["reaction_failed"], True)
                last_reaction = ""
                with contextlib.suppress(Exception):
                    if delivery:
                        notice = ("贴吧内容已解析并生成卡片，但 QQ 图片发送未完成。\n"
                                  "结果已保留，可在贴吧拾光面板预览或下载。")
                        if job.get("sent_messages") or not is_confirmed_upload_failure(exc):
                            notice += "\n部分消息可能已经送达，请先核对聊天记录。"
                        elif is_upload_failure(exc):
                            notice += "\n图片上传连接超时，已自动重试；可稍后重新发送链接。"
                        else:
                            notice += "\nQQ 未接受本次图片，请查看任务中的错误原因，处理后可重新发送链接。"
                    elif isinstance(exc, RendererUnavailable):
                        notice = str(exc)
                    else:
                        notice = f"贴吧解析失败：{message}\n可稍后重新发送链接，或在贴吧拾光面板重新解析。"
                    await event.send(event.plain_result(notice))
        finally:
            if admitted:
                self.in_flight -= 1
            if client_key and client_key in self.clients:
                self.clients[client_key]["users"] -= 1
            if event and last_reaction:
                await self._react(event, settings, last_reaction, False)
            job["duration_ms"] = int((time.monotonic() - started) * 1000)
            if event:
                scope_key = str(event.unified_msg_origin) + "|" + dedup_url
                self.pending_links.discard(scope_key)
                if job.get("delivery_status") in {"sent", "unknown"} or job.get("sent_messages", 0) > 0:
                    self.recent[scope_key] = time.monotonic()
                    self.recent.move_to_end(scope_key)
                    job["dedup_until"] = time.time() + settings["dedup_seconds"]
                else:
                    self.recent.pop(scope_key, None)
            try:
                await self._save(job)
            except Exception as exc:
                job["persistence_error"] = True
                logger.warning("[贴吧拾光] 任务 %s 最终记录暂未写入：%s", job["id"], self._safe_error(exc, settings))
            self._trim_memory()

    async def _increment_stat(self, name: str):
        try:
            await asyncio.to_thread(self.store.increment, name)
        except Exception as exc:
            logger.warning("[贴吧拾光] 任务计数暂未写入：%s", type(exc).__name__)

    @staticmethod
    def _safe_error(exc: Exception, settings: dict) -> str:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return "读取或渲染超时，请稍后重试。"
        message = str(exc) or type(exc).__name__
        if settings.get("cookie"):
            message = message.replace(settings["cookie"], "[已隐藏]")
        message = re.sub(r"(?i)(BDUSS|STOKEN|Cookie|authorization)\s*[:=]\s*[^\s;,]+", r"\1=[已隐藏]", message)
        return message[:300]

    async def _react(self, event, settings: dict, emoji: str, add: bool):
        if not settings["reactions"] or not event.get_group_id():
            return
        try:
            message_id = str(event.message_obj.message_id)
            if not re.fullmatch(r"-?[0-9]+", message_id):
                return
            bot = getattr(event, "bot", None)
            if bot is not None:
                await asyncio.wait_for(bot.call_action("set_msg_emoji_like", message_id=int(message_id),
                    emoji_id=str(emoji), emoji_type="1", set=add), timeout=5)
        except Exception:
            # 不支持贴表情的适配器仍能正常解析和发送。
            logger.debug("[贴吧拾光] 当前消息无法设置表情回应")

    async def _send(self, event, job: dict, settings: dict):
        files = list(job["files"])
        mode = settings["output_mode"]
        originals = job.get("original_files", []) if settings["forward_original_images"] else []
        forward = mode == "forward" or (mode == "auto" and len(files) > 1) or bool(originals)
        has_onebot = event.get_platform_name() == "aiocqhttp"
        async def deliver(build):
            attempts = 0
            async def send():
                nonlocal attempts
                attempts += 1
                try:
                    return await event.send(event.chain_result(build()))
                except Exception as exc:
                    if is_upload_failure(exc) and attempts < 3:
                        logger.warning("[贴吧拾光] 任务 %s 图片上传失败，将重试当前消息（%s/3）", job["id"], attempts + 1)
                    raise
            await send_with_retry(send)
            job["sent_messages"] = job.get("sent_messages", 0) + 1
            try:
                await self._save(job)
            except Exception as exc:
                # QQ has acknowledged this message. A ledger write cannot
                # turn it into a failed send or replay previously sent pages.
                logger.warning("[贴吧拾光] 任务 %s 发送记录暂未写入：%s", job["id"], self._safe_error(exc, settings))

        if forward and has_onebot:
            title = f"{job['title']}\n共 {len(files)} 页"
            if originals:
                title += f" · {len(originals)} 张正文原图"
            if settings["show_source"]:
                title += "\n" + job["source_url"]
            def build_forward():
                nodes = [Comp.Node(uin=str(event.get_self_id()), name="贴吧拾光", content=[Comp.Plain(title)])]
                for idx, file in enumerate(files):
                    nodes.append(Comp.Node(uin=str(event.get_self_id()), name=f"贴吧拾光 · {idx + 1}/{len(files)}",
                                           content=[Comp.Image.fromFileSystem(file)]))
                for idx, original in enumerate(originals):
                    nodes.append(Comp.Node(uin=str(event.get_self_id()), name=str(original.get("label") or f"正文原图 {idx + 1}")[:60],
                                           content=[Comp.Image.fromFileSystem(original["path"])]))
                return [Comp.Nodes(nodes=nodes)]
            await deliver(build_forward)
            return
        for idx, file in enumerate(files):
            def build_card(file=file, idx=idx):
                chain = [Comp.Image.fromFileSystem(file)]
                if idx == len(files) - 1 and settings["show_source"]:
                    chain.append(Comp.Plain(f"\n{job['source_url']}"))
                return chain
            await deliver(build_card)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=25)
    async def on_message(self, event: AstrMessageEvent):
        settings = self.settings(str(event.get_group_id()))
        if not self._allowed(event, settings) or not settings["auto_parse"]:
            return
        text = event.message_str or ""
        if text.lstrip().startswith(("/贴吧", "/tieba", "贴吧帮助", "贴吧状态")):
            return
        payload = []
        for component in event.get_messages():
            if isinstance(component, (Comp.Reply, Comp.Node, Comp.Nodes)):
                continue
            try:
                payload.append(component.toDict())
            except (AttributeError, TypeError):
                payload.append(str(getattr(component, "text", "")))
        targets = extract_targets(payload, limit=settings["max_links"])
        if not targets:
            return
        accepted = await self._dispatch_message(event, targets, settings, manual=False)
        if accepted:
            event.stop_event()

    def _allowed(self, event, settings: dict) -> bool:
        gid = str(event.get_group_id())
        return bool(settings["enabled"] and gid not in self.settings()["blacklist"] and
                    (gid or settings["allow_private"]))

    async def _dispatch_message(self, event, targets: list[dict], settings: dict, *, manual: bool) -> bool:
        async with self.dispatch_lock:
            return await self._dispatch_message_locked(event, targets, settings, manual=manual)

    async def _dispatch_message_locked(self, event, targets: list[dict], settings: dict, *, manual: bool) -> bool:
        scope = str(event.unified_msg_origin)
        current = time.monotonic()
        accepted = False
        if current - self.cooldowns.get(scope, 0) < settings["cooldown_seconds"]:
            if manual:
                await event.send(event.plain_result("解析太频繁，请稍后再试。"))
            return True
        if not await self.render_manager.is_ready():
            await event.send(event.plain_result(self.render_manager.unavailable_message()))
            self.cooldowns[scope] = current
            return True
        for target in targets:
            key = scope + "|" + target["url"]
            if key in self.pending_links or current - self.recent.get(key, 0) < settings["dedup_seconds"]:
                if manual:
                    await event.send(event.plain_result("这条链接刚刚解析过，稍后可再次解析。"))
                accepted = True
                continue
            try:
                self.pending_links.add(key)
                await self.submit(target, settings, event=event)
            except ValueError as exc:
                self.pending_links.discard(key)
                await event.send(event.plain_result(str(exc)))
                return True
            except BaseException:
                self.pending_links.discard(key)
                raise
            self.recent[key] = current
            self.recent.move_to_end(key)
            self.cooldowns[scope] = current
            accepted = True
        return accepted

    @filter.command("贴吧", alias={"tieba", "/贴吧", "/tieba"})
    async def command_parse(self, event: AstrMessageEvent, args: GreedyStr):
        """解析贴吧帖子或主页：/贴吧 链接 --评论 12 --楼中楼 6 --记录。"""
        settings = self.settings(str(event.get_group_id()))
        if not self._allowed(event, settings):
            yield event.plain_result("当前会话已停用贴吧解析。")
            return
        try:
            text, patch = self._parse_args(str(args or ""))
            if not text:
                yield event.plain_result(self.help_text())
                return
            settings.update(validate_patch(patch))
            targets = extract_targets(text, limit=settings["max_links"])
            if not targets and re.fullmatch(r"[^\s<>:/\\]{1,40}", text):
                from urllib.parse import quote
                target = parse_url("https://tieba.baidu.com/f?kw=" + quote(text))
                targets = [target] if target else []
            if not targets:
                raise ValueError("未找到有效贴吧链接。支持帖子、吧主页、用户主页和贴吧分享卡片。")
            await self._dispatch_message(event, targets, settings, manual=True)
            event.stop_event()
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @staticmethod
    def _parse_args(raw: str) -> tuple[str, dict]:
        parts = shlex.split(raw)
        patch = {}
        text = []
        numeric = {"--评论": "comment_limit", "--楼中楼": "subcomment_limit", "--帖子": "forum_thread_limit"}
        i = 0
        while i < len(parts):
            part = parts[i]
            if part in numeric:
                if i + 1 >= len(parts):
                    raise ValueError(f"{part} 后需要填写数量")
                try:
                    patch[numeric[part]] = int(parts[i + 1])
                except ValueError:
                    raise ValueError(f"{part} 后应为整数") from None
                i += 2
                continue
            if part in {"--记录", "--卡片"}:
                patch["output_mode"] = "forward" if part == "--记录" else "card"
            elif part == "--只看楼主":
                patch["only_author"] = True
            elif part in {"--原图", "--不附原图"}:
                patch["forward_original_images"] = part == "--原图"
            elif part in {"--最新", "--热门"}:
                patch["sort"] = "latest" if part == "--最新" else "hot"
            elif part == "--无图":
                patch["include_images"] = False
            elif part in {"--长图", "--分页"}:
                patch["single_image"] = part == "--长图"
            elif part == "--主题":
                if i + 1 >= len(parts):
                    raise ValueError("--主题 后需要填写 纸、夜、雾、达妮娅、初音 或 B站")
                patch["theme"] = {"纸": "paper", "夜": "midnight", "雾": "mist",
                                  "达妮娅": "denia", "初音": "miku", "B站": "bilibili"}.get(parts[i + 1], parts[i + 1])
                i += 1
            elif part == "--字体":
                if i + 1 >= len(parts):
                    raise ValueError("--字体 后需要填写 文楷 或 自定义")
                patch["font_override"] = {"文楷": "lxgw", "霞鹜文楷": "lxgw", "自定义": "custom"}.get(parts[i + 1], parts[i + 1])
                i += 1
            elif part.startswith("--"):
                raise ValueError(f"未知参数：{part}")
            else:
                text.append(part)
            i += 1
        return " ".join(text), patch

    @staticmethod
    def help_text() -> str:
        return ("贴吧拾光\n直接发送贴吧链接、分享卡片或小程序即可解析。\n"
                "/贴吧 链接或吧名 [--评论 12] [--楼中楼 6] [--记录|--卡片]\n"
                "可选：--只看楼主、--最新、--热门、--无图、--原图、--不附原图、--长图|--分页\n"
                "--主题 纸|夜|雾|达妮娅|初音|B站 · --字体 文楷|自定义\n"
                "/贴吧状态 · /贴吧面板 · /贴吧帮助\n"
                "管理员：/贴吧启用、/贴吧禁用、/贴吧黑名单 添加|删除 群号、/贴吧清缓存")

    @filter.command("贴吧帮助", alias={"/贴吧帮助"})
    async def command_help(self, event: AstrMessageEvent):
        """查看贴吧拾光使用说明。"""
        yield event.plain_result(self.help_text())

    @filter.command("贴吧面板", alias={"/贴吧面板"})
    async def command_panel(self, event: AstrMessageEvent):
        """查看管理面板入口。"""
        yield event.plain_result("打开 AstrBot WebUI → 插件 → 贴吧拾光 → 贴吧控制台。\n面板支持解析预览、导出、分群设置、黑名单、任务历史和缓存管理。")

    @filter.command("贴吧状态", alias={"/贴吧状态"})
    async def command_status(self, event: AstrMessageEvent):
        """查看当前会话的解析设置。"""
        settings = self.settings(str(event.get_group_id()))
        yield event.plain_result(f"贴吧拾光 v{VERSION}\n当前会话：{'启用' if self._allowed(event, settings) else '停用'}"
            f"\n评论 {settings['comment_limit']} 楼，楼中楼每楼 {settings['subcomment_limit']} 条"
            f"\n主题 {settings['theme']} · 输出 {settings['output_mode']}\n正在处理 {len(self.tasks)} 个任务")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("贴吧禁用", alias={"/贴吧禁用"})
    async def command_disable(self, event: AstrMessageEvent):
        """管理员：将当前群加入解析黑名单。"""
        gid = str(event.get_group_id())
        if not gid:
            yield event.plain_result("请在需要停用的群里执行。")
            return
        black = self.settings()["blacklist"]
        if gid not in black:
            black.append(gid)
        await self._update_settings({"blacklist": black})
        yield event.plain_result("本群已停用贴吧解析。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("贴吧启用", alias={"/贴吧启用"})
    async def command_enable(self, event: AstrMessageEvent):
        """管理员：移除当前群黑名单并恢复自动解析。"""
        gid = str(event.get_group_id())
        if not gid:
            yield event.plain_result("请在需要启用的群里执行。")
            return
        settings = self.settings()
        groups = settings["group_overrides"]
        groups.setdefault(gid, {}).update(enabled=True, auto_parse=True)
        await self._update_settings({"blacklist": [v for v in settings["blacklist"] if v != gid],
                                    "group_overrides": groups})
        yield event.plain_result("本群已启用贴吧解析。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("贴吧黑名单", alias={"/贴吧黑名单"})
    async def command_blacklist(self, event: AstrMessageEvent, args: GreedyStr):
        """管理员：/贴吧黑名单 添加|删除 群号，或 /贴吧黑名单 列表。"""
        parts = str(args or "").split()
        black = self.settings()["blacklist"]
        if not parts or parts == ["列表"]:
            yield event.plain_result("贴吧群黑名单：\n" + ("\n".join(black) or "暂无"))
            return
        if len(parts) != 2 or parts[0] not in {"添加", "删除"}:
            yield event.plain_result("用法：/贴吧黑名单 添加|删除 群号")
            return
        if parts[0] == "添加":
            black.append(parts[1])
        else:
            black = [gid for gid in black if gid != parts[1]]
        try:
            await self._update_settings({"blacklist": black})
            yield event.plain_result("群黑名单已更新。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("贴吧清缓存", alias={"/贴吧清缓存"})
    async def command_clear(self, event: AstrMessageEvent):
        """管理员：清理贴吧内容缓存。"""
        count = await asyncio.to_thread(self.store.clear_cache)
        yield event.plain_result(f"已清理 {count} 条内容缓存。")

    async def _update_settings(self, payload: dict) -> dict:
        patch = validate_patch(payload)
        async with self.settings_lock:
            old = dict(self.config)
            try:
                self.config.update(patch)
                await asyncio.to_thread(self.config.save_config)
            except Exception:
                self.config.clear()
                self.config.update(old)
                raise
        return public_settings(self.settings())

    async def web_state(self):
        jobs = await asyncio.to_thread(self.store.list_jobs, 100)
        stats = await asyncio.to_thread(self.store.stats)
        stats["active"] = len(self.tasks)
        return json_response({"settings": public_settings(self.settings()), "stats": stats,
            "jobs": [j for j in jobs if j["id"] in self.tasks],
            "history": [j for j in jobs if j["id"] not in self.tasks],
            "cache": await asyncio.to_thread(self.store.cache_info),
            "font": await asyncio.to_thread(self.fonts.info),
            "renderer": await self.render_manager.status(),
            "capabilities": {"version": VERSION, "themes": sorted(ENUMS["theme"]),
                             "fonts": sorted(ENUMS["font_override"]), "single_image": True,
                             "forward": True, "reactions": True, "exports": ["png", "json", "markdown", "zip"]}})

    async def web_renderer(self):
        return json_response({"renderer": await self.render_manager.status(refresh=True),
                              "settings": public_settings(self.settings())})

    async def web_renderer_prepare(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError("请选择需要准备的浏览器内核")
            self.render_manager.prepare(payload.get("engine", "headless"))
            return json_response({"renderer": await self.render_manager.status(),
                                  "settings": public_settings(self.settings())})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_font(self):
        try:
            if request.method == "GET":
                return json_response({"font": await asyncio.to_thread(self.fonts.info),
                                      "settings": public_settings(self.settings())})
            length = request.headers.get("content-length", "0")
            if length.isdigit() and int(length) > 2 * 1024 * 1024:
                raise ValueError("单次字体分块超过 2 MB，请使用面板分块上传")
            body = await request.body()
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("单次字体分块超过 2 MB")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise ValueError("字体请求格式无效") from None
            async with self.font_lock:
                encoded = payload.get("data", "") if isinstance(payload, dict) else ""
                decoded_size = len(encoded) // 4 * 3 - len(encoded) + len(encoded.rstrip("=")) if isinstance(encoded, str) else 0
                final = isinstance(payload, dict) and ("total_size" not in payload or
                    (isinstance(payload.get("offset"), int) and isinstance(payload.get("total_size"), int) and
                     payload["offset"] + decoded_size == payload["total_size"]))
                snapshot = await asyncio.to_thread(self.fonts.load) if final else None
                if isinstance(payload, dict) and "total_size" in payload:
                    result = await asyncio.to_thread(self.fonts.upload_chunk, payload)
                else:
                    result = {"complete": True, "font": await asyncio.to_thread(self.fonts.upload, payload)}
                if result.get("complete"):
                    try:
                        result["settings"] = await self._update_settings({"font_override": "custom"})
                    except Exception:
                        await asyncio.to_thread(self.fonts.restore, snapshot)
                        raise
                return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_font_reset(self):
        async with self.font_lock:
            settings = await self._update_settings({"font_override": "lxgw"})
            font = await asyncio.to_thread(self.fonts.reset)
        return json_response({"font": font, "settings": settings})

    async def web_settings(self):
        try:
            return json_response({"settings": await self._update_settings(await request.json(default={}))})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_parse(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError("请求应为 JSON 对象")
            text = payload.get("input", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 65536:
                raise ValueError("请填写有效的贴吧链接或分享卡片，最多 65536 字符")
            settings = self.settings()
            if not settings["enabled"]:
                raise ValueError("插件已停用，请先在设置中启用")
            patch = validate_patch(payload.get("options", {}))
            if set(patch) - PARSE_KEYS:
                raise ValueError("临时解析参数包含全局维护配置，请在设置中修改")
            settings.update(patch)
            if not await self.render_manager.is_ready():
                raise ValueError(self.render_manager.unavailable_message())
            targets = extract_targets(text, limit=1)
            if not targets:
                raise ValueError("未识别到贴吧帖子或主页链接")
            job = await self.submit(targets[0], settings, input_text=targets[0]["url"])
            return json_response({"job": summary(job)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_job(self, job_id: str = ""):
        job_id = job_id or request.query.get("id", "")
        job = await self._get_job(job_id)
        if not job:
            return error_response("任务不存在或已过期", status_code=404)
        return json_response({"job": {k: v for k, v in job.items() if k not in {"files", "original_files", "options"}}})

    async def web_retry(self, job_id: str = ""):
        if not job_id:
            payload = await request.json(default={})
            job_id = payload.get("id", "") if isinstance(payload, dict) else ""
        job = await self._get_job(job_id)
        if not job:
            return error_response("任务不存在或已过期", status_code=404)
        if job_id in self.tasks:
            return error_response("此任务仍在处理中", status_code=409)
        try:
            targets = extract_targets(job["source_url"], limit=1)
            if not targets:
                raise ValueError("原链接无效")
            settings = self.settings()
            if not settings["enabled"]:
                raise ValueError("插件已停用")
            settings.update(job.get("options", {}))
            result = await self.submit(targets[0], settings)
            return json_response({"job": summary(result)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _artifact(self):
        job_id = request.query.get("id", "")
        job = await self._get_job(job_id)
        if not job or not (job.get("result_ready") or job.get("status") == "done"):
            raise ValueError("解析结果尚未完成、已过期或不存在")
        return job

    async def web_preview(self):
        try:
            async with self.artifact_lock:
                job = await self._artifact()
                page = request.query.get("page", 0, type=int)
                if page < 0 or page >= len(job["files"]):
                    raise ValueError("页码无效")
                raw = await asyncio.to_thread(self._thumbnail, job["files"][page])
            return json_response({"data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()})
        except (ValueError, FileNotFoundError) as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _thumbnail(file: str) -> bytes:
        from PIL import Image
        with Image.open(file) as image:
            image.thumbnail((960, 5000))
            if image.mode != "RGB":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85)
            return output.getvalue()

    async def web_export(self):
        try:
            async with self.artifact_lock:
                job = await self._artifact()
                fmt = request.query.get("format", "png")
                page = request.query.get("page", 0, type=int)
                if fmt not in {"png", "json", "markdown", "zip"}:
                    raise ValueError("不支持的导出格式")
                raw = await asyncio.to_thread(self._export_bytes, fmt, page, job)
            ext = "md" if fmt == "markdown" else fmt
            filename = f"tieba-{job['id']}-{page + 1}.{ext}" if fmt == "png" else f"tieba-{job['id']}.{ext}"
            async def chunks():
                for start in range(0, len(raw), 65536):
                    yield raw[start:start + 65536]
            return stream_response(chunks(), content_type={"png": "image/png", "json": "application/json", "markdown": "text/markdown; charset=utf-8", "zip": "application/zip"}[fmt],
                headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(len(raw))})
        except (ValueError, FileNotFoundError) as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _export_bytes(fmt: str, page: int, job: dict) -> bytes:
        # 在清理锁内建立有界快照，下载流不依赖可被回收的文件。
        budget = 96 * 1024 * 1024
        if fmt == "png":
            if page < 0 or page >= len(job["files"]):
                raise ValueError("页码无效")
            path = Path(job["files"][page])
            if path.stat().st_size > budget:
                raise ValueError("单页超过 96 MB 导出限制，请降低卡片尺寸")
            return path.read_bytes()
        data = json.dumps(job["document"], ensure_ascii=False, indent=2).encode("utf-8")
        text = markdown(job["document"]).encode("utf-8")
        original_paths = [original["path"] for original in job.get("original_files", [])]
        if len(data) + len(text) + (sum(Path(p).stat().st_size for p in job["files"] + original_paths) if fmt == "zip" else 0) > budget:
            raise ValueError("整帖导出超过 96 MB，请分别下载卡片或缩小内容范围")
        if fmt == "zip":
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
                for file in job["files"]:
                    bundle.write(file, Path(file).name)
                for file in original_paths:
                    bundle.write(file, "原图/" + Path(file).name)
                bundle.writestr("原帖.md", text)
                bundle.writestr("原帖.json", data)
            return output.getvalue()
        return text if fmt == "markdown" else data

    async def web_clear_cache(self):
        return json_response({"cleared": await asyncio.to_thread(self.store.clear_cache)})

    async def web_clear_history(self):
        async with self.submit_lock, self.artifact_lock:
            active = set(self.tasks)
            count = await asyncio.to_thread(self.store.clear_history, active)
            self.jobs = {k: v for k, v in self.jobs.items() if k in active}
        return json_response({"cleared": count})

    async def web_diagnostics(self):
        clients = [entry["client"].diagnostics() for entry in self.clients.values()]
        renderer = await self.render_manager.status()
        engine = {"service": "AstrBot 渲染服务", "local": "本地浏览器", "pending": "等待可用环境"}[renderer["active"]]
        return json_response({"plugin_version": VERSION,
            "client": clients or {"status": "尚未发起取数请求"},
            "render": {"engine": engine, "environment": renderer, "themes": sorted(ENUMS["theme"]),
                       "fonts": sorted(ENUMS["font_override"]), "single_image": True},
            "storage": await asyncio.to_thread(self.store.cache_info),
            "active_tasks": len(self.tasks), "queue_limit": 16,
            "retention_hours": self.settings()["retention_hours"],
            "privacy": "Cookie 仅用于取数，面板读取与导出均不包含 Cookie。"})
