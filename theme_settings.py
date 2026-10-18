"""Theme palette generation and the sidebar's draft/apply settings panel."""
from __future__ import annotations

import re

import streamlit as st


def _hex(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("颜色必须为六位十六进制色值")
    return value.upper()


def _mix(color: str, target: str, amount: float) -> str:
    a, b = _hex(color)[1:], _hex(target)[1:]
    return "#" + "".join(
        f"{round(int(a[i:i + 2], 16) * (1 - amount) + int(b[i:i + 2], 16) * amount):02X}"
        for i in (0, 2, 4)
    )


def _luminance(color: str) -> float:
    channels = [int(_hex(color)[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))


def _ink(background: str) -> str:
    return "#FFFFFF" if _luminance(background) < .179 else "#000000"


def theme_palette(themes: dict, name: str, custom: dict | None = None) -> dict:
    """Return an independent palette; custom colors update surfaces and text together."""
    palette = dict(themes.get(name) or next(iter(themes.values())))
    if not custom:
        return palette
    accent, background = _hex(custom["accent"]), _hex(custom["background"])
    light = _luminance(background) >= .179
    ink = _ink(background)
    panel = _mix(background, "#FFFFFF" if light else "#000000", .15)
    secondary = _mix(ink, background, .24)
    border = _mix(ink, background, .75)
    # A more contrasty accent is used for small text; button fill keeps the chosen hue.
    readable_accent = accent
    for _ in range(12):
        la, lb = _luminance(readable_accent), _luminance(background)
        if (max(la, lb) + .05) / (min(la, lb) + .05) >= 4.5:
            break
        readable_accent = _mix(readable_accent, ink, .2)
    palette.update({
        "--grad-page": f"linear-gradient(180deg,{background},{background})",
        "--grad-side": "linear-gradient(180deg,#07142B,#102F59)",
        "--grad-panel": f"linear-gradient(180deg,{panel},{panel})",
        "--grad-accent": f"linear-gradient(160deg,{accent},{accent})",
        "--grad-accent2": f"linear-gradient(90deg,{accent},{accent})",
        "--bg-card": panel, "--bg-soft": panel, "--surface": panel,
        "--floating-surface": panel, "--floating-hover": _mix(panel, ink, .08),
        "--bg-tint": _mix(background, accent, .12), "--brd": border,
        "--tx-strong": ink, "--tx-body": ink, "--on-surface": ink,
        "--tx-dim": secondary, "--tx-soft": secondary, "--tx-cap": secondary,
        "--accent": readable_accent, "--accent-bg": accent,
        "--tx-onaccent": _ink(accent), "--accent-tint": _mix(background, accent, .15),
        "--cyan": readable_accent, "--cyan-vivid": readable_accent,
        "--composer-backdrop": background,
        "--gd-cell": panel, "--gd-cell2": background, "--gd-head": panel,
        "--gd-tx": ink, "--gd-tx2": secondary, "--gd-tx3": secondary,
        "--gd-tx-head": readable_accent, "--gd-accent": readable_accent,
        "--gd-brd": border, "--gd-brd2": border,
        "--fw-body": "400" if light else "500",
    })
    return palette


def render_theme_settings(themes: dict, default: str) -> None:
    """Apply/reset callbacks commit theme settings before CSS injection."""
    state = st.session_state
    if state.get("ui_theme") not in themes:
        state["ui_theme"] = default
    # Do not mount controls or columns into the login screen's element tree.
    if not state.get("authenticated", False):
        return
    defaults = {"🌌 深空蓝": "#0A2C66", "🔮 星云紫": "#2C1A62", "☁️ 浅色雅蓝": "#DAE6F6"}

    def choose_preset():
        name = state["theme_draft_preset"]
        state["theme_draft_accent"] = themes[name]["--accent-bg"]
        state["theme_draft_background"] = defaults.get(name, "#0A2C66")

    def apply():
        name = state["theme_draft_preset"]
        state["ui_theme"] = name
        colors = {"accent": state["theme_draft_accent"], "background": state["theme_draft_background"]}
        state["ui_theme_custom"] = (
            colors if colors != {"accent": themes[name]["--accent-bg"], "background": defaults.get(name, "#0A2C66")} else {}
        )
        state["theme_notice"] = "主题已应用，本次会话中保持生效。"

    def reset():
        state["ui_theme"] = default
        state["ui_theme_custom"] = {}
        state["theme_draft_preset"] = default
        choose_preset()
        state["theme_notice"] = "已恢复默认主题。"

    state.setdefault("theme_draft_preset", state["ui_theme"])
    custom = state.get("ui_theme_custom") or {}
    state.setdefault("theme_draft_accent", custom.get("accent", themes[state["ui_theme"]]["--accent-bg"]))
    state.setdefault("theme_draft_background", custom.get("background", defaults.get(state["ui_theme"], "#0A2C66")))
    with st.sidebar:
        with st.expander("🎨 界面主题", expanded=False):
            current = "自定义配色" if state.get("ui_theme_custom") else state["ui_theme"]
            st.caption(f"当前：{current}")
            st.radio("选择主题", list(themes), key="theme_draft_preset", horizontal=True, on_change=choose_preset)
            st.markdown("**自定义配色**")
            left, right = st.columns(2)
            with left:
                st.color_picker("主题色", key="theme_draft_accent")
            with right:
                st.color_picker("背景色", key="theme_draft_background")
            st.caption("选择配色后，点击应用主题更新整个工作台。")
            left, right = st.columns(2)
            left.button("应用主题", key="theme_apply", on_click=apply, use_container_width=True)
            right.button("恢复默认", key="theme_reset", on_click=reset, use_container_width=True)
            if state.get("theme_notice"):
                st.caption(state["theme_notice"])
