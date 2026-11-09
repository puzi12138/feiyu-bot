"""A dedicated, user-operated official billing browser and download worker.

Playwright and every browser object stay on one background thread.  This module
never reads cookies, storage, passwords, existing Chrome profiles, or login form
values.  The user signs in on the official site in the visible dedicated window.
Browser actions use only the exact observed export and fixed day controls.
Daily export retains its observed Today/Yesterday controls. The separate fixed
seven-day audit may select only the observed Near-7-days and All-Keys choices;
it returns bounded summary numbers and never a Key name or authentication data.
"""

from __future__ import annotations

import importlib
import json
import queue
import re
import runpy
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


OFFICIAL_USAGE = "https://platform.deepseek.com/usage"
DEFAULT_PROFILE = Path("D:/QQBot/data/deepseek-billing/browser-profile")
DEFAULT_DOWNLOADS = Path("D:/QQBot/data/token-usage/official-imports/.pending")
BEIJING = timezone(timedelta(hours=8))
TARIFF_POLICY = Path("D:/QQBot/data/astrbot/plugins/feiyu_steward/token_pricing.py")
# Browser actions may wait or cause follow-up requests inside Playwright.  Do
# not start one close to peak; an already issued request cannot be recalled.
AUTOMATIC_BROWSER_LEAD_SECONDS = 120
MAX_DOWNLOAD = 20 * 1024 * 1024
EXPORT_LABELS = ("导出", "Export")
LOGIN_LABELS = ("登录", "Log in", "Sign in")
USAGE_HEADINGS = ("用量", "Usage")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
OPERATOR_ID = re.compile(r"[a-f0-9]{32}\Z")
OPERATOR_COMMANDS = frozenset({"inspect_usage", "inspect_date_options", "select_today_for_audit", "select_yesterday_for_audit", "select_last7_all_for_audit", "summary_today", "summary_yesterday", "summary_last7", "close_browser", "reopen_browser", "shutdown_worker"})
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
OFFICIAL_TIME_NOTICE = "所有日期均按 GMT+8 时间显示，数据可能有 5 分钟延迟。"
_NOTICE_PREFIX = r"\s*".join(re.escape(char) for char in "所有日期均按GMT+8时间显示")
_NOTICE_SUFFIX = r"\s*".join(re.escape(char) for char in "数据可能有5分钟延迟")
OFFICIAL_TIME_NOTICE_PATTERN = re.compile(r"^\s*" + _NOTICE_PREFIX + r"\s*[,，.。]\s*" + _NOTICE_SUFFIX + r"\s*[.。]\s*$")
NOTICE_SEPARATORS = (("，", "cjk_comma"), ("。", "cjk_period"), (",", "ascii_comma"), (".", "ascii_period"))
NOTICE_TERMINATORS = (("。", "cjk_period"), (".", "ascii_period"))
NOTICE_VARIANTS = tuple(
    (separator_code, terminator_code,
     re.compile(r"^\s*" + _NOTICE_PREFIX + r"\s*" + re.escape(separator) + r"\s*"
                + _NOTICE_SUFFIX + r"\s*" + re.escape(terminator) + r"\s*$"))
    for separator, separator_code in NOTICE_SEPARATORS
    for terminator, terminator_code in NOTICE_TERMINATORS
)
UI_SCOPE_SCHEMA = "deepseek_usage_gmt8_filters_v1"
DATE_OPTIONS_SCHEMA = "deepseek_visible_date_options_v1"
DATE_OPTION_LABEL = re.compile(r"(?:今天|昨天|本周|上周|本月|上月|自定义|选择日期|时间范围|日期范围|近(?:[1-9]\d{0,2})天|最近(?:[1-9]\d{0,2})天)\Z")
LAST7_LABEL = re.compile(r"^近\s*7\s*天$")
LAST30_LABEL = re.compile(r"^近\s*30\s*天$")

# This audit is deliberately tied to the two labels seen in the real official
# screenshot. It returns a boolean only, never neighboring text or Key names.
_YESTERDAY_FILTER_SCOPE_SCRIPT = """label => {
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const text = el => (el.textContent || '').trim();
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').trim();
    const openRole = '[aria-expanded="true"],[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]';
    if (text(label) !== '时间维度' || !visible(label) || label.closest(openRole)) return false;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 3; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        if (elements.some(el => ['API Key', '导出', '调用类型'].includes(text(el)))) break;
        if (group.matches(openRole) || elements.some(el => el.matches(openRole))) return false;
        const leaves = elements.filter(el => !el.contains(label) && !label.contains(el)
            && !['svg', 'path', 'title'].includes((el.tagName || '').toLowerCase()) && Boolean(ownText(el)));
        if (leaves.length === 1 && ownText(leaves[0]) === '昨天') return true;
    }
    return false;
}"""

# Discover only visible date-option-shaped leaf labels. Neither arbitrary body
# text nor values/attributes from inputs, accounts or keys can leave the page.
_DATE_OPTIONS_SCRIPT = """body => {
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const allowed = /^(今天|昨天|本周|上周|本月|上月|自定义|选择日期|时间范围|日期范围|近[1-9]\\d{0,2}天|最近[1-9]\\d{0,2}天)$/;
    const labels = [];
    let scanned = 0;
    for (const el of body.querySelectorAll('*')) {
        if (++scanned > 10000 || labels.length >= 32) break;
        if (!visible(el) || ['INPUT', 'TEXTAREA', 'SCRIPT', 'STYLE', 'svg', 'path', 'title'].includes(el.tagName)) continue;
        const label = Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
            .map(node => node.textContent).join('').replace(/\\s/g, '');
        if (label.length <= 40 && allowed.test(label) && !labels.includes(label)) labels.push(label);
    }
    return labels;
}"""

# Read only a fixed selected date value adjacent to the observed time label.
# Unlike the closed-filter click guard, this may inspect an open filter, but
# menu/option nodes cannot establish what is currently selected.
_DATE_FILTER_SELECTED_SCRIPT = """(label, expected) => {
    if (!['今天', '昨天', '近7天', '近30天'].includes(expected)) return false;
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const compact = el => (el.textContent || '').replace(/\\s/g, '');
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    if (compact(label) !== '时间维度' || !visible(label)
        || label.closest('[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]')) return false;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 3; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        if (elements.some(el => ['APIKey', '导出', '调用类型'].includes(compact(el)))) break;
        const choices = elements.filter(el => !el.contains(label) && !label.contains(el)
            && !el.closest('[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]')
            && !['svg', 'path', 'title'].includes((el.tagName || '').toLowerCase()) && Boolean(ownText(el)));
        if (choices.length === 1 && ownText(choices[0]) === expected) return true;
    }
    return false;
}"""

# Only a CLOSED time-dimension control proves its selected calendar day.
# An expanded list of choices, chart label or neighboring Key cannot do so.
_CALENDAR_DAY_SCOPE_SCRIPT = """label => {
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const compact = el => (el.textContent || '').replace(/\\s/g, '');
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    const openRole = '[aria-expanded="true"],[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]';
    if (compact(label) !== '时间维度' || !visible(label) || label.closest(openRole)) return null;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 3; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        if (elements.some(el => ['APIKey', '导出', '调用类型'].includes(compact(el)))) break;
        if (group.matches(openRole) || elements.some(el => el.matches(openRole))) return null;
        const choices = elements.filter(el => !el.contains(label) && !label.contains(el)
            && !['svg', 'path', 'title'].includes((el.tagName || '').toLowerCase()) && Boolean(ownText(el)));
        if (choices.length === 1) {
            const value = ownText(choices[0]);
            if (value === '今天') return 'today';
            if (value === '昨天') return 'yesterday';
        }
    }
    return null;
}"""

# The seven labels were observed together in the official date menu. Only a
# unique visible Today inside exactly this date-only menu may be clicked.
_DAY_MENU_SCOPE_SCRIPT = """(choice, expected) => {
    if (!['今天', '昨天', '近7天'].includes(expected)) return false;
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    const allowed = ['今天', '昨天', '近7天', '近30天', '本月', '上月', '自定义'];
    if (ownText(choice) !== expected || !visible(choice)) return false;
    let group = choice.parentElement;
    for (let depth = 0; group && depth < 5; depth++, group = group.parentElement) {
        const labels = Array.from(group.querySelectorAll('*')).filter(el => visible(el)
            && !['svg', 'path', 'title'].includes((el.tagName || '').toLowerCase()))
            .map(ownText).filter(Boolean);
        if (labels.some(label => !allowed.includes(label))) return false;
        if (labels.length === allowed.length && allowed.every(label => labels.includes(label))) return true;
    }
    return false;
}"""
_TODAY_MENU_SCOPE_SCRIPT = "choice => (" + _DAY_MENU_SCOPE_SCRIPT + ")(choice, '今天')"
_YESTERDAY_MENU_SCOPE_SCRIPT = "choice => (" + _DAY_MENU_SCOPE_SCRIPT + ")(choice, '昨天')"
_LAST7_MENU_SCOPE_SCRIPT = "choice => (" + _DAY_MENU_SCOPE_SCRIPT + ")(choice, '近7天')"

# Open only the single selected value in an already verified closed Key filter.
# The selected name is used only inside this DOM scope and is never returned.
_KEY_FILTER_OPEN_SCRIPT = """label => {
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const compact = el => (el.textContent || '').replace(/\\s/g, '');
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    const openRole = '[aria-expanded="true"],[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]';
    if (compact(label) !== 'APIKey' || !visible(label) || label.closest(openRole)) return false;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 3; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        if (elements.some(el => ['时间维度', '导出', '调用类型'].includes(compact(el)))) break;
        if (group.matches(openRole) || elements.some(el => el.matches(openRole))) return false;
        const choices = elements.filter(el => !el.contains(label) && !label.contains(el)
            && !['svg', 'path', 'title', 'input', 'textarea'].includes((el.tagName || '').toLowerCase())
            && !el.children.length && Boolean(ownText(el)));
        if (choices.length === 1 && ownText(choices[0]) !== '全部') {
            const trigger = choices[0].closest('[aria-controls],[aria-owns]');
            if (!trigger || (!group.contains(trigger) && !trigger.contains(group))) return false;
            const menuId = (trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns') || '').trim();
            if (!/^[A-Za-z0-9_:.-]{1,128}$/.test(menuId)) return false;
            choices[0].click();
            return {opened: true, menu_id: menuId};
        }
    }
    return false;
}"""

# An All option must be outside the closed toolbar/card and inside a visible
# options-only ancestor. Other option names stay in the DOM and are not read out.
_KEY_ALL_MENU_SCOPE_SCRIPT = """(choice, menuId) => {
    const visible = el => {
        const s = getComputedStyle(el);
        return el.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const compact = el => (el.textContent || '').replace(/\\s/g, '');
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    if (typeof menuId !== 'string' || !/^[A-Za-z0-9_:.-]{1,128}$/.test(menuId)) return false;
    const controlled = document.getElementById(menuId);
    const option = choice.closest('[role="option"],[role="menuitem"]');
    if (!option || !visible(option) || !visible(choice) || ownText(choice) !== '全部'
        || choice.closest('a,input,textarea,form')) return false;
    const menu = option.closest('[role="listbox"],[role="menu"]');
    const allOptions = controlled ? Array.from(controlled.querySelectorAll('[role="option"],[role="menuitem"]'))
        .filter(el => visible(el) && compact(el) === '全部') : [];
    return Boolean(controlled && visible(controlled) && controlled.contains(choice)
        && menu && visible(menu) && (controlled === menu || controlled.contains(menu))
        && allOptions.length === 1 && allOptions[0].contains(choice));
}"""

# Called only on one exact allowlisted headline. Return one numeric candidate,
# never arbitrary neighboring text, a whole page, input values or account data.
_USAGE_HEADLINE_NUMBER_SCRIPT = """(label, kind) => {
    const allowed = {cost: '消费金额', requests: 'API请求次数', tokens: 'Tokens'};
    const visible = el => {
        const s = getComputedStyle(el);
        if (!el.getClientRects().length || s.visibility === 'hidden' || s.display === 'none') return false;
        for (let node = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.opacity === '0' || style.visibility === 'hidden' || style.display === 'none') return false;
        }
        if (typeof el.getBoundingClientRect === 'function') {
            const r = el.getBoundingClientRect();
            if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
            if (typeof document.elementFromPoint === 'function') {
                const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                if (!hit || (!el.contains(hit) && !hit.contains(el))) return false;
            }
        }
        return true;
    };
    const compact = el => (el.textContent || '').replace(/\\s/g, '');
    const ownText = el => Array.from(el.childNodes || []).filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').replace(/\\s/g, '');
    if (!Object.hasOwn(allowed, kind) || compact(label) !== allowed[kind] || !visible(label)) return null;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 4; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        if (elements.some(el => Object.values(allowed).some(other => other !== allowed[kind] && compact(el) === other))) return null;
        if (group.querySelector('input,textarea,form')) return null;
        const leaves = elements.filter(el => !['svg', 'path', 'title'].includes((el.tagName || '').toLowerCase()) && Boolean(ownText(el)));
        const numeric = leaves.map(ownText).filter(text => text.length <= 40
            && (kind === 'cost' ? /^[¥￥]?(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:[.][0-9]{1,12})?(?:CNY)?$/.test(text)
                              : /^(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)$/.test(text)));
        if (numeric.length === 1) {
            if (kind === 'cost' && !leaves.some(el => /CNY/.test(ownText(el)))
                && !/^[¥￥]/.test(numeric[0])) return null;
            return numeric[0].replace(/^[¥￥]/, '').replace(/CNY$/, '').replace(/,/g, '');
        }
        if (numeric.length > 1) return null;
    }
    return null;
}"""

# Evaluated only on an exact, already observed filter label. No selected Key
# value, page-wide text, credential or arbitrary attribute is returned.
_FILTER_SCOPE_SCRIPT = """(label, kind) => {
    const compact = text => (text || '').replace(/\\s/g, '');
    const visible = el => {
        const style = getComputedStyle(el);
        return el.getClientRects().length && style.visibility !== 'hidden' && style.display !== 'none';
    };
    if (label.closest('[aria-expanded="true"],[role="option"],[role="menuitem"],[role="listbox"],[role="menu"]')) return null;
    let group = label.parentElement;
    for (let depth = 0; group && depth < 3; depth++, group = group.parentElement) {
        const elements = Array.from(group.querySelectorAll('*')).filter(visible);
        // An open menu shows choices, not evidence of the selected value.
        if (group.getAttribute('aria-expanded') === 'true' || elements.some(el =>
            el.getAttribute('aria-expanded') === 'true' || ['option', 'menuitem', 'listbox', 'menu'].includes(el.getAttribute('role')))) return null;
        // Do not walk into the other filter or the surrounding export toolbar.
        const forbidden = kind === 'key' ? ['时间维度', '导出', '调用类型'] : ['APIKey', '导出'];
        if (elements.some(el => forbidden.includes(compact(el.textContent)))) break;
        const selections = elements.filter(el => !el.contains(label) && !label.contains(el)
            && !el.children.length && Boolean(el.textContent.trim()));
        if (kind === 'key') {
            if (selections.length === 1 && compact(selections[0].textContent) === '全部') return 'all';
            // A single visibly selected nonempty adjacent value may be
            // classified locally even in the observed custom div control;
            // its actual Key name is never copied or returned. The chart's
            // separate "调用类型 / API Key" toggle is explicitly excluded.
            if (selections.length === 1) return 'filtered';
        } else {
            const matches = selections.flatMap(el => {
                const matched = compact(el.textContent).match(/^近([1-9]\\d{0,2})天$/);
                return matched ? [Number(matched[1])] : [];
            });
            const days = [...new Set(matches)].filter(value => value <= 366);
            if (selections.length === 1 && days.length === 1) return days[0];
        }
    }
    return null;
}"""


@dataclass(frozen=True)
class DateControls:
    """Optional controls, configured ONLY after observing the real official UI.

    Empty defaults disable custom input-based date changes. The independent
    fixed Today/Yesterday adapter requires its observed labels and GMT+8 fact.
    An exact
    accessible label and a confirmed rendered timezone are required.  Only
    ISO-date inputs are supported; arbitrary CSS, JS, URLs and click sequences
    cannot be supplied by this adapter.
    """

    start_label: str = ""
    end_label: str = ""
    apply_button: str = ""
    timezone_label: str = ""
    key_filter_label: str = ""
    all_keys_text: str = ""
    # Must identify a RESULT summary that updates only after filters finish
    # applying, not the editable date inputs themselves.  Verified on real UI.
    applied_range_template: str = ""
    start_date_inclusive: bool = False
    end_date_inclusive: bool = False

    def __post_init__(self):
        for label in (self.start_label, self.end_label, self.apply_button,
                      self.timezone_label, self.key_filter_label, self.all_keys_text):
            if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 for c in label):
                raise ValueError("Controls require short exact visible labels.")
        # This is a billing filter adapter, never an authentication form reader.
        for label in (self.start_label, self.end_label, self.key_filter_label):
            if any(word in label.casefold() for word in ("password", "cookie", "token", "密码", "验证码")):
                raise ValueError("Authentication controls are not supported.")
        if self.timezone_label and self.timezone_label not in ("UTC+8", "UTC+08:00", "北京时间", "Asia/Shanghai"):
            raise ValueError("Only an explicitly observed Beijing timezone is supported.")
        template = self.applied_range_template
        if not isinstance(template, str):
            raise ValueError("Applied range must be a text template.")
        if template:
            rest = template.replace("{start}", "").replace("{end}", "")
            if (len(template) > 140 or any(ord(c) < 32 for c in template)
                    or "{start}" not in template or "{end}" not in template
                    or "{" in rest or "}" in rest):
                raise ValueError("Applied range requires an exact observed result text with date placeholders.")


def _official_page(url: str, *, usage_only: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "platform.deepseek.com"
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and (not usage_only or parsed.path.rstrip("/") == "/usage")
        )
    except (TypeError, ValueError):
        return False


def _visible_exact(page, role: str, label: str):
    if not label:
        return None
    candidate = page.get_by_role(role, name=label, exact=True)
    if candidate.count() == 1 and candidate.is_visible():
        return candidate
    return None


def _labelled_control(page, label: str):
    if not label:
        return None
    candidate = page.get_by_label(label, exact=True)
    if candidate.count() == 1 and candidate.is_visible():
        return candidate
    return None


def _read_date(page, label: str) -> str | None:
    control = _labelled_control(page, label)
    if control is None:
        return None
    try:
        value = control.input_value(timeout=1000).strip()
        if ISO_DAY.fullmatch(value):
            return date.fromisoformat(value).isoformat()
    except Exception:
        pass
    return None


def _official_notice_evidence(page) -> dict:
    """Diagnose only the known GMT+8/5-minute notice, never return its text."""
    result = {"matched": False, "element_count": 0, "visible_unique": False,
              "separator": None, "terminator": None, "variant_counts": {}}
    if not _official_page(page.url, usage_only=True):
        return result
    fixed = page.get_by_text(OFFICIAL_TIME_NOTICE_PATTERN, exact=True)
    count = fixed.count()
    result["element_count"] = min(count, 33)
    result["visible_unique"] = count == 1 and fixed.is_visible()
    matching_variants = []
    for separator, terminator, pattern in NOTICE_VARIANTS:
        candidate = page.get_by_text(pattern, exact=True)
        variant_count = candidate.count()
        result["variant_counts"][separator + "__" + terminator] = min(variant_count, 33)
        if variant_count == 1 and candidate.is_visible():
            matching_variants.append((separator, terminator))
    if result["visible_unique"] and len(matching_variants) == 1:
        result["matched"] = True
        result["separator"], result["terminator"] = matching_variants[0]
    return result


def _official_ui_scope(page, *, observed_at: datetime | None = None) -> dict:
    """Read only controls and text proven by the official usage screenshot."""
    scope = {
        "schema": UI_SCOPE_SCHEMA, "timezone_evidence": None,
        "fixed_text_evidence": [], "data_lag_minutes": None,
        "relative_range": None, "key_filter": "unknown",
    }
    if not _official_page(page.url, usage_only=True):
        return scope
    # Match only the known fixed sentence, allowing whitespace inserted by
    # spans/layout. No body text, input values or neighboring labels are read.
    if _official_notice_evidence(page)["matched"]:
        scope.update(timezone_evidence="official_gmt8_notice",
                     fixed_text_evidence=[OFFICIAL_TIME_NOTICE], data_lag_minutes=5)
    key_label = page.get_by_text("API Key", exact=True)
    key_count = key_label.count()
    # The chart also contains an "API Key" display toggle.  Only a matched
    # filter group can provide scope; chart text alone is not selection proof.
    classifications = set()
    if 0 < key_count <= 8:
        for index in range(key_count):
            label = key_label.nth(index)
            if not label.is_visible():
                continue
            classified = label.evaluate(_FILTER_SCOPE_SCRIPT, "key")
            if classified in ("all", "filtered"):
                classifications.add(classified)
    if len(classifications) == 1:
        scope["key_filter"] = next(iter(classifications))
    time_label = page.get_by_text("时间维度", exact=True)
    if time_label.count() == 1 and time_label.is_visible():
        days = time_label.evaluate(_FILTER_SCOPE_SCRIPT, "time")
        if type(days) is int and 1 <= days <= 366:
            scope["relative_range"] = {"kind": "last_n_days", "days": days}
        # The observed Today/Yesterday calendar choices cover a whole GMT+8
        # natural day, inclusive UI endpoints. CSV content remains the second
        # authority for the actual exported rows and completion cutoff.
        export_visible = any((button := _visible_exact(page, "button", label)) is not None
                             and button.is_enabled() for label in EXPORT_LABELS)
        no_login = not any(_visible_exact(page, "button", label) is not None for label in LOGIN_LABELS)
        if scope["timezone_evidence"] == "official_gmt8_notice" and export_visible and no_login:
            selected = time_label.evaluate(_CALENDAR_DAY_SCOPE_SCRIPT)
            # A custom popup may be portalled outside the filter and expose no
            # ARIA role. Its other fixed day option must not be mistaken for a
            # closed selection, even then. Read only these two known labels.
            selected_label = "今天" if selected == "today" else "昨天" if selected == "yesterday" else ""
            other_label = "昨天" if selected == "today" else "今天"
            selected_control = page.get_by_text(selected_label, exact=True) if selected_label else None
            other_control = page.get_by_text(other_label, exact=True)
            other_count = other_control.count()
            no_other_day = (other_count == 0 or (other_count <= 8 and not any(
                other_control.nth(index).is_visible() for index in range(other_count))))
            selection_unique = (selected_control is not None and selected_control.count() == 1
                                and selected_control.is_visible() and no_other_day)
            now = observed_at if observed_at is not None else datetime.now(BEIJING)
            if (selection_unique and selected in ("today", "yesterday") and isinstance(now, datetime)
                    and now.tzinfo is not None and now.utcoffset() is not None):
                day = now.astimezone(BEIJING).date() - timedelta(days=selected == "yesterday")
                scope["relative_range"] = {"kind": "calendar_day", "selection": selected}
                scope["calendar_selection"] = {
                    "start": day.isoformat(), "end": day.isoformat(), "inclusive": True,
                    "timezone": "UTC+08:00", "evidence": "closed_time_dimension_fixed_day",
                }
    return scope


def _range_metadata(page, controls: DateControls, *, observed_at: datetime | None = None) -> dict:
    ui_scope = _official_ui_scope(page, observed_at=observed_at)
    start = _read_date(page, controls.start_label)
    end = _read_date(page, controls.end_label)
    timezone_known = ui_scope["timezone_evidence"] == "official_gmt8_notice"
    if controls.timezone_label:
        text = page.get_by_text(controls.timezone_label, exact=True)
        timezone_known = timezone_known or (text.count() == 1 and text.is_visible())
    applied = False
    if start and end and controls.applied_range_template:
        label = controls.applied_range_template.format(start=start, end=end)
        summary = page.get_by_text(label, exact=True)
        applied = summary.count() == 1 and summary.is_visible()
    known = bool(start and end and start <= end and timezone_known and applied
                 and controls.start_date_inclusive and controls.end_date_inclusive)
    calendar = ui_scope.get("calendar_selection")
    if isinstance(calendar, dict):
        if known and (start != calendar["start"] or end != calendar["end"]):
            # Conflicting independent controls do not establish export scope.
            known, start, end = False, None, None
        else:
            known, start, end = True, calendar["start"], calendar["end"]
    key_filter = ui_scope["key_filter"]
    key = _labelled_control(page, controls.key_filter_label)
    if key is not None and controls.all_keys_text:
        try:
            # Compare locally, retaining no selected name, key or account data.
            value = key.input_value(timeout=1000).strip()
        except Exception:
            value = key.inner_text(timeout=1000).strip()
        key_filter = "all" if value == controls.all_keys_text else "filtered"
    return {
        "date_range": {"status": "verified" if known else "unknown", "start": start,
                       "end": end, "inclusive": bool(known)},
        "timezone": "UTC+08:00" if timezone_known else "unknown",
        "key_filter": key_filter,
        "ui_scope": ui_scope,
        # Exporting all account keys does not establish which calls were Feiyu.
        "automatic_compare_allowed": False,
    }


class _AutomaticPeakPaused(PermissionError):
    pass


class _PlaywrightSession:
    def __init__(self, profile_dir: Path, download_dir: Path):
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.playwright = None
        self.context = None
        self.page = None
        self.preflight = lambda: None

    def open(self):
        self.preflight()
        sync_api = importlib.import_module("playwright.sync_api")
        self.playwright = sync_api.sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chrome", headless=False,
            accept_downloads=True, downloads_path=str(self.download_dir),
        )
        # Never attach to an existing user Chrome profile or existing browser.
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.preflight()
        self.page.goto(OFFICIAL_USAGE, wait_until="domcontentloaded", timeout=45000)
        return self.page

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.playwright is not None:
                self.playwright.stop()


class BrowserManager:
    def __init__(
        self, *, import_callback: Callable[[Path, dict], dict],
        profile_dir: Path = DEFAULT_PROFILE,
        download_dir: Path = DEFAULT_DOWNLOADS,
        runtime_factory=None, clock=None, poll_seconds: float = 3.0,
        date_controls: DateControls | None = None,
        inspect_ready_once: bool = False,
        scheduled_mode: str = "verified_yesterday",
    ):
        self.profile_dir = Path(profile_dir)
        self.download_dir = Path(download_dir)
        self._import_callback = import_callback
        self._runtime_factory = runtime_factory or _PlaywrightSession
        self._clock = clock or (lambda: datetime.now(BEIJING))
        self._poll_seconds = max(0.02, float(poll_seconds))
        self._controls = date_controls or DateControls()
        if scheduled_mode not in ("verified_yesterday", "current_official_snapshot", "on_demand"):
            raise ValueError("Unsupported scheduled export mode.")
        self._scheduled_mode = scheduled_mode
        self._inspect_ready_once = bool(inspect_ready_once)
        self._inspection_done = False
        self._operator_last_id = None
        self._automatic_query_active = False
        self._automatic_open_pending = False
        self._automatic_open_generation = 0
        self._state_file = self.profile_dir.parent / "browser-worker-state.json"
        self._lock = threading.RLock()
        self._commands: queue.Queue = queue.Queue()
        self._pending: set[str] = set()
        self._thread = None
        self._stop = False
        self._closing = False
        self._session = None
        self._page = None
        self._last_scheduled_day = None
        self._last_scheduled_open_day = None
        self._state = {
            "state": "closed", "message": "独立浏览器尚未打开。",
            "can_open": True, "can_export": False, "range_verified": False,
            "last_export_at": None, "schedule_state": "waiting_for_browser",
            "login_persistence": "专用本机资料目录会保留登录；有效期由官网决定。",
        }
        self._restore_schedule()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone-aware.")
        return now.astimezone(BEIJING)

    def _require_automatic_off_peak(self):
        """Use the live, verified pricing calendar; unknown fails closed."""
        if not self._automatic_query_active:
            return
        try:
            policy = runpy.run_path(str(TARIFF_POLICY))
            peak = policy.get("_peak")
            now = self._now().timestamp()
            if (callable(peak) and peak(now) is False
                    and peak(now + AUTOMATIC_BROWSER_LEAD_SECONDS) is False):
                return
        except Exception:
            pass
        raise _AutomaticPeakPaused("peak_tariff")

    def _restore_schedule(self):
        try:
            if self._state_file.stat().st_size > 4096 or self._state_file.is_symlink():
                return
            stored = json.loads(self._state_file.read_text(encoding="utf-8"))
            day = stored.get("last_scheduled_day")
            if isinstance(day, str) and ISO_DAY.fullmatch(day):
                self._last_scheduled_day = date.fromisoformat(day).isoformat()
            opened_day = stored.get("last_scheduled_open_day")
            if isinstance(opened_day, str) and ISO_DAY.fullmatch(opened_day):
                self._last_scheduled_open_day = date.fromisoformat(opened_day).isoformat()
            last = stored.get("last_export_at")
            if isinstance(last, str) and datetime.fromisoformat(last).tzinfo is not None:
                self._state["last_export_at"] = last
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_schedule(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        pending = self._state_file.with_name("browser-worker-state.pending.json")
        payload = {"last_scheduled_day": self._last_scheduled_day,
                   "last_scheduled_open_day": self._last_scheduled_open_day,
                   "last_export_at": self._state["last_export_at"]}
        pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        pending.replace(self._state_file)

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "automatic_open_deferred": bool(self._automatic_open_pending)}

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _submit(self, command: str, argument=None) -> dict:
        with self._lock:
            if command == "close":
                if self._thread is None or not self._thread.is_alive():
                    self._state.update(state="closed", can_open=True, can_export=False,
                                       message="独立浏览器已关闭，专用登录资料已保留。")
                    return dict(self._state)
                self._closing = True
                self._state.update(can_export=False, can_open=False)
            elif self._stop or self._closing:
                if command in ("open", "open_automatic") and (self._thread is None or not self._thread.is_alive()):
                    self._stop = False
                    self._closing = False
                else:
                    return dict(self._state)
            if self._thread is None or not self._thread.is_alive():
                if command not in ("open", "open_automatic"):
                    return dict(self._state)
                # An ordinary service exit must not kill Chrome halfway through
                # closing its profile. shutdown() provides an explicit join.
                self._thread = threading.Thread(target=self._run, name="deepseek-billing-browser", daemon=False)
                self._thread.start()
            if command not in self._pending:
                self._pending.add(command)
                self._commands.put((command, argument))
                if command in ("open", "open_automatic"):
                    self._state.update(state="opening", can_open=False, can_export=False,
                                       message="正在打开独立的官方用量页面。")
                elif command == "export":
                    self._state.update(state="exporting", can_export=False,
                                       message="正在检查官网当前筛选并导出账单。")
            return dict(self._state)

    def open(self) -> dict:
        with self._lock:
            self._automatic_open_generation += 1
            self._automatic_open_pending = False
        return self._submit("open")

    def open_automatically(self) -> dict:
        """Queue only the configured startup open, with the automatic gate."""
        with self._lock:
            self._automatic_open_generation += 1
            generation = self._automatic_open_generation
        return self._submit("open_automatic", generation)

    def export(self) -> dict:
        """Request export of CURRENT official filters; never invent yesterday."""
        with self._lock:
            if not self._state["can_export"]:
                return dict(self._state)
        return self._submit("export")

    manual_export_request = export

    def close(self) -> dict:
        with self._lock:
            self._automatic_open_generation += 1
            self._automatic_open_pending = False
        return self._submit("close")

    def shutdown(self, timeout: float = 90.0) -> dict:
        """Process-exit hook only; HTTP handlers should use nonblocking close()."""
        self.close()
        thread = self._thread
        if thread is not None and thread.ident != threading.get_ident():
            thread.join(timeout=max(0, float(timeout)))
        return self.status()

    def request_screenshot(self) -> dict:
        """Operator-only local preview; do not expose this through the portal."""
        return self._submit("screenshot")

    def request_inspection(self) -> dict:
        """Local diagnostics of allowlisted UI controls; no form values/body."""
        return self._submit("inspect")

    def _run(self):
        try:
            while not self._stop:
                try:
                    command, argument = self._commands.get(timeout=self._poll_seconds)
                except queue.Empty:
                    self._tick()
                    continue
                try:
                    if command == "open":
                        self._automatic_open_pending = False
                        self._open()
                    elif command == "open_automatic":
                        with self._lock:
                            current = argument == self._automatic_open_generation
                            if current:
                                self._automatic_open_pending = True
                        if current:
                            self._attempt_automatic_open()
                    elif command == "export":
                        self._export("manual")
                    elif command == "screenshot" and self._export_button() is not None:
                        self._page.screenshot(path=str(self.profile_dir.parent / "browser-preview.png"), full_page=False)
                    elif command == "inspect":
                        self._inspect()
                    elif command == "close":
                        self._stop = True
                except ModuleNotFoundError:
                    self._update(state="unavailable", can_open=True, can_export=False,
                                 message="独立浏览器运行组件尚未就绪。")
                except Exception:
                    self._update(state="error", can_open=True, can_export=False,
                                 message="浏览器操作未确认完成；请查看官方窗口后重试。")
                finally:
                    with self._lock:
                        self._pending.discard(command)
                    self._commands.task_done()
        finally:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
            self._session = None
            self._page = None
            with self._lock:
                while True:
                    try:
                        self._commands.get_nowait()
                        self._commands.task_done()
                    except queue.Empty:
                        break
                self._pending.clear()
            self._update(state="closed", can_open=True, can_export=False,
                         schedule_state="waiting_for_browser",
                         message="独立浏览器已关闭，专用登录资料已保留。")

    def _open(self):
        self._require_automatic_off_peak()
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    self._page.bring_to_front()
                    self._observe()
                    return
            except Exception:
                pass
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._session = self._runtime_factory(self.profile_dir, self.download_dir)
        if isinstance(self._session, _PlaywrightSession):
            self._session.preflight = self._require_automatic_off_peak
        self._page = self._session.open()
        self._observe()

    def _attempt_automatic_open(self):
        """Defer one startup open until off peak; never retry an uncertain open."""
        with self._lock:
            if not self._automatic_open_pending:
                return
            generation = self._automatic_open_generation
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    with self._lock:
                        self._automatic_open_pending = False
                    return
            except Exception:
                with self._lock:
                    self._automatic_open_pending = False
                self._update(state="error", can_open=True, can_export=False,
                             message="独立浏览器状态未确认，请手动检查。")
                return
        self._automatic_query_active = True
        try:
            self._require_automatic_off_peak()
            with self._lock:
                if generation != self._automatic_open_generation:
                    return
                self._automatic_open_pending = False
            self._open()
        except _AutomaticPeakPaused:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    with self._lock:
                        self._automatic_open_pending = False
                    self._update(state="error", can_open=True, can_export=False,
                                 message="独立浏览器关闭结果未确认，请手动检查。")
                    return
                self._session = None
                self._page = None
            with self._lock:
                if generation == self._automatic_open_generation:
                    self._automatic_open_pending = True
            self._update(state="closed", can_open=True, can_export=False,
                         schedule_state="peak_tariff", message="自动打开等待非高峰时段。")
        finally:
            self._automatic_query_active = False

    def _export_button(self):
        if self._page is None or not _official_page(self._page.url, usage_only=True):
            return None
        for label in EXPORT_LABELS:
            button = _visible_exact(self._page, "button", label)
            if button is not None and button.is_enabled():
                return button
        return None

    def _inspect(self):
        if self._page is None or not _official_page(self._page.url, usage_only=True) or self._export_button() is None:
            return
        buttons = []
        for label in (*LOGIN_LABELS, *EXPORT_LABELS, "查询", "应用", "确定", "昨天", "今天", "最近7天", "最近30天"):
            button = _visible_exact(self._page, "button", label)
            if button is not None:
                buttons.append({"label": label, "enabled": bool(button.is_enabled())})
        timezones = []
        for label in ("UTC+8", "UTC+08:00", "北京时间", "Asia/Shanghai"):
            text = self._page.get_by_text(label, exact=True)
            if text.count() == 1 and text.is_visible():
                timezones.append(label)
        # Attribute-only observation of visible DATE controls.  Never read
        # input.value, form content, profile storage or page-wide text.
        dates = self._page.locator("input").evaluate_all("""elements => elements.flatMap(el => {
            const style = getComputedStyle(el);
            if (!el.getClientRects().length || style.visibility === 'hidden' || style.display === 'none') return [];
            const type = (el.getAttribute('type') || '').toLowerCase();
            const placeholder = el.getAttribute('placeholder') || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            const labels = Array.from(el.labels || []).map(label => label.textContent.trim());
            const hint = [placeholder, ariaLabel, ...labels].join(' ');
            if (!['date', 'datetime-local'].includes(type) && !/(日期|开始时间|结束时间|start date|end date|yyyy[-/]mm[-/]dd)/i.test(hint)) return [];
            const safe = s => s.length <= 80 && !/[\\u0000-\\u001f]/.test(s) && !/(password|token|cookie|密码|验证码)/i.test(s);
            return [{type, placeholder: safe(placeholder) ? placeholder : '',
                     aria_label: safe(ariaLabel) ? ariaLabel : '', labels: labels.filter(safe)}];
        })""")
        result = {"official_usage_page": _official_page(self._page.url, usage_only=True),
                  "buttons": buttons, "date_controls": dates[:12], "timezone_labels": timezones,
                  "note": "只记录可见控件标签与日期属性，不读取输入值、账号、Key或页面全文。"}
        # Some official pages render a custom date picker as plain spans.  Keep
        # only date-shaped visible text, never a container's arbitrary content.
        rendered_dates = self._page.locator("body").evaluate("""body => {
            const visible = el => {
                const s = getComputedStyle(el);
                return el.getClientRects().length && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const date = /(?:20\\d{2})[-/年](?:1[0-2]|0?[1-9])[-/月](?:3[01]|[12]\\d|0?[1-9])(?!\\d)日?/g;
            const onlyDateText = text => text.length <= 140 && /^[\\d年/月日:：.\\-\\s~～—至（）()]+$/.test(text) && date.test(text);
            const fragments = [];
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            let node, scanned = 0;
            while ((node = walker.nextNode()) && scanned++ < 10000 && fragments.length < 32) {
                if (!node.parentElement || !visible(node.parentElement)) continue;
                const text = node.textContent.trim();
                date.lastIndex = 0;
                if (onlyDateText(text)) {
                    date.lastIndex = 0;
                    fragments.push(...Array.from(text.matchAll(date), match => match[0]));
                }
            }
            const controls = [];
            for (const el of body.querySelectorAll('button,[role="button"],[role="combobox"],[tabindex],select')) {
                if (controls.length >= 20 || !visible(el)) continue;
                const text = (el.innerText || '').trim();
                const label = el.getAttribute('aria-label') || '';
                date.lastIndex = 0;
                const named = /^(今天|昨天|近7天|近30天|最近7天|最近30天|本月|上月|自定义|选择日期|时间范围|日期范围)$/.test(text);
                if (onlyDateText(text) || named) {
                    controls.push({tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                        text, aria_label: /^(选择日期|时间范围|日期范围)$/.test(label) ? label : ''});
                }
            }
            return {fragments: [...new Set(fragments)], controls};
        }""")
        safe_fragments = []
        for fragment in rendered_dates.get("fragments", [])[:32]:
            if not isinstance(fragment, str):
                continue
            normalized = fragment.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
            try:
                year, month, day = (int(part) for part in normalized.split("-"))
                date(year, month, day)
                safe_fragments.append(fragment)
            except (ValueError, TypeError):
                pass
        result["date_fragments"] = safe_fragments
        result["rendered_date_controls"] = rendered_dates.get("controls", [])[:20]
        result["inspection_version"] = 2
        result["notice_evidence"] = _official_notice_evidence(self._page)
        result["ui_scope"] = _official_ui_scope(self._page, observed_at=self._now())
        path = self.profile_dir.parent / "browser-controls.json"
        self._atomic_json(path, result)
        self._inspection_done = True

    def _visible_date_option_labels(self):
        raw = self._page.locator("body").evaluate(_DATE_OPTIONS_SCRIPT)
        if not isinstance(raw, list) or len(raw) > 32:
            return []
        # Apply the same whitelist again outside the page. Treat all browser
        # output as untrusted and never serialize an unexpected string.
        return list(dict.fromkeys(value for value in raw
                                  if isinstance(value, str) and DATE_OPTION_LABEL.fullmatch(value)))

    def _selected_date_is(self, expected):
        label = self._page.get_by_text("时间维度", exact=True)
        return (label.count() == 1 and label.is_visible()
                and label.evaluate(_DATE_FILTER_SELECTED_SCRIPT, expected) is True)

    def _date_audit_ready(self):
        self._require_automatic_off_peak()
        return (self._page is not None and self._export_button() is not None
                and not any(_visible_exact(self._page, "button", label) is not None
                            for label in LOGIN_LABELS))

    def _inspect_date_options(self):
        """Open only the observed yesterday filter, then read date labels.

        This local audit never selects a new period, exports data, reads input
        values or accepts a selector, script, URL, date or Key from its caller.
        """
        # A stale export button alone must not permit a login-page capture.
        if not self._date_audit_ready():
            return "not_ready"
        time_label = self._page.get_by_text("时间维度", exact=True)
        selected = self._page.get_by_text("昨天", exact=True)
        if (time_label.count() != 1 or not time_label.is_visible()
                or selected.count() != 1 or not selected.is_visible()
                or time_label.evaluate(_YESTERDAY_FILTER_SCOPE_SCRIPT) is not True):
            return "date_filter_not_verified"
        before = self._visible_date_option_labels()
        selected.click()
        if not self._date_audit_ready():
            return "not_ready"
        after = self._visible_date_option_labels()
        for _ in range(20):
            if any(label not in before for label in after):
                break
            self._page.wait_for_timeout(100)
            if not self._date_audit_ready():
                return "not_ready"
            after = self._visible_date_option_labels()
        confirmed = any(label not in before for label in after)
        if not self._date_audit_ready():
            return "not_ready"
        result = {
            "schema": DATE_OPTIONS_SCHEMA,
            "filter_clicked": True,
            "selected_label": "昨天",
            "visible_date_labels": after,
            "newly_visible_date_labels": [label for label in after if label not in before],
            "new_period_selected": False,
            "menu_labels_confirmed": confirmed,
        }
        self._atomic_json(self.profile_dir.parent / "browser-date-options.json", result)
        # Existing operator-only screenshot policy: only the authenticated
        # official usage page, kept in a fixed local file and never via HTTP.
        self._page.screenshot(path=str(self.profile_dir.parent / "browser-date-options.png"), full_page=False)
        return "done" if confirmed else "date_options_not_confirmed"

    def _select_today_for_audit(self):
        """Select the observed Today choice, without changing Key or export."""
        if not self._date_audit_ready():
            return "not_ready"
        today = self._page.get_by_text("今天", exact=True)
        if (not self._selected_date_is("昨天") or today.count() != 1 or not today.is_visible()
                or today.evaluate(_TODAY_MENU_SCOPE_SCRIPT) is not True):
            return "date_menu_not_verified"
        today.click()
        confirmed = False
        for _ in range(21):
            if not self._date_audit_ready():
                return "not_ready"
            if self._selected_date_is("今天"):
                confirmed = True
                break
            self._page.wait_for_timeout(100)
        if not self._date_audit_ready():
            return "not_ready"
        self._atomic_json(self.profile_dir.parent / "browser-today-audit.json", {
            "schema": "deepseek_today_selection_audit_v1",
            "requested_label": "今天",
            "selection_confirmed": confirmed,
            "export_requested": False,
            "key_filter_changed": False,
        })
        self._page.screenshot(path=str(self.profile_dir.parent / "browser-today-audit.png"), full_page=False)
        return "done" if confirmed else "today_selection_not_confirmed"

    def _fixed_calendar_day_is(self, selection, observed_at):
        if not self._date_audit_ready():
            return False
        metadata = _range_metadata(self._page, self._controls, observed_at=observed_at)
        calendar = metadata["ui_scope"].get("calendar_selection")
        wanted = (observed_at.astimezone(BEIJING).date()
                  - timedelta(days=selection == "yesterday")).isoformat()
        return (metadata["date_range"] == {"status": "verified", "start": wanted, "end": wanted, "inclusive": True}
                and isinstance(calendar, dict) and calendar.get("start") == wanted
                and metadata["ui_scope"].get("relative_range") == {"kind": "calendar_day", "selection": selection})

    def _ensure_fixed_yesterday(self, observed_at):
        """Use only observed fixed day controls; never infer a custom range."""
        if self._fixed_calendar_day_is("yesterday", observed_at):
            return True
        if self._fixed_calendar_day_is("today", observed_at):
            current_label = "今天"
        elif self._date_audit_ready() and self._selected_date_is("近7天"):
            scope = _official_ui_scope(self._page, observed_at=observed_at)
            if (scope.get("timezone_evidence") != "official_gmt8_notice"
                    or scope.get("relative_range") != {"kind": "last_n_days", "days": 7}):
                return False
            current_label = "近7天"
        else:
            return False
        current = self._page.get_by_text(LAST7_LABEL if current_label == "近7天" else current_label, exact=True)
        if current.count() != 1 or not current.is_visible():
            return False
        # The closed fixed-filter evidence proves this unique label belongs
        # to the observed time dimension, so opening it cannot change a Key.
        self._require_automatic_off_peak()
        current.click()
        yesterday = None
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            candidate = self._page.get_by_text("昨天", exact=True)
            if (candidate.count() == 1 and candidate.is_visible()
                    and candidate.evaluate(_YESTERDAY_MENU_SCOPE_SCRIPT) is True):
                yesterday = candidate
                break
            self._page.wait_for_timeout(100)
        if yesterday is None:
            return False
        self._require_automatic_off_peak()
        yesterday.click()
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            if self._fixed_calendar_day_is("yesterday", observed_at):
                return True
            self._page.wait_for_timeout(100)
        return False

    def _select_yesterday_for_audit(self):
        if not self._date_audit_ready():
            return "not_ready"
        observed_at = self._now()
        confirmed = self._ensure_fixed_yesterday(observed_at)
        if not self._date_audit_ready():
            return "not_ready"
        self._atomic_json(self.profile_dir.parent / "browser-yesterday-audit.json", {
            "schema": "deepseek_yesterday_selection_audit_v1",
            "requested_label": "昨天", "selection_confirmed": confirmed,
            "date_range": {"start": (observed_at.date() - timedelta(days=1)).isoformat(),
                           "end": (observed_at.date() - timedelta(days=1)).isoformat(), "inclusive": True}
                          if confirmed else None,
            "observed_at": observed_at.isoformat(),
            "export_requested": False, "key_filter_changed": False,
        })
        self._page.screenshot(path=str(self.profile_dir.parent / "browser-yesterday-audit.png"), full_page=False)
        return "done" if confirmed else "yesterday_selection_not_confirmed"

    def _last7_all_scope(self):
        """Return fixed scope evidence, never infer absolute daily endpoints."""
        if not self._date_audit_ready():
            return None
        scope = _official_ui_scope(self._page, observed_at=self._now())
        if (scope.get("timezone_evidence") != "official_gmt8_notice"
                or scope.get("relative_range") != {"kind": "last_n_days", "days": 7}
                or scope.get("key_filter") != "all"):
            return None
        return scope

    def _ensure_fixed_last7(self):
        if not self._date_audit_ready():
            return False
        if self._selected_date_is("近7天"):
            return True
        # Only observed fixed Today/Yesterday/Near-30-days starting states are
        # authorized; custom or other relative ranges stop without a click.
        start = next((label for label in ("今天", "昨天", "近30天")
                      if self._selected_date_is(label)), None)
        if start is None:
            return False
        if start == "近30天":
            scope = _official_ui_scope(self._page, observed_at=self._now())
            if (scope.get("timezone_evidence") != "official_gmt8_notice"
                    or scope.get("relative_range") != {"kind": "last_n_days", "days": 30}):
                return False
        selected = self._page.get_by_text(LAST30_LABEL if start == "近30天" else start, exact=True)
        if selected.count() != 1 or not selected.is_visible():
            return False
        self._require_automatic_off_peak()
        selected.click()
        choice = None
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            candidate = self._page.get_by_text(LAST7_LABEL, exact=True)
            if (candidate.count() == 1 and candidate.is_visible()
                    and candidate.evaluate(_LAST7_MENU_SCOPE_SCRIPT) is True):
                choice = candidate
                break
            self._page.wait_for_timeout(100)
        if choice is None:
            return False
        self._require_automatic_off_peak()
        choice.click()
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            if self._selected_date_is("近7天"):
                return True
            self._page.wait_for_timeout(100)
        return False

    def _ensure_all_keys(self):
        """The fixed All choice is the only Key change this audit permits."""
        if not self._date_audit_ready():
            return False
        scope = _official_ui_scope(self._page, observed_at=self._now())
        if scope.get("key_filter") == "all":
            return True
        if scope.get("key_filter") != "filtered":
            return False
        # This exact clear-filter control was observed on the official usage
        # view. Prefer its single fixed action over guessing an unlabelled Key
        # popup. It changes only the usage-page filters, not account API Keys.
        clear = self._page.get_by_text("清除筛选条件", exact=True)
        if (scope.get("timezone_evidence") == "official_gmt8_notice"
                and scope.get("relative_range") == {"kind": "last_n_days", "days": 7}
                and self._selected_date_is("近7天")
                and clear.count() == 1 and clear.is_visible()):
            self._require_automatic_off_peak()
            clear.click()
            # Never repeat this click or fall through to another Key action if
            # the result is not established. Wait only for a closed All scope.
            for _ in range(21):
                if not self._date_audit_ready():
                    return False
                current = _official_ui_scope(self._page, observed_at=self._now())
                if current.get("key_filter") == "all":
                    break
                self._page.wait_for_timeout(100)
            else:
                return False
            if current.get("relative_range") != {"kind": "last_n_days", "days": 7}:
                # The official Clear action was observed resetting this view
                # to Near-30-days. Restore only this now-verified fixed state;
                # never repeat Clear or infer an unknown/custom reset range.
                if (current.get("timezone_evidence") != "official_gmt8_notice"
                        or current.get("relative_range") != {"kind": "last_n_days", "days": 30}
                        or not self._ensure_fixed_last7()):
                    return False
            return self._last7_all_scope() is not None
        labels = self._page.get_by_text("API Key", exact=True)
        if not 0 < labels.count() <= 8:
            return False
        matched = []
        for index in range(labels.count()):
            candidate = labels.nth(index)
            if (candidate.is_visible()
                    and candidate.evaluate(_FILTER_SCOPE_SCRIPT, "key") == "filtered"):
                matched.append(candidate)
        if len(matched) != 1:
            return False
        self._require_automatic_off_peak()
        opened = matched[0].evaluate(_KEY_FILTER_OPEN_SCRIPT)
        if (not isinstance(opened, dict) or set(opened) != {"opened", "menu_id"}
                or opened.get("opened") is not True or not isinstance(opened.get("menu_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", opened["menu_id"])):
            return False
        choice = None
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            candidate = self._page.get_by_text("全部", exact=True)
            if (candidate.count() == 1 and candidate.is_visible()
                    and candidate.evaluate(_KEY_ALL_MENU_SCOPE_SCRIPT, opened["menu_id"]) is True):
                choice = candidate
                break
            self._page.wait_for_timeout(100)
        if choice is None:
            return False
        self._require_automatic_off_peak()
        choice.click()
        for _ in range(21):
            if not self._date_audit_ready():
                return False
            if _official_ui_scope(self._page, observed_at=self._now()).get("key_filter") == "all":
                return True
            self._page.wait_for_timeout(100)
        return False

    def _read_visible_usage_headlines(self):
        values = {}
        labels = {
            "cost": "消费金额",
            "requests": re.compile(r"^API\s*请求次数$"),
            "tokens": "Tokens",
        }
        for kind, label in labels.items():
            candidates = self._page.get_by_text(label, exact=True)
            total = candidates.count()
            visible = [candidates.nth(index) for index in range(min(total, 16))
                       if candidates.nth(index).is_visible()]
            readings = []
            if total <= 16:
                for candidate in visible:
                    candidate_raw = candidate.evaluate(_USAGE_HEADLINE_NUMBER_SCRIPT, kind)
                    if isinstance(candidate_raw, str) and len(candidate_raw) <= 40:
                        readings.append(candidate_raw)
            # The official responsive cards expose equivalent visible matches.
            # Accept only one distinct numeric value across exact-label cards.
            readings = list(dict.fromkeys(readings))
            if total > 16 or len(readings) != 1:
                self._atomic_json(self.profile_dir.parent / "browser-headline-read-diagnostic.json", {
                    "kind": kind, "matching_count": min(total, 100),
                    "visible_count": len(visible), "numeric_readback": False,
                    "valid_card_readings": len(readings),
                    "numeric_candidates": [v for v in readings if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,12})?", v)]})
                return None
            raw = readings[0]
            if kind == "cost":
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,12})?", raw):
                    return None
                try:
                    value = Decimal(raw)
                except InvalidOperation:
                    return None
                if not value.is_finite() or not 0 <= value <= 10**12:
                    return None
                values["cost_cny"] = format(value, "f")
            else:
                if not re.fullmatch(r"[0-9]{1,18}", raw):
                    return None
                values["request_count" if kind == "requests" else "total_tokens"] = int(raw)
        return values

    def _select_last7_all_for_audit(self):
        """Select a fixed public filter scope and read its stable headline only.

        No caller-selected range/Key/script is accepted. The relative Near-7-days
        snapshot includes current-day activity and is NOT a complete daily bill
        or evidence of an exact rolling-168-hour interval or bot exclusivity.
        """
        if not self._date_audit_ready():
            return "not_ready"
        if not self._ensure_fixed_last7():
            return "last7_selection_not_confirmed"
        if not self._ensure_all_keys():
            return "all_keys_selection_not_confirmed"
        try:
            self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            return "usage_summary_not_settled"
        if self._last7_all_scope() is None:
            return "last7_all_scope_not_confirmed"
        first = self._read_visible_usage_headlines()
        self._page.wait_for_timeout(700)
        # Reading is repeated after settling and after scope re-validation; no
        # extra navigation, export, model request or platform write occurs.
        if not self._date_audit_ready() or self._last7_all_scope() is None:
            return "last7_all_scope_changed"
        second = self._read_visible_usage_headlines()
        if first is None or second is None or first != second:
            return "usage_summary_not_stable"
        if not self._date_audit_ready() or self._last7_all_scope() is None:
            return "last7_all_scope_changed"
        observed = self._now()
        self._atomic_json(self.profile_dir.parent / "browser-last7-all-summary.json", {
            "schema": "deepseek_last7_all_summary_v1",
            "request_id": self._operator_last_id,
            "source": "official_visible_usage_headlines",
            "observed_at": observed.isoformat(),
            "timezone": "UTC+08:00",
            "relative_range": {"kind": "last_n_days", "days": 7},
            "absolute_interval": None,
            "key_filter": "all", "currency": "CNY",
            "data_lag_minutes": 5,
            "summary_stable_readback": True,
            "contains_current_day": True,
            "complete_calendar_days": False,
            "bot_exclusivity_established": False,
            "summary": second,
            "export_requested": False,
        })
        return "done"

    def _select_official_summary(self, period):
        """Fixed visible official periods only; no export, arbitrary ranges or Key reads."""
        if period not in ("today", "yesterday", "last7") or not self._date_audit_ready():
            return "not_ready"
        self._require_automatic_off_peak()
        self._page.goto(OFFICIAL_USAGE, wait_until="domcontentloaded", timeout=45000)
        self._observe()
        if self.status().get("state") == "login_required":
            return "official_login_required"
        try:
            self._page.get_by_role("button", name="导出", exact=True).wait_for(state="visible", timeout=15000)
        except Exception:
            return "official_page_not_ready"
        if not self._ensure_fixed_last7() or not self._ensure_all_keys():
            return "official_scope_not_confirmed"
        if period in ("today", "yesterday"):
            label = "今天" if period == "today" else "昨天"
            current = self._page.get_by_text(LAST7_LABEL, exact=True)
            if not self._selected_date_is("近7天") or current.count() != 1 or not current.is_visible():
                return "official_scope_not_confirmed"
            self._require_automatic_off_peak()
            current.click()
            chosen = False
            for _ in range(31):
                candidate = self._page.get_by_text(label, exact=True)
                if (candidate.count() == 1 and candidate.is_visible()
                        and candidate.evaluate(_DAY_MENU_SCOPE_SCRIPT, label) is True):
                    self._require_automatic_off_peak()
                    candidate.click()
                    chosen = True
                    break
                self._page.wait_for_timeout(100)
            if not chosen:
                return "official_period_not_confirmed"
        def scope_matches():
            if not self._date_audit_ready():
                return False
            scope = _official_ui_scope(self._page, observed_at=self._now())
            expected = ({"kind": "last_n_days", "days": 7} if period == "last7"
                        else {"kind": "calendar_day", "selection": period})
            return (scope.get("timezone_evidence") == "official_gmt8_notice"
                    and scope.get("key_filter") == "all" and scope.get("relative_range") == expected)
        for _ in range(31):
            if scope_matches():
                break
            self._page.wait_for_timeout(100)
        else:
            return "official_scope_not_confirmed"
        try:
            self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            return "usage_summary_not_settled"
        self._require_automatic_off_peak()
        before = self._now()
        first = self._read_visible_usage_headlines()
        self._page.wait_for_timeout(700)
        self._require_automatic_off_peak()
        second = self._read_visible_usage_headlines()
        observed = self._now()
        if first is None or first != second or not scope_matches() or before.date() != observed.date():
            return "usage_summary_not_stable"
        day = (observed.date() - timedelta(days=period == "yesterday")).isoformat()
        self._atomic_json(self.profile_dir.parent / ("browser-summary-" + period + ".json"), {
            "schema": "deepseek_official_period_summary_v1", "request_id": self._operator_last_id,
            "source": "official_visible_usage_headlines", "observed_at": observed.isoformat(),
            "timezone": "UTC+08:00", "period": period, "day": day if period != "last7" else None,
            "relative_range": ({"kind": "last_n_days", "days": 7} if period == "last7"
                               else {"kind": "calendar_day", "selection": period}),
            "absolute_interval": None, "key_filter": "all", "currency": "CNY",
            "data_lag_minutes": 5, "summary_stable_readback": True,
            "contains_current_day": period != "yesterday", "summary": second,
            "export_requested": False,
        })
        return "done"

    @staticmethod
    def _atomic_json(path: Path, payload: dict):
        pending = path.with_name(path.name + ".pending")
        pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(path)

    def _operator_command(self):
        """Fixed local operator commands; no HTTP, paths, URLs or JS input."""
        path = self.profile_dir.parent / "operator-command.json"
        if not path.exists() or path.is_symlink() or not path.is_file():
            return
        request_id = None
        outcome = "rejected"
        try:
            if not 0 < path.stat().st_size <= 1024:
                return
            request = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(request, dict)
                    or set(request) not in ({"command", "request_id"}, {"command", "request_id", "origin"})
                    or not isinstance(request.get("command"), str)
                    or request.get("command") not in OPERATOR_COMMANDS
                    or not isinstance(request.get("request_id"), str)
                    or not OPERATOR_ID.fullmatch(request["request_id"])
                    or ("origin" in request and (request["origin"] != "periodic_reconcile_v1"
                        or request["command"] not in ("summary_today", "summary_yesterday")))):
                return
            request_id = request["request_id"]
            if request_id == self._operator_last_id:
                outcome = "duplicate"
                return
            self._operator_last_id = request_id
            command = request["command"]
            self._automatic_query_active = request.get("origin") == "periodic_reconcile_v1"
            self._require_automatic_off_peak()
            if command == "inspect_usage":
                if self._page is None or self._export_button() is None:
                    outcome = "not_ready"
                    return
                self._inspect()
                self._page.screenshot(path=str(self.profile_dir.parent / "browser-preview.png"), full_page=False)
            elif command == "inspect_date_options":
                outcome = self._inspect_date_options()
                return
            elif command == "select_today_for_audit":
                outcome = self._select_today_for_audit()
                return
            elif command == "select_yesterday_for_audit":
                outcome = self._select_yesterday_for_audit()
                return
            elif command == "select_last7_all_for_audit":
                outcome = self._select_last7_all_for_audit()
                return
            elif command in ("summary_today", "summary_yesterday", "summary_last7"):
                outcome = self._select_official_summary(command[len("summary_"):])
                return
            elif command in ("close_browser", "shutdown_worker"):
                with self._lock:
                    self._automatic_open_generation += 1
                    self._automatic_open_pending = False
                self._close_browser()
                if command == "shutdown_worker":
                    self._stop = True
            elif command == "reopen_browser":
                with self._lock:
                    self._automatic_open_generation += 1
                    self._automatic_open_pending = False
                self._close_browser()
                self._update(state="opening", can_open=False, can_export=False,
                             message="正在用专用本机资料重新打开官方用量页面。")
                self._open()
            outcome = "done"
        except _AutomaticPeakPaused:
            outcome = "peak_tariff"
        except Exception:
            outcome = "error"
        finally:
            self._automatic_query_active = False
            # Names and contents are fixed and local; no supplied path is used.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._atomic_json(self.profile_dir.parent / "operator-result.json", {
                "request_id": request_id, "state": outcome,
                "completed_at": self._now().isoformat(),
            })

    def _close_browser(self):
        if self._session is not None:
            self._session.close()
        self._session = None
        self._page = None
        self._update(state="closed", can_open=True, can_export=False,
                     schedule_state="waiting_for_browser",
                     message="独立浏览器已关闭，专用登录资料已保留。")

    def _observe(self):
        if self._page is None:
            return
        if self._page.is_closed():
            self._update(state="closed", can_open=True, can_export=False,
                         message="官方窗口已关闭，重新打开会复用专用登录资料。")
            return
        if not _official_page(self._page.url):
            self._update(state="error", can_open=True, can_export=False,
                         message="当前页面不是已识别的官方用量页面；已停止自动操作。")
            return
        for label in LOGIN_LABELS:
            if _visible_exact(self._page, "button", label) is not None:
                self._update(state="login_required", can_open=True, can_export=False,
                             schedule_state="waiting_for_login", range_verified=False,
                             message="请在独立官方窗口自行登录；登录到期后也会在这里提示。")
                return
        button = self._export_button()
        if button is not None:
            metadata = _range_metadata(self._page, self._controls, observed_at=self._now())
            known = metadata["date_range"]["status"] == "verified"
            self._update(state="export_available", can_open=True, can_export=True,
                         range_verified=known,
                         message="已识别官方导出按钮，可导出当前筛选；未知日期或Key范围会标为待核定。")
            if self._inspect_ready_once and not self._inspection_done:
                self._inspect()
            return
        usage = any(_visible_exact(self._page, "heading", label) is not None for label in USAGE_HEADINGS)
        self._update(state="ready" if usage else "error", can_open=True, can_export=False,
                     message="官方页面已打开，尚未识别可用的导出按钮。")

    def _tick(self):
        try:
            # This is also polled while the browser is deliberately closed, so
            # the operator can reopen the SAME profile without a second owner.
            self._operator_command()
            if self._stop or self._closing:
                return
            if self._automatic_open_pending:
                self._attempt_automatic_open()
            if self._scheduled_mode == "on_demand":
                self._observe()
                return
            self._automatic_query_active = True
            self._require_automatic_off_peak()
            now = self._now()
            today = now.date().isoformat()
            if (self._scheduled_mode == "current_official_snapshot"
                    and now.time() >= time(9, 10)
                    and self._last_scheduled_day != today):
                missing_page = self._session is None or self._page is None
                if not missing_page:
                    try:
                        missing_page = self._page.is_closed()
                    except Exception:
                        missing_page = True
                if missing_page:
                    if self._last_scheduled_open_day == today:
                        return
                    # Persist BEFORE launch. A launch failure or closed login
                    # window must not create another Chrome every poll/restart.
                    self._last_scheduled_open_day = today
                    self._save_schedule()
                    self._update(state="opening", can_open=False, can_export=False,
                                 schedule_state="opening_scheduled_browser",
                                 message="每日核对时间已到，正在用专用本机资料打开官方用量页面。")
                    self._open()
            if self._page is None:
                return
            # Keep a completed export visible until the page materially changes.
            before = self.status()
            self._observe()
            after = self.status()
            if before["state"] in ("received_pending_review", "export_needs_range_review") and after["state"] == "export_available":
                self._update(state=before["state"], message=before["message"])
            if now.time() < time(9, 10) or self._last_scheduled_day == today or not self.status()["can_export"]:
                return
            if self._scheduled_mode == "current_official_snapshot":
                # A refresh/download attempt is persisted BEFORE doing browser
                # I/O. An unknown result never triggers another automatic click.
                self._last_scheduled_day = today
                self._save_schedule()
                self._update(schedule_state="refreshing_official_snapshot", can_export=False,
                             message="正在刷新官网当前用量范围，导出后按账单实际日期核对。")
                self._require_automatic_off_peak()
                self._page.goto(OFFICIAL_USAGE, wait_until="domcontentloaded", timeout=45000)
                try:
                    self._page.get_by_role("button", name="导出", exact=True).wait_for(state="visible", timeout=15000)
                except Exception:
                    # English UI is only accepted through its exact known label.
                    try:
                        self._page.get_by_role("button", name="Export", exact=True).wait_for(state="visible", timeout=1000)
                    except Exception:
                        pass
                self._observe()
                if self.status()["state"] == "login_required":
                    # No export click happened. A later successful user login
                    # may run today's first real export; never retry a download.
                    self._last_scheduled_day = None
                    self._save_schedule()
                    self._update(schedule_state="waiting_for_login")
                    return
                if self._export_button() is None:
                    self._update(schedule_state="snapshot_unavailable",
                                 message="官网刷新后尚未识别可导出的用量，今天未再次自动尝试。")
                    return
                # Refresh can restore Today or another persisted filter. Daily
                # billing must establish the previous full GMT+8 natural day
                # before its one download attempt, using only observed UI.
                if not self._ensure_fixed_yesterday(now):
                    self._update(schedule_state="date_controls_required",
                                 message="09:10官网刷新后未确认昨天的完整自然日范围，未导出；请检查官方日期筛选。")
                    return
                self._export("scheduled", required_calendar_day=now.date() - timedelta(days=1))
                return
            if not self._select_yesterday(now.date() - timedelta(days=1)):
                # One explicit notice per day; no guessing and no polling clicks.
                self._last_scheduled_day = today
                self._save_schedule()
                self._update(schedule_state="date_controls_required",
                             message="09:10自动导出等待核实官网日期控件和时区；当前可手动导出所选范围。")
                return
            self._last_scheduled_day = today
            self._save_schedule()
            self._export("scheduled")
        except _AutomaticPeakPaused:
            self._update(schedule_state="peak_tariff")
        except Exception:
            self._update(state="error", can_export=False,
                         message="自动导出尚未确认完成，请检查独立官方窗口。")
        finally:
            self._automatic_query_active = False

    def _select_yesterday(self, yesterday: date) -> bool:
        controls = self._controls
        if not (controls.start_label and controls.end_label and controls.timezone_label
                and controls.start_date_inclusive and controls.end_date_inclusive
                and controls.applied_range_template):
            return False
        if not _official_page(self._page.url, usage_only=True):
            return False
        before = _range_metadata(self._page, controls, observed_at=self._now())
        if before["date_range"]["status"] != "verified":
            return False
        start = _labelled_control(self._page, controls.start_label)
        end = _labelled_control(self._page, controls.end_label)
        if not start.is_editable() or not end.is_editable():
            return False
        wanted = yesterday.isoformat()
        self._require_automatic_off_peak()
        start.fill(wanted)
        self._require_automatic_off_peak()
        end.fill(wanted)
        if controls.apply_button:
            apply_button = _visible_exact(self._page, "button", controls.apply_button)
            if apply_button is None or not apply_button.is_enabled():
                return False
            self._require_automatic_off_peak()
            apply_button.click()
        # An observed result-region completion marker must confirm the applied
        # dates; just reading back input fields cannot establish export scope.
        applied_text = controls.applied_range_template.format(start=wanted, end=wanted)
        summary = self._page.get_by_text(applied_text, exact=True)
        try:
            summary.wait_for(state="visible", timeout=15000)
        except Exception:
            return False
        if summary.count() != 1 or not summary.is_visible():
            return False
        after = _range_metadata(self._page, controls, observed_at=self._now())
        return (after["date_range"]["status"] == "verified"
                and after["date_range"]["start"] == wanted
                and after["date_range"]["end"] == wanted
                and self._export_button() is not None)

    def _export(self, trigger: str, *, unknown_snapshot: bool = False,
                required_calendar_day: date | None = None):
        button = self._export_button()
        if button is None:
            self._observe()
            return
        observed_at = self._now()
        metadata = _range_metadata(self._page, self._controls, observed_at=observed_at)
        if required_calendar_day is not None:
            wanted = required_calendar_day.isoformat()
            expected_range = {"status": "verified", "start": wanted, "end": wanted, "inclusive": True}
            # Recheck on the SAME fresh metadata that will enter the sidecar.
            # A user change/re-render or midnight crossing after selection
            # cannot cause the daily job to download Today by accident.
            if (metadata["date_range"] != expected_range or metadata["timezone"] != "UTC+08:00"
                    or metadata["ui_scope"].get("relative_range") != {"kind": "calendar_day", "selection": "yesterday"}
                    or not self._date_audit_ready()):
                self._update(schedule_state="date_controls_required", range_verified=False,
                             message="导出前未再次确认昨天的完整自然日范围，未点击导出；今天不自动重试。")
                return
        if unknown_snapshot and metadata["date_range"]["status"] != "verified":
            # The CSV establishes absolute dates; observed UI scope remains
            # useful evidence and must not be replaced with an invented range.
            metadata["date_range"] = {"status": "unknown", "start": None, "end": None, "inclusive": False}
            metadata["automatic_compare_allowed"] = False
        metadata.update(trigger=trigger, observed_at=observed_at.isoformat(),
                        source="official_visible_export")
        self._update(state="exporting", can_export=False, message="正在下载官网导出的账单文件。")
        temporary = None
        try:
            self._require_automatic_off_peak()
            with self._page.expect_download(timeout=25000) as waiting:
                self._require_automatic_off_peak()
                button.click()
            download = waiting.value
            suffix = Path(download.suggested_filename).suffix.casefold()
            if suffix not in (".csv", ".zip"):
                try:
                    download.cancel()
                except Exception:
                    pass
                self._update(state="error", can_export=True,
                             message="官网返回的文件不是支持的ZIP或CSV，未导入。")
                return
            self.download_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.download_dir / f"download-{uuid.uuid4().hex}{suffix}"
            download.save_as(str(temporary))
            if not 0 < temporary.stat().st_size <= MAX_DOWNLOAD:
                self._update(state="error", can_export=True,
                             message="官网导出文件为空或超过20MB，未导入。")
                return
            result = self._import_callback(temporary, metadata)
            if not isinstance(result, dict) or result.get("state") != "received_pending_review":
                self._update(state="error", can_export=True,
                             message="账单下载后未确认导入成功，请保留官网导出文件核查。")
                return
            now = self._now().isoformat()
            known = metadata["date_range"]["status"] == "verified"
            receipt = result.get("receipt", {})
            digest = receipt.get("sha256") if isinstance(receipt, dict) else None
            updates = {"state": "received_pending_review" if known else "export_needs_range_review",
                       "can_export": True, "last_export_at": now, "range_verified": known,
                       "schedule_state": "exported" if trigger == "scheduled" else self.status()["schedule_state"],
                       "duplicate": bool(result.get("duplicate")),
                       "message": "官方账单已保存并去重，等待核对。" if known else
                       "官方账单已保存并去重；日期、时区或Key范围尚未核定，未直接认定与本机一致。"}
            if isinstance(digest, str) and SHA256.fullmatch(digest):
                updates["last_receipt_sha256"] = digest
            self._update(**updates)
            self._save_schedule()
        except _AutomaticPeakPaused:
            raise
        except Exception:
            # Never echo Playwright errors, which may contain signed URLs or UI data.
            self._update(state="error", can_export=True,
                         message="官方导出未确认完成，未自动重试；请检查页面后手动重试。")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
