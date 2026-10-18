# -*- coding: utf-8 -*-
"""v17 冒烟：3 套主题 × 3 个视图 = 9 组，全部要求无异常 + 无 CSS 泄漏。"""
import sys, io, os, re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)

from streamlit.testing.v1 import AppTest

APP = os.path.join(_HERE, "app.py")
THEMES = ["🌌 深空蓝", "🔮 星云紫", "☁️ 浅色雅蓝"]
VIEWS = ["🏢", "⚖️", "📖"]
BAD = []


def leaks(at):
    out = []
    for m in at.markdown:
        v = str(getattr(m, "value", "") or "")
        if "<style>" not in v:
            continue
        rest = re.sub(r"<[^>]+>", "", re.sub(r"<style>.*?</style>", "", v, flags=re.S))
        if rest.strip():
            out.append(rest.strip()[:100])
    return out


for th in THEMES:
    for vw in VIEWS:
        at = AppTest.from_file(APP, default_timeout=240)
        at.run()
        at.session_state["authenticated"] = True
        at.session_state["ui_theme"] = th
        at.session_state["main_view"] = vw
        at.run()
        ex = [str(e.value)[:160] for e in at.exception]
        lk = leaks(at)
        ok = (not ex) and (not lk)
        print(("  OK  " if ok else "  FAIL") + f" {th} / {vw}"
              + (f"  exc={ex}" if ex else "")
              + (f"  leak={lk}" if lk else ""))
        if not ok:
            BAD.append(f"{th}/{vw}")

print()
if BAD:
    print("SMOKE FAILED:", BAD)
    sys.exit(1)
print("SMOKE_9_9_PASSED")
sys.exit(0)
