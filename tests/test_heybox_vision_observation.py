"""Offline tests for recovery observation; all file data is synthetic."""

import asyncio
import ast
from contextvars import ContextVar
import functools
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "plugins" / "feiyu_steward"
package = types.ModuleType("vision_recovery_test")
package.__path__ = [str(HERE), str(SOURCE)]
sys.modules[package.__name__] = package


def module(name, path):
    spec = importlib.util.spec_from_file_location(package.__name__ + "." + name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


failure_module = module("failure_diagnostics", SOURCE / "failure_diagnostics.py")
transport_module = module("model_transport_diagnostics", SOURCE / "model_transport_diagnostics.py")
observation_module = module("vision_recovery_observation", SOURCE / "vision_recovery_observation.py")


def transport_record(event_id, outcome, started_at, *, generation="a" * 32):
    return {
        "schema": 1, "generation": generation, "invocation": "b" * 32,
        "event_id": event_id, "phase": "vision", "started_at": started_at,
        "elapsed_ms": 80, "outcome": outcome, "provider": "c" * 64,
        "sdk": "d" * 64, "client": "e" * 64, "tls_context": "f" * 64,
        "host_sha256": "1" * 64, "path_sha256": "2" * 64,
        "certificate_verification": True, "hostname_verification": True,
        "request_count": 1, "requests_truncated": False,
        "trace_truncated": False, "trace": [], "existing_trace_skipped": False,
        "route_changed": False, "scope_task_mismatch": False,
    }


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = self.root / "failure-diagnostics"
        self.folder.mkdir()
        self.now = time.time()
        self.failure = transport_record(10, "failed", self.now - 15)
        self.success = transport_record(11, "success", self.now - 5)
        self.failure_at = self.now - 10
        self.write_failure()
        self.result = self.folder / "heybox_vision-recovery.json"

    def write_failure(self):
        value = {"schema": 1, "feature": "heybox_vision", "last_vision_provider": {
            "at": self.failure_at, "diagnostic": {"stage": "vision_provider", "model_transport": self.failure}}}
        (self.folder / "heybox_vision.json").write_text(json.dumps(value), encoding="utf-8")

    def test_distinct_verified_success_links_once_with_metadata_only(self):
        observation_module.record_following_vision_success(self.root, self.success)
        record = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(record["failure"]["event_id"], 10)
        self.assertEqual(record["first_observed_success"]["event_id"], 11)
        self.assertEqual(set(record), {"schema", "feature", "relationship", "failure", "first_observed_success"})
        later = dict(self.success, event_id=12, started_at=self.now - 3)
        observation_module.record_following_vision_success(self.root, later)
        self.assertEqual(record, json.loads(self.result.read_text(encoding="utf-8")))

    def test_unrelated_or_incomplete_records_never_link(self):
        variants = (
            dict(self.success, event_id=10),
            dict(self.success, started_at=self.now - 20),
            dict(self.success, outcome="failed"),
            dict(self.success, request_count=0),
            dict(self.success, route_changed=True),
            dict(self.success, scope_task_mismatch=True),
            dict(self.success, generation="9" * 32),
            dict(self.success, client="9" * 64),
            dict(self.success, certificate_verification=False),
        )
        for candidate in variants:
            with self.subTest(candidate=candidate):
                observation_module.record_following_vision_success(self.root, candidate)
                self.assertFalse(self.result.exists())

    def test_new_failure_cannot_reuse_old_success(self):
        observation_module.record_following_vision_success(self.root, self.success)
        first = self.result.read_text(encoding="utf-8")
        self.failure = transport_record(12, "failed", self.now - 4)
        self.failure_at = self.now - 3
        self.write_failure()
        observation_module.record_following_vision_success(self.root, self.success)
        self.assertEqual(first, self.result.read_text(encoding="utf-8"))
        newer = dict(self.success, event_id=13, started_at=self.now - 2)
        observation_module.record_following_vision_success(self.root, newer)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["failure"]["event_id"], 12)
        self.assertEqual(result["first_observed_success"]["event_id"], 13)

    def test_malformed_existing_evidence_is_not_overwritten(self):
        self.result.write_text("{", encoding="utf-8")
        observation_module.record_following_vision_success(self.root, self.success)
        self.assertEqual(self.result.read_text(encoding="utf-8"), "{")


class CollectorTests(unittest.TestCase):
    class Event:
        pass

    def test_only_verified_completed_image_invocation_is_exposed(self):
        collector = transport_module.ModelTransportDiagnostics()
        event = self.Event()
        data = transport_record(20, "running", time.time() - 1)
        span = {"data": data, "event": event, "start": time.monotonic(), "active": True,
                "checked": object(), "sdk": object(), "client": object(), "context": object(),
                "host": "unit", "pool": object()}
        span["token"] = transport_module._CURRENT.set(span)
        collector.finish(span, "success", verified_image=False)
        self.assertIsNone(collector.last_verified_vision(event))
        span = {"data": transport_record(20, "running", time.time() - 1), "event": event,
                "start": time.monotonic(), "active": True, "checked": object(), "sdk": object(),
                "client": object(), "context": object(), "host": "unit", "pool": object()}
        span["token"] = transport_module._CURRENT.set(span)
        collector.finish(span, "success", verified_image=True)
        self.assertEqual(collector.last_verified_vision(event)["event_id"], 20)
        self.assertIsNone(collector.last_verified_vision(self.Event()))
        collector.close()


class GroupTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((SOURCE / "incident_hooks.py").read_text(encoding="utf-8"))
        hook_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and any(isinstance(item, ast.FunctionDef) and item.name == "_vision_group"
                                  for item in node.body))
        method = next(node for node in hook_class.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_vision_group")
        code = compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                       str(SOURCE / "incident_hooks.py"), "exec")
        self.namespace = {"__package__": package.__name__, "functools": functools,
                          "asyncio": asyncio, "_HEYBOX_VISION_GROUP": ContextVar("test_group"),
                          "IncidentFeaturePaused": RuntimeError}
        exec(code, self.namespace)
        self.calls = []
        self.old_record = observation_module.record_following_vision_success
        observation_module.record_following_vision_success = lambda root, value: self.calls.append((root, value))
        self.addCleanup(setattr, observation_module, "record_following_vision_success", self.old_record)
        self.event = object()
        self.collector = types.SimpleNamespace(values={})
        self.collector.last_verified_vision = lambda event: self.collector.values.get(event)
        self.hooks = types.SimpleNamespace(_model_transport_diagnostics=self.collector,
                                           _vision_event_bound=lambda *a: True,
                                           _vision_event_finished=lambda *a: False,
                                           host=types.SimpleNamespace(root="test-root"))
        self.settings = {"context_settings": {"enable_image_understanding": True}, "urls": ["synthetic"]}

    def run_group(self, cache_hit="", *, new_invocation=True):
        collector, event = self.collector, self.event

        class Plugin:
            async def original(self, event, **kwargs):
                if new_invocation:
                    collector.values[event] = {"invocation": "new"}
                return types.SimpleNamespace(cache_hit=cache_hit)

        wrapped = self.namespace["_vision_group"](self.hooks, Plugin().original)
        return asyncio.run(wrapped(event, **self.settings))

    def test_new_verified_non_cached_result_is_observed(self):
        self.run_group()
        self.assertEqual(self.calls, [("test-root", {"invocation": "new"})])

    def test_cached_or_previous_invocation_is_not_observed(self):
        self.run_group(cache_hit="memory")
        self.collector.values[self.event] = {"invocation": "same"}
        self.run_group(new_invocation=False)
        self.assertEqual(self.calls, [])

    def test_observation_failure_does_not_change_result(self):
        def fail(*args):
            raise OSError("synthetic")
        observation_module.record_following_vision_success = fail
        self.assertEqual(self.run_group().cache_hit, "")


class CheckedProviderBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for method in ("connect", "connect_ex"):
            guard = patch(f"socket.socket.{method}", side_effect=AssertionError("network blocked"))
            guard.start()
            self.addCleanup(guard.stop)
        fixture_path = HERE / "test_heybox_vision.py"
        spec = importlib.util.spec_from_file_location("vision_strict_fixture", fixture_path)
        self.fixture = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.fixture
        spec.loader.exec_module(self.fixture)
        tree = ast.parse((SOURCE / "incident_hooks.py").read_text(encoding="utf-8"))
        hook_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and any(isinstance(item, ast.FunctionDef) and item.name == "_model_transport_call"
                                  for item in node.body))
        method = next(node for node in hook_class.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_model_transport_call")
        code = compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                       str(SOURCE / "incident_hooks.py"), "exec")
        self.events = []

        class Collector:
            def begin(inner, checked, event, phase):
                self.events.append(("begin", phase))
                return object()

            def finish(inner, span, outcome, *, verified_image=False):
                self.events.append(("finish", outcome, verified_image))

        collector = Collector()
        plugin = object()
        self.hooks = types.SimpleNamespace(_model_transport_diagnostics=collector, _active=True,
                                           host=types.SimpleNamespace(plugin=lambda name: plugin),
                                           _vision_event_bound=lambda p, e: p is plugin)
        self.group = ContextVar("test_model_group", default=None)
        namespace = {"__package__": package.__name__, "functools": functools, "asyncio": asyncio,
                     "_HEYBOX_VISION_GROUP": self.group, "_ACTIVE_INCIDENT_HOOKS": self.hooks}
        exec(code, namespace)
        self.wrapped = namespace["_model_transport_call"](
            self.hooks, self.fixture.candidate._CheckedProvider.text_chat)

    async def test_actual_checked_provider_propagates_verified_image(self):
        checked, sdk, _ = self.fixture.make_checked_provider()
        token = self.group.set({"task": asyncio.current_task(), "event": object()})
        try:
            await self.wrapped(checked, prompt="synthetic", image_urls=["one"])
        finally:
            self.group.reset(token)
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(self.events, [("begin", "vision"), ("finish", "success", True)])

    async def test_image_failure_does_not_report_verified_success(self):
        checked, sdk, _ = self.fixture.make_checked_provider(error=self.fixture.InvalidAttachment())
        token = self.group.set({"task": asyncio.current_task(), "event": object()})
        try:
            with self.assertRaises(ValueError):
                await self.wrapped(checked, prompt="synthetic", image_urls=["one"])
        finally:
            self.group.reset(token)
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(self.events, [("begin", "vision"), ("finish", "failed", False)])


if __name__ == "__main__":
    unittest.main()
