# -*- coding: utf-8 -*-
"""复现 2026-09-28 真机"当前建议为空"：按截图状态走规则链。

观察事实：
- 英雄 卡牌大师 / 路线 AP / 大乱斗（map 未知：中途启动大概率 game_map=""）
- 21:56:36 攒钱提示 "距实现器还差1400，先攒钱"（序列首位=实现器，组件插入）
- 之后买出 实现器（2800G = 成型对比 1/6 2800G）
- 21:59:21 KDA event_note（当时建议非空）
- 22:00 主窗/浮窗建议均为占位符 ⇒ suggestion.render()=="" ⇒ next_items 空序列+空附注
"""
import sys
sys.path.insert(0, r"D:\project\HexARAMHelper")

from hexa.knowledge.kb import KnowledgeBase
from hexa.core.rules_engine import RulesEngine

kb = KnowledgeBase()
rules = RulesEngine(kb)

HERO = "卡牌大师"
ENEMY = ["堕落天使", "皮城女警", "灵罗娃娃", "流光镜影", "愁云使者"]


def show(tag, my_items, path="ap", game_map="", gold=0.0):
    threat = rules.threat_profile(ENEMY)
    seq = rules.build_sequence(my_items, path, threat,
                               hero=HERO, game_map=game_map)
    save = rules.saving_advice(my_items, gold, seq, game_map=game_map)
    target, note = rules.component_advice(my_items, path, game_map=game_map,
                                          prefer=[c.name for c in seq])
    render = ""
    if seq:
        render = "出装主线"
    elif note:
        render = f"(note) {note}"
    print(f"[{tag}] items={my_items} path={path} map={game_map!r}")
    print(f"   seq={[c.name for c in seq]} save={save!r}")
    print(f"   component_advice=({target}, {note!r})  render={render!r}")
    print()


# ① 21:56 前：只有实现器的组件（失落的章节 3802）
show("①组件期", ["失落的章节"], gold=1400)
# ② 买出实现器后（成型对比 1/6 2800G）
show("②买出实现器", ["实现器"], gold=800)
# ③ 再带个小件
show("③实现器+小件", ["实现器", "增幅典籍"], gold=800)
# ④ path 为空串的变体
show("④path空", ["实现器"], path="", gold=800)
# ⑤ map=12 变体
show("⑤map12", ["实现器"], game_map="12", gold=800)
# ⑥ hero 未注入引擎（_hero=""）—— 中途启动 late_start 未触发的假设
show("⑥hero空", ["实现器"], gold=800)
threat = rules.threat_profile(ENEMY)
seq = rules.build_sequence(["实现器"], "ap", threat, hero="", game_map="")
print("⑥hero空 build_sequence =", [c.name for c in seq])
