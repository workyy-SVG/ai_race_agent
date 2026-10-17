# -*- coding: utf-8 -*-
"""【已迁移】本文件已被 eval_agent_trace.py 取代，保留仅为避免误删历史。

新脚本：python eval_agent_trace.py
（断言更完整：页签数量/位置 + 台账自洽 + 失败步骤是否被如实记录）
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
print("本验证脚本已迁移至 eval_agent_trace.py")
sys.exit(os.system(f'"{sys.executable}" '
                   f'"{os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_agent_trace.py")}"'))
