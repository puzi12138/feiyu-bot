"""Best-effort, content-free link between a vision failure and a later success."""

import json
import math
import os
import time
import uuid
from pathlib import Path

from .failure_diagnostics import _file_lock, _valid_model_transport


def record_following_vision_success(root, success):
    """Record one later verified invocation for the same event route, without influencing it."""
    temporary = None
    try:
        if root is None or not _valid_model_transport(success):
            return
        if (success["phase"] != "vision" or success["outcome"] != "success"
                or success["request_count"] < 1 or success["route_changed"]
                or success["scope_task_mismatch"] or not success["certificate_verification"]
                or not success["hostname_verification"]):
            return
        root = Path(root)
        folder = root / "failure-diagnostics"
        failure_path = folder / "heybox_vision.json"
        success_path = folder / "heybox_vision-recovery.json"
        if (not root.is_absolute() or root.is_symlink() or folder.is_symlink()
                or not folder.is_dir() or failure_path.is_symlink() or success_path.is_symlink()):
            return
        with _file_lock(folder / "heybox_vision.lock"):
            if not failure_path.is_file() or failure_path.stat().st_size > 32768:
                return
            failure_store = json.loads(failure_path.read_text(encoding="utf-8"))
            if (type(failure_store) is not dict or failure_store.get("schema") != 1
                    or failure_store.get("feature") != "heybox_vision"):
                return
            last = failure_store.get("last_vision_provider")
            if type(last) is not dict or set(last) != {"at", "diagnostic"}:
                return
            failure_at, diagnostic = last["at"], last["diagnostic"]
            if (type(failure_at) not in (int, float) or not math.isfinite(failure_at)
                    or not 0 < failure_at < success["started_at"] <= time.time() + 2
                    or type(diagnostic) is not dict or diagnostic.get("stage") != "vision_provider"):
                return
            failure = diagnostic.get("model_transport")
            if (not _valid_model_transport(failure) or failure["phase"] != "vision"
                    or failure["outcome"] != "failed" or failure["event_id"] == success["event_id"]
                    or failure["route_changed"] or failure["scope_task_mismatch"]
                    or not failure["certificate_verification"] or not failure["hostname_verification"]
                    or any(failure[key] != success[key] for key in (
                        "generation", "provider", "sdk", "client", "tls_context", "host_sha256", "path_sha256"))):
                return
            if success_path.exists():
                if not success_path.is_file() or success_path.stat().st_size > 2048:
                    return
                previous = json.loads(success_path.read_text(encoding="utf-8"))
                if (type(previous) is not dict or previous.get("schema") != 1
                        or previous.get("feature") != "heybox_vision"
                        or type(previous.get("failure")) is not dict
                        or type(previous["failure"].get("at")) not in (int, float)
                        or not math.isfinite(previous["failure"]["at"])):
                    return
                if previous["failure"]["at"] >= failure_at:
                    return
            value = {
                "schema": 1,
                "feature": "heybox_vision",
                "relationship": "same_generation_provider_client_route",
                "failure": {"event_id": failure["event_id"], "at": failure_at},
                "first_observed_success": {"event_id": success["event_id"],
                                           "started_at": success["started_at"],
                                           "elapsed_ms": success["elapsed_ms"]},
            }
            temporary = folder / ("heybox_vision-recovery." + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, success_path)
            temporary = None
    except Exception:
        # Diagnostics must not alter an already-authorized business result.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
