"""Presentation helpers for the approved workbench layout.

No data fetching, scoring, authentication or budget writes belong here.
"""
from pathlib import Path
import html
import json

ASSET_DIR = Path(__file__).resolve().parent / "ui"


def normalize_theme(name):
    """Migrate saved theme choices without resetting any business state."""
    return {"🌌 深空蓝": "🏖️ 暖白砂岩", "🔮 星云紫": "🪻 淡雾紫"}.get(name, name)


def history_height(count):
    """Short histories fit their contents; longer histories scroll in place."""
    return "content" if count <= 2 else 250


def workbench_themes(original):
    """Keep the existing theme API, replacing its palette as one coherent set."""
    palettes = {
        "☁️ 浅色雅蓝": dict(page="#EDF2F9", panel="#FFFFFF", soft="#F5F7FB",
            field="#FFFFFF", line="#DCE4EF", text="#172B49", body="#334760",
            muted="#596B82", accent="#2756D8", button="#2756D8", tint="#EAF0FF",
            side="#DEE7F3", side_panel="#F2F6FC", side_line="#C4D1E3",
            side_muted="#50637E", side_active="#C7D7F2", side_text="#203653",
            side_accent="#234BAF", green="#19734C", red="#B93848", gold="#916316",
            scheme="light"),
        "🏖️ 暖白砂岩": dict(page="#F5F2ED", panel="#FFFEFC", soft="#F8F5F0",
            field="#FFFEFC", line="#E2DACF", text="#3D342B", body="#554B40",
            muted="#756757", accent="#806044", button="#806044", tint="#EFE4D7",
            side="#E7DFD3", side_panel="#F6F1E9", side_line="#D5C7B7",
            side_muted="#6C5B49", side_active="#DCCAB4", side_accent="#6F4E30",
            side_text="#433526", green="#28744E", red="#B54142", gold="#8B601C", scheme="light"),
        "🪻 淡雾紫": dict(page="#F3F0F7", panel="#FFFFFF", soft="#F8F5FC",
            field="#FFFFFF", line="#E0D8E9", text="#342C46", body="#504461",
            muted="#70627F", accent="#7353A0", button="#7353A0", tint="#EDE5F6",
            side="#E4DDEE", side_panel="#F5F0FA", side_line="#CEC0DF",
            side_muted="#675676", side_active="#D5C4E8", side_accent="#63418A",
            side_text="#3D2E50", green="#287451", red="#B63D59", gold="#89601F", scheme="light"),
    }
    result = {}
    for name, p in palettes.items():
        t = dict(original["☁️ 浅色雅蓝"])
        t.update({
            "--grad-page": p["page"], "--page-bg": p["page"],
            "--grad-side": p["side"], "--side-edge": p["side_line"],
            "--grad-panel": p["panel"], "--bg-card": p["panel"],
            "--grad-accent": p["button"], "--grad-accent2": p["button"],
            "--grad-ok": p["soft"], "--bg-tint": p["tint"], "--bg-soft": p["soft"],
            "--brd": p["line"], "--tx-strong": p["text"], "--tx-body": p["body"],
            "--tx-dim": p["muted"], "--tx-soft": p["body"], "--tx-cap": p["muted"],
            "--tx-onaccent": "#FFFFFF", "--accent": p["accent"],
            "--accent-bg": p["button"], "--accent-tint": p["tint"],
            "--cyan": p["accent"], "--cyan-vivid": p["accent"], "--gold": p["gold"],
            "--ok-tx": p["green"], "--bad-tx": p["red"],
            "--surface": p["field"], "--on-surface": p["text"],
            "--floating-surface": p["panel"], "--floating-hover": p["soft"],
            "--floating-shadow": "0 10px 28px rgba(0,0,0,.12)",
            "--composer-backdrop": p["page"], "--color-scheme": p["scheme"],
            "--fw-body": "400", "--fw-label": "500", "--fw-strong": "600",
            "--gd-cell": p["panel"], "--gd-cell2": p["soft"], "--gd-head": p["soft"],
            "--gd-tx": p["text"], "--gd-tx2": p["body"], "--gd-tx3": p["muted"],
            "--gd-tx-head": p["text"], "--gd-brd": p["line"], "--gd-brd2": p["line"],
            "--gd-accent": p["accent"],
            "--side-panel": p["side_panel"], "--side-line": p["side_line"],
            "--side-muted": p["side_muted"], "--side-active": p["side_active"],
            "--side-accent": p["side_accent"], "--side-text": p["side_text"],
            "--tooltip-bg": p["panel"], "--tooltip-text": p["body"],
            "--tooltip-border": p["line"],
            "--diagram-blue-bg": "#EDF3FC" if p["scheme"] == "light" else "#293A51",
            "--diagram-blue": "#1E40AF" if p["scheme"] == "light" else "#ABC7FF",
            "--diagram-purple-bg": "#F3E8FD" if p["scheme"] == "light" else "#40334F",
            "--diagram-purple": "#7C3AED" if p["scheme"] == "light" else "#D1B5F5",
            "--diagram-green-bg": "#E7F6EC" if p["scheme"] == "light" else "#253F38",
            "--diagram-green": "#19734C" if p["scheme"] == "light" else "#99D8B8",
            "--diagram-orange-bg": "#FFF7E6" if p["scheme"] == "light" else "#453B2D",
            "--diagram-orange": "#916316" if p["scheme"] == "light" else "#E8C78B",
        })
        result[name] = t
    return result


def styles():
    return (ASSET_DIR / "workbench.css").read_text(encoding="utf-8")


def company_focus_script(index, token):
    # Only an integer index and a server-generated integer nonce enter the JS.
    # Company names / API output / uploaded content are never executable HTML.
    config = json.dumps({"index": int(index), "token": str(int(token))})
    script = (ASSET_DIR / "company_focus.js").read_text(encoding="utf-8")
    return "<script>\n" + script.replace("__FOCUS_CONFIG__", config) + "\n</script>"


def quota_html(used, limit, polish, embed):
    remaining = max(0, min(limit, limit - max(0, int(used))))
    percent = remaining / max(limit, 1) * 100
    return (
        '<div class="wb-quota"><span>今日剩余分析额度</span>'
        f'<div class="wb-quota-value"><strong>{remaining}</strong> / {limit}</div>'
        f'<div class="wb-quota-track" role="progressbar" aria-label="今日剩余分析额度" '
        f'aria-valuemin="0" aria-valuemax="{limit}" aria-valuenow="{remaining}">'
        f'<div style="width:{percent:.2f}%"></div></div>'
        f'<small>每日上限 {limit}</small>'
        f'<dl><div><dt>今日润色</dt><dd>{max(0, int(polish)):,} 次</dd></div>'
        f'<div><dt>今日向量化</dt><dd>{max(0, int(embed)):,} 批</dd></div></dl></div>'
    )


def page_heading(title, subtitle):
    return (f'<div class="wb-heading"><h1>{html.escape(title)}</h1>'
            f'<p>{html.escape(subtitle)}</p></div>')


def chart_palette(theme):
    """One palette for all chart types; data and financial semantics stay upstream."""
    dark = theme["--color-scheme"] == "dark"
    series = (["#8EAFFC", "#E7BD76", "#76CBB9", "#C5A4F0", "#EEA1AC"]
              if dark else ["#355FE5", "#B8751E", "#168879", "#8B5CCD", "#CC5F67"])
    if theme["--page-bg"] == "#F3F0F7":
        series[0], series[3] = series[3], series[0]
    elif theme["--page-bg"] == "#F5F2ED":
        series[0] = theme["--accent"]
    return dict(purple=series[0], blue=series[0], gold=series[1],
                up=theme["--bad-tx"], down=theme["--ok-tx"],
                neutral=theme["--tx-dim"], card=theme["--bg-card"],
                text=theme["--tx-body"], title=theme["--tx-strong"],
                grid=theme["--brd"], soft=theme["--bg-soft"], series=series)


def chart_label_color(rgb):
    """Choose readable text on a chart segment using its actual fill color."""
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb[:3]]
    luminance = sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722)))
    return "#172B49" if luminance > .35 else "#FFFFFF"


def polish_chart(fig, palette):
    """Style existing artists without changing values, labels, limits or series."""
    from matplotlib.container import BarContainer

    fig.set_facecolor(palette["card"])
    for ax in fig.axes:
        ax.set_facecolor(palette["card"])
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_color(palette["grid"])
            spine.set_linewidth(.7)
        for grid in ax.get_xgridlines() + ax.get_ygridlines():
            grid.set_color(palette["grid"])
            grid.set_alpha(.65)
            grid.set_linewidth(.65)
        for line in ax.lines:
            if line.get_linewidth() >= 2:
                line.set_linewidth(2.4)
                line.set_solid_capstyle("round")
                if line.get_marker() not in (None, "None", "", " "):
                    line.set_markersize(6)
                    line.set_markeredgecolor(palette["card"])
                    line.set_markeredgewidth(1.2)
        for container in ax.containers:
            if isinstance(container, BarContainer):
                for bar in container.patches:
                    bar.set_edgecolor(palette["card"])
                    bar.set_linewidth(.7)
