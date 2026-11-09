"""Verify the native Companion image chain without sending anything to QQ.

The default mode reads only configuration, extension status, and broker health.
--generate runs exactly one native text-to-image troubleshooting test. That
endpoint retains its generated file and troubleshooting record, but has no QQ,
QQ Space, or memory-writeback send step. It does not exercise the LLM executor,
owner ACL, or the per-tool timeout guard. No generation is retried here.
--recover-last reads the retained native receipt and verifies the existing files;
it never posts a generation request. Public diagnostics strip file paths, so a
unique file is recovered from the owned directory using receipt time and bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("D:/QQBot")
DATA_ROOT = ROOT / "data/astrbot"
PLUGIN_ID = "astrbot_plugin_image_companion"
CONFIG = DATA_ROOT / "config" / f"{PLUGIN_ID}_config.json"
PAGE = "/plugins/extensions/astrbot_plugin_private_companion/page"
PROMPT = (
    "masterpiece, best quality, anime illustration, a cute orange cat beside "
    "a blue ceramic cup, cozy sunny room, clean line art, soft colors"
)
EAST_ASIAN = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
COMPANION_IMAGES = DATA_ROOT / "plugin_data/astrbot_plugin_private_companion/generated_photos"
BROKER_ROOT = ROOT / "data/image-generation"
COMPANION_NAME = re.compile(r"private_companion_troubleshooting_\d{8}_\d{6}_[a-f0-9]{8}\.png\Z")
BROKER_NAME = re.compile(r"image-[a-f0-9]{32}\.png\Z")


class VerificationError(RuntimeError):
    """A safe, fixed diagnostic code rather than an upstream response body."""


def checked_object(value: object, code: str) -> dict:
    if not isinstance(value, dict):
        raise VerificationError(code)
    return value


def page_data(value: object) -> dict:
    envelope = checked_object(value, "invalid_page_response")
    if envelope.get("success") is not True:
        raise VerificationError("page_request_failed")
    return checked_object(envelope.get("data"), "invalid_page_data")


def managed_file(raw: object, *, suffix: str) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 1000:
        raise VerificationError("invalid_artifact_path")
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() != suffix:
        raise VerificationError("invalid_artifact_path")
    resolved = path.resolve(strict=True)
    root = DATA_ROOT.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise VerificationError("artifact_outside_astrbot_data")
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise VerificationError("artifact_link_not_allowed")
        if parent == DATA_ROOT:
            break
    if not stat.S_ISREG(path.stat().st_mode):
        raise VerificationError("artifact_not_regular_file")
    return resolved


def preflight(api) -> dict:
    config = checked_object(json.loads(CONFIG.read_text(encoding="utf-8-sig")), "invalid_config")
    image = checked_object(config.get("image"), "invalid_image_config")
    status = page_data(api("GET", PAGE + "/extensions/image/status", timeout=30))
    generation = checked_object(status.get("generation", {}), "invalid_generation_status")
    with urllib.request.urlopen("http://127.0.0.1:18889/health", timeout=10) as response:
        health = checked_object(json.load(response), "invalid_broker_health")
    fixed_keys = (
        "photo_generation_fixed_prompt", "photo_generation_text2img_fixed_prompt",
        "photo_generation_scene_presets",
    )
    configured_text = " ".join(str(image.get(key, "")) for key in fixed_keys)
    report = {
        "mode": "read_only_preflight",
        "extension_available": status.get("available") is True,
        "generation_available": generation.get("available") is True,
        "generation_count": status.get("generation_count", 0),
        "broker_ready": health.get("status") == "ready",
        "broker_busy": health.get("busy") is True,
        "broker_on_demand": health.get("on_demand") is True,
        "edits_supported": health.get("edits_supported") is True,
        "config_matches_local_backend": (
            config.get("enabled") is True
            and config.get("engine", {}).get("mode") == "legacy"
            and image.get("photo_generation_backend") == "external"
            and image.get("external_image_api_platform") == "openai"
            and image.get("external_image_api_base_url") == "http://127.0.0.1:18889/v1"
            and image.get("external_image_api_model") == "Counterfeit-V3.0"
            and image.get("external_image_api_size") == "512x512"
        ),
        "anime_style_configured": image.get("photo_generation_style") == "二次元",
        "fixed_config_contains_east_asian_text": bool(EAST_ASIAN.search(configured_text)),
        "test_prompt_is_english": not bool(EAST_ASIAN.search(PROMPT)),
        "test_prompt_requests_anime": "anime illustration" in PROMPT,
        "qq_send_steps": 0,
    }
    return report


def receipt_window(result: dict) -> tuple[float, float]:
    end = result.get("ran_at")
    elapsed = result.get("elapsed_ms")
    if type(end) not in (int, float) or type(elapsed) not in (int, float):
        raise VerificationError("receipt_timing_unavailable")
    if not 0 < elapsed <= 930000 or not 0 <= time.time() - end <= 86400:
        raise VerificationError("receipt_timing_out_of_range")
    return end - elapsed / 1000 - 5, end + 5


def resolve_native_artifact(result: dict) -> Path:
    if result.get("path"):
        return managed_file(result["path"], suffix=".png")
    start, end = receipt_window(result)
    matches = []
    for candidate in COMPANION_IMAGES.iterdir():
        if not COMPANION_NAME.fullmatch(candidate.name):
            continue
        checked = managed_file(str(candidate), suffix=".png")
        metadata = checked.stat()
        if start <= metadata.st_mtime <= end and metadata.st_size == result.get("file_size"):
            matches.append(checked)
    if len(matches) != 1:
        raise VerificationError("native_artifact_match_not_unique")
    return matches[0]


def verify_broker_artifact(result: dict, expected_hash: str, file_size: int) -> dict:
    """Match numeric audit metadata and the exact retained backend PNG bytes."""
    start, end = receipt_window(result)
    rows = []
    for audit_path in (BROKER_ROOT / "audit").glob("audit-????-??-??.jsonl"):
        if audit_path.is_symlink() or audit_path.stat().st_size > 10 * 1024 * 1024:
            raise VerificationError("unexpected_broker_audit_file")
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            row = checked_object(json.loads(line), "invalid_broker_audit_row")
            if (type(row.get("at")) in (int, float) and start <= row["at"] <= end
                    and row.get("success") is True and row.get("outcome") == 0
                    and row.get("width") == 512 and row.get("height") == 512
                    and row.get("bytes") == file_size):
                rows.append(row)
    if len(rows) != 1:
        raise VerificationError("broker_audit_match_not_unique")
    matches = []
    generated_root = (BROKER_ROOT / "generated").resolve(strict=True)
    for candidate in generated_root.iterdir():
        if not BROKER_NAME.fullmatch(candidate.name):
            continue
        if (candidate.is_symlink() or not candidate.is_file()
                or not candidate.resolve(strict=True).is_relative_to(generated_root)):
            raise VerificationError("unexpected_broker_image_file")
        metadata = candidate.stat()
        if (start <= metadata.st_mtime <= end and metadata.st_size == file_size
                and hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash):
            matches.append(candidate)
    if len(matches) != 1:
        raise VerificationError("broker_image_hash_match_not_unique")
    return {"audit_matched": True, "backend_image_sha256_matches": True,
            "backend_elapsed_ms": rows[0].get("elapsed_ms"), "backend_path": str(matches[0])}


def verify_generation_result(result: dict, *, recovered: bool) -> dict:
    if result.get("ok") is not True:
        raise VerificationError("native_chain_generation_failed")
    if result.get("called_plugin") != PLUGIN_ID or result.get("workflow_kind") != "text2img":
        raise VerificationError("unexpected_generation_chain")
    path = resolve_native_artifact(result)
    file_size = path.stat().st_size
    if not 0 < file_size <= 25 * 1024 * 1024 or result.get("file_size") != file_size:
        raise VerificationError("invalid_artifact_size")
    from PIL import Image
    with Image.open(path) as image:
        if image.format != "PNG" or image.size != (512, 512):
            raise VerificationError("unexpected_image_dimensions_or_format")
        image.verify()
    artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "mode": "recover_existing_native_chain" if recovered else "native_chain_generation",
        "ok": True,
        "path": str(path),
        "dimensions": [512, 512],
        "bytes": file_size,
        "sha256": artifact_hash,
        "elapsed_ms": result.get("elapsed_ms"),
        "called_plugin": PLUGIN_ID,
        "called_plugin_version": result.get("called_plugin_version"),
        "called_plugin_api_version": result.get("called_plugin_api_version"),
        "qq_send_steps": 0,
        "scope": "Companion native chain -> Image extension -> broker -> local PNG",
        "not_exercised": ["LLM executor", "owner ACL", "per-tool timeout", "QQ send"],
    }
    report.update(verify_broker_artifact(result, artifact_hash, file_size))
    if result.get("prompt_path"):
        prompt_path = managed_file(result["prompt_path"], suffix=".json")
        if prompt_path.stat().st_size > 1024 * 1024:
            raise VerificationError("unexpected_prompt_record_size")
        record = checked_object(json.loads(prompt_path.read_text(encoding="utf-8-sig")), "invalid_prompt_record")
        final_prompt = str(record.get("final_prompt") or "")
        final_hash = hashlib.sha256(final_prompt.encode("utf-8")).hexdigest()
        hashes_match = bool(final_prompt) and final_hash == record.get("submitted_prompt_sha256")
        report["submitted_prompt_checked"] = hashes_match
        report["submitted_prompt_is_english"] = hashes_match and not bool(EAST_ASIAN.search(final_prompt))
        report["submitted_prompt_requests_anime"] = hashes_match and "anime" in final_prompt.lower()
        if not hashes_match or EAST_ASIAN.search(final_prompt) or "anime" not in final_prompt.lower():
            raise VerificationError("unexpected_submitted_prompt")
    else:
        report["submitted_prompt_checked"] = False
    return report


def main(generate: bool, recover_last: bool = False) -> dict:
    from astrbot_api import api
    if recover_last:
        retained = page_data(api("GET", PAGE + "/troubleshooting", timeout=45))
        tests = checked_object(retained.get("chain_tests"), "missing_retained_chain_tests")
        result = checked_object(tests.get("image_generation_text2img"), "missing_retained_image_test")
        return verify_generation_result(result, recovered=True)
    report = preflight(api)
    if not generate:
        return report
    if not all(report[key] for key in (
        "extension_available", "generation_available", "broker_ready",
        "config_matches_local_backend", "test_prompt_is_english", "test_prompt_requests_anime",
    )) or report["broker_busy"]:
        raise VerificationError("generation_preflight_failed")
    result = page_data(api("POST", PAGE + "/troubleshooting/test", {
        "type": "image_generation_text2img", "prompt": PROMPT,
    }, timeout=930))
    return {"preflight": report, "generation": verify_generation_result(result, recovered=False)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true", help="Generate one image via native troubleshooting; never send QQ.")
    mode.add_argument("--recover-last", action="store_true", help="Only read and verify the retained receipt and existing image; never generate.")
    args = parser.parse_args()
    try:
        outcome = main(args.generate, args.recover_last)
    except VerificationError as exc:
        outcome = {"ok": False, "error_code": str(exc)}
    except urllib.error.HTTPError as exc:
        outcome = {"ok": False, "error_code": "http_error", "http_status": exc.code}
    except Exception as exc:  # noqa: BLE001 -- avoid printing response bodies, credentials, or prompt text
        outcome = {"ok": False, "error_code": "verification_exception", "exception_type": type(exc).__name__}
    print(json.dumps(outcome, ensure_ascii=True, indent=2))
    sys.exit(1 if outcome.get("ok") is False else 0)
