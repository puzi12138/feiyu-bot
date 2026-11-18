"""Offline regression tests against the candidate and the installed provider."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugins" / "astrbot_plugin_xiaoheihe_adapter"
astrbot_root = os.environ.get("ASTRBOT_ROOT")
if astrbot_root:
    sys.path.insert(0, str(Path(astrbot_root).resolve()))
spec = importlib.util.spec_from_file_location(
    "feiyu_strict_vision_candidate", PLUGIN_DIR / "main.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
candidate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = candidate
spec.loader.exec_module(candidate)

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from openai.types.chat import ChatCompletion


class InvalidAttachment(Exception):
    body = {"error": {"code": "invalid_attachment", "message": "invalid attachment"}}


class FakeSDK:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error
        self.check = None

    async def create(self, **kwargs):
        if self.check:
            self.check(kwargs)
        self.calls.append(copy.deepcopy(kwargs))
        if self.error:
            raise self.error
        return ChatCompletion(id="fake", choices=[], created=0, model="unit", object="chat.completion")


def make_checked_provider(*, error=None, omit=()):
    sdk = FakeSDK(error=error)
    provider = object.__new__(ProviderOpenAIOfficial)
    provider.provider_config = {}
    provider.api_keys = ["synthetic-test-key"]
    provider.client = SimpleNamespace(
        api_key="", base_url=SimpleNamespace(host="unit.invalid"),
        chat=SimpleNamespace(completions=SimpleNamespace(create=sdk.create)),
    )
    provider.default_params = {"messages", "model"}
    provider.get_model = lambda: "unit-image"

    async def materialize(ref, *, mode="safe"):
        if ref in omit:
            return None
        return ref if ref.startswith("data:image/") else "data:image/png;base64,YQ=="

    async def parse(completion, tools):
        return SimpleNamespace(completion_text="可见的图片描述", role="assistant", is_chunk=False)

    provider._image_ref_to_data_url = materialize
    provider._parse_openai_completion = parse

    async def approved():
        return True

    async def closed():
        raise AssertionError("a payload failure must not call the policy-denial callback")

    checked = candidate._CheckedProvider(
        provider, approved, closed, valid=lambda: True,
        expected_model="unit-image", disable_tools=True,
    )
    return checked, sdk, provider


class OfflineVisionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for method in ("connect", "connect_ex"):
            guard = patch(f"socket.socket.{method}", side_effect=AssertionError("network blocked"))
            guard.start()
            self.addCleanup(guard.stop)


class StrictPayloadTests(OfflineVisionTest):
    async def test_real_provider_sends_all_images_and_sets_witness(self):
        checked, sdk, _ = make_checked_provider()
        reply = await checked.text_chat(prompt="describe", image_urls=["one", "two"])
        self.assertEqual(reply.completion_text, "可见的图片描述")
        self.assertTrue(checked._last_vision_query_verified)
        parts = sdk.calls[0]["messages"][-1]["content"]
        self.assertEqual(sum(p.get("type") == "image_url" for p in parts), 2)
        self.assertEqual(len(sdk.calls), 1)

    async def test_failed_materialization_never_reaches_sdk(self):
        checked, sdk, _ = make_checked_provider(omit={"two"})
        with self.assertRaisesRegex(ValueError, "vision_payload_missing_images"):
            await checked.text_chat(prompt="describe", image_urls=["one", "two"])
        self.assertEqual(sdk.calls, [])
        self.assertFalse(checked._last_vision_query_verified)

    async def test_real_invalid_attachment_cannot_send_text_only_fallback(self):
        checked, sdk, _ = make_checked_provider(error=InvalidAttachment())
        with self.assertRaisesRegex(ValueError, "vision_payload_missing_images"):
            await checked.text_chat(prompt="describe", image_urls=["one"])
        self.assertEqual(len(sdk.calls), 1)
        self.assertFalse(checked._last_vision_query_verified)

    async def test_unchecked_response_is_rejected_and_main_text_is_unaffected(self):
        checked, sdk, provider = make_checked_provider()

        async def bypass(*args, **kwargs):
            return SimpleNamespace(completion_text="未经识图的回答", role="assistant")

        provider.text_chat = bypass
        with self.assertRaisesRegex(ValueError, "vision_query_unverified"):
            await checked.text_chat(prompt="describe", image_urls=["one"])
        self.assertFalse(checked._last_vision_query_verified)
        provider.text_chat = ProviderOpenAIOfficial.text_chat.__get__(provider)
        response = await checked.text_chat(prompt="plain text")
        self.assertEqual(response.completion_text, "可见的图片描述")
        self.assertFalse(checked._last_vision_query_verified)
        self.assertEqual(len(sdk.calls), 1)

    async def test_wrong_message_or_empty_image_is_rejected(self):
        checked, sdk, _ = make_checked_provider()
        state = {"expected_count": 1, "checked": False}
        token = checked._vision_request.set(state)
        try:
            for payload in (
                {"model": "unit-image", "messages": [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]},
                {"model": "unit-image", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}]}]},
            ):
                with self.assertRaises(ValueError):
                    await checked._require(payload, None)
        finally:
            checked._vision_request.reset(token)
        self.assertFalse(state["checked"])
        self.assertEqual(sdk.calls, [])

    async def test_custom_extra_body_cannot_replace_messages_or_model(self):
        for key in ("messages", "model"):
            checked, sdk, provider = make_checked_provider()
            provider.provider_config["custom_extra_body"] = {key: "replacement"}
            with self.assertRaisesRegex(ValueError, "vision_payload_override_not_allowed"):
                await checked.text_chat(prompt="describe", image_urls=["one"])
            self.assertEqual(sdk.calls, [])
            self.assertFalse(checked._last_vision_query_verified)

    async def test_real_sanitizer_preserves_final_user_images(self):
        checked, sdk, _ = make_checked_provider()
        await checked.text_chat(
            prompt="describe", image_urls=["one"],
            contexts=[{"role": "assistant", "content": None}],
        )
        self.assertEqual(len(sdk.calls), 1)
        last = sdk.calls[0]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][-1]["type"], "image_url")

    async def test_witness_is_private_to_each_asyncio_task(self):
        checked, _, _ = make_checked_provider()
        await checked.text_chat(prompt="describe", image_urls=["one"])
        self.assertTrue(checked._last_vision_query_verified)
        async def child():
            inherited_before_call = checked._last_vision_query_verified
            await checked.text_chat(prompt="plain text")
            return inherited_before_call, checked._last_vision_query_verified
        self.assertEqual(await asyncio.create_task(child()), (False, False))
        self.assertTrue(checked._last_vision_query_verified)
        async def sibling(image):
            await checked.text_chat(prompt="describe", image_urls=["one"] if image else None)
            return checked._last_vision_query_verified
        self.assertEqual(await asyncio.gather(sibling(True), sibling(False)), [True, False])
        self.assertTrue(checked._last_vision_query_verified)


class CacheTests(OfflineVisionTest):
    async def test_legacy_records_fail_closed_and_new_fingerprint_is_bound(self):
        cls = candidate.XiaoheiheAdapterPlugin
        text = "图片描述"
        old = {"caption": text, "caption_hash": hashlib.sha256(text.encode()).hexdigest(), "image_fingerprint": "a" * 64}
        self.assertEqual(cls._validated_visual_record_caption(old), ("", "unverified_legacy_vision"))
        new = dict(old, image_fingerprint=cls._image_fingerprint(["https://example.test/pic.png"]))
        self.assertEqual(cls._validated_visual_record_caption(new), (text, ""))
        self.assertNotEqual(cls._image_fingerprint(["https://example.test/a"]), cls._image_fingerprint(["https://example.test/b"]))

    async def test_trusted_memory_cache_works_without_provider_but_owner_mismatch_does_not(self):
        plugin = object.__new__(candidate.XiaoheiheAdapterPlugin)
        plugin._image_caption_cache = OrderedDict()
        plugin._image_caption_cache_lock = asyncio.Lock()
        plugin.runtime = SimpleNamespace(started=False, logging=SimpleNamespace(emit=lambda *a, **k: None))
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message={"route": {"post_id": "post-a"}}),
            get_extra=lambda *args: None,
            get_sender_id=lambda: "commenter",
            get_sender_name=lambda: "commenter",
        )
        urls = ["https://example.test/pic.png"]
        await plugin._store_cached_image_caption(
            provider_label="vision", model="unit-image", profile_id="default", post_id="post-a",
            source="original_post", urls=urls, caption="可信的图像描述", owner_uid="author-a",
            owner_nickname="作者甲", owner_role=candidate.ContentOwnerRole.POST_AUTHOR.value,
        )
        same = candidate.ImageAttribution("original_post", "author-a", "作者甲", candidate.ContentOwnerRole.POST_AUTHOR.value)
        other = candidate.ImageAttribution("original_post", "author-b", "作者乙", candidate.ContentOwnerRole.POST_AUTHOR.value)
        async def run(attribution, images=urls):
            return await plugin._caption_thread_image_group(
                event, attribution=attribution, urls=images, provider_settings={}, profile_id="default",
                context_settings={"thread_reply_compressed_image_chars": 200},
                deadline=0, candidates=[],
            )
        hit = await run(same)
        self.assertEqual(hit.caption, "可信的图像描述")
        self.assertIsNone(await run(other))
        self.assertIsNone(await run(same, ["https://example.test/different.png"]))

    async def test_persistent_cache_needs_verified_fingerprint_and_matching_author(self):
        plugin = object.__new__(candidate.XiaoheiheAdapterPlugin)
        plugin._image_caption_cache = OrderedDict()
        plugin._image_caption_cache_lock = asyncio.Lock()
        urls = ["https://example.test/pic.png"]
        text = "可信的持久化图片描述"
        record = {
            "id": 42, "profile_id": "default", "post_id": "post-a", "source": "original_post",
            "caption": text, "caption_hash": hashlib.sha256(text.encode()).hexdigest(),
            "image_fingerprint": "a" * 64, "owner_uid": "author-a", "owner_nickname": "作者甲",
            "owner_role": candidate.ContentOwnerRole.POST_AUTHOR.value,
            "owner_identity_key": "post:default:post-a:author", "expires_at": time.time() + 60,
        }
        class Repository:
            async def visual_context_for_post(self, profile, post, fingerprint):
                return dict(record)
        plugin.runtime = SimpleNamespace(started=True, repository=Repository(), logging=SimpleNamespace(emit=lambda *a, **k: None))
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message={"route": {"post_id": "post-a"}}),
            get_extra=lambda *args: None, get_sender_id=lambda: "commenter", get_sender_name=lambda: "commenter",
        )
        author = candidate.ImageAttribution("original_post", "author-a", "作者甲", candidate.ContentOwnerRole.POST_AUTHOR.value)
        async def run(attribution=author):
            return await plugin._caption_thread_image_group(
                event, attribution=attribution, urls=urls, provider_settings={}, profile_id="default",
                context_settings={"thread_reply_compressed_image_chars": 200}, deadline=0, candidates=[],
            )
        self.assertIsNone(await run())
        record["image_fingerprint"] = plugin._image_fingerprint(urls)
        self.assertEqual((await run()).caption, text)
        plugin._image_caption_cache.clear()
        record["owner_uid"] = "author-b"
        self.assertIsNone(await run())


class GuardIntegrationTests(OfflineVisionTest):
    async def test_real_peak_scope_and_incident_group_preserve_checked_provider(self):
        package = ModuleType("offline_steward")
        package.__path__ = [str(ROOT / "plugins" / "feiyu_steward")]
        sys.modules[package.__name__] = package
        peak = importlib.import_module("offline_steward.heybox_peak")
        hooks_module = importlib.import_module("offline_steward.incident_hooks")
        peak._PLUGIN_MODULE = candidate.__name__
        original_scope = candidate.auxiliary_model_scope
        candidate.auxiliary_model_scope = peak.auxiliary_model_scope
        try:
            for mode in ("success", "empty", "invalid", "role_error"):
                error = InvalidAttachment() if mode == "invalid" else None
                checked, sdk, provider = make_checked_provider(error=error, omit={"one"} if mode == "empty" else ())
                failures = []
                terminated = []
                if mode == "role_error":
                    async def parse_error(completion, tools):
                        return SimpleNamespace(completion_text="服务失败", role="err", is_chunk=False)
                    provider._parse_openai_completion = parse_error

                class Runtime:
                    started = False
                    logging = SimpleNamespace(emit=lambda *args, **kwargs: None)
                    def auxiliary_provider_available(self, *args):
                        return True
                    def report_auxiliary_provider_failure(self, *args, **kwargs):
                        failures.append(type(args[3]).__name__)
                    def report_auxiliary_provider_success(self, *args):
                        pass
                    async def fail_reply(self, **kwargs):
                        terminated.append(kwargs["event_id"])

                runtime = Runtime()
                plugin = object.__new__(candidate.XiaoheiheAdapterPlugin)
                plugin.runtime = runtime
                plugin._image_caption_cache = OrderedDict()
                plugin._image_caption_cache_lock = asyncio.Lock()
                async def checked_provider(event, actual):
                    self.assertIs(actual, provider)
                    return checked
                plugin._checked_provider = checked_provider
                binding = ("synthetic", mode)
                plugin._automatic_binding = lambda event: binding
                event = SimpleNamespace(
                    runtime=runtime, route=SimpleNamespace(profile_id="default", post_id="post-a"),
                    event_id=101, _final_send_done=False, _completion=asyncio.Event(), _send_lock=asyncio.Lock(),
                    message_obj=SimpleNamespace(raw_message={"route": {"post_id": "post-a"}}),
                    get_extra=lambda key, default=None: binding if key == "xiaoheihe_auto_guard_binding" else default,
                    get_platform_name=lambda: "xiaoheihe", get_sender_id=lambda: "sender",
                    get_sender_name=lambda: "sender",
                )
                event.stop_event = lambda: setattr(event, "stopped", True)
                event.is_stopped = lambda: getattr(event, "stopped", False)
                feed = SimpleNamespace(runtime=runtime, host=SimpleNamespace(plugin=lambda name: plugin))
                scope = SimpleNamespace(grant=SimpleNamespace(event=event, candidate=SimpleNamespace(feed=feed)))
                peak._event_scope_valid = lambda item, require_authorized=False: item is scope
                peak._event_image_refs_valid = lambda actual_event, grant, urls: actual_event is event and bool(urls)
                scope_token = peak._EVENT.set(scope)
                base_text_chat = provider.text_chat
                async def permitted_text_chat(*args, **kwargs):
                    self.assertTrue(peak.provider_allowed(
                        kwargs.get("session_id"), provider=provider, method="text_chat", args=args, kwargs=kwargs,
                    ))
                    return await base_text_chat(*args, **kwargs)
                provider.text_chat = permitted_text_chat
                def check_sdk(kwargs):
                    self.assertTrue(peak.provider_allowed(None, provider=provider, sdk=True, kwargs=kwargs))
                sdk.check = check_sdk

                hooks = object.__new__(hooks_module.IncidentHooks)
                incident_state = {"blocked": False}
                hooks.host = SimpleNamespace(root=None, incidents=SimpleNamespace(blocked=lambda feature: incident_state["blocked"]))
                hooks._temporary_blocks = {}
                hooks._local_blocked = set()
                hooks._model_transport_diagnostics = None
                stopped = []
                hooks._trial_failure_started = lambda *args: (False, None)
                async def report(runtime_arg, event_id, route, feature, category, stage, failure_kind, *, image_cooldown=None):
                    stopped.append((feature, category, failure_kind))
                    incident_state["blocked"] = True
                hooks._retry_report_failure = report
                runtime.report_auxiliary_provider_failure = hooks._vision_provider_failure(runtime.report_auxiliary_provider_failure)
                run_group = hooks._vision_group(plugin._caption_thread_image_group)
                attribution = candidate.ImageAttribution(
                    "original_post", "author-a", "作者甲", candidate.ContentOwnerRole.POST_AUTHOR.value,
                )
                params = dict(
                    attribution=attribution, urls=["one"], provider_settings={}, profile_id="default",
                    context_settings={"thread_reply_compressed_image_chars": 200},
                    deadline=asyncio.get_running_loop().time() + 5, candidates=[(provider, "unit")],
                )
                try:
                    if mode == "success":
                        result = await run_group(event, **params)
                        self.assertEqual(result.caption, "可见的图片描述")
                        self.assertTrue(checked._last_vision_query_verified)
                        self.assertEqual(stopped, [])
                    else:
                        with self.assertRaises(hooks_module.IncidentFeaturePaused):
                            await run_group(event, **params)
                        self.assertEqual(len(stopped), 1)
                        self.assertEqual(stopped[0][:2], ("heybox_vision", "vision_failed"))
                        self.assertEqual(terminated, [101])
                        self.assertTrue(event.stopped)
                        self.assertTrue(event._completion.is_set())
                        self.assertTrue(event._final_send_done)
                        self.assertTrue(incident_state["blocked"])
                        self.assertEqual(failures, ["ValueError"])
                        self.assertFalse(checked._last_vision_query_verified)
                    self.assertEqual(len(sdk.calls), 0 if mode == "empty" else 1)
                finally:
                    peak._EVENT.reset(scope_token)
        finally:
            candidate.auxiliary_model_scope = original_scope


if __name__ == "__main__":
    unittest.main(verbosity=2)
