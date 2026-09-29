"""离线冒烟测试：mock 驱动 P0~P2 全链路（无 LCU / 无游戏 / 无网络）

覆盖：
1. KnowledgeBase 加载与查询（含 173 英雄 OP.GG builds / 语义标签接口）
2. RulesEngine：current_path / OP.GG 出门装+联动 / OP.GG 高频下一件候选 /
   小件合成 / 海克斯时刻评级
3. GameSession 全生命周期：选人→加载→对局→出门装状态机→死亡触发→
   敌方关键装/KDA 触发→结束重置（含秒退重开）+ AI 精修 serial 防旧覆新
   + 满六神装替换建议（卖X换Y差价 / 替换预告 / AI 候选对选优）
   + 军师提示滚动列表（阵容缺口/敌方缺口反向/顺逆风/攒钱/替换提醒，
   去重节流+保留上限）+ 攒钱决策（攒大件 vs 买小件阈值分支）
   + 成型度对比（playerlist 六神装进度 x/6+装备估值/敌我分组/深拷贝/
   无快照占位）+ F9 撤销最近记账（revoke_latest_augment）
4. PhaseMachine 状态转换（含 dodge_requeue 与断连事件）
5. DetectorCore 迟滞状态机 + Layout 坐标 + 鼠标落点映射
6. AI 引擎（FakeClient）：System2 生成/幻觉过滤/规则降级/预算与间隔守门/
   System1 防幻觉/复盘日志
10. 点击链丢弃诊断（rect_miss/no_hex_evidence/reroll_no_evidence 限频上
    报+timeline 留痕）+ LogRedirector tee 文件留档（2026-09-29 台账复发案）

运行: uv run python tests/smoke_offline.py   （全绿 exit 0）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.ai.client import UsageStats
from hexa.ai.engine import (AIDecisionEngine, KIND_NEXT, KIND_OPENING,
                            RuleDecisionEngine)
from hexa.core.game_state import GameSession
from hexa.core.live_api import LivePoller, LiveSnapshot, PlayerInfo
from hexa.core.lcu import PhaseMachine
from hexa.core.mouse_ledger import CardLedger
from hexa.core.rules_engine import (RulesEngine, SwapCandidate, ThreatProfile,
                                    is_full_build)
from hexa.core.screen_detect import (DEFAULT_LAYOUT_169, DetectorCore, Layout,
                                     capture_region, is_fullscreen_cover,
                                     point_in_rect)
from hexa.knowledge.kb import KnowledgeBase

# 复盘日志隔离：GameSession 局终落盘按 BASE_DIR 定位 log/games/，
# 测试进程统一指到临时目录（测试结束自动清理），不再污染真实日志。
# 两处都要改：落盘读 game_state 的模块副本，[6b] 断言读 hexa.config。
import atexit
import shutil
import tempfile

import hexa.config as _config
import hexa.core.game_state as _game_state

_SMOKE_BASE = Path(tempfile.mkdtemp(prefix="hexaram_smoke_"))
_config.BASE_DIR = _SMOKE_BASE
_game_state.BASE_DIR = _SMOKE_BASE
atexit.register(shutil.rmtree, _SMOKE_BASE, ignore_errors=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


# ── 测试用公共对象 ────────────────────────────────────────────────

kb = KnowledgeBase()
rules = RulesEngine(kb)

# 用真实存在的海克斯名做台账测试（"心之刚"是计划示例名，不在当前名单，
# 联动逻辑单独用直调测试覆盖）
REAL_AUG = sorted(kb.augment_names)[0]


def make_snap(my_hero_en="Annie", my_items=None, dead=False, enemy_items=None,
              enemy_kda=(0, 0, 0), my_team="ORDER", my_gold=0):
    snap = LiveSnapshot(ts=time.time(), ok=True)
    snap.my_champion_en = my_hero_en
    snap.my_champion = kb.champion_cn(my_hero_en) or my_hero_en
    snap.my_is_dead = dead
    snap.my_current_gold = my_gold
    snap.my_items = [{"name": n, "count": 1} for n in (my_items or [])]
    snap.players.append(PlayerInfo(
        champion=snap.my_champion, champion_en=my_hero_en, team=my_team,
        items=snap.my_items, kills=2, deaths=0, assists=3))
    snap.players.append(PlayerInfo(
        champion="盲僧", champion_en="LeeSin", team="CHAOS",
        items=[{"name": n, "count": 1} for n in (enemy_items or [])],
        kills=enemy_kda[0], deaths=enemy_kda[1], assists=enemy_kda[2]))
    for cn in ("黑暗之女", "九尾妖狐", "皎月女神"):  # 3 个 AP 英雄做威胁画像
        snap.players.append(PlayerInfo(champion=cn, champion_en="", team="CHAOS"))
    return snap


def full_snap(my_hero_en, my_rows, foe_rows, my_items=None, my_gold=0):
    """5v5 快照构造器（军师/顺逆风测试用）。

    my_rows: [(中文名, k, d, a), ...] 我方 5 人（首位=我，配 my_items）
    foe_rows: [(中文名, k, d, a, [装备名]), ...] 敌方 5 人
    """
    snap = LiveSnapshot(ts=time.time(), ok=True)
    snap.my_champion_en = my_hero_en
    snap.my_champion = kb.champion_cn(my_hero_en) or my_hero_en
    snap.my_current_gold = my_gold
    snap.my_items = [{"name": n, "count": 1} for n in (my_items or [])]
    cn0, k0, d0, a0 = my_rows[0]
    snap.players.append(PlayerInfo(
        champion=snap.my_champion, champion_en=my_hero_en, team="ORDER",
        items=snap.my_items, kills=k0, deaths=d0, assists=a0))
    for cn, k, d, a in my_rows[1:]:
        snap.players.append(PlayerInfo(champion=cn, champion_en="",
                                       team="ORDER", kills=k, deaths=d,
                                       assists=a))
    for cn, k, d, a, items in foe_rows:
        snap.players.append(PlayerInfo(
            champion=cn, champion_en="", team="CHAOS",
            items=[{"name": n, "count": 1} for n in (items or [])],
            kills=k, deaths=d, assists=a))
    return snap


# ── 1. KnowledgeBase ─────────────────────────────────────────────

def test_kb():
    print("\n[1] KnowledgeBase")
    check("173 英雄先验", len(kb.hero_augments) == 173, f"got {len(kb.hero_augments)}")
    check("海克斯名单（剔除 ？？？ 占位后 228）",
          len(kb.augment_names) == 228 and "？？？" not in kb.augment_names,
          f"got {len(kb.augment_names)}")
    check("装备库加载", len(kb.items) > 250, f"got {len(kb.items)}")
    check("DD 版本号", kb.dd_version.startswith("16."), kb.dd_version)
    # 峡谷/大乱斗商店买不到的模式专属件不入库（推荐出去 = 幻觉）
    check("竞技场专属装备已剔除",
          all(n not in kb.items for n in
              ("飞升护符", "黑洞护手", "兰德里的苦楚", "传说级战士装备")),
          str([n for n in ("飞升护符", "黑洞护手", "兰德里的苦楚")
               if n in kb.items]))
    check("乱斗同名条目不压峡谷价",
          kb.items.get("放血者的诅咒", {}).get("gold") == 2900,
          str(kb.items.get("放血者的诅咒", {}).get("gold")))
    # 按图可买性（"11"=峡谷 / "12"=大乱斗；KIWI 件(77xxx)虽被 DD 标 map12，
    # 真机商店没有 → 不入库，麦瑞德裂血手套 即此类）
    check("按图可买性接口",
          not kb.valid_item("麦瑞德裂血手套")
          and kb.item_available("无尽之刃", "11")
          and kb.item_available("无尽之刃", "12")
          and not kb.item_available("麦瑞德裂血手套", "")
          and not kb.item_available("不存在的装备", "11")
          and kb.item_available("无尽之刃", "")  # 空图=宽松兜底
          and not kb.item_available("魔切", "11"),  # 自动升级件不可直购，不推荐
          "")
    # 虚空献祭案 2026-09-29：海斗任务合成件（升级祭献/提亚马特任务等海克斯
    # 解锁，商店货架没有）——DD 标 map12 可买，守卫摘除可买地图；库内保留
    # （任务合成后会上身，台账/幻觉校验/卖价要用）。大乱斗货架真卖的专属
    # 件（救生索/幽魂弯刀/毁坏仪式，带 from 合成链）不误伤
    check("任务合成件入库但不可推荐",
          all(kb.valid_item(n) for n in
              ("虚空献祭", "终极九头蛇", "沃格勒特的巫师帽"))
          and not kb.item_available("虚空献祭", "12")
          and not kb.item_available("虚空献祭", "")
          and not kb.item_available("终极九头蛇", "12")
          and not kb.item_available("沃格勒特的巫师帽", "12"), "")
    check("大乱斗货架专属件不受任务件守卫误伤",
          all(kb.item_available(n, "12")
              for n in ("救生索", "幽魂弯刀", "毁坏仪式")), "")
    check("拼音搜索", kb.search_hero("ayjm") == (["暗裔剑魔"], True))
    check("英中转换", kb.champion_cn("Aatrox") == "暗裔剑魔")
    check("英雄校验模糊", kb.validate_hero("剑魔") == "暗裔剑魔")
    top = kb.top_augments("暗裔剑魔", 10)
    check("海克斯 top 查询", len(top) == 10 and all(kb.valid_augment(a) for a in top))

    # P1：OP.GG builds 接入
    check("173 英雄 builds", len(kb.builds) == 173, f"got {len(kb.builds)}")
    check("builds 版本", kb.builds_patch.startswith("16."), kb.builds_patch)
    pool = kb.core_item_pool("暗裔剑魔")
    check("核心装池非空", len(pool) >= 5, str(pool[:3]))
    check("核心装池全真实", all(kb.valid_item(n) for n in pool))
    combos = kb.starter_combos("暗裔剑魔")
    check("出门装组合", len(combos) >= 1 and all(kb.valid_item(n)
                                                for c in combos for n in c))
    skill = kb.skill_advice("暗裔剑魔")
    check("加点建议", bool(skill["priority"]) and len(skill["order"]) >= 10,
          str(skill["priority"]))
    boots = kb.boots_options("暗裔剑魔")
    check("鞋子选项", len(boots) >= 1 and kb.valid_item(boots[0][0]))
    # 语义标签接口（库缺失时返回空 dict 不报错）
    check("标签接口安全", isinstance(kb.augment_tag_info(REAL_AUG), dict))


# ── 2. RulesEngine ───────────────────────────────────────────────

def test_rules():
    print("\n[2] RulesEngine")
    check("空输入路线未定", rules.infer_current_path([], []) == "")
    check("AD 装备推断", rules.infer_current_path(["无尽之刃"], []) == "ad")
    check("AP 装备推断", rules.infer_current_path(["灭世者的死亡之帽", "爆裂魔杖"], []) == "ap")
    check("坦克装备推断", rules.infer_current_path(["日炎圣盾"], []) == "tank")
    check("海克斯偏移(心之刚示例)",
          rules.infer_current_path(["巨人腰带"], ["心之刚"]) == "tank")

    # 出门装：OP.GG starter 优先（P1 升级），手工池补足，全部真实装备
    cands = rules.opening_candidates("黑暗之女")  # Annie=ap
    check("出门装三选", len(cands) == 3, str([c.name for c in cands]))
    check("出门装 OP.GG 优先", cands[0].name ==
          kb.starter_combos("黑暗之女")[0][0], str([c.name for c in cands]))
    check("出门装全真实", all(kb.valid_item(c.name) for c in cands))
    check("出门装带价格", all(c.price > 0 for c in cands))
    # 心之刚联动（计划验收场景：拿到心之刚 → 巨人腰带，联动置顶）
    cands = rules.opening_candidates("暗裔剑魔", first_augment="心之刚")
    check("心之刚→巨人腰带联动置顶", cands[0].name == "巨人腰带",
          str([c.name for c in cands]))
    check("联动候选全部真实装备", all(kb.valid_item(c.name) for c in cands))

    # 下一件：OP.GG 高频池 + 路线约束 + 抗性放行
    threat = ThreatProfile(ap_count=3, ad_count=1)
    cands = rules.next_item_candidates(["爆裂魔杖"], "ap", threat, hero="黑暗之女")
    check("下一件三选", len(cands) == 3, str([c.name for c in cands]))
    check("下一件候选全真实", all(kb.valid_item(c.name) for c in cands))
    opgg = set(kb.core_item_pool("黑暗之女"))
    got = {c.name for c in rules.next_item_candidates(
        ["爆裂魔杖"], "ap", ThreatProfile(), hero="黑暗之女", limit=6)}
    check("AP 下一件 OP.GG 高频主导", len(got & opgg) >= 3,
          f"{sorted(got)} vs pool {sorted(opgg)[:6]}")
    # AD 路线不串 AP 主线件（抗性件除外）
    ad_cands = rules.next_item_candidates(["多兰之刃"], "ad", ThreatProfile(),
                                          hero="暗裔剑魔", limit=6)
    bad = [c.name for c in ad_cands
           if kb.item_path_bias(c.name) not in ("ad", "tank", None)]
    check("AD 候选不串 AP 主线", not bad, str(bad))
    # 抗性件语义（真机回归 20260927：AD 小丑被"对面物伤多"推护甲装）
    # 输出位（ap/ad）：候选只来自 OP.GG 池，敌方构成不再往里塞抗性件，
    # 带抗性的输出件（如中娅）理由也不挂"补护甲/补魔抗"误导文案
    tp = ThreatProfile(ap_count=3)
    cands2 = rules.next_item_candidates(["爆裂魔杖"], "ap", tp, hero="黑暗之女",
                                        limit=20)
    misleading = [c.name for c in cands2
                  if "补魔抗" in c.reason or "补护甲" in c.reason]
    check("输出位不推抗性装不挂误导文案",
          not misleading and all(kb.item_available(c.name, "12")
                                 for c in cands2), str(misleading))
    # 坦克位：敌方物伤多 → 抗性件池照常开放且加权
    opgg_pool_t = set(kb.core_item_pool("暗裔剑魔"))
    cands3 = rules.next_item_candidates(["巨人腰带"], "tank",
                                        ThreatProfile(ad_count=4),
                                        hero="暗裔剑魔", limit=10)
    check("坦克位对抗物伤推护甲",
          any("Armor" in kb.items.get(c.name, {}).get("tags", [])
              for c in cands3), str([c.name for c in cands3][:5]))

    # v4.3 核心线优先（用户验收 20260927：先出自己流派的核心三件套，
    # 敌方阵容是后续调整；小丑 ad=收集者线 / ap=黯炎火炬·面具线）
    seq = rules.build_sequence([], "ad", ThreatProfile(), hero="恶魔小丑")
    check("小丑AD核心线置顶",
          [c.name for c in seq] == ["收集者", "电震涡流剑", "无尽之刃"]
          and all("核心装" in c.reason for c in seq),
          str([(c.name, c.reason) for c in seq]))
    seq_ap = rules.build_sequence([], "ap", ThreatProfile(), hero="恶魔小丑")
    check("小丑AP切法系核心线",
          seq_ap and seq_ap[0].name == "黯炎火炬"
          and "兰德里的折磨" in [c.name for c in seq_ap],
          str([c.name for c in seq_ap]))
    seq_j = rules.build_sequence([], "ad", ThreatProfile(), hero="暗裔剑魔")
    # 2026-09-29 重抓后 OP.GG 剑魔第一线为 锯齿短匕·狂妄 开局（4 件全
    # ad bias，票数压过 星蚀·焚天·死亡之舞 的 3 票）
    check("剑魔爆杀流核心线", seq_j and seq_j[0].name == "锯齿短匕",
          str([c.name for c in seq_j]))
    seq2 = rules.build_sequence(["收集者"], "ad", ThreatProfile(),
                                hero="恶魔小丑")
    check("核心线跳过已拥有", seq2 and seq2[0].name == "电震涡流剑",
          str([c.name for c in seq2]))
    # 按当前地图过滤（真机回归：峡谷局曾推出 KIWI 专属 麦瑞德裂血手套）
    tp_ad = ThreatProfile(ad_count=4)
    got11 = {c.name for c in rules.next_item_candidates(
        ["多兰之刃"], "ad", tp_ad, hero="恶魔小丑", limit=30, game_map="11")}
    got12 = {c.name for c in rules.next_item_candidates(
        ["多兰之刃"], "ad", tp_ad, hero="恶魔小丑", limit=30, game_map="12")}
    check("峡谷局不推乱斗/KIWI 专属件",
          "麦瑞德裂血手套" not in got11
          and all(kb.item_available(n, "11") for n in got11),
          str(sorted(got11)[:8]))
    check("大乱斗局不推峡谷专属件",
          all(kb.item_available(n, "12") for n in got12),
          str(sorted(got12 - got11)[:8]))
    # 虚空献祭案 2026-09-29：任务合成件混进 System2 方案池也必须被
    # item_available 拦下（真机曾把它推成主线②"还差4956"，
    # 商店里根本搜不到）
    seq_task = rules.build_sequence(
        [], "tank", ThreatProfile(), hero="曙光女神", game_map="12",
        pool=["虚空献祭", "钢铁烈阳之匣", "石像鬼石板甲"])
    check("任务合成件不进主线（方案池过滤）",
          seq_task and seq_task[0].name == "钢铁烈阳之匣"
          and "虚空献祭" not in [c.name for c in seq_task],
          str([c.name for c in seq_task]))

    # 小件合成：爆裂魔杖 → 某个 AP 传说件
    target, note = rules.component_advice(["爆裂魔杖"], "ap")
    check("小件合成建议存在", target is not None, note)
    check("合成目标路线一致", target and kb.item_path_bias(target) == "ap", str(target))

    # 回归 2026-09-27 真机：心之钢已买出、身上第二件巨人腰带——旧版按 into
    # 数据序把已拥有的心之钢又顶回推荐①位；现在目标排除已拥有且优先对齐
    # 推荐序列（腰带往正在攒的坦克核心件合）
    mine_t = ["心之钢", "巨人腰带"]
    seq_t = rules.build_sequence(mine_t, "tank", ThreatProfile(), hero="曙光女神")
    check("坦克核心线排除已拥有心之钢",
          "心之钢" not in [c.name for c in seq_t], str([c.name for c in seq_t]))
    target_t, note_t = rules.component_advice(mine_t, "tank", game_map="12",
                                              prefer=[c.name for c in seq_t])
    check("腰带不再往已拥有的心之钢合",
          target_t and target_t != "心之钢", note_t)
    check("腰带目标对齐推荐序列",
          target_t in [c.name for c in seq_t], str(target_t))
    target_f, _ = rules.component_advice(mine_t, "tank", game_map="12")
    check("数据序兜底同样排除已拥有",
          target_f and target_f != "心之钢", str(target_f))

    # 防御件让位核心（真机 2026-09-29 小火龙案：长剑→饮魔刀置顶压过未成型
    # 核心三件套——"哪怕对面 AP 多也应先出核心"）。坦克/战士与核心已成型
    # 者不退让；让位时返回 (None, 提示语)，由 GameSession 降级为附注行。
    # 威胁给"魔抗优先"使防御件排到合成候选首位（双抗均衡时进攻向小件
    # 会先胜出，让位分支不触发——那是另一条正确路径）
    tp_mr = ThreatProfile(ap_count=3, ad_count=1)
    tgt_def, note_def = rules.component_advice(["长剑"], "ad",
                                               hero="炽炎雏龙", threat=tp_mr)
    check("输出英雄核心未成型：防御合成让位核心（饮魔刀）",
          tgt_def is None and "核心优先" in note_def and "饮魔刀" in note_def,
          (tgt_def, note_def))
    tgt_off, _ = rules.component_advice(["考尔菲德的战锤"], "ad",
                                        hero="炽炎雏龙", threat=tp_mr)
    check("进攻向合成不退让（照常置顶）",
          tgt_off is not None, str(tgt_off))
    tgt_tk, _ = rules.component_advice(["巨人腰带"], "tank",
                                       hero="曙光女神", threat=tp_mr)
    check("坦克防御合成不退让（防御件本就是主线）",
          tgt_tk is not None, str(tgt_tk))
    line_sm = kb.core_build_line("炽炎雏龙", "ad")
    tgt_done, _ = rules.component_advice(["长剑"] + line_sm, "ad",
                                         hero="炽炎雏龙", threat=tp_mr)
    check("核心已成型恢复防御置顶", tgt_done is not None, str(tgt_done))

    # 威胁画像：敌方英雄 + 装备扫描
    tp = rules.threat_profile(
        ["九尾妖狐", "皎月女神", "黑暗之女", "盲僧", "众星之子"],
        [PlayerInfo(champion="盲僧", items=[{"name": "荆棘之甲", "count": 1}])])
    check("AP 数统计", tp.ap_count == 4, str(tp.ap_count))
    # 4 AP = 九尾妖狐/皎月女神/黑暗之女/众星之子（皎月女神经画像特例
    # 修正为 ap，辅助按 Support 标签归 ap；盲僧物伤+荆棘之甲坦票重≥2
    # → 只计坦克，退出物伤计数）
    check("治疗识别", tp.healers == ["众星之子"])
    check("敌方关键装扫描", tp.key_items and tp.key_items[0]["item"] == "荆棘之甲")
    tp_multi = ThreatProfile(key_items=[
        {"champion": "a", "item": "荆棘之甲", "note": ""},
        {"champion": "b", "item": "中娅沙漏", "note": ""},
        {"champion": "c", "item": "莫雷洛秘典", "note": ""},
        {"champion": "d", "item": "兰顿之兆", "note": ""}])
    check("威胁摘要关键装取前3件去重（原只显示第一件）",
          "关键装:荆棘之甲/中娅沙漏/莫雷洛秘典" in tp_multi.summary(),
          tp_multi.summary())

    # 伤害类型按实际出装推断（装备覆盖英雄默认）：女枪默认 AD，出 AP 传说件按法伤计
    tp_ap = rules.threat_profile(
        ["赏金猎人"],
        [PlayerInfo(champion="赏金猎人",
                    items=[{"name": "灭世者的死亡之帽", "count": 1},
                           {"name": "虚空之杖", "count": 1}])])
    check("女枪 AP 出装按法伤计",
          tp_ap.ap_count == 1 and tp_ap.ad_count == 0, tp_ap.summary())
    # 单小件证据不足，仍按英雄默认（防早期误翻转）
    tp_early = rules.threat_profile(
        ["赏金猎人"],
        [PlayerInfo(champion="赏金猎人", items=[{"name": "增幅典籍", "count": 1}])])
    check("单小件不推翻默认路线",
          tp_early.ad_count == 1 and tp_early.ap_count == 0, tp_early.summary())

    # 坦克出装退出伤害威胁计数（真机 2026-09-28 木木局泰坦案）：泰坦底板
    # damage=ap+is_tank，全出坦装（坦克票重≥2）时只计坦克不计法伤——
    # 否则 ap_count 凑到 3 误触发"补魔抗"建议
    tp_tank = rules.threat_profile(
        ["深海泰坦", "迅捷斥候", "灰烬驱魔人"],
        [PlayerInfo(champion="深海泰坦",
                    items=[{"name": "日炎圣盾", "count": 1},
                           {"name": "荆棘之甲", "count": 1}])])
    check("坦装泰坦只计坦克不计法伤",
          tp_tank.ap_count == 2 and tp_tank.tank_count == 1, tp_tank.summary())
    # 对比：泰坦出 AP 传说件（法伤票重≥2）时仍按法伤计（底板+出装双证）
    tp_ap_naut = rules.threat_profile(
        ["深海泰坦"],
        [PlayerInfo(champion="深海泰坦",
                    items=[{"name": "灭世者的死亡之帽", "count": 1}])])
    check("泰坦出法伤装仍计法伤",
          tp_ap_naut.ap_count == 1 and tp_ap_naut.tank_count == 1,
          tp_ap_naut.summary())
    # 坦克底板无实证不计伤害（泰坦案口径推广到名字先验，2026-09-29 小火龙
    # 局祖安狂人案）：底板 ap+is_tank 早期无出装时双计 法伤+坦克，法伤×3
    # 实为×2；≥2 票重的非坦出装才允许坦克计回伤害侧
    tp_mundo = rules.threat_profile(["祖安狂人", "发条魔灵"])
    check("坦克底板无出装实证只计坦克不计法伤",
          tp_mundo.ap_count == 1 and tp_mundo.tank_count == 1,
          tp_mundo.summary())
    tp_mundo_ap = rules.threat_profile(
        ["祖安狂人"],
        [PlayerInfo(champion="祖安狂人",
                    items=[{"name": "灭世者的死亡之帽", "count": 1}])])
    check("坦克出法伤传说件仍计法伤",
          tp_mundo_ap.ap_count == 1 and tp_mundo_ap.tank_count == 1,
          tp_mundo_ap.summary())

    # 威胁加权重定向（真机 2026-09-29 剑圣案）：人数打平时按装备估值定向、
    # 死亡来源破局——被谁砍死谁才是真威胁
    tp_fed = rules.threat_profile(
        ["无极剑圣", "迅捷斥候", "深海泰坦"],
        [PlayerInfo(champion="无极剑圣",
                    items=[{"name": "破败王者之刃", "count": 1},
                           {"name": "鬼索的狂暴之刃", "count": 1},
                           {"name": "暴食胫甲", "count": 1}]),
         PlayerInfo(champion="迅捷斥候",
                    items=[{"name": "增幅典籍", "count": 1}]),
         PlayerInfo(champion="深海泰坦",
                    items=[{"name": "日炎圣盾", "count": 1},
                           {"name": "荆棘之甲", "count": 1}])])
    check("估值按最终路线累计（坦线泰坦不入两侧）",
          tp_fed.ad_value >= 5000 and 0 < tp_fed.ap_value < tp_fed.ad_value,
          f"ad={tp_fed.ad_value:.0f} ap={tp_fed.ap_value:.0f}")
    check("champ_damage 逐敌登记（坦线记空串）",
          tp_fed.champ_damage == {"无极剑圣": "ad", "迅捷斥候": "ap",
                                  "深海泰坦": ""}, str(tp_fed.champ_damage))
    check("打平+估值悬殊→护甲优先",
          ThreatProfile(ap_count=2, ad_count=2, ap_value=3000,
                        ad_value=11000).resist_advice() == "护甲优先")
    tp_even = ThreatProfile(ap_count=2, ad_count=2,
                            ap_value=22100, ad_value=23850)
    check("估值接近（真机 7.5% 差）保持双抗均衡",
          tp_even.resist_advice() == "双抗均衡", tp_even.resist_advice())
    tp_even.killer_counts = {"无极剑圣": 3}
    tp_even.champ_damage = {"无极剑圣": "ad"}
    check("死亡实证（剑圣连杀×3）破平局→护甲优先",
          tp_even.resist_advice() == "护甲优先", tp_even.resist_advice())
    check("摘要显示死亡来源", "死于:无极剑圣×3" in tp_even.summary(),
          tp_even.summary())
    check("AP 击杀者反向加票",
          ThreatProfile(ap_count=2, ad_count=2,
                        champ_damage={"九尾妖狐": "ap"},
                        killer_counts={"九尾妖狐": 2}
                        ).resist_advice() == "魔抗优先")
    check("单杀1次视为噪音",
          ThreatProfile(ap_count=2, ad_count=2, ap_value=22100,
                        ad_value=23850, champ_damage={"九尾妖狐": "ap"},
                        killer_counts={"九尾妖狐": 1}
                        ).resist_advice() == "双抗均衡")
    check("坦线击杀者不映射方向",
          ThreatProfile(ap_count=2, ad_count=2, ap_value=22100,
                        ad_value=23850, champ_damage={"深海泰坦": ""},
                        killer_counts={"深海泰坦": 4}
                        ).resist_advice() == "双抗均衡")

    # 海克斯时刻评级（S/A/B 合成评分）
    top3 = kb.top_augments("暗裔剑魔", 3)
    ranks = rules.rank_augments("暗裔剑魔", top3)
    check("评级三项", len(ranks) == 3 and all(r["grade"] in "SABC" for r in ranks))
    check("榜首 S 档", ranks[0]["grade"] == "S", str(ranks[0]))
    offmeta = sorted(kb.augment_names - set(kb.top_augments("暗裔剑魔", 40)))[0]
    check("榜内但榜外(该英雄) C 档",
          rules.rank_augments("暗裔剑魔", [offmeta])[0]["grade"] == "C", offmeta)
    check("非库名 '?' 档", rules.rank_augments(
        "暗裔剑魔", ["不存在的海克斯名"])[0]["grade"] == "?")
    check("评级名字全真实", all(kb.valid_augment(r["name"]) for r in ranks))


# ── 3. GameSession 生命周期 ──────────────────────────────────────

def test_session():
    print("\n[3] GameSession 全生命周期")
    session = GameSession(kb, rules)

    # 选人
    session.on_champ_select({"ally": ["黑暗之女"], "enemy": [], "my_hero": "黑暗之女",
                             "source": "test"})
    check("选人阶段", session.stage == "champ_select")
    check("英雄跟随", session.my_hero == "黑暗之女")

    # 骰子换人 → 重新跟随
    session.on_champ_select({"ally": ["暗裔剑魔"], "enemy": [], "my_hero": "暗裔剑魔",
                             "source": "test"})
    check("换人跟随", session.my_hero == "暗裔剑魔")

    # 加载（阵容锁定）
    teams = {"ally": ["暗裔剑魔", "九尾妖狐", "皎月女神", "盲僧", "众星之子"],
             "enemy": ["黑暗之女", "狂暴之心", "放逐之刃", "诺克萨斯之手", "琴瑟仙女"],
             "my_hero": "暗裔剑魔", "source": "test"}
    session.on_game_start(teams)
    check("阵容锁定", len(session.ally) == 5 and len(session.enemy) == 5)
    check("威胁画像生成", session.threat and session.threat.ap_count >= 2,
          session.threat.summary() if session.threat else "None")
    session.on_in_progress()
    check("对局中", session.stage == "in_game")

    # 出门装状态机分支一：首个海克斯记账时未购装备 → 出门装三选
    snap = make_snap(my_hero_en="Aatrox", my_items=[])
    session.on_snapshot(snap)
    session.record_augment(REAL_AUG, tier="棱彩")
    check("首海克斯→出门装", session.suggestion and session.suggestion.kind == "opening",
          session.suggestion.render() if session.suggestion else "None")
    check("出门装三选项", len(session.suggestion.candidates) == 3)

    # 分支二 + 定时快照 diff（v4.2）：买到装备后（无死亡）签名变化即转入出装主线
    snap = make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"])
    session.on_snapshot(snap)
    check("AD 路线推断", session.current_path == "ad", session.current_path)
    check("购装备后自动转主线(无死亡)", session.suggestion.kind == "next_items"
          and session.opening_state == "bypassed", session.suggestion.kind)
    seq1 = session.suggestion
    check("主线≥2件且带价格", len(seq1.candidates) >= 2
          and all(c.price > 0 for c in seq1.candidates),
          str([(c.name, c.price) for c in seq1.candidates]))
    # 相同快照 → 不重算（防刷新）；金币档变化 → 重算
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"]))
    check("快照无变化不刷新", session.suggestion is seq1)
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"], my_gold=4000))
    check("金币变化触发重算", session.suggestion is not seq1)
    text = session.suggestion.render()
    check("金币感知渲染", "金币 4000" in text and "可购买" in text, text.split(chr(10))[0])
    # 首件若可负担，"可购买"必须标在首件（防"先算钱后插入合成线"的顺序回归）
    first = session.suggestion.candidates[0]
    if first.price <= 4000:
        check("首件可购买标注", first.afford_note == "可购买",
              f"{first.name} {first.price}G -> {first.afford_note!r}")

    # 死亡（事件触发保留）→ 立即重算主线
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"], dead=True))
    check("死亡→重算主线", session.suggestion.kind == "next_items",
          session.suggestion.kind)

    # 小件合成优先呈现
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"]))
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"], dead=True))
    check("小件合成判断", session.suggestion.candidates[0].reason
          and ("合成" in session.suggestion.candidates[0].reason
               or "攒钱" in session.suggestion.candidates[0].reason),
          session.suggestion.candidates[0].reason)

    # 敌方关键装备触发
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"],
                                  enemy_items=["荆棘之甲"]))
    check("敌方关键装提示", session.suggestion and session.suggestion.kind == "event_note"
          and "荆棘之甲" in session.suggestion.render(),
          session.suggestion.render() if session.suggestion else "None")

    # 敌方 KDA 起飞触发（每局限 2 次）
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"],
                                  enemy_kda=(6, 0, 4)))
    check("敌方起飞提示", session.suggestion and "起飞" in session.suggestion.render(),
          session.suggestion.render() if session.suggestion else "None")

    # 海克斯台账 + current_path 重估
    before = len(session.augments)
    check("台账防重复", session.record_augment(REAL_AUG) is False)
    check("结束前台账非空", len(session.augments) == max(before, 1))

    # 海克斯识别状态标识（overlay 状态流转数据源）
    session.on_hex_ui(True)
    st = session.ui_state()
    check("hex 界面在屏未识别", st["hex_active"] and not st["hex_recognized"]
          and st["hex_last"] == "" and st["hex_picked"] is None)
    session.rank_augment_cards([REAL_AUG] * 3)
    st = session.ui_state()
    check("hex 识别成功标记", st["hex_active"] and st["hex_recognized"]
          and len(st["card_ranks"]) == 3, str(st["hex_recognized"]))
    other = sorted(kb.augment_names)[1]
    session.record_augment(other, card_idx=1)
    st = session.ui_state()
    check("hex 点选卡标记", st["hex_picked"] == [1, other], str(st["hex_picked"]))
    session.on_hex_reroll()
    st = session.ui_state()
    check("hex reroll 回识别中", st["hex_active"] and not st["hex_recognized"]
          and st["hex_picked"] is None and st["card_ranks"])
    session.rank_augment_cards([REAL_AUG] * 3)
    session.on_hex_ui(False)
    st = session.ui_state()
    check("hex 关闭留成功摘要", not st["hex_active"] and "✓" in st["hex_last"],
          st["hex_last"])
    session.on_hex_ui(True)
    session.on_hex_ui(False)
    st = session.ui_state()
    check("hex 关闭留失败摘要", "✗" in st["hex_last"], st["hex_last"])
    session.on_hex_ui(True)   # 复位（下面 end_of_game reset 也会清）

    # 结束 → 重置
    session.on_end_of_game("EndOfGame")
    st = session.ui_state()
    check("结束重置", session.stage == "idle" and not st["augments"]
          and session.suggestion is None and session.my_hero is None)

    # ui_state 快照完整性
    check("ui_state 键齐全", {"stage", "my_hero", "ally", "enemy", "augments",
                              "current_path", "threat", "suggestion",
                              "dd_version", "path_drift",
                              "build_progress"} <= set(st.keys()))
    fresh = session.data_freshness()
    check("新鲜度字段", "date" in fresh and fresh["dd_version"].startswith("16."))


# ── 4. PhaseMachine ──────────────────────────────────────────────

class FakeLCU:
    def __init__(self):
        self.phase_script = []
        self.teams = {"ally": ["黑暗之女"], "enemy": [], "my_hero": "黑暗之女",
                      "source": "FakeLCU"}

    def is_connected(self):
        return True

    def connect(self):
        return True

    def get_gameflow_phase(self):
        return self.phase_script.pop(0) if self.phase_script else None

    def get_both_teams(self):
        return self.teams


def test_phase_machine():
    print("\n[4] PhaseMachine 状态转换")
    lcu = FakeLCU()
    events = []
    pm = PhaseMachine(lcu, callbacks={
        "on_champ_select": lambda t: events.append(("champ_select",)),
        "on_game_start": lambda t: events.append(("game_start",)),
        "on_in_progress": lambda: events.append(("in_progress",)),
        "on_end_of_game": lambda r: events.append(("end_of_game", r)),
        "on_disconnect": lambda: events.append(("disconnect",)),
        "on_reconnect": lambda: events.append(("reconnect",)),
    })

    def run(phase):
        pm._handle(phase)

    run("Lobby")            # idle
    run("ChampSelect")      # 进入选人
    run("ChampSelect")      # 选人持续刷新（骰子跟随）
    run("GameStart")
    run("InProgress")
    run("WaitingForStats")  # 正常结束
    kinds = [e[0] for e in events]
    check("选人事件≥2（含刷新）", kinds.count("champ_select") >= 2, str(kinds))
    check("GameStart 事件", "game_start" in kinds)
    check("InProgress 事件", "in_progress" in kinds)
    check("结束事件", "end_of_game" in kinds and
          events[kinds.index("end_of_game")][1] == "WaitingForStats")

    # 回归（2026-09-25 真机丢日志）：局终前接口瞬时 None 曾把 _phase 从
    # InProgress 覆盖成 None，永久错过 InProgress→IDLE 转换 → 复盘日志不落盘
    events.clear()
    pm2 = PhaseMachine(FakeLCU(), callbacks={
        "on_in_progress": lambda: events.append(("in_progress",)),
        "on_end_of_game": lambda r: events.append(("end_of_game", r)),
    })
    pm2._handle("InProgress")
    pm2._handle(None)               # 瞬时抖动：保留上一阶段
    pm2._handle("InProgress")       # 恢复（cur==prev 不重发）
    pm2._handle("WaitingForStats")  # 局终必须照常触发
    kinds2 = [e[0] for e in events]
    check("瞬时 None 不吞局终事件",
          kinds2 == ["in_progress", "end_of_game"], str(kinds2))


# 秒退重开：InProgress → ChampSelect → end_of_game(dodge_requeue) + 新选人
    events.clear()
    run("InProgress")
    run("ChampSelect")
    kinds = [e[0] for e in events]
    check("秒退触发结束", "end_of_game" in kinds and
          events[kinds.index("end_of_game")][1] == "dodge_requeue", str(events))
    check("秒退后重新选人", kinds.count("champ_select") >= 1)

    # 回归（2026-09-28 真机）：真机 gameflow 序列 InProgress → PreEndOfGame
    # → EndOfGame → None——PreEndOfGame 不算收尾时 on_end_of_game 整局不
    # 触发（会话卡"对局中"、复盘日志不落盘）
    events.clear()
    pm3 = PhaseMachine(FakeLCU(), callbacks={
        "on_in_progress": lambda: events.append(("in_progress",)),
        "on_end_of_game": lambda r: events.append(("end_of_game", r)),
    })
    pm3._handle("InProgress")
    pm3._handle("PreEndOfGame")   # 结算画面出现（胜利/失败界面）
    pm3._handle("EndOfGame")
    pm3._handle("None")           # 回大厅
    kinds3 = [e[0] for e in events]
    check("PreEndOfGame 即局终（真机序列）",
          kinds3 == ["in_progress", "end_of_game"]
          and events[1][1] == "PreEndOfGame", str(events))

    # 断连（In game 中连续取不到 phase）
    class DeadLCU(FakeLCU):
        def is_connected(self):
            return False

        def connect(self):
            return False

    pm2 = PhaseMachine(DeadLCU(), callbacks={"on_disconnect": lambda: events.append(("disconnect",))})
    pm2._phase = "InProgress"
    for _ in range(PhaseMachine.DISCONNECT_NEEDED):
        check_phase = pm2._poll_phase()
    check("断连事件", ("disconnect",) in events, str(events[-3:]))


# ── 4.5 Live API 本地玩家定位（国服兜底） ────────────────────────

def test_live_me_match():
    print("\n[4.5] Live API 本地玩家定位（国服 activeplayer 缺字段兜底）")
    poller = LivePoller(kb)
    # 国服实测形态：activeplayer.championName=None，playerlist 中文名 + raw 英文键
    cn_pl = [
        {"championName": "曙光女神",
         "rawChampionName": "game_character_displayname_Leona",
         "summonerName": "玩家甲", "team": "ORDER",
         "items": [{"name": "圣物之盾", "count": 1}]},
        {"championName": "黑暗之女",
         "rawChampionName": "game_character_displayname_Annie",
         "summonerName": "我我我", "team": "CHAOS"},
    ]
    me, via = poller._match_me(
        {"championName": None, "currentGold": 2774.9,
         "rawChampionName": "game_character_displayname_Annie"}, cn_pl)
    check("国服 rawChampionName 定位", me is cn_pl[1] and via == "rawChampionName",
          f"via={via}")

    # 极端：raw 也缺失 → 退到 LCU 召唤师名（summonerName / riotId）
    poller2 = LivePoller(kb, summoner_name_fn=lambda: "我我我")
    me2, via2 = poller2._match_me({"championName": None}, cn_pl)
    check("召唤师名定位", me2 is cn_pl[1] and via2 == "summoner", f"via={via2}")

    # 国际服 championName 路径不回归
    intl = [{"championName": "Leona", "team": "ORDER"},
            {"championName": "Annie", "team": "CHAOS"}]
    me3, via3 = poller2._match_me({"championName": "Annie"}, intl)
    check("国际服 championName 定位", me3 is intl[1] and via3 == "championName",
          f"via={via3}")

    # 定位不到 → (None, "")，且不误配
    me4, via4 = poller2._match_me({}, [{"championName": "Leona"}])
    check("定位不到不误配", me4 is None and via4 == "", f"via={via4}")
    # playerlist 空 / championName 为 null 均安全
    check("空 playerlist 安全", poller._match_me({}, []) == (None, ""))
    check("None 防护", str(poller._norm_champ_raw(None)) == "")

    # 死亡来源（/eventdata ChampionKill → 谁在杀我）：增量去重 + 新局清零
    poller._my_event_names = {"我我我"}
    poller._name_to_champ = {"yisum": "无极剑圣", "nautsum": "深海泰坦"}
    kill_payload = {"events": [
        {"EventID": 1, "EventName": "GameStart"},
        {"EventID": 2, "EventName": "ChampionKill",
         "KillerName": "yisum", "VictimName": "我我我"},
        {"EventID": 3, "EventName": "ChampionKill",
         "KillerName": "Turret", "VictimName": "我我我"},
        {"EventID": 4, "EventName": "ChampionKill",
         "KillerName": "nautsum", "VictimName": "队友甲"},
        {"EventID": 5, "EventName": "ChampionKill",
         "KillerName": "yisum#CN1", "VictimName": "我我我#CN1"},
    ]}
    poller._process_kill_events(kill_payload, my_disp="曙光女神")
    check("击杀者计数（只认英雄+Victim=我，#tag 归一）",
          poller._kill_counts == {"无极剑圣": 2}, str(poller._kill_counts))
    poller._process_kill_events(kill_payload, my_disp="曙光女神")
    check("EventID 单调去重不重复计数",
          poller._kill_counts == {"无极剑圣": 2}, str(poller._kill_counts))
    poller._process_kill_events({"events": [
        {"EventID": 2, "EventName": "ChampionKill",
         "KillerName": "nautsum", "VictimName": "我我我"}]}, my_disp="x")
    check("新局 EventID 回退清零重计",
          poller._kill_counts == {"深海泰坦": 1}, str(poller._kill_counts))


# ── 5. 画面检测状态机 / 坐标 / 记账映射（纯逻辑，离线） ──────────

def test_detect_and_layout():
    print("\n[5] DetectorCore / Layout / CardLedger")
    core = DetectorCore()
    check("初始未激活", core.state == "inactive")
    core.feed_titles(2)
    check("单帧命中不激活（迟滞）", core.state == "inactive")
    core.feed_titles(3)
    check("连续 2 帧命中激活", core.state == "active" and core.round_id == 1)
    core.feed_cards(["贪欲束缚", None, "秘术射击"])
    check("单帧内容不稳定", core.stable_cards is None)
    core.feed_cards(["贪欲束缚", None, "秘术射击"])
    check("稳定 2 帧出结果", core.stable_cards == ["贪欲束缚", None, "秘术射击"])
    core.consume_cards()
    # reroll：内容变化 → 重新等待稳定
    core.feed_cards(["秘术射击", "贪欲束缚", None])
    check("内容变化重置", core.stable_cards is None)
    core.feed_cards(["秘术射击", "贪欲束缚", None])
    check("重扫后稳定", core.stable_cards == ["秘术射击", "贪欲束缚", None])
    # 瞬时漏读容忍（真机 2026-09-27 复盘：流光动画下整帧/部分漏读逐帧交替
    # → 内容永不稳定 → last_cards 空 → 点击记账被静默丢弃、台账空白）
    core3 = DetectorCore()
    core3.feed_titles(2)
    core3.feed_titles(2)
    core3.feed_cards(["贪欲束缚", None, "秘术射击"])
    core3.feed_cards([None, None, None])   # 整帧漏读 = 上帧"子集"
    check("整帧漏读不重置稳定", core3.stable_cards == ["贪欲束缚", None, "秘术射击"])
    core3.consume_cards()
    core3.feed_cards([None, None, None])
    check("漏读帧不重发（闩锁保持）", core3.stable_cards is None)
    check("last_content 含补齐读数", core3.last_content
          == ("贪欲束缚", None, "秘术射击"))
    core3.feed_cards(["秘术射击", "贪欲束缚", None])   # 有槽位读出不同名 = 真变化
    check("真变化不被补齐吞掉", core3.stable_cards is None
          and core3.last_content == ("秘术射击", "贪欲束缚", None))
    core3.feed_cards(["秘术射击", "贪欲束缚", None])
    check("真变化后重新稳定", core3.stable_cards == ["秘术射击", "贪欲束缚", None])
    # 消失迟滞：连续 3 帧无命中
    core.feed_titles(0)
    check("1 帧无命中仍活跃", core.state == "active")
    core.feed_titles(0)
    core.feed_titles(0)
    check("连续 3 帧无命中消失", core.state == "inactive")
    # 再出现 → 轮次 +1
    core.feed_titles(2)
    core.feed_titles(2)
    check("再出现轮次递增", core.state == "active" and core.round_id == 2)

    # Layout：默认 16:9 → 像素；21:9 中心锚定
    lay = Layout()
    px = lay.to_pixels(1920, 1080)
    cards = px["cards"]
    check("卡片像素在屏内", all(0 <= r[0] and r[0] + r[2] <= 1920 and
                                0 <= r[1] and r[1] + r[3] <= 1080 for r in cards))
    check("三卡片水平有序", cards[0][0] < cards[1][0] < cards[2][0])
    # 默认布局卡片必须覆盖卡名行（真值 y≈0.398~0.427H）——历史缺陷：cards
    # 从名行下方开始导致游戏内永不识别
    check("卡片含名行带", all(r[1] <= 0.396 * 1080 and r[1] + r[3] >= 0.43 * 1080
                              for r in cards), str(cards))
    # rerolls ×3：默认布局 3 个刷新按钮，屏内、有序、且在卡片下方（不交叠）
    rrs = px["rerolls"]
    check("rerolls 三个且屏内", len(rrs) == 3 and
          all(0 <= r[0] and r[0] + r[2] <= 1920 and r[1] + r[3] <= 1080
              for r in rrs), str(rrs))
    check("rerolls 有序且低于卡片", rrs[0][0] < rrs[1][0] < rrs[2][0]
          and all(r[1] >= c[1] + c[3] for r, c in zip(rrs, cards)))
    wide = lay.to_pixels(3440, 1440)  # 21:9
    wcx = wide["cards"][1][0] + wide["cards"][1][2] / 2
    check("21:9 中心锚定", abs(wcx - 3440 / 2) < 120, f"cx={wcx}")
    # 往返一致性（H 单位换算无损）
    lay2 = Layout(lay.as_169())
    check("Layout 往返一致", lay2.title_bands == lay.title_bands
          and lay2.cards == lay.cards and lay2.rerolls == lay.rerolls)

    # 旧标定兼容：单 reroll 文件可读取（复制三份），其余区域照常
    legacy = {"units": "h",
              "title_bands": lay.title_bands, "cards": lay.cards,
              "reroll": lay.rerolls[0]}
    lay_old = Layout(legacy)
    check("旧单 reroll 兼容读取", len(lay_old.rerolls) == 3
          and all(r == lay.rerolls[0] for r in lay_old.rerolls))

    # _regions_valid：卡片交叠的垃圾推导必须被拒（默认布局须通过）
    from hexa.core.screen_detect import ScreenDetector
    det_v = ScreenDetector(kb, type("StubOcr", (), {"available": True})())
    check("默认布局过区域校验", det_v._regions_valid(lay.as_169()))
    overlapping = {"title_bands": lay.as_169()["title_bands"],
                   "cards": [[0.20, 0.18, 0.20, 0.49],
                             [0.35, 0.18, 0.20, 0.49],
                             [0.60, 0.18, 0.20, 0.49]],
                   "rerolls": lay.as_169()["rerolls"]}
    check("卡片交叠拒绝落盘", not det_v._regions_valid(overlapping))

    # 回归：活跃期自愈标定分支曾误写 self.MIN_TITLE_HITS（常量定义在
    # DetectorCore 上），每帧 AttributeError 被线程兜底吞掉 → 活跃期卡片
    # 识别整体失效且日志刷屏。桩掉截屏/OCR 后跑两帧全帧识别，on_cards 必须触发
    import numpy as _np
    det_a = ScreenDetector(kb, type("StubOcr", (), {"available": True})(),
                           layout=Layout(lay.as_169()))
    det_a._screen_size = lambda: (1920, 1080)
    det_a._grab = lambda: _np.zeros((1080, 1920, 3), dtype=_np.uint8)
    _cards3 = ["有始有终", "黎明使者的坚决", "鲨鱼诱饵"]
    det_a._ocr_title_strips = lambda strips: (3, list(_cards3))
    det_a._ocr_cards = lambda frame, rects, prev_texts=None: (list(_cards3), True)
    det_a._auto_calibrate_frame = lambda frame: None
    det_a.core.state = "active"
    _got = []
    det_a.on_cards = lambda cards, rid: _got.append((list(cards), rid))
    _aerr = ""
    try:
        det_a._tick()
        det_a._tick()
    except Exception as e:  # noqa: BLE001
        _aerr = repr(e)
    check("活跃期全帧识别不抛 MIN_TITLE_HITS",
          bool(_got) and _got[0][0] == _cards3, _aerr)

    # 点击时刻补扫：识别未稳定时的记账兜底（真机 2026-09-28 复盘：
    # 界面刚出现的窗口期点击被静默丢弃 → 台账缺记录）。补扫必须直接
    # 消费 recognize 原文——经 _ocr_cards@72 预过滤时库外文本早被置
    # None，留痕恒 raw=None（2026-09-29 核验实锤后改直连）
    ch0 = cards[0][3] or 1
    _raw_hit = [("「有始有终」", [8, int(0.45 * ch0), 120, 40], 0.95)]
    _raw_miss = [("无漆安途", [8, int(0.45 * ch0), 120, 40], 0.95)]

    class _SyncOcr:
        available = True
        out = _raw_hit

        @staticmethod
        def recognize(image):
            return list(_SyncOcr.out)

        @staticmethod
        def match_augment(text, threshold=72):
            # 镜像真实引擎：先清洗标点（ocr_engine.py:77-80）再匹配；
            # 库名恒命中；库外文本低于任意正阈值，仅 threshold=0
            # （模糊兜底）返回最低分候选
            cleaned = "".join(
                ch for ch in text
                if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
            if cleaned in _cards3:
                return (cleaned, 99)
            return None if threshold > 0 else (cleaned or text, 0)

    det_a.ocr = _SyncOcr()
    got_raw = det_a.sync_ocr_card_raw(0)
    check("点击补扫命中：raw=装饰原文（非归一化名）",
          got_raw == (_cards3[0], "「有始有终」"), str(got_raw))
    check("薄壳 sync_ocr_card 返回库名", det_a.sync_ocr_card(0) == _cards3[0])
    _SyncOcr.out = _raw_miss
    got_miss = det_a.sync_ocr_card_raw(1)
    check("点击补扫库外名：raw 留原文（不再恒 None）",
          got_miss == (None, "无漆安途"), str(got_miss))
    check("补扫越界安全", det_a.sync_ocr_card(9) is None)

    # save_layout 并发原子性（检测线程自愈 × 手动标定线程两写者回归）：
    # 交错写后文件必须仍是完整 JSON，且无 .tmp 残留
    import tempfile as _tempfile
    import threading as _threading
    from pathlib import Path as _Path
    import json as _json
    from hexa.core import screen_detect as _sd
    regions_ok = lay.as_169()
    with _tempfile.TemporaryDirectory() as td:
        old_dir, _sd.CALIB_DIR = _sd.CALIB_DIR, _Path(td)
        try:
            errs = []

            def _writer(tag):
                try:
                    for _ in range(30):
                        _sd.save_layout(1234, 567, regions_ok, 0.9)
                except Exception as e:  # noqa: BLE001
                    errs.append(e)
            ts = [_threading.Thread(target=_writer, args=(i,)) for i in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            doc = _json.loads((_Path(td) / "1234x567.json").read_text(
                encoding="utf-8"))
            leftovers = list(_Path(td).glob("*.tmp"))
        finally:
            _sd.CALIB_DIR = old_dir
        check("并发 save_layout 不损坏", not errs and
              doc["regions"]["cards"] == regions_ok["cards"], str(errs))
        check("并发写无 .tmp 残留", not leftovers, str(leftovers))

    # 窗口化/副屏适配：窗口矩形 → 截屏区域/坐标换算（纯函数，不依赖真窗口）
    check("客户区→mss截屏区域参数", capture_region(100, 50, 1600, 900) ==
          {"left": 100, "top": 50, "width": 1600, "height": 900}
          and capture_region(-1920, 200, 1280, 720)["left"] == -1920)
    check("整屏覆盖判定", is_fullscreen_cover((0, 0, 1920, 1080), 1920, 1080)
          and not is_fullscreen_cover((8, 31, 1912, 1049), 1920, 1080)
          and not is_fullscreen_cover((0, 0, 1600, 900), 1920, 1080))
    # Layout 绑定窗口：默认输出屏幕坐标（+原点偏移，鼠标落点记账直读），
    # canvas=True 输出画布坐标（截屏帧裁剪）；旧调用方的过期入参被覆盖
    lay_w = Layout()
    lay_w.bind_window((100, 50, 1600, 900))
    px_win = lay_w.to_pixels(1920, 1080)             # 过期的整屏入参
    px_cvs = lay_w.to_pixels(1920, 1080, canvas=True)
    w0, c0 = px_win["cards"][0], px_cvs["cards"][0]
    check("绑定窗口输出屏幕坐标",
          w0[:2] == [100 + c0[0], 50 + c0[1]] and w0[2:] == c0[2:],
          str((w0, c0)))
    check("画布坐标按客户区尺寸换算",
          all(0 <= r[0] and r[0] + r[2] <= 1600 and 0 <= r[1]
              and r[1] + r[3] <= 900 for r in px_cvs["cards"] + px_cvs["rerolls"]),
          str(px_cvs["cards"] + px_cvs["rerolls"]))
    lay_w.bind_window(None)
    check("解绑恢复整屏坐标", lay_w.to_pixels(1920, 1080) ==
          Layout().to_pixels(1920, 1080))

    # 鼠标落点 → 卡片序号
    rects = [tuple(r) for r in px["cards"]]
    cl = CardLedger(rects_provider=lambda: rects)
    inside = (rects[1][0] + 5, rects[1][1] + 5)
    check("落点映射卡片2", cl.card_at(*inside) == 1)
    check("空白处无映射", cl.card_at(5, 5) is None)
    check("point_in_rect", point_in_rect(10, 10, [5, 5, 10, 10])
          and not point_in_rect(16, 10, [5, 5, 10, 10]))

    # MouseLedger 矩形归一化：单矩形/列表/None
    from hexa.core.mouse_ledger import MouseLedger
    check("归一化单矩形", MouseLedger._as_rect_list([1, 2, 3, 4]) == [[1, 2, 3, 4]])
    check("归一化矩形列表", MouseLedger._as_rect_list(
        [[1, 2, 3, 4], [5, 6, 7, 8]]) == [[1, 2, 3, 4], [5, 6, 7, 8]])
    check("归一化空值", MouseLedger._as_rect_list(None) == []
          and MouseLedger._as_rect_list([]) == [])

    # 点击记账证据门：迟滞窗口内的点击也要能记账（台账空白根因回归）
    check("无证据默认不记账", det_v.recent_hex_activity() is False)
    det_v.core.state = "active"
    check("活跃期允许记账", det_v.recent_hex_activity() is True)
    det_v.core.state = "inactive"

    # on_cards 发射门：≥2 个有效库名才上报（复用上面 det_v 实例）
    check("发射门：全有效名 3", det_v.count_valid_cards(
        [REAL_AUG, REAL_AUG, REAL_AUG]) == 3)
    check("发射门：单名+垃圾 1（拒发）", det_v.count_valid_cards(
        [REAL_AUG, "幻觉卡名文本", None]) == 1)
    check("发射门：两个真名过门", det_v.count_valid_cards(
        ["贪欲束缚", REAL_AUG, None]) >= 2)


# ── 6. AI 引擎（FakeClient，不联网） ─────────────────────────────

class FakeSettings:
    """Settings 协议子集（get/set），预置 AI 开 + 两槽位已配置"""

    def __init__(self, budget=15, min_interval=0.0):
        self._d = {
            "ai": {"enabled": True, "budget_per_game": budget,
                   "min_interval": min_interval},
            "system1": {"model": "fake-flash", "price_in": 0.8, "price_out": 2.7},
            "system2": {"model": "fake-max", "price_in": 2.4, "price_out": 9.6},
            "triggers": {},
        }

    def get(self, *keys, default=None):
        node = self._d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def get_float(self, *keys, default):
        try:
            return float(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

    def resolve_api_key(self, slot):
        # Settings.resolve_api_key 的明文分支（冒烟不依赖环境变量兜底）
        return str(self.get(slot, "api_key", default="") or "").strip()

    def set(self, *keys_and_value):
        *keys, value = keys_and_value
        node = self._d
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value


class FakeClient:
    """LLMClient 协议子集：按脚本依次返回 canned 文本；记录调用"""

    def __init__(self, script):
        self.script = list(script)   # 每次调用弹出一个（耗尽则返回 "{}"）
        self.calls = []
        self.usage = UsageStats()
        self.slot = "fake"

    @property
    def configured(self):
        return True

    def chat(self, system, user, max_tokens=None, json_mode=True):
        self.calls.append(user)   # 存完整 user（重试带因等断言要看尾部）
        text = self.script.pop(0) if self.script else "{}"
        self.usage.add(100, 50)
        return text, {"model": "fake", "latency": 0.01,
                      "tokens_in": 100, "tokens_out": 50}


def build_ai_engine(script_s2=(), script_s1=(), budget=15, min_interval=0.0):
    st = FakeSettings(budget=budget, min_interval=min_interval)
    delegate = RuleDecisionEngine(kb, rules)
    c2 = FakeClient(list(script_s2))
    c1 = FakeClient(list(script_s1))
    from hexa.ai import system1, system2
    eng = AIDecisionEngine(delegate, st, c2, c1, system2, system1)
    return eng, c1, c2


def test_ai_engine():
    print("\n[6] AI 引擎（FakeClient）")
    hero = "暗裔剑魔"
    # ---- System2 正常生成（canned 合法 JSON，全部真实名字） ----
    top = kb.top_augments(hero, 10)
    pool = kb.core_item_pool(hero)
    ctx_json = {
        "augment_affinity": [{"name": n, "bias": "ad", "note": "测试"} for n in top],
        "opening_items": [{"combo": pool[:1] or ["长剑"], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [{"name": "爆杀流", "path": "ad", "items": pool[:4],
                    "note": "主线"}],
        "skill_order": "主Q副E",
        "component_rules": [{"item": "考尔菲德的战锤",
                             "upgrade_to": pool[0] if pool else "星蚀",
                             "hold_when": "钱不够先留"}],
        "augment_item_links": [{"augment": top[0], "shift": "偏 ad",
                                "reason": "测试联动"}],
    }
    import json as _json
    eng, c1, c2 = build_ai_engine(script_s2=[_json.dumps(ctx_json)])
    results = []
    eng.bind(lambda kind, payload, serial: results.append((kind, payload, serial)))
    eng.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    ctx = eng.cache.get()
    check("System2 上下文生成", ctx is not None and ctx.source == "ai")
    check("亲和表有效", ctx and len(ctx.augment_affinity) == 10)
    check("加点来自 ctx（顶层单值）", eng.skill_order(hero) == "主Q副E")
    check("规则模式无 ctx 时加点", RuleDecisionEngine(kb, rules)
          .skill_order(hero) == "主Q副E")
    check("方案池查询（pool_for 首方案序）",
          ctx and ctx.pool_for("ad") == pool[:4],
          str(ctx.pool_for("ad") if ctx else None))
    # ---- Prompt 强化回归：敌方明细管道 / 幽灵字段清除 / 语义标签 ----
    check("engine 敌方明细（名+伤害+Role 画像数据直出）",
          eng._enemy_detail() == ["黑暗之女(ap,Mage/Support)"],
          str(eng._enemy_detail()))
    check("engine 适配池查询（ctx 就绪；路线无匹配→None 回落）",
          eng.pool_for("ad") == pool[:4] and eng.pool_for("tank") is None)
    from hexa.ai import system2 as _s2
    check("System2 无幽灵字段指令（is_upgrade/置信度指向不存在的 schema 字段）",
          "is_upgrade 标记" not in _s2.SYSTEM_PROMPT
          and "标注置信度" not in _s2.SYSTEM_PROMPT)
    aug_line = _s2._fmt_augments(kb, hero)
    check("海克斯名单带 KB 语义标签（bias 等，防零先验考幻觉）",
          "bias=" in aug_line, aug_line[:80])
    comp_line = _s2._fmt_enemy_comp(kb, ["九尾妖狐", "暗裔剑魔", "牛头酋长"])
    # 画像原样直出（damage+Role），治疗/坦克识别交给模型判断
    check("敌方构成行（画像数据直出，零语义加工）",
          "九尾妖狐(ap,Mage/Assassin)" in comp_line
          and "暗裔剑魔(ad,Fighter)" in comp_line
          and "牛头酋长(ap,Tank/Support)" in comp_line, comp_line)
    cleaned = _s2.validate_and_clean(_json.loads(_json.dumps(ctx_json)),
                                     kb, hero)
    check("component_rules 已移除（零消费输出不再生成；旧输入键被忽略）",
          "component_rules" not in cleaned)
    # ---- builds 方案校验（防幻觉/碎件/坏 path/杂项守卫/去重） ----
    bad_builds = _s2.validate_and_clean({
        "augment_affinity": [{"name": n, "bias": "ad", "note": "t"}
                             for n in top],
        "opening_items": [{"combo": ["长剑"], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [
            {"name": "杂项流", "path": "ad",
             "items": ["属性加成", "长剑", "焚天", "星蚀"], "note": "x"},
            {"name": "碎件流", "path": "ad", "items": ["长剑"], "note": "x"},
            {"name": "坏path流", "path": "apx",
             "items": ["长剑", "焚天", "星蚀"], "note": "x"},
            {"name": "幻觉流", "path": "ad",
             "items": ["不存在的装备", "长剑", "焚天"], "note": "x"},
        ],
        "skill_order": "主Q副E",
        "augment_item_links": []}, kb, hero)
    got_b = bad_builds["builds"]
    check("builds 校验：杂项守卫/碎件剔除/坏path剔除/幻觉名过滤",
          len(got_b) == 1 and got_b[0]["name"] == "杂项流"
          and got_b[0]["items"] == ["长剑", "焚天", "星蚀"], str(got_b))
    # ---- System1 出门装精修（异步回调） ----
    threat = ThreatProfile(ap_count=2)
    base = eng.opening_candidates(top[0], threat, serial=1)
    check("出门装同步规则底线", len(base) == 3)
    order = [p["name"] for p in _json.dumps({}) and []]  # noqa: F841（占位说明）
    c1.script.append(_json.dumps({"picks": [
        {"name": "红水晶", "reason": "对面法伤多"},
        {"name": "多兰之刃", "reason": "标准"},
        {"name": "幻觉装备名", "reason": "应被过滤"}]}))
    eng.opening_candidates(top[0], threat, serial=2)
    deadline = time.time() + 5
    while not any(r[0] == KIND_OPENING for r in results) and time.time() < deadline:
        time.sleep(0.02)
    opening_payloads = [p for k, p, _s in results if k == KIND_OPENING]
    check("出门装 AI 精修回调", bool(opening_payloads))
    if opening_payloads:
        names = [c.name for c in opening_payloads[-1]]
        check("AI 精修防幻觉过滤", "幻觉装备名" not in names and len(names) == 3,
              str(names))
    # ---- System1 下一件 + 守门 ----
    c1.script.append(_json.dumps({"picks": [
        {"name": pool[0], "reason": "版本答案"},
        {"name": pool[1] if len(pool) > 1 else "焚天", "reason": "次选"}]}))
    eng.next_candidates(["多兰之刃"], 3000, "ad", threat, serial=3, reason="event")
    deadline = time.time() + 5
    while not any(r[0] == KIND_NEXT for r in results) and time.time() < deadline:
        time.sleep(0.02)
    next_payloads = [p for k, p, _s in results if k == KIND_NEXT]
    check("下一件 AI 精修回调", bool(next_payloads))
    if next_payloads:
        check("精修保持候选内",
              all(kb.valid_item(c.name) for c in next_payloads[-1]))
    # ---- 海克斯时刻评级精修 ----
    c1.script.append(_json.dumps({"grades": [
        {"name": top[0], "grade": "S", "reason": "版本亲和第一"}]}))
    eng.rank_cards(hero, top[:3], [], [], threat, serial=4)
    deadline = time.time() + 5
    got_ranks = None
    while time.time() < deadline:
        got_ranks = getattr(eng, "_last_rank_result", None)
        r0 = [p for k, p, _s in results if k == "augment_ranks"]
        if r0:
            got_ranks = r0[-1]
            break
        time.sleep(0.02)
    check("评级 AI 精修回调", got_ranks is not None and got_ranks[0]["grade"] == "S",
          str(got_ranks[:1] if got_ranks else None))

    # ---- 预算守门：budget=2 时第 3 次 periodic 不发调用 ----
    eng_b, c1b, _c2b = build_ai_engine(
        script_s2=[_json.dumps(ctx_json)], budget=2)
    eng_b.bind(lambda *a: None)
    eng_b.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_b.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    eng_b.opening_candidates(top[0], threat, serial=11)   # 1 次
    time.sleep(0.3)
    eng_b.next_candidates(["多兰之刃"], 0, "ad", threat, serial=12,
                          reason="event")                 # 2 次
    time.sleep(0.3)
    calls_after_budget = len(c1b.calls)
    eng_b.next_candidates(["多兰之刃"], 0, "ad", threat, serial=13,
                          reason="event")                 # 超预算，不调用
    time.sleep(0.3)
    check("预算守门生效", len(c1b.calls) == calls_after_budget == 2,
          f"calls={len(c1b.calls)}")

    # ---- 间隔守门：min_interval 很大时 periodic 被压制，event 不受限 ----
    eng_i, c1i, _ = build_ai_engine(script_s2=[_json.dumps(ctx_json)],
                                    min_interval=3600)
    eng_i.bind(lambda *a: None)
    eng_i.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_i.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    eng_i._last_refine["next_items"] = time.time()  # 模拟刚精修过
    eng_i.next_candidates(["多兰之刃"], 0, "ad", threat, serial=21,
                          reason="periodic")
    time.sleep(0.3)
    check("间隔守门压制 periodic", len(c1i.calls) == 0, f"calls={len(c1i.calls)}")
    eng_i.next_candidates(["多兰之刃"], 0, "ad", threat, serial=22, reason="event")
    time.sleep(0.5)
    check("event 不受间隔守门", len(c1i.calls) >= 1, f"calls={len(c1i.calls)}")

    # ---- System2 幻觉/坏 JSON → 规则版上下文降级 ----
    eng_bad, _c1b2, _c2b2 = build_ai_engine(
        script_s2=["{\"augment_affinity\": [{\"name\": \"幻觉海克斯\"}]}",
                   "not a json"])
    eng_bad.bind(lambda *a: None)
    eng_bad.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_bad.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    ctx_bad = eng_bad.cache.get()
    check("坏输出降级规则版 Context",
          ctx_bad is not None and ctx_bad.source == "rule", str(ctx_bad.source if ctx_bad else None))
    # 回归：降级版联动的海克斯名也须过 B 层校验（曾硬编码"心之刚"——
    # 不在 229 名单内，AI 路径有 validate_and_clean 过滤、降级路径没有）
    bad_links = ctx_bad.augment_item_links if ctx_bad else []
    check("降级联动名全过海克斯校验",
          bad_links and all(kb.valid_augment(l["augment"]) for l in bad_links),
          str([l.get("augment") for l in bad_links]))

    # ---- System2 重试带失败原因（原样重发等于再掷同一次骰子） ----
    eng_rt, _c1rt, c2rt = build_ai_engine(
        script_s2=["not a json", _json.dumps(ctx_json)])
    eng_rt.bind(lambda *a: None)
    eng_rt.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 6
    while eng_rt.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    check("System2 重试上下文附上次失败原因",
          len(c2rt.calls) == 2 and "上一次输出未通过校验" in c2rt.calls[1],
          f"calls={len(c2rt.calls)}")


def test_ai_build_pool():
    print("\n[6c] AI 适配出装方案池（方案制 schema → 规则底线/System1 权衡）")
    import json as _json
    hero = "暗裔剑魔"
    tp = ThreatProfile()

    # ---- 规则层 pool 参数：池序即主线；模型产出仍过杂项/幻觉/异路线过滤 ----
    pool = ["不存在的装备", "属性加成", "焚天", "星蚀", "死亡之舞", "荆棘之甲"]
    seq = rules.build_sequence([], "ad", tp, hero=hero, pool=pool)
    names = [c.name for c in seq]
    check("方案池主线过滤幻觉/杂项",
          "不存在的装备" not in names and "属性加成" not in names
          and seq and seq[0].name == "焚天"
          and "核心装" in seq[0].reason, str(names))
    cands = rules.next_item_candidates([], "ad", tp, hero=hero, limit=5,
                                       pool=pool)
    check("方案池下一件带适配序文案（区别于 OP.GG 高频）",
          cands and cands[0].name == "焚天" and "适配方案" in cands[0].reason,
          str([(c.name, c.reason) for c in cands[:2]]))
    seq_none = rules.build_sequence([], "ad", tp, hero=hero)
    check("pool=None 回 OP.GG 现状（规则兜底不变）",
          seq_none and seq_none[0].name == "锯齿短匕",
          str([c.name for c in seq_none]))

    # ---- 降级 rule_context 同步方案制（OP.GG 组合 → 方案） ----
    from hexa.ai import system2 as _s2x
    rcx = _s2x.ChampionContext(_s2x.rule_context(kb, hero), hero,
                               source="rule")
    check("降级 Context 方案制（path 投票 + pool_for 可查）",
          rcx.builds and rcx.builds[0]["path"] == "ad"
          and rcx.pool_for("ad") and rcx.skill_order_text() == "主Q副E",
          str(rcx.builds)[:120])
    from hexa.ai import system1
    from hexa.core.rules_engine import Candidate as _Cand
    line = system1._builds_line(rcx, "ad")
    check("降级上下文方案段照常渲染",
          "本局适配出装方案" in line and "（AD）" in line, line[:100])

    # ---- engine 接线：ctx 就绪后 base 跟随 current_path 切方案池 ----
    two_builds = {
        "augment_affinity": [{"name": n, "bias": "ad", "note": "t"}
                             for n in kb.top_augments(hero, 8)],
        "opening_items": [{"combo": ["长剑"], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [
            {"name": "爆杀流", "path": "ad",
             "items": ["焚天", "星蚀", "死亡之舞"], "note": "主线"},
            {"name": "消耗流", "path": "ap",
             "items": ["黯炎火炬", "瑞莱的冰晶节杖", "灭世者的死亡之帽"],
             "note": "海克斯偏AP时切换"}],
        "skill_order": "主Q副E",
        "augment_item_links": []}
    eng, _c1, _c2 = build_ai_engine(script_s2=[_json.dumps(two_builds)])
    check("ctx 未就绪 pool_for 为 None（回 OP.GG 池）",
          eng.pool_for("ad") is None and eng._extra_allowed() == [])
    eng.bind(lambda *a: None)
    eng.start_game(hero, [hero], ["黑暗之女"])
    deadline = time.time() + 6
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    check("engine.pool_for 当前路线方案池",
          eng.pool_for("ad")[:3] == ["焚天", "星蚀", "死亡之舞"],
          str(eng.pool_for("ad")))
    base_ap = eng.next_candidates(["长剑"], 0, "ap", ThreatProfile(),
                                  serial=1, reason="event")
    check("base 跟随 current_path 切池（海克斯偏 AP 场景）",
          base_ap and [c.name for c in base_ap][:2]
          == ["黯炎火炬", "瑞莱的冰晶节杖"],
          str([c.name for c in base_ap]))
    base_ad = eng.next_candidates(["长剑"], 0, "ad", ThreatProfile(),
                                  serial=2, reason="event")
    check("base 回切 AD 方案池", base_ad and base_ad[0].name == "焚天",
          str([c.name for c in base_ad]))
    extra = eng._extra_allowed()
    check("跨方案白名单=全方案并集（杂项过滤）",
          "黯炎火炬" in extra and "焚天" in extra, str(extra))

    # ---- System1 跨方案改选：extra_allowed 进 allowed，防幻觉边界不变 ----
    class _CapX:
        configured = True
        usage = UsageStats()

        def __init__(self):
            self.user = ""

        def chat(self, system, user, max_tokens=None, json_mode=True):
            self.user = user
            return (_json.dumps({"picks": [
                {"name": "黯炎火炬", "reason": "海克斯全偏AP"},
                {"name": "焚天", "reason": "主线"}]}), {})
    ctx2b = _s2x.ChampionContext(two_builds, hero)
    capx = _CapX()
    out = system1.suggest_next(capx, ctx2b, ["长剑"], 900, "ad", "威胁", [],
                               [_Cand("焚天", "r", price=3000)],
                               extra_allowed=extra)
    check("System1 跨方案取件（其他方案件进 picks）",
          out and out[0]["name"] == "黯炎火炬", str(out))
    capx2 = _CapX()
    out2 = system1.suggest_next(capx2, ctx2b, ["长剑"], 900, "ad", "威胁", [],
                                [_Cand("焚天", "r", price=3000)],
                                extra_allowed=[])
    # 无 extra_allowed：模型给的黯炎火炬不在候选内 → 有效项<2 → 回规则版
    check("无白名单时跨方案名触发规则版回退",
          out2 and out2[0]["name"] == "焚天", str(out2))

    # ---- 会话级：KIND_CTX 就绪自动刷新建议（方案池替换 OP.GG 兜底） ----
    s7 = GameSession(kb, rules)
    eng7, _f1, _f2 = build_ai_engine(script_s2=[_json.dumps(two_builds)])
    s7.engine = eng7
    eng7.bind(s7._on_engine_result)
    s7.on_game_start({"ally": [hero], "enemy": ["黑暗之女"],
                      "my_hero": hero, "source": "test"})
    s7.on_in_progress()
    s7.record_augment(REAL_AUG)
    s7.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["长剑"],
                             my_gold=800))
    deadline = time.time() + 8
    while time.time() < deadline:
        s = s7.suggestion
        if (s and s.kind == "next_items" and s.candidates
                and s.candidates[0].name == "焚天"):
            break
        time.sleep(0.02)
    s = s7.suggestion
    check("KIND_CTX 就绪自动刷新（规则底线切方案池）",
          s and s.kind == "next_items"
          and s.candidates and s.candidates[0].name == "焚天",
          s.render() if s else "no suggestion")


def test_session_ai_serial():
    print("\n[6b] GameSession × AI 精修 serial 防旧覆新")
    session = GameSession(kb, rules)
    session.on_game_start({"ally": ["暗裔剑魔"], "enemy": ["黑暗之女"],
                           "my_hero": "暗裔剑魔", "source": "test"})
    session.on_in_progress()
    session.record_augment(REAL_AUG, tier="棱彩")
    serial_at_opening = session.suggestion.serial
    # 旧 serial 的 AI 精修结果（应被忽略）
    session._on_engine_result(KIND_OPENING,
                              [__import__("hexa.core.rules_engine",
                                          fromlist=["Candidate"]).Candidate(
                                  "幻觉覆盖", "旧包")], serial_at_opening - 1)
    check("旧 serial 不覆盖", session.suggestion.source == "rule")
    # 新 serial 覆盖（source→ai）
    from hexa.core.rules_engine import Candidate
    session._on_engine_result(KIND_OPENING,
                              [Candidate("红水晶", "AI 建议防守")],
                              serial_at_opening)
    check("新 serial 覆盖为 ai", session.suggestion.source == "ai"
          and session.suggestion.candidates[0].name == "红水晶")
    # next_items 精修覆盖 + 金币可买性重算
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                                  my_gold=5000))
    serial_next = session.suggestion.serial
    session._on_engine_result(KIND_NEXT,
                              [Candidate("星蚀", "AI 版本答案", price=3300),
                               Candidate("焚天", "AI 次选", price=3200)],
                              serial_next)
    check("下一件 AI 覆盖", session.suggestion.source == "ai"
          and session.suggestion.candidates[0].name == "星蚀")
    check("AI 覆盖后可买性重算",
          session.suggestion.candidates[0].afford_note == "可购买"
          and "还差" in session.suggestion.candidates[1].afford_note,
          str([(c.name, c.afford_note) for c in session.suggestion.candidates]))
    # 类型不匹配不覆盖
    session._on_engine_result(KIND_NEXT, [Candidate("X", "y")], serial_at_opening)
    check("类型不匹配不覆盖", session.suggestion.candidates[0].name == "星蚀")

    # 回归 2026-09-27 真机：已拥有成品不回推荐 + 可买性按身上小件抵扣
    # （腰带 1000G 在手，推荐件不该按全价报"还差"）
    session.on_snapshot(make_snap(my_hero_en="Aatrox",
                                  my_items=["心之钢", "巨人腰带"], my_gold=846))
    cands = session.suggestion.candidates
    check("已拥有心之钢不出现在推荐",
          "心之钢" not in [c.name for c in cands],
          str([(c.name, c.afford_note) for c in cands]))
    c0 = cands[0]
    exp = c0.price - 846 - kb.items["巨人腰带"]["gold"]
    check("可买性按身上小件抵扣", exp > 0 and c0.afford_note == f"还差{exp:.0f}",
          f"{c0.name} {c0.afford_note}")

    # 复盘日志（结束触发写盘到 log/games/）
    import json as _json2
    from hexa.config import BASE_DIR
    n_logs = len(list((BASE_DIR / "log" / "games").glob("*.json"))) \
        if (BASE_DIR / "log" / "games").exists() else 0
    session.on_end_of_game("EndOfGame")
    logs = list((BASE_DIR / "log" / "games").glob("*.json")) \
        if (BASE_DIR / "log" / "games").exists() else []
    check("复盘日志落盘", len(logs) == n_logs + 1)
    if logs:
        doc = _json2.loads(logs[-1].read_text(encoding="utf-8"))
        check("复盘日志字段", doc.get("hero") == "暗裔剑魔"
              and doc.get("suggestions"), str(doc.get("hero")))


# ── 6c. 满六神装替换建议（v4.4：格满后改推"卖X换Y"） ────────────

def test_swap_mode():
    print("\n[6c] 满六神装替换建议")
    # AP 法师六神装：5 传说件 + 靴子（含唯一魔抗件 振奋盔甲 供威胁对照）
    full_ap = ["灭世者的死亡之帽", "虚空之杖", "瑞莱的冰晶节杖", "振奋盔甲",
               "残疫", "明朗之靴"]

    # ---- 规则层：替换对的 sell 排序（威胁画像 vs 已有装备） ----
    tp_ap = ThreatProfile(ap_count=4)   # 敌方 4 AP → 魔抗优先
    swaps = rules.replacement_candidates(full_ap, 2000, "ap", tp_ap,
                                         hero="黑暗之女", game_map="12")
    sells = {s.sell for s in swaps}
    check("替换 sell 排序合理（威胁vs已有）",
          bool(swaps)
          and "明朗之靴" not in sells                     # 靴子默认不卖
          and "振奋盔甲" not in sells                     # 唯一魔抗件是威胁答案，不反卖
          and all(kb.items[s.sell]["gold"] >= 2200        # 只卖传说件
                  and "Boots" not in kb.items[s.sell]["tags"] for s in swaps),
          str(sells))
    check("替换 buy 针对性补威胁短板",
          all(s.buy not in full_ap                        # 不与已拥有重复（含被卖件）
              and kb.item_available(s.buy, "12") for s in swaps)
          and any("SpellBlock" in kb.items[s.buy]["tags"] for s in swaps),
          str([(s.sell, s.buy) for s in swaps]))
    c0 = swaps[0]
    check("差价净差按 DD 卖出价核算",
          c0.sell_price == kb.item_sell_price(c0.sell) > 0
          and c0.price == kb.items[c0.buy]["gold"]
          and c0.net_cost == c0.price - c0.sell_price - 2000,
          f"sell{c0.sell_price} buy{c0.price} net{c0.net_cost}")

    # ---- 会话层：满装切替换模式；未满装（药水占格）行为不变 ----
    session = GameSession(kb, rules)   # 默认规则引擎 = AI 关闭
    session.on_game_start({"ally": ["黑暗之女"],
                           "enemy": ["九尾妖狐", "皎月女神", "乐芙兰",
                                     "盲僧", "众星之子"],
                           "my_hero": "黑暗之女", "source": "test"})
    session.on_in_progress()
    session.on_snapshot(make_snap(my_hero_en="Annie",
                                  my_items=full_ap[:5] + ["生命药水"],
                                  my_gold=1500))
    check("未满装（消耗品占格）不受影响",
          session.suggestion.kind == "next_items", session.suggestion.kind)

    # ---- 回归 2026-09-28 真机（炽炎雏龙/木木局）：满装判据收紧 ----
    # 修复前按"6 占格"判满装：1 传说+魄罗佳肴凑格、3 传说+2 组件+鞋都
    # 被切进替换流推"卖X换Y"幻影替换。修复后 is_full_build = 格满 且
    # 传说档非靴件 ≥5（FULL_BUILD_LEGENDARY_MIN）。
    check("占位条目（魄罗佳肴）不入满装格",
          "魄罗佳肴" not in kb.permanent_items(
              ["夺萃之镰", "暴食胫甲", "魄罗佳肴", "狂热", "红水晶", "布甲"]))
    recon_2233 = ["夺萃之镰", "暴食胫甲", "狂热", "红水晶", "布甲", "布甲"]
    check("1传说+鞋+组件凑格不判满装",
          not is_full_build(kb, recon_2233), str(is_full_build(kb, recon_2233)))
    full5_no_boots = ["灭世者的死亡之帽", "虚空之杖", "瑞莱的冰晶节杖",
                      "振奋盔甲", "残疫", "红水晶"]
    check("5传说+1组件（无靴）判满装",
          is_full_build(kb, full5_no_boots))
    session_amumu = GameSession(kb, rules)
    session_amumu.on_game_start({"ally": ["殇之木乃伊"], "enemy": ["九尾妖狐"],
                                 "my_hero": "殇之木乃伊", "source": "test"})
    session_amumu.on_in_progress()
    items_amumu = ["日炎圣盾", "心之钢", "无终恨意", "棘刺背心", "巨人腰带",
                   "暴食胫甲"]
    session_amumu.on_snapshot(make_snap(my_hero_en="Amumu",
                                        my_items=items_amumu, my_gold=515))
    check("木木式 3传说+2组件+鞋 不切替换流（保持买装）",
          session_amumu.suggestion.kind == "next_items",
          session_amumu.suggestion.kind)
    check("木木式局面 session._is_full_build=False",
          not session_amumu._is_full_build())

    session.on_snapshot(make_snap(my_hero_en="Annie", my_items=full_ap,
                                  my_gold=2000))
    text = session.suggestion.render()
    check("满六神装切替换模式（AI 关规则版完整）",
          session.suggestion.kind == "swap_items"
          and session.suggestion.source == "rule"
          and "🔄" in text and "差价" in text and "卖" in text and "买" in text
          and all(isinstance(c, SwapCandidate)
                  for c in session.suggestion.candidates),
          text.split(chr(10))[0])
    # 满装后身上金币变化仍在替换模式内刷新（差价含金币感知）
    session.on_snapshot(make_snap(my_hero_en="Annie", my_items=full_ap,
                                  my_gold=500))
    check("满装金币变化仍走替换流",
          session.suggestion.kind == "swap_items"
          and "现金足够" in session.suggestion.render(),
          session.suggestion.render().split(chr(10))[1][:40])
    # 替换预告：换成无魔抗六神装 → 敌 AP 成型而我无魔抗（提示级，不改主建议）
    full_ap_nomr = ["灭世者的死亡之帽", "虚空之杖", "瑞莱的冰晶节杖",
                    "莫雷洛秘典", "残疫", "明朗之靴"]
    session.on_snapshot(make_snap(my_hero_en="Annie", my_items=full_ap_nomr,
                                  my_gold=600))
    st = session.ui_state()
    check("替换预告触发且不改主建议",
          "魔抗" in st["swap_forecast"]
          and st["suggestion_kind"] == "swap_items",
          st["swap_forecast"])

    # ---- AI 精修：候选对内选优 + 幻觉过滤 + 映射回替换对 ----
    import json as _json
    hero = "黑暗之女"
    top = kb.top_augments(hero, 8)
    pool2 = kb.core_item_pool(hero) or ["长剑"]
    ctx_json = _json.dumps({
        "augment_affinity": [{"name": n, "bias": "ap", "note": "t"} for n in top],
        "opening_items": [{"combo": pool2[:1] or ["多兰之戒"], "when": "标准"},
                          {"combo": ["多兰之戒"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [{"name": "灼烧流", "path": "ap", "items": pool2[:3],
                    "note": "主线"}],
        "skill_order": "主Q副W",
        "component_rules": [], "augment_item_links": []})
    buy_top = [c.buy for c in session.suggestion.candidates]
    eng, c1, _c2 = build_ai_engine(
        script_s2=[ctx_json],
        script_s1=[_json.dumps({"picks": [
            {"name": buy_top[-1], "reason": "AI 认为这件更稳"},
            {"name": "幻觉装备名", "reason": "应被过滤"},
            {"name": buy_top[0], "reason": "AI 次选"}]})])
    session.engine = eng
    eng.bind(session._on_engine_result)
    eng.start_game(hero, [hero], ["黑暗之女"])
    deadline = time.time() + 6
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    session.on_snapshot(make_snap(my_hero_en="Annie", my_items=full_ap_nomr,
                                  my_gold=2600))   # 金币档变化触发 periodic 精修
    deadline = time.time() + 6
    while session.suggestion.source != "ai" and time.time() < deadline:
        time.sleep(0.02)
    ai_c = session.suggestion.candidates
    check("替换 AI 精修映射回候选对",
          session.suggestion.source == "ai"
          and all(isinstance(c, SwapCandidate) for c in ai_c)
          and ai_c[0].buy == buy_top[-1]           # AI 选中的对被置顶
          and ai_c[0].reason == "AI 认为这件更稳"
          and "幻觉" not in session.suggestion.render(),
          str([(c.buy, c.reason) for c in ai_c]))


# ── 8. 军师提示 / 阵容缺口 / 攒钱决策（v4.5） ────────────────────

def test_coach_save_gap():
    print("\n[8] 军师提示 / 阵容缺口 / 攒钱决策")
    from hexa.core.rules_engine import SAVE_GOLD_THRESHOLD
    from hexa.core.rules_engine import Candidate as _Cand

    # ---- 阵容缺口计算（champ_damage_profile 画像统计） ----
    all_ad = ["暗裔剑魔", "影哨", "残月之肃", "寒冰射手", "赏金猎人"]
    g1 = rules.ally_gap(all_ad, my_hero="暗裔剑魔")
    check("缺口：全AD无前排判缺前排+缺AP",
          g1.tanks == 0 and g1.ap == 0 and g1.ad == 5
          and g1.need_tank and g1.need_ap and not g1.need_ad
          and "缺前排" in g1.summary() and "缺AP" in g1.summary(),
          g1.summary())
    balanced = ["牛头酋长", "殇之木乃伊", "暗裔剑魔", "影哨", "九尾妖狐"]
    g2 = rules.ally_gap(balanced)
    check("缺口：双前排+双输出=均衡", not g2.gaps and g2.tanks == 2
          and g2.ap == 3 and g2.ad == 2, g2.summary())
    # 缺前排 → tank 加分只给可扛英雄（is_tank 或 Fighter）
    check("tank 加分只给可扛英雄",
          rules.tank_lean("暗裔剑魔", g1) == 1.5
          and rules.tank_lean("九尾妖狐", g1) == 0.0
          and rules.tank_lean("暗裔剑魔", g2) == 0.0)
    # 缺口影响路线推断：坦/输出各一件的并列压向坦线；纯输出出装不翻转
    check("缺口影响路线推断",
          rules.infer_current_path(["长剑", "红水晶"], [], hero="暗裔剑魔",
                                   gap=g1) == "tank"
          and rules.infer_current_path(["长剑", "红水晶"], [], hero="暗裔剑魔",
                                       gap=g2) == "ad"
          and rules.infer_current_path(["长剑"], [], hero="暗裔剑魔",
                                       gap=g1) == "ad"
          and rules.infer_current_path([], [], hero="暗裔剑魔", gap=g1) == "")
    # gap 经 threat.ally_gap 穿透 engine 层（engine.next_candidates 不透传）
    tp0, tp1 = ThreatProfile(), ThreatProfile()
    tp1.ally_gap = g1
    base = [c.name for c in rules.next_item_candidates(
        ["红水晶"], "tank", tp0, hero="暗裔剑魔", limit=8)]
    withg = [c.name for c in rules.next_item_candidates(
        ["红水晶"], "tank", tp1, hero="暗裔剑魔", limit=8)]
    check("缺口经 threat 穿透影响下一件排序",
          base != withg
          and sum(kb.item_path_bias(n) == "tank" for n in withg)
          >= sum(kb.item_path_bias(n) == "tank" for n in base),
          f"{base[-2:]} vs {withg[-2:]}")

    # ---- 攒钱决策（攒大件 vs 买小件，阈值模块常量） ----
    cap = "灭世者的死亡之帽"
    cap_price = kb.items[cap]["gold"]
    comps = sorted({kb.item_id_to_name.get(i)
                    for i in kb.items[cap]["from"]} - {None})
    comp_sum = sum(kb.items[c]["gold"] for c in comps)
    check("攒钱阈值模块常量=500", SAVE_GOLD_THRESHOLD == 500,
          str(SAVE_GOLD_THRESHOLD))
    seq1 = [_Cand(cap, "测试", price=cap_price)]
    # 组件齐、只差 400 合成款（≤500）且无未拥有组件 → 可攒
    note1 = rules.saving_advice(comps, cap_price - comp_sum - 400, seq1)
    check("攒钱：差价≤500且组件齐→可攒", "可攒" in note1 and "400" in note1
          and note1.startswith("💰"), note1)
    # 空身、现金恰够最便宜组件 → 买小件并写清剩余
    gold2 = min(kb.items[c]["gold"] for c in comps)
    cheap = min(comps, key=lambda c: kb.items[c]["gold"])
    note2 = rules.saving_advice([], gold2, seq1)
    check("攒钱：差价大→先买小件写清剩余",
          "买小件" in note2 and cheap in note2
          and f"剩{gold2 - kb.items[cheap]['gold']:.0f}" in note2, note2)
    # 组件齐但差价 501（超阈值 1 金）→ 不判"可攒"
    note3 = rules.saving_advice(comps, cap_price - comp_sum - 501, seq1)
    check("攒钱：差价超阈值不判可攒", "可攒" not in note3 and "先攒钱" in note3,
          note3)
    # 整件已买得起 → 无附注（金币感知行已说清）
    check("攒钱：整件可购无附注",
          rules.saving_advice([], cap_price + 100, seq1) == "")

    # ---- 军师：同 key 去重 / 保留上限 / 冷却节流 ----
    s4 = GameSession(kb, rules)
    s4._push_coach("t0", "提示0")
    s4._push_coach("t0", "提示0")
    check("军师同 key 去重", len(s4.coach_notes) == 1,
          str([c["text"] for c in s4.coach_notes]))
    for i in range(1, 5):
        s4._push_coach(f"t{i}", f"提示{i}")
    check("军师保留上限 3 条（最新在下）",
          len(s4.coach_notes) == 3
          and [c["text"] for c in s4.coach_notes] == ["提示2", "提示3", "提示4"],
          str([c["text"] for c in s4.coach_notes]))
    s4._push_coach("t0", "提示0")   # 被挤出后冷却期内不回灌
    check("军师冷却节流（60s 内不回灌）",
          "提示0" not in [c["text"] for c in s4.coach_notes]
          and len(s4.coach_notes) == 3)

    # ---- 军师：会话集成（缺口 / 敌方缺口反向 / 顺逆风 / 同快照去重） ----
    enemy_ap = ["九尾妖狐", "皎月女神", "黑暗之女", "铸星龙王", "众星之子"]
    session = GameSession(kb, rules)
    session.on_game_start({"ally": all_ad, "enemy": enemy_ap,
                           "my_hero": "暗裔剑魔", "source": "test"})
    session.on_in_progress()
    ally_rows = [(cn, 6, 1, 4) for cn in all_ad]
    foe_rows = [(cn, 1, 6, 2, []) for cn in enemy_ap]
    # gold=0：攒钱附注不生成（金币未知口径），滚动列表只留三条状态提示，
    # 缺口文案不被挤出（保留上限 3 条与多来源竞争的实态由 s4/逆风场景覆盖）
    session.on_snapshot(full_snap("Aatrox", ally_rows, foe_rows,
                                  my_items=["长剑"], my_gold=0))
    texts = [c["text"] for c in session.coach_notes]
    check("阵容缺口+坦装放大入军师",
          any("缺前排" in t and "坦装价值放大" in t for t in texts), str(texts))
    check("敌方缺口反向提示（全AP→偏魔抗）",
          any("魔抗" in t for t in texts), str(texts))
    check("顺风判断入军师", any("顺风" in t for t in texts), str(texts))
    n_before = len(session.coach_notes)
    n_win = sum("顺风" in c["text"] for c in session.coach_notes)
    session.on_snapshot(full_snap("Aatrox", ally_rows, foe_rows,
                                  my_items=["长剑"], my_gold=0))
    check("军师同快照去重不刷（5s 轮询节流）",
          len(session.coach_notes) == n_before
          and sum("顺风" in c["text"] for c in session.coach_notes) == n_win)

    # 逆风 + 敌方起飞（阵容均衡且敌我输出均衡：军师只含事件+顺逆风两类，
    # 起飞提示不被缺口类挤出；KDA 提示同时压制本帧 periodic → 无攒钱条）
    foe_mix2 = ["盲僧", "放逐之刃", "牛头酋长", "诡术妖姬", "黑暗之女"]
    session2 = GameSession(kb, rules)
    session2.on_game_start({"ally": balanced, "enemy": foe_mix2,
                            "my_hero": "暗裔剑魔", "source": "test"})
    session2.on_in_progress()
    session2.on_snapshot(full_snap("Aatrox", [(cn, 1, 6, 2) for cn in balanced],
                                   [(cn, 6, 1, 4, []) for cn in foe_mix2],
                                   my_items=["长剑"], my_gold=300))
    texts2 = [c["text"] for c in session2.coach_notes]
    check("逆风行动建议（保命装优先）",
          any("逆风" in t and "保命" in t for t in texts2), str(texts2))
    check("敌方起飞入军师", any("起飞" in t for t in texts2), str(texts2))

    # 敌方关键装克制提示入军师（军师格式，滚动保留）
    session3 = GameSession(kb, rules)
    foe_mix = ["盲僧", "放逐之刃", "诺克萨斯之手", "狂暴之心", "诡术妖姬"]
    session3.on_game_start({"ally": balanced, "enemy": foe_mix,
                            "my_hero": "黑暗之女", "source": "test"})
    session3.on_in_progress()
    session3.on_snapshot(full_snap(
        "Annie", [(cn, 2, 2, 2) for cn in balanced],
        [(cn, 2, 2, 2, ["荆棘之甲"] if cn == "盲僧" else []) for cn in foe_mix],
        my_items=["多兰之戒"], my_gold=500))
    check("敌方关键装入军师",
          any("荆棘之甲" in c["text"] for c in session3.coach_notes),
          str([c["text"] for c in session3.coach_notes]))

    # ---- 会话层：攒钱附注随建议渲染 + ui_state 军师字段 ----
    s5 = GameSession(kb, rules)
    s5.on_game_start({"ally": balanced, "enemy": foe_mix,
                      "my_hero": "黑暗之女", "source": "test"})
    s5.on_in_progress()
    s5.record_augment(REAL_AUG)
    # 安妮 AP 主线首件=残疫(2700)，身上双组件(章节1200+魔杖850)、现金250
    # → 差价 400（≤500）且无未拥有组件 → 可攒
    s5.on_snapshot(full_snap("Annie", [(cn, 2, 2, 2) for cn in balanced],
                             [(cn, 2, 2, 2, []) for cn in foe_mix],
                             my_items=["遗失的章节", "爆裂魔杖"], my_gold=250))
    check("攒钱附注随建议渲染（可攒分支）",
          s5.suggestion.kind == "next_items"
          and s5.suggestion.note.startswith("💰")
          and "可攒" in s5.suggestion.note
          and "💰" in s5.suggestion.render(),
          s5.suggestion.note)
    check("攒钱提醒入军师滚动列表",
          any("可攒" in c["text"] for c in s5.coach_notes),
          str([c["text"] for c in s5.coach_notes]))
    st5 = s5.ui_state()
    check("ui_state 军师字段",
          "coach" in st5 and "coach_latest" in st5 and st5["coach"]
          and st5["coach_latest"] == st5["coach"][-1]["text"]
          and len(st5["coach"]) <= 3)

    # ---- System1：军师上下文进 prompt + 可选 coach 字段解析 ----
    from hexa.ai import system1

    class _CoachClient:
        configured = True
        usage = UsageStats()

        def __init__(self):
            self.user = ""

        def chat(self, system, user, max_tokens=None, json_mode=True):
            self.user = user
            return ('{"picks": [{"name": "星蚀", "reason": "r"}],'
                    ' "coach": "逆风先保命，魔抗优先"}', {})

    system1.set_coach_context(["己方阵容缺前排", "敌方缺物伤，魔抗收益更高"])
    cc = _CoachClient()
    picks = system1.suggest_next(cc, None, ["长剑"], 900, "ad", "威胁", [],
                                 [_Cand("星蚀", "r", price=3200)])
    check("军师上下文进 System1 prompt",
          "军师提示" in cc.user and "缺前排" in cc.user and "魔抗" in cc.user)
    check("System1 coach 字段解析（≤30字）",
          system1.last_coach() == "逆风先保命，魔抗优先"
          and picks and picks[0]["name"] == "星蚀",
          system1.last_coach())
    system1.set_coach_context([])
    system1.clear_coach()

    # ---- System1 prompt 强化回归：格式下放/价格/路线/敌方明细 ----
    # 曾在 SYSTEM_PROMPT 全局写死 picks JSON 契约，与 rank_augments 的
    # grades 契约互斥——flash 档模型服从 system 格式时评级精修静默回落
    class _Cap:
        configured = True
        usage = UsageStats()

        def __init__(self):
            self.system = self.user = ""

        def chat(self, system, user, max_tokens=None, json_mode=True):
            self.system, self.user = system, user
            return ('{"picks": [{"name": "星蚀", "reason": "r"}]}', {})

    from hexa.ai import system2 as _s2c
    check("SYSTEM_PROMPT 不含 picks 契约（已下放各任务 user prompt）",
          '"picks"' not in system1.SYSTEM_PROMPT
          and "《输出格式》" in system1.PICKS_FORMAT)
    ctx_r = _s2c.ChampionContext(
        {"augment_affinity": [], "opening_items": [],
         "builds": [{"name": "穿甲流", "path": "ad", "items": ["星蚀"],
                     "note": "poke 局先穿甲"}],
         "skill_order": "主Q副E",
         "augment_item_links": []}, "暗裔剑魔")
    cap = _Cap()
    system1.suggest_next(cap, ctx_r, ["长剑"], 900, "ad", "威胁", [],
                         [_Cand("星蚀", "r", price=3200)],
                         enemy=["黑暗之女(ap)"])
    check("输出格式在 user prompt、system 不再写死",
          "《输出格式》" in cap.user and '"picks"' not in cap.system)
    check("候选行带价格（金币判断有依据）",
          "3200G" in cap.user, cap.user[-200:])
    check("出装方案进 prompt（当前路线方案置前）",
          "本局适配出装方案" in cap.user and "穿甲流（AD）" in cap.user
          and "poke 局先穿甲" in cap.user)
    check("敌方明细进 prompt（死管道复活）",
          "敌方明细" in cap.user and "黑暗之女(ap)" in cap.user)
    cap2 = _Cap()
    system1.rank_augments(cap2, ctx_r, "暗裔剑魔", ["贪欲束缚"], [], [],
                          "威胁", [{"name": "贪欲束缚", "grade": "A",
                                    "reason": "r"}],
                          enemy=["盲僧(ad)"])
    check("评级契约独立声明（system 无 picks 契约干扰）+ 敌方进评级语境",
          '"grades"' in cap2.user and '"picks"' not in cap2.system
          and "敌方：盲僧(ad)" in cap2.user)

    # ---- AI coach 覆盖规则军师文案（会话级端到端） ----
    import json as _json
    hero = "暗裔剑魔"
    top8 = kb.top_augments(hero, 8)
    pool_a = kb.core_item_pool(hero) or ["长剑"]
    ctx2 = _json.dumps({
        "augment_affinity": [{"name": n, "bias": "ad", "note": "t"} for n in top8],
        "opening_items": [{"combo": pool_a[:1], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [{"name": "爆杀流", "path": "ad", "items": pool_a[:3],
                    "note": "主线"}],
        "skill_order": "主Q副E",
        "component_rules": [], "augment_item_links": []})
    s6 = GameSession(kb, rules)
    eng6, _c16, _c26 = build_ai_engine(
        script_s2=[ctx2],
        script_s1=[_json.dumps({"picks": [
            {"name": pool_a[0], "reason": "AI 版本答案"}],
            "coach": "AI军师：先出核心再补抗性"})])
    s6.engine = eng6
    eng6.bind(s6._on_engine_result)
    s6.on_game_start({"ally": all_ad, "enemy": enemy_ap, "my_hero": hero,
                      "source": "test"})
    s6.on_in_progress()
    deadline = time.time() + 6
    while eng6.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    s6.on_snapshot(full_snap("Aatrox", ally_rows, foe_rows,
                             my_items=["长剑"], my_gold=900))
    deadline = time.time() + 6
    while s6.suggestion.source != "ai" and time.time() < deadline:
        time.sleep(0.02)
    check("AI coach 覆盖最新军师文案",
          s6.suggestion.source == "ai" and s6.coach_notes
          and s6.coach_notes[-1]["text"] == "AI军师：先出核心再补抗性",
          str([c["text"] for c in s6.coach_notes]))
    # AI 精修文案打 src="ai" 标记后不再回注 System1 prompt（防自反馈回路：
    # 模型把自己的旧话当"规则层判断"越滚越偏）
    s6._set_coach_context()
    check("AI coach 不回注 prompt",
          all("AI军师" not in t for t in system1.coach_context()),
          str(system1.coach_context()))


# ── 9. 成型度对比 + F9 撤销（v4.6） ──────────────────────────────

def test_build_progress():
    print("\n[9] 成型度对比（六神装进度）+ F9 撤销最近记账")
    # 我在首位（full_snap 约定：首位=我，配 my_items/my_hero）
    ally5 = ["暗裔剑魔", "牛头酋长", "殇之木乃伊", "影哨", "九尾妖狐"]
    foe5 = ["盲僧", "放逐之刃", "诺克萨斯之手", "狂暴之心", "诡术妖姬"]
    legend2 = ["灭世者的死亡之帽", "虚空之杖"]   # 双传说件成型对照
    legend2_gold = sum(kb.items[n]["gold"] for n in legend2)
    session = GameSession(kb, rules)
    session.on_game_start({"ally": ally5, "enemy": foe5,
                           "my_hero": "暗裔剑魔", "source": "test"})
    session.on_in_progress()
    session.on_snapshot(full_snap(
        "Aatrox", [(cn, 2, 2, 2) for cn in ally5],
        [("盲僧", 2, 2, 2, legend2),
         ("放逐之刃", 2, 2, 2, ["生命药水", "多兰之刃"]),   # 药水=消耗品对照
         ("诺克萨斯之手", 2, 2, 2, []),
         ("狂暴之心", 2, 2, 2, []),
         ("诡术妖姬", 2, 2, 2, [])],
        my_items=["长剑"], my_gold=0))
    bp = session.ui_state().get("build_progress")
    rows = {r["name"]: (r["legendary"], r["gold"])
            for r in (bp or {}).get("enemy", [])}
    # 传说件计数 x/6 + 装备估值=DD 总价；药水不占格不计值（多兰之刃照算）
    check("成型度：传说件计数与装备估值",
          bp is not None
          and rows.get("盲僧") == (2, legend2_gold)
          and rows.get("放逐之刃") == (0, kb.items["多兰之刃"]["gold"]),
          str(rows))
    # 敌我分组齐全 + 行内成型度降序 + 返回值深拷贝（改了不脏会话）
    order = [(r["legendary"], r["gold"]) for r in bp["enemy"]]
    bp["ally"][0]["legendary"] = 99
    check("ui_state 成型度字段（敌我分组/降序/深拷贝）",
          set(bp) == {"ally", "enemy"}
          and sorted(r["name"] for r in bp["ally"]) == sorted(ally5)
          and len(bp["enemy"]) == 5
          and order == sorted(order, reverse=True)
          and session.ui_state()["build_progress"]["ally"][0]["legendary"] != 99,
          str(order))
    # 快照未到（待机/装备数据缺失）→ None 占位，UI 按占位渲染不报错
    check("无快照时成型度占位 None",
          GameSession(kb, rules).ui_state()["build_progress"] is None)

    # F9 撤销最近一条（app.py 热键入口）：按台账末条撤；空台账安全返回 None
    session.record_augment(REAL_AUG)
    other = sorted(kb.augment_names)[1]
    session.record_augment(other)
    check("F9 撤销最近一条（revoke_latest_augment）",
          session.revoke_latest_augment() == other
          and [a["name"] for a in session.augments] == [REAL_AUG]
          and session.revoke_latest_augment() == REAL_AUG
          and session.revoke_latest_augment() is None,
          str([a["name"] for a in session.augments]))


# ── 7. Review 修复回归（P0 闩锁 / epoch 代际 / 双触发 / 排序 / 单位） ──

def test_review_fixes():
    print("\n[7] Review 修复回归")
    hero = "暗裔剑魔"
    import json as _json

    def ctx_json_for(h):
        top = kb.top_augments(h, 8)
        pool = kb.core_item_pool(h) or ["长剑"]
        return _json.dumps({
            "augment_affinity": [{"name": n, "bias": "ad", "note": "t"} for n in top],
            "opening_items": [{"combo": pool[:1], "when": "标准"},
                              {"combo": ["多兰之刃"], "when": "进攻"},
                              {"combo": ["红水晶"], "when": "防守"}],
            "builds": [{"name": "爆杀流", "path": "ad", "items": pool[:3],
                        "note": "主线"}],
            "skill_order": "主Q副E",
            "component_rules": [],
            "augment_item_links": []})

    # -- P0：稳定内容闩锁——同内容不重复发射 on_cards（防烧穿 AI 预算） --
    core = DetectorCore()
    core.feed_titles(2); core.feed_titles(2)
    cards = ["贪欲束缚", None, "秘术射击"]
    emissions = 0
    for _ in range(12):  # 12 帧稳定内容
        core.feed_cards(cards)
        if core.consume_cards():
            emissions += 1
    check("闩锁：稳定内容只发一次", emissions == 1, f"emissions={emissions}")
    # reroll 闩锁清零后可再次发射
    core.reset_round()
    core.feed_cards(cards); core.feed_cards(cards)
    check("闩锁：reroll 后可重发", core.consume_cards() is not None)

    # -- P1：epoch 代际——旧局在途结果整包丢弃 --
    eng, c1, _c2 = build_ai_engine(script_s2=[ctx_json_for(hero),
                                              ctx_json_for("黑暗之女")])
    results = []
    eng.bind(lambda kind, payload, serial: results.append((kind, payload, serial)))
    eng.start_game(hero, [], [])
    eng.start_game("黑暗之女", [], [])   # 秒退重开：代际 +1，game1 在途作废
    deadline = time.time() + 6
    while time.time() < deadline and (eng.cache.get() is None
                                      or eng.cache.pending):
        time.sleep(0.02)
    ctx = eng.cache.get()
    check("epoch：缓存英雄=新局", ctx is not None and ctx.hero == "黑暗之女",
          str(ctx.hero if ctx else None))
    eng.rank_cards("黑暗之女", kb.top_augments("黑暗之女", 3), [], [],
                   ThreatProfile(), serial=1)
    deadline = time.time() + 6
    while time.time() < deadline and not any(k == "augment_ranks"
                                             for k, _p, _s in results):
        time.sleep(0.02)
    check("epoch：新局结果正常回调",
          any(k == "augment_ranks" for k, _p, _s in results))

    # -- P2：死亡同帧不双发（event 发出后 periodic 跳过） --
    st = FakeSettings()
    sess = GameSession(kb, rules, settings=st,
                       engine=build_ai_engine(
                           script_s2=[ctx_json_for(hero)],
                           script_s1=[_json.dumps({"picks": [
                               {"name": "星蚀", "reason": "r"}]})])[0])
    sess.on_game_start({"ally": [hero], "enemy": ["黑暗之女"],
                        "my_hero": hero, "source": "test"})
    sess.on_in_progress()
    sess.record_augment(REAL_AUG)
    fake_eng, fc1, _ = build_ai_engine(
        script_s2=[ctx_json_for(hero)],
        script_s1=[_json.dumps({"picks": [{"name": "星蚀", "reason": "r"}]}),
                   _json.dumps({"picks": [{"name": "焚天", "reason": "r"}]})])
    sess.engine = fake_eng
    sess.engine.bind(sess._on_engine_result)
    sess.engine.start_game(hero, [hero], ["黑暗之女"])
    deadline = time.time() + 6
    while sess.engine.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    sess.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                               my_gold=1000))
    deadline = time.time() + 6
    while time.time() < deadline and len(fc1.calls) < 1:
        time.sleep(0.02)  # 首次 periodic 精修
    n_before_death = len(fc1.calls)
    sess.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                               my_gold=1000, dead=True))
    time.sleep(1.0)
    check("死亡同帧不双发", len(fc1.calls) == n_before_death + 1,
          f"before={n_before_death} after={len(fc1.calls)}")

    # -- P2：suggest_opening 保留模型排序 --
    from hexa.ai import system1
    from hexa.ai import system2 as system2_mod
    class _C:
        configured = True
        usage = UsageStats()
        def chat(self, *a, **k):
            return (_json.dumps({"picks": [
                {"name": "红水晶", "reason": "b"},
                {"name": "多兰之刃", "reason": "c"},
                {"name": "长剑", "reason": "a"}]}), {})
    ctx_o = system2_mod.ChampionContext(
        {"augment_affinity": [], "opening_items": [
            {"combo": ["长剑"], "when": "1"}, {"combo": ["多兰之刃"], "when": "2"},
            {"combo": ["红水晶"], "when": "3"}],
         "builds": [], "skill_order": "", "augment_item_links": []},
        hero)
    out = system1.suggest_opening(_C(), ctx_o, hero, "", "", [])
    check("opening 保留模型排序", [o["name"] for o in out] ==
          ["红水晶", "多兰之刃", "长剑"], str([o["name"] for o in out]))

    # -- P2：Layout units:"h" 往返零漂移（任意宽高比） --
    h_regions = {"units": "h",
                 "title_bands": [[-0.3, 0.42, 0.32, 0.05],
                                 [0.0, 0.42, 0.32, 0.05],
                                 [0.3, 0.42, 0.32, 0.05]],
                 "cards": [[-0.35, 0.46, 0.35, 0.4],
                           [0.0, 0.46, 0.35, 0.4],
                           [0.35, 0.46, 0.35, 0.4]],
                 "rerolls": [[-0.35, 0.88, 0.10, 0.06],
                             [0.0, 0.88, 0.10, 0.06],
                             [0.35, 0.88, 0.10, 0.06]]}
    for w, hgt in ((1920, 1080), (3440, 1440), (2560, 1600), (1600, 1200)):
        lay = Layout(dict(h_regions))
        px = lay.to_pixels(w, hgt)
        # 逐矩形比对独立公式（H 单位：x=W/2+xu*H，宽高/纵向均为 *H）
        ok = True
        for got, (xu, y, wu, hh) in zip(
                px["cards"] + px["rerolls"],
                h_regions["cards"] + h_regions["rerolls"]):
            exp = [int(w / 2 + xu * hgt), int(y * hgt),
                   int(wu * hgt), int(hh * hgt)]
            if got != exp:
                ok = False
                check(f"units:h {w}x{hgt} 零漂移", False,
                      f"{got} != {exp}")
                break
        if not ok:
            break
    else:
        check("units:h 4 种分辨率零漂移", True)

    # -- P2：预算原子占位（budget=1 顺序+并发提交只打 1 次） --
    eng_b, c1b, _ = build_ai_engine(script_s2=[ctx_json_for(hero)], budget=1)
    eng_b.bind(lambda *a: None)
    eng_b.start_game(hero, [], [])
    deadline = time.time() + 6
    while eng_b.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    for s in range(5):  # 连发 5 个请求，只有第 1 个应占位成功
        eng_b.next_candidates(["多兰之刃"], 0, "ad", ThreatProfile(),
                              serial=100 + s, reason="event")
    time.sleep(1.0)
    check("预算原子占位", len(c1b.calls) == 1, f"calls={len(c1b.calls)}")

    # -- P0 回归：历史 settings.json 价格空串 → stats() 不炸（防误报"AI: 关"） --
    eng_s, _fc1, _fc2 = build_ai_engine(script_s2=[ctx_json_for(hero)])
    eng_s.settings._d["system1"]["price_in"] = ""    # 旧版设置窗口保存的空串
    eng_s.settings._d["system2"]["price_out"] = ""
    try:
        st_stats = eng_s.stats()
        ok = (st_stats["mode"] == "ai" and st_stats["budget"] == 15
              and st_stats["cost_game"] >= 0)
    except Exception as e:  # noqa: BLE001 —— 回归点就是"不该抛"
        ok = False
        print("stats() 异常:", e)
    check("stats 空价格回退默认", ok, str(st_stats.get("cost_game")))

    # -- P1 回归：build_engine 两槽位用量独立计量（曾共享一份 UsageStats，
    #    stats() 会把同一份 tokens 按两套单价各算一遍，费用虚高近一倍） --
    from hexa.ai.engine import build_engine as _build_engine
    st_be = FakeSettings()
    for _slot in ("system1", "system2"):   # 槽位配好 Key 才会走 AI 分支
        st_be.set(_slot, "api_key", "smoke-key")
    eng_be = _build_engine(kb, rules, st_be)
    check("build_engine 两槽位用量独立",
          eng_be.mode == "ai"
          and eng_be.client_s1.usage is not eng_be.client_s2.usage,
          eng_be.mode)

    # -- Settings.get_float：空串/非法值/缺失键均回退默认 --
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from hexa.config import Settings as _Settings
    with _tempfile.TemporaryDirectory() as td:
        _p = _Path(td) / "settings.json"
        _p.write_text(_json.dumps(
            {"system1": {"price_in": "", "timeout": "abc"}}), encoding="utf-8")
        s = _Settings(path=_p)
        check("get_float 空串回退",
              s.get_float("system1", "price_in", default=0.8) == 0.8)
        check("get_float 非法回退",
              s.get_float("system1", "timeout", default=60) == 60)
        check("get_float 缺键回退",
              s.get_float("system1", "price_out", default=2.7) == 2.7)
        check("get_float 默认价合并",
              s.get_float("system2", "price_in", default=0) == 2.4)


def test_thinking_params():
    """思考强度设置 → 服务商参数映射（off/low/high/auto × deepseek/qwen3/glm）"""
    print("\n[10] LLM 思考强度参数")
    from hexa.ai.client import LLMClient as _LC

    class _S:
        def __init__(self, thinking):
            self.t = thinking

        def get(self, *keys, default=None):
            return self.t if keys[-1] == "thinking" else default

        def get_float(self, *keys, default=0.0):
            return default

        def resolve_api_key(self, _slot):
            return "sk-test"

    def params(model, thinking):
        return _LC(_S(thinking), "system1")._thinking_params(model, 800)

    # 默认 off：deepseek 显式关闭（修默认思考烧穿 max_tokens → 慢 + 静默降级）
    body, mt, on = params("deepseek-v4.1-flash", "off")
    check("off→deepseek 显式关",
          body == {"enable_thinking": False} and mt == 800 and not on)
    # high：开启 + 预算档 + max_tokens 加思考配额
    body, mt, on = params("deepseek-v4.1-flash", "high")
    check("high→开+预算+配额",
          on and body.get("enable_thinking") is True
          and body.get("thinking_budget") == 16384 and mt == 800 + 16384)
    # auto：跟随服务商默认（不发参数）
    body, mt, on = params("deepseek-v4.1-flash", "auto")
    check("auto→不发参数", body is None and mt == 800 and not on)
    # qwen3 历史行为：auto 也强制关（P0.5 非流式长输入深度思考必超时）
    body, mt, on = params("qwen3.8-max", "auto")
    check("qwen3 auto→强制关",
          body == {"enable_thinking": False} and not on)
    body, mt, on = params("qwen3.8-max", "low")
    check("qwen3 low→开+预算",
          on and body.get("enable_thinking") is True
          and body.get("thinking_budget") == 1024)
    # GLM 走 thinking.type（无预算档）
    body, _mt, on = params("glm-5.3", "off")
    check("glm off→disabled",
          body == {"thinking": {"type": "disabled"}} and not on)
    body, _mt, on = params("glm-5.3", "medium")
    check("glm medium→enabled",
          body == {"thinking": {"type": "enabled"}} and on)

    # _call_once 端到端：思考开启去 temperature / max_tokens 放大 / extra_body 下发
    class _Msg:
        content = '{"ok": 1}'

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 20

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    seen = {}

    class _API:
        @staticmethod
        def create(**kw):
            seen.update(kw)
            return _Resp()

    class _SDK:
        chat = type("_Chat", (), {"completions": _API()})()

    lc = _LC(_S("high"), "system1")
    lc._sdk_client = lambda: _SDK()
    lc.chat("sys", "user")
    check("思考开启去 temperature", "temperature" not in seen)
    check("生效 max_tokens 加配额", seen.get("max_tokens") == 800 + 16384)
    check("extra_body 下发",
          (seen.get("extra_body") or {}).get("enable_thinking") is True)


def test_ai_inflight():
    """AI 在途生成计数（浮窗"⟳ AI 生成中…"标识数据源）：
    提交后 inflight≥1，完成/降级后归零。用门闩客户端确定性捕捉中间态。"""
    print("\n[11] AI 在途计数")
    import threading

    hero = "暗裔剑魔"

    class SlowClient(FakeClient):
        def __init__(self, script):
            super().__init__(script)
            self.gate = threading.Event()

        def chat(self, *a, **kw):
            self.gate.wait(timeout=5)
            return super().chat(*a, **kw)

    def wait_inflight(eng, want, timeout=5.0):
        deadline = time.time() + timeout
        while eng.stats()["inflight"] != want and time.time() < deadline:
            time.sleep(0.01)
        return eng.stats()["inflight"]

    eng, _c1, _c2 = build_ai_engine()      # "{}" 脚本 → 安全降级仍走完整流程
    eng.client_s2 = SlowClient(["{}"])
    eng.client_s1 = SlowClient(["{}"])
    got = []
    eng.bind(lambda kind, payload, serial: got.append(kind))

    eng.start_game(hero, [], ["黑暗之女"])
    check("System2 生成中 inflight=1", wait_inflight(eng, 1) == 1)
    check("ctx_source 生成中", eng.stats()["ctx_source"] == "生成中")
    eng.client_s2.gate.set()
    check("System2 完成 inflight 归零", wait_inflight(eng, 0) == 0)
    check("ctx 回调已发", "ctx" in got, str(got))

    # System1 精修（ctx 已就绪 + 预算内 → _submit 计数）
    eng.client_s1.gate.clear()
    eng.rank_cards(hero, [REAL_AUG] * 3, [], [], None, 1)
    check("System1 精修中 inflight=1", wait_inflight(eng, 1) == 1)
    eng.client_s1.gate.set()
    check("System1 完成 inflight 归零", wait_inflight(eng, 0) == 0)
    deadline = time.time() + 5
    while "augment_ranks" not in got and time.time() < deadline:
        time.sleep(0.01)
    check("评级回调已发", "augment_ranks" in got, str(got))


# ── 10. 点击链诊断 + stdout tee 留档（2026-09-29 台账复发案） ─────

def test_click_diag_and_tee():
    """台账复发案回归：海克斯识别成功但台账空，旧版 _on_click 静默丢弃
    分支（矩形未命中/无海克斯证据/reroll 无证据）无线索可查，且控制台
    一关 stdout 全丢。修复：三处丢弃全上限频诊断（面板+timeline 双落），
    LogRedirector 加 tee 文件留档。"""
    import io
    import queue as _queue
    from hexa.ui.theme import LogRedirector

    print("\n[10] 点击链丢弃诊断 + stdout tee 留档")

    tmp = Path(tempfile.mkdtemp(prefix="smoke_tee_"))
    tee_path = tmp / "app.log"
    q = _queue.Queue()
    orig = io.StringIO()
    red = LogRedirector(q, orig, tee_path=tee_path)
    red.write("hello tee\n")
    check("tee: 日志面板收到带时间戳行", q.qsize() == 1
          and "hello tee" in q.get())
    check("tee: 原流保留", orig.getvalue() == "hello tee\n")
    check("tee: 文件落盘", tee_path.exists()
          and "hello tee" in tee_path.read_text(encoding="utf-8"))
    bad = LogRedirector(q, None, tee_path=tmp / "no_dir" / "x.log")
    bad.write("still ok\n")
    check("tee: 路径打不开不崩、队列照收（留档失败不阻塞日志主链路）",
          q.qsize() == 1)

    app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    seg_click = app_src[app_src.index("def _on_click")
                        :app_src.index("def _record_click")]
    seg_reroll = app_src[app_src.index("def _on_reroll")
                         :app_src.index("ledger = MouseLedger")]
    check("_on_click rect_miss：界面在屏但未命中卡片矩形 → 诊断"
          "（标定错位直接证据，含落点+矩形）",
          "rect_miss" in seg_click and "标定错位嫌疑" in seg_click
          and 'core.state == "active"' in seg_click)
    check("_on_click no_hex_evidence：矩形命中但无海克斯证据 → 诊断",
          "no_hex_evidence" in seg_click)
    check("_on_reroll reroll_no_evidence：刷新命中但无证据 → 诊断",
          "reroll_no_evidence" in seg_reroll)
    check("诊断写 timeline（click_dropped 随复盘日志事后可查）",
          "click_dropped" in app_src)
    check("诊断按 key 限频（防对局期刷屏）",
          "_click_diag_ts.get(key" in app_src)
    check("stdout/stderr tee 落盘接线（log/app_启动时刻.log）",
          "tee_path=_tee_path" in app_src
          and '"log"' in app_src
          and "app_{time.strftime('%Y%m%d_%H%M%S')}" in app_src)


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_kb()
    test_rules()
    test_session()
    test_phase_machine()
    test_live_me_match()
    test_detect_and_layout()
    test_ai_engine()
    test_ai_build_pool()
    test_ai_inflight()
    test_session_ai_serial()
    test_swap_mode()
    test_coach_save_gap()
    test_build_progress()
    test_review_fixes()
    test_thinking_params()
    test_click_diag_and_tee()
    print(f"\n{'='*40}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✅")
