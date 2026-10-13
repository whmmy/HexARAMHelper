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
11. 点击链黑匣子：clicks 终点记录随复盘落盘（环形上限/纯点击局 flush 门）
    + 定格截图 capture_sink 落库 + 补扫 crop 透传（2026-09-29 二阶段）
12. 轮次记账闸门：成功记账登记 round_id，同轮后续点击=游玩点击不入台账
    （20261012 赏金猎人案：选卡后窗口期幻影记账）
13. 描述自举：卡面原文描述 OCR 写回知识库（持久化/覆写取新）+ 进评级
    prompt 且声明冲突时以描述为准（20261013 大法师误标案）
14. 稳定性批修：is_elevated 恒 False（wt NameError）/Live 轮询单帧异常
    不杀线程/overlay 刷新链防断/settings 原子写+损坏备份/DEFAULTS 漂移键/
    kb 损坏 JSON 不崩启动/OCR 失败可重试/钩子回调零 print/C1 描述贯通
    System2/C2 刷新数据按钮（20261013 盘点案）
15. runes 落库探针（§6 复核）：extract_players 有/无 runes 字段与
    id>100000 启发式/JSONL 落盘字段齐+局号递增/探针炸不影响 _poll_once
    快照主链/summarize 脚本对样例 exit 0

运行: uv run python tests/smoke_offline.py   （全绿 exit 0）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.ai.client import UsageStats
from hexa.ai.engine import (AIDecisionEngine, KIND_NEXT, KIND_OPENING,
                            KIND_RANKS, RuleDecisionEngine)
from hexa.core.game_state import GameSession
from hexa.core.live_api import LivePoller, LiveSnapshot, PlayerInfo
from hexa.core.lcu import PhaseMachine
from hexa.core.mouse_ledger import CardLedger, MouseLedger
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

    # ── 20261012 赏金猎人案回归：轮次记账闸门（防选卡后幻影记账）──
    # 真机实锤：选卡完成游戏内界面即关，检测器消失迟滞（5~9s）+ 10s 证据
    # 窗内，商店推荐面板/移动点击落在旧卡矩形上按残留卡名重复入账（一局
    # 台账 5 条 vs 真实 3 选）。闸门数据源：成功记账登记 round_id，app 对
    # 同轮后续卡矩形/刷新按钮点击直接丢弃（_on_click round_recorded 分支）。
    s_gate = GameSession(kb, rules)
    n_a, n_b = sorted(kb.augment_names)[2:4]
    check("轮次闸门：未记账轮放行", s_gate.round_recorded(3) is False)
    check("轮次闸门：带轮次首记",
          s_gate.record_augment(n_a, card_idx=0, round_id=3) is True)
    check("轮次闸门：记账登记本轮", s_gate.round_recorded(3) is True)
    check("轮次闸门：邻轮不受影响", s_gate.round_recorded(4) is False)
    check("轮次闸门：跨轮新名可记",
          s_gate.record_augment(n_b, card_idx=1, round_id=4) is True
          and s_gate.round_recorded(4) is True)
    s_gate.reset()
    check("轮次闸门：会话重置清登记",
          not s_gate.round_recorded(3) and not s_gate.round_recorded(4))

    # ── 20261013 描述自举：卡面原文描述知识库（写回/读取/持久化）──
    # 真机实锤：标签库离线从名字猜（大法师被标 ap/法穿/爆发，原文是技能
    # 花费返还）——原文才是事实源，OCR 写回随对局自增长，prompt 冲突时
    # 以描述为准。
    kb_desc = KnowledgeBase(data_dir=_SMOKE_BASE / "kb_desc_d",
                            know_dir=_SMOKE_BASE / "kb_desc_k")
    check("描述库初始为空", kb_desc.augment_desc("大法师") == "")
    check("描述写回可读",
          kb_desc.record_augment_desc("大法师", "施放一个技能会返还另一个随机技能的花费，数额相当于30%原技能冷却时间")
          and kb_desc.augment_desc("大法师").startswith("施放一个技能"))
    kb_desc.record_augment_desc("大法师", "改版后的新描述文本，长度足够入库直出")
    check("描述覆写取新", kb_desc.augment_desc("大法师") == "改版后的新描述文本，长度足够入库直出")
    check("描述同名同文跳过写盘", kb_desc.record_augment_desc("大法师", "改版后的新描述文本，长度足够入库直出") is False)
    kb_desc2 = KnowledgeBase(data_dir=_SMOKE_BASE / "kb_desc_d",
                             know_dir=_SMOKE_BASE / "kb_desc_k")
    check("描述落盘持久化", kb_desc2.augment_desc("大法师") == "改版后的新描述文本，长度足够入库直出")
    check("描述未收录返回空", kb_desc2.augment_desc("库外海克斯") == "")

    # 海克斯识别状态标识（overlay 状态流转数据源）
    session.on_hex_ui(True)
    st = session.ui_state()
    check("hex 界面在屏未识别", st["hex_active"] and not st["hex_recognized"]
          and st["hex_last"] == "" and st["hex_picked"] is None)
    session.rank_augment_cards([REAL_AUG] * 3)
    st = session.ui_state()
    check("hex 识别成功标记", st["hex_active"] and st["hex_recognized"]
          and len(st["card_ranks"]) == 3, str(st["hex_recognized"]))
    check("规则模式评级无等待态（基准即最终展示）",
          st["card_ranks_pending"] is False, str(st["card_ranks_pending"]))
    # AI 评级等待态契约（2026-09-30 先显卡名后放评级）：None payload =
    # 精修失败/空结果信号 → 清等待态，规则基准保留为最终展示
    session.card_ranks_pending = True
    session._on_engine_result(KIND_RANKS, None, session._card_serial)
    st = session.ui_state()
    check("评级失败信号清等待态（基准保留）",
          not st["card_ranks_pending"] and len(st["card_ranks"]) == 3,
          f"pending={st['card_ranks_pending']}")
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
    check("ui_state 键齐全", {"stage", "my_hero", "my_level", "ally", "enemy",
                              "augments", "current_path", "threat", "suggestion",
                              "dd_version", "path_drift",
                              "build_progress"} <= set(st.keys()))
    fresh = session.data_freshness()
    check("新鲜度字段", "date" in fresh and fresh["dd_version"].startswith("16."))


# ── 3.5 引擎开局兜底② ────────────────────────────────────────────

def test_engine_late_start_net():
    """2026-09-29 曙光女神案：选人期已锁定英雄的局错过 GameStart 转换后，
    中途启动兜底（hero 未知分支）因 my_hero 早有值而永不触发，引擎整局
    空转（复盘 ctx_source="-"、calls=0、费用 ¥0.0000）。快照兜底②按
    "本局引擎未启动"判据补 start_game，英雄已知也救，且幂等。"""
    print("\n[3.5] 引擎开局快照兜底（选人直进对局案）")
    session = GameSession(kb, rules)

    calls = []

    class RecordingEngine:
        """记录 start_game，其余全部委托规则引擎"""

        def __init__(self, inner):
            self._inner = inner

        def start_game(self, hero, ally, enemy, game_map=""):
            calls.append((hero, tuple(ally), tuple(enemy), game_map))
            self._inner.start_game(hero, ally, enemy, game_map=game_map)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    session.engine = RecordingEngine(session.engine)

    # 选人拿到英雄 → GameStart 被跳过 → 直接对局
    session.on_champ_select({"ally": ["暗裔剑魔"], "enemy": [],
                             "my_hero": "暗裔剑魔", "source": "test"})
    session.on_in_progress()
    check("跳过开局时引擎未启动", not calls, str(calls))

    # 首个快照：英雄已知也必须补 start_game
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=[]))
    check("快照兜底补开局", len(calls) == 1 and calls[0][0] == "暗裔剑魔",
          str(calls))

    # 后续快照幂等，不重复开局（System2/预算计数不被反复清零）
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"]))
    check("兜底幂等不重复开局", len(calls) == 1, str(calls))

    session.on_end_of_game("EndOfGame")
    check("局终复位开局标记", not session._engine_started, session._engine_started)

    # 新局同路径再来一次（防"兜底只对第一局生效"）
    session.on_champ_select({"ally": ["黑暗之女"], "enemy": [],
                             "my_hero": "黑暗之女", "source": "test"})
    session.on_in_progress()
    session.on_snapshot(make_snap(my_hero_en="Annie", my_items=[]))
    check("新局可再开局", len(calls) == 2 and calls[1][0] == "黑暗之女",
          str(calls))




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

    # 回归（2026-09-29 曙光女神/战争之影案）：GameStart 阶段一闪而过被 2s
    # 轮询跳过——ChampSelect 直进 InProgress 时 on_game_start 永不触发，
    # System2 整局空转（复盘 ctx_source="-"、calls=0、¥0.0000）
    events.clear()
    pm4 = PhaseMachine(FakeLCU(), callbacks={
        "on_champ_select": lambda t: events.append(("champ_select",)),
        "on_game_start": lambda t: events.append(("game_start",)),
        "on_in_progress": lambda: events.append(("in_progress",)),
    })
    pm4._handle("ChampSelect")
    pm4._handle("InProgress")     # GameStart 被跳过
    kinds4 = [e[0] for e in events]
    check("选人直进对局补发开局",
          kinds4 == ["champ_select", "game_start", "in_progress"], str(events))

    # 启动即已在对局（首见阶段就是 InProgress）同样补发开局
    events.clear()
    pm5 = PhaseMachine(FakeLCU(), callbacks={
        "on_game_start": lambda t: events.append(("game_start",)),
        "on_in_progress": lambda: events.append(("in_progress",)),
    })
    pm5._handle("InProgress")
    check("启动即在对局补发开局",
          [e[0] for e in events] == ["game_start", "in_progress"], str(events))

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


def test_runes_probe():
    print("\n[4.6] runes 落库探针（§6 复核：runes 字段存不存在、长什么样）")
    import json as _json
    import os as _os
    import subprocess as _sp

    from hexa.core.runes_probe import RunesProbe, extract_players

    # extract_players：有/无 runes 字段、id>100000 启发式、字段缺失容错
    rows = extract_players([
        {"championName": "曙光女神", "team": "ORDER",
         "runes": {"generalRunes": [
             {"id": 8100}, {"id": 1000100}, {"id": 1000200}]}},
        {"rawChampionName": "game_character_displayname_Annie",
         "team": "CHAOS"},                                    # 无 runes 键
        {"championName": None, "team": "CHAOS",
         "runes": {"generalRunes": []}},                      # 空条目表
        "not-a-dict",                                         # 非 dict 跳过
        None,
    ])
    check("extract 有 runes 字段解析 augs（id>100000 过滤）",
          rows[0]["runes_field"] == "present" and rows[0]["total"] == 3
          and rows[0]["augs"] == [1000100, 1000200]
          and rows[0]["champion"] == "曙光女神" and rows[0]["team"] == "ORDER",
          str(rows[0]))
    check("extract 无 runes 字段 absent + raw 尾段兜底",
          rows[1]["runes_field"] == "absent" and rows[1]["total"] == 0
          and rows[1]["augs"] == [] and rows[1]["champion"] == "Annie",
          str(rows[1]))
    check("extract 空 generalRunes= present/0 条/无英雄名",
          rows[2]["runes_field"] == "present" and rows[2]["total"] == 0
          and rows[2]["champion"] == "", str(rows[2]))
    check("extract 非 dict 元素跳过", len(rows) == 3, str(len(rows)))

    # RunesProbe 落盘：JSONL 行可解析且字段齐；new_game 局号 +1；
    # on_allgamedata 整包（allPlayers 键）/裸列表均可
    probe_dir = Path(tempfile.mkdtemp(prefix="hexa_runes_probe_"))
    probe = RunesProbe(log_dir=probe_dir)
    probe.on_playerlist(
        [{"championName": "曙光女神", "team": "ORDER"}], 111.0)
    probe.new_game()
    probe.on_allgamedata(
        {"allPlayers": [{"championName": "Annie", "team": "CHAOS",
                         "runes": {"generalRunes": [{"id": 1000100}]}}]},
        222.0)
    lines = (probe_dir / "runes_probe.jsonl").read_text(
        encoding="utf-8").splitlines()
    ok = len(lines) == 2
    r1 = _json.loads(lines[0]) if ok else {}
    r2 = _json.loads(lines[1]) if ok and len(lines) > 1 else {}
    check("JSONL 追加两行且 utf-8 可解析", ok, str(lines))
    check("行字段齐（ts/session/src/players）且局号 +1",
          r1.get("ts") == 111.0 and r1.get("session") == 0
          and r1.get("src") == "playerlist"
          and isinstance(r1.get("players"), list) and r1["players"]
          and r1["players"][0].get("runes_field") == "absent"
          and r2.get("session") == 1 and r2.get("src") == "allgamedata"
          and r2["players"][0].get("augs") == [1000100],
          f"{r1} | {r2}")
    probe.on_allgamedata(
        [{"championName": "X", "team": "ORDER"}], 333.0)   # 裸列表形态
    r3 = _json.loads((probe_dir / "runes_probe.jsonl").read_text(
        encoding="utf-8").splitlines()[2])
    check("on_allgamedata 兼容裸玩家列表",
          r3.get("src") == "allgamedata"
          and r3["players"][0]["champion"] == "X", str(r3))

    # 功能性断言：探针抛异常时 _poll_once 仍正常返回快照（两路喂点都被
    # 炸过且都被吞掉——证明隔离靠 try/except，不是探针没被调）
    class _BoomProbe:
        def __init__(self):
            self.calls = []

        def on_playerlist(self, players, ts):
            self.calls.append("playerlist")
            raise RuntimeError("boom")

        def on_allgamedata(self, players, ts):
            self.calls.append("allgamedata")
            raise RuntimeError("boom")

    boom = _BoomProbe()
    poller = LivePoller(kb, runes_probe=boom)
    fake = {
        "/activeplayer": {"championName": "Annie", "currentGold": 100.0,
                          "level": 6},
        "/playerlist": [
            {"championName": "曙光女神", "team": "ORDER"},
            {"championName": "Annie", "team": "CHAOS"}],
        "/eventdata": {"events": []},
        "/liveclientdata/allgamedata": {"allPlayers": [
            {"championName": "Annie", "team": "CHAOS"}]},
    }
    poller._get_json = lambda endpoint: fake.get(endpoint)  # monkeypatch 断网
    poller._agd_tick = 5        # 本帧即触发低频 allgamedata 路径（第 6 周期）
    snap = poller._poll_once()
    check("探针两路喂点均被调用且异常被吞", boom.calls == ["playerlist",
          "allgamedata"], str(boom.calls))
    check("探针炸不影响快照主链", snap is not None and snap.ok
          and len(snap.players) == 2 and snap.my_champion == "黑暗之女",
          f"snap={snap and (snap.ok, len(snap.players), snap.my_champion)}")
    check("allgamedata 低频计数归零重计", poller._agd_tick == 0,
          str(poller._agd_tick))

    # summarize 脚本对样例 JSONL exit 0（纯 stdlib，无网络）
    script = (Path(__file__).resolve().parent.parent / "experiments"
              / "summarize_runes_probe.py")
    proc = _sp.run(
        [sys.executable, str(script), str(probe_dir / "runes_probe.jsonl")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**_os.environ, "PYTHONIOENCODING": "utf-8"})
    check("summarize 对样例 JSONL exit 0",
          proc.returncode == 0 and "playerlist" in (proc.stdout or "")
          and "allgamedata" in (proc.stdout or ""),
          f"rc={proc.returncode} out={(proc.stdout or '')[:200]} "
          f"err={(proc.stderr or '')[:200]}")


# ── 4.7 Live 免费事实入上下文与 UI（等级/召唤师技能/队友出装/台账品质） ──
# 案由（20261013）：Live API 已返回但没进 ui_state/模型上下文的四件免费事实。
# 全部数据直出、不做代码侧预消化结论（判断归模型，AGENTS.md §1/§4）；
# 解析一律防御式——真机无 fixtures 样本，字段缺失/脏值不许炸快照线程。

def test_live_facts_context():
    print("\n[4.7] Live 免费事实：等级/敌方召唤师技能/队友出装/台账品质")
    import json as _json

    # ---- ① 等级 + ② 召唤师技能：poller 解析层（monkeypatch 断网） ----
    fake = {
        "/activeplayer": {"championName": "Annie", "currentGold": 100.0,
                          "level": 7},
        "/playerlist": [
            {"championName": "曙光女神", "team": "ORDER", "level": 9,
             "summonerSpells": {"summonerSpell1": {"displayName": "闪现"},
                                "summonerSpell2": {"displayName": "点燃"}},
             "items": [{"displayName": "圣物之盾", "count": 1}]},
            {"championName": "Annie", "team": "CHAOS", "level": 7,
             "summonerSpells": {"summonerSpell1": {"displayName": "闪现"},
                                "summonerSpell2": {"displayName": "屏障"}}},
            # 防御式对照：level 脏值 / displayName=null / 两个键都缺
            {"championName": "盲僧", "team": "CHAOS", "level": "abc",
             "summonerSpells": {"summonerSpell1": {"displayName": None}}},
            {"championName": "MonkeyKing", "team": "ORDER"},
        ],
        "/eventdata": {"events": []},
    }
    poller = LivePoller(kb)
    poller._get_json = lambda endpoint: fake.get(endpoint)
    snap = poller._poll_once()
    by_champ = {p.champion or p.champion_en: p for p in snap.players}
    mk = snap.players[3]   # MonkeyKing（英文经 KB 转 中文名，按位置取）
    check("等级解析：playerlist level 直出",
          by_champ["曙光女神"].level == 9 and by_champ["黑暗之女"].level == 7,
          str({k: p.level for k, p in by_champ.items()}))
    check("等级解析：脏值/缺字段归 0 不炸",
          by_champ["盲僧"].level == 0 and mk.level == 0,
          str([p.level for p in snap.players]))
    check("召唤师技能 displayName 直出（两技能保序）",
          by_champ["曙光女神"].spells == ["闪现", "点燃"]
          and by_champ["黑暗之女"].spells == ["闪现", "屏障"],
          str({k: p.spells for k, p in by_champ.items()}))
    check("召唤师技能缺失/None 安全跳过",
          by_champ["盲僧"].spells == [] and mk.spells == [],
          str([p.spells for p in snap.players]))
    check("我的等级：activeplayer 优先",
          snap.my_level == 7, str(snap.my_level))

    # 我的等级兜底：activeplayer 缺 level（国服字段缺失同 championName）
    fake2 = dict(fake)
    fake2["/activeplayer"] = {"championName": "Annie", "currentGold": 100.0}
    poller2 = LivePoller(kb)
    poller2._get_json = lambda endpoint: fake2.get(endpoint)
    snap2 = poller2._poll_once()
    check("我的等级：activeplayer 缺失回退 playerlist 匹配条",
          snap2.my_level == 7, str(snap2.my_level))

    # ---- 快照进会话：ui_state.my_level + 引擎挂载（enemy_spells/ally_builds） ----
    session = GameSession(kb, rules)
    session.on_game_start({"ally": ["黑暗之女", "牛头酋长"],
                           "enemy": ["盲僧"],
                           "my_hero": "黑暗之女", "source": "test"})
    session.on_in_progress()
    snap3 = LiveSnapshot(ts=time.time(), ok=True)
    snap3.my_champion_en = "Annie"
    snap3.my_champion = "黑暗之女"
    snap3.my_level = 8
    snap3.players = [
        PlayerInfo(champion="黑暗之女", champion_en="Annie", team="ORDER",
                   level=8, items=[{"name": "多兰之刃", "count": 1}]),
        PlayerInfo(champion="牛头酋长", team="ORDER", level=6),   # 无装备
        PlayerInfo(champion="盲僧", team="CHAOS", level=9,
                   spells=["闪现", "点燃"],
                   items=[{"name": "长剑", "count": 1}]),
        PlayerInfo(champion="放逐之刃", team="CHAOS", level=4),   # 无技能
    ]
    session.on_snapshot(snap3)
    st = session.ui_state()
    check("ui_state 增 my_level（快照直出）", st["my_level"] == 8,
          str(st["my_level"]))
    check("敌方召唤师技能进引擎（英雄名映射，整表替换）",
          getattr(session.engine, "enemy_spells", {}).get("盲僧")
          == ["闪现", "点燃"]
          and "放逐之刃" not in session.engine.enemy_spells,
          str(getattr(session.engine, "enemy_spells", {})))
    ally_rows = list(getattr(session.engine, "ally_builds", []))
    check("队友出装摘要逐人行（含我，装备顿号连接）",
          "黑暗之女: 多兰之刃" in ally_rows, str(ally_rows))
    check("队友出装摘要：无装备标「无」不空串",
          "牛头酋长: 无" in ally_rows, str(ally_rows))
    # 会话重置清等级（UI 侧无数据即省略 Lv.，不得残留上一局数值）
    session.reset()
    check("会话重置清 my_level", session.ui_state()["my_level"] == 0,
          str(session.ui_state()["my_level"]))

    # ---- ②③ 进 System1 prompt（FakeClient，不联网） ----
    hero = "暗裔剑魔"
    top = kb.top_augments(hero, 8)
    pool = kb.core_item_pool(hero) or ["长剑"]
    ctx_json = {
        "augment_affinity": [{"name": n, "bias": "ad", "note": "t"} for n in top],
        "opening_items": [{"combo": pool[:1], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "builds": [{"name": "爆杀流", "path": "ad", "items": pool[:3],
                    "note": "主线"}],
        "skill_order": "主Q副E",
        "augment_item_links": []}
    eng, c1, _c2 = build_ai_engine(script_s2=[_json.dumps(ctx_json)])
    results = []
    eng.bind(lambda kind, payload, serial: results.append((kind, payload, serial)))
    eng.start_game(hero, [], ["黑暗之女"])
    deadline = time.time() + 6
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    check("测试前置：ctx 就绪", eng.cache.get() is not None)
    # 引擎挂载直填（GameSession.on_snapshot 写入点的等效形态）
    eng.enemy_spells = {"黑暗之女": ["闪现", "屏障"]}
    eng.ally_builds = ["牛头酋长: 圣物之盾", "黑暗之女: 多兰之刃"]
    det = eng._enemy_detail()
    check("engine 敌方明细带召唤师技能（displayName 原文直出）",
          det == ["黑暗之女(ap,Mage/Support) 召唤师技能:闪现+屏障"], str(det))
    check("engine 队友摘要透传截断（≤5 行）",
          eng._ally_detail() == ["牛头酋长: 圣物之盾", "黑暗之女: 多兰之刃"],
          str(eng._ally_detail()))
    # 新局开局清空跨局残留（早退路径 hero 空 也必须清）
    eng_stale, _, _ = build_ai_engine()
    eng_stale.enemy_spells = {"旧敌": ["闪现"]}
    eng_stale.ally_builds = ["旧友: 旧装"]
    eng_stale.start_game("", [], [])
    check("新局开局清空 Live 事实挂载（防跨局残留）",
          eng_stale.enemy_spells == {} and eng_stale.ally_builds == [],
          f"{eng_stale.enemy_spells} {eng_stale.ally_builds}")

    # 下一件精修 prompt：队友摘要段 + 敌方召唤师技能行
    from hexa.ai.engine import KIND_NEXT as _KN
    threat = ThreatProfile(ap_count=1)
    c1.script.append(_json.dumps({"picks": [
        {"name": pool[0], "reason": "r1"},
        {"name": pool[1] if len(pool) > 1 else "焚天", "reason": "r2"}]}))
    eng.next_candidates(["多兰之刃"], 0, "ad", threat, serial=31,
                        reason="event")
    deadline = time.time() + 6
    while not any(k == _KN for k, _p, _s in results) and time.time() < deadline:
        time.sleep(0.02)
    check("下一件 prompt 带《队友装备摘要》段",
          "《队友装备摘要（快照直出）》" in c1.calls[-1]
          and "牛头酋长: 圣物之盾" in c1.calls[-1], c1.calls[-1][:150])
    check("下一件 prompt 敌方行带召唤师技能",
          "召唤师技能:闪现+屏障" in c1.calls[-1], c1.calls[-1][:150])

    # 海克斯时刻评级 prompt：同吃队友摘要（参考 enemy 参数模式）
    from hexa.ai.engine import KIND_RANKS as _KR
    c1.script.append(_json.dumps({"grades": [
        {"name": top[0], "grade": "S", "reason": "r"}]}))
    eng.rank_cards(hero, top[:3], [], [], threat, serial=32)
    deadline = time.time() + 6
    while not any(k == _KR for k, _p, _s in results) and time.time() < deadline:
        time.sleep(0.02)
    check("评级 prompt 带队友出装摘要",
          "牛头酋长: 圣物之盾" in c1.calls[-1], c1.calls[-1][:150])

    # ---- ④ 台账品质：ui_state 带 tier + ledger_line 渲染单源 ----
    s_tier = GameSession(kb, rules)
    other = sorted(kb.augment_names)[1]
    s_tier.record_augment(REAL_AUG, tier="棱彩")
    s_tier.record_augment(other)                       # 未带 tier（OCR 缺品质）
    entries = s_tier.ui_state()["augments"]
    check("台账条目带品质（name+tier 直出）",
          entries == [{"name": REAL_AUG, "tier": "棱彩"},
                      {"name": other, "tier": ""}], str(entries))

    from hexa.ui import theme as _theme
    check("台账行渲染：轮序+品质『① 贪欲束缚（棱彩）』",
          _theme.ledger_line(0, "贪欲束缚", "棱彩") == "① 贪欲束缚（棱彩）",
          _theme.ledger_line(0, "贪欲束缚", "棱彩"))
    check("台账行渲染：tier 缺省省略括号",
          _theme.ledger_line(0, "贪欲束缚", "") == "① 贪欲束缚"
          and _theme.ledger_line(2, "秘术射击", None) == "③ 秘术射击",
          f"{_theme.ledger_line(0, '贪欲束缚', '')!r}")
    check("台账行渲染：脏 tier（纯空白）省略括号",
          _theme.ledger_line(1, "x", "  ") == "② x",
          _theme.ledger_line(1, "x", "  "))
    check("台账行渲染：超带圈数字表回退 N. 不炸",
          _theme.ledger_line(10, "x", "y") == "11. x（y）"
          and _theme.ledger_line(-1, "x", "") == "0. x",
          _theme.ledger_line(10, "x", "y"))
    check("带圈轮序过 emoji 清洗（渲染边界不误伤）",
          _theme.strip_emoji("① 贪欲束缚（棱彩）") == "① 贪欲束缚（棱彩）")

    # UI 消费点接线守卫（防 tier 进 ui_state 后漏接某个面板）
    ui_dir = Path(__file__).resolve().parent.parent / "hexa" / "ui"
    ov_src = (ui_dir / "overlay.py").read_text(encoding="utf-8")
    mw_src = (ui_dir / "main_window.py").read_text(encoding="utf-8")
    check("台账渲染单源接线（overlay/main_window 均走 theme.ledger_line）",
          "ledger_line(" in ov_src and "ledger_line(" in mw_src)
    check("overlay 英雄行接 my_level（Lv. 直出）",
          "my_level" in ov_src and "Lv." in ov_src)


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

    # 点击证据黑匣子（2026-09-29 台账复发案）：定格帧经 capture_sink
    # 落盘（IO 策略在 app 侧，核心层只回调）；补扫 crop 透传复用同一
    # 张——截图与识别消费同一像素；sink 抛异常不影响返回 crop
    _sunk = []

    def _sink(idx, crop, tag):
        _sunk.append((idx, tag))
        return f"{tag}_stub_card{idx + 1}.png"

    det_a.capture_sink = _sink
    crop_e, img_e = det_a.capture_click_evidence(1, tag="click")
    check("capture_click_evidence：sink 回调并返回文件名",
          crop_e is not None and img_e == "click_stub_card2.png"
          and _sunk == [(1, "click")], str(_sunk))
    det_a.capture_sink = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
    crop_x, img_x = det_a.capture_click_evidence(0)
    check("sink 抛异常不崩：返回 (crop, None)", crop_x is not None
          and img_x is None)
    det_a.capture_sink = None
    crop_n, img_n = det_a.capture_click_evidence(0)
    check("sink 未接（None）：返回 (crop, None)", crop_n is not None
          and img_n is None)

    _grab_seen = []
    det_a._grab_card_crop = lambda i: _grab_seen.append(i) or None
    _SyncOcr.out = _raw_hit      # 前面用例切到了 miss mock，恢复命中口径
    thru = det_a.sync_ocr_card_raw(2, crop=crop_e)
    check("补扫 crop 透传：外部定格帧直接分析、不再内部抓帧",
          _grab_seen == [] and thru == (_cards3[0], "「有始有终」"),
          f"grabs={_grab_seen} thru={thru}")
    del det_a._grab_card_crop

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

    def __init__(self, budget=0, min_interval=0.0):   # 默认随产品：0 = 不限
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


def build_ai_engine(script_s2=(), script_s1=(), budget=0, min_interval=0.0):
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
    ret = eng.rank_cards(hero, top[:3], [], [], threat, serial=4)
    check("评级 AI 在途标记（先显卡名后放评级）",
          isinstance(ret, tuple) and len(ret[0]) == 3 and ret[1] is True,
          str(ret[1] if isinstance(ret, tuple) else ret))
    eng_nosub, _c1x, _c2x = build_ai_engine()   # 未 start_game：ctx 未就绪
    check("ctx 未就绪不进评级等待态（基准直显）",
          eng_nosub.rank_cards(hero, top[:3], [], [], None, 1)[1] is False)
    check("规则引擎评级无等待态（基准直显）",
          RuleDecisionEngine(kb, rules)
          .rank_cards(hero, top[:3], [], [], None, 1)[1] is False)
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

    # ---- 描述自举：卡面原文进评级 prompt（20261013 大法师案）----
    # 真机实锤：标签库把 大法师 标成 ap/法穿/爆发，原文是技能花费返还——
    # 描述（事实直出）必须进 prompt 且声明冲突时以描述为准
    c1.script.append(_json.dumps({"grades": [
        {"name": top[0], "grade": "A", "reason": "描述实锤跟随"}]}))
    ret = eng.rank_cards(hero, top[:3], [], [], threat, serial=5,
                         desc_fetcher=lambda: {
                             top[0]: "施放一个技能会返还另一个随机技能的花费，数额相当于30%原技能冷却时间。"})
    check("评级描述自举在途标记", isinstance(ret, tuple) and ret[1] is True,
          str(ret[1] if isinstance(ret, tuple) else ret))
    deadline = time.time() + 5
    while time.time() < deadline and not any(
            k == "augment_ranks" and p and p[0]["grade"] == "A"
            for k, p, _s in results):
        time.sleep(0.02)
    check("评级 prompt 带卡面原文描述", "返还另一个随机技能" in c1.calls[-1],
          c1.calls[-1][:120])
    check("评级 prompt 声明描述优先", "以描述为准" in c1.calls[-1])
    check("规则引擎接受描述参数不派发",
          RuleDecisionEngine(kb, rules)
          .rank_cards(hero, top[:3], [], [], None, 1,
                      desc_fetcher=lambda: None)[1] is False)

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

    # ---- 预算 0 = 不限次（2026-09-30 默认）：连续占用远超旧上限仍放行 ----
    eng_u, _c1u, _c2u = build_ai_engine(budget=0)
    ok_unl = all(eng_u._try_reserve() for _ in range(30)) \
        and eng_u._calls == 30
    check("预算 0 = 不限次", ok_unl, f"calls={eng_u._calls}")

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
    session = GameSession(
        kb, rules,
        # 复盘 JSON mouse_stats 字段（20261002 寒冰案：鼠标双通道统计窗
        # 随复盘落盘，台账空的局级铁证不必再翻 app_*.log）
        mouse_stats_fn=lambda: [{"ts": 0.0, "hook": 0, "poll": 7,
                                 "dup": 0, "game": True, "warn": False}])
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
    # 草稿周期落盘后，局终写对本局文件是"覆盖"而非"新增"，且局终
    # reset 会清 _review_path——断言语义改为"最新写出的 json 即本局
    # 终态"（测试单线程，局终写必是最新），再以 hero 字段互证。
    import json as _json2
    from hexa.config import BASE_DIR
    games_dir_6b = BASE_DIR / "log" / "games"
    session.on_end_of_game("EndOfGame")
    newest = sorted(games_dir_6b.glob("*.json"),
                    key=lambda p: p.stat().st_mtime)
    review_path = newest[-1] if newest else None
    check("复盘日志落盘", review_path is not None
          and review_path.exists())
    if review_path and review_path.exists():
        doc = _json2.loads(review_path.read_text(encoding="utf-8"))
        check("复盘日志字段", doc.get("hero") == "暗裔剑魔"
              and doc.get("suggestions"), str(doc.get("hero")))
        check("复盘日志含鼠标双通道统计（mouse_stats）",
              doc.get("mouse_stats")
              and doc["mouse_stats"][0]["poll"] == 7
              and doc["mouse_stats"][0]["game"] is True,
              str(doc.get("mouse_stats"))[:100])


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
        ok = (st_stats["mode"] == "ai" and st_stats["budget"] == 0
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

    # -- P3：复盘草稿周期落盘（进程死亡护身符；20260929 人马案：日女局
    #    正常落盘后同进程再打人马局，App 进程无声消失，复盘零记录——
    #    数据全程在内存、局终才落盘，强杀/关机时两条兜底都来不及跑） --
    games_dir = _game_state.BASE_DIR / "log" / "games"
    n_logs = len(list(games_dir.glob("*.json")))
    draft_hero = "黑暗之女"   # 独立英雄名，避免与本节前文暗裔剑魔局终文件撞 glob
    sess = GameSession(kb, rules, settings=FakeSettings())
    sess.on_game_start({"ally": [draft_hero], "enemy": [hero],
                        "my_hero": draft_hero, "source": "test"})
    sess.on_in_progress()
    # 空转局：无建议/无海克斯/无点击 → 草稿不产文件
    sess._maybe_flush_review_draft()
    check("草稿：零内容局不产文件",
          len(list(games_dir.glob("*.json"))) == n_logs)
    # 有内容 + hero 未定：不写（避免文件名被钉死为 unknown）
    sess.my_hero = None
    sess.history.append((time.time(), "next_items", "测试建议"))
    sess._maybe_flush_review_draft()
    check("草稿：hero 未定不落盘",
          len(list(games_dir.glob("*.json"))) == n_logs)
    # hero 到位 → 草稿落盘（不置幂等位，限频生效）
    sess.my_hero = draft_hero
    sess._maybe_flush_review_draft()
    draft_path = games_dir / sess._review_path
    check("草稿：有内容即落盘", draft_path.exists(),
          f"total={len(list(games_dir.glob('*.json')))} n_logs={n_logs}")
    check("草稿：不置幂等位", sess._review_flushed is False)
    doc = _json.loads(draft_path.read_text(encoding="utf-8"))
    check("草稿：内容含建议历史", any("测试建议" in s["text"]
                                      for s in doc["suggestions"]))
    last_draft_ts = sess._draft_ts
    sess._maybe_flush_review_draft()
    check("草稿：周期限频", sess._draft_ts == last_draft_ts)
    # 幂等位消费路径：终局事件丢失时 flush 补写置位，二次调用空转
    sess.flush_pending_review()
    check("终态：flush 补写置幂等位", sess._review_flushed is True)
    sess.flush_pending_review()
    check("终态：flush 幂等不重复落盘",
          len(list(games_dir.glob("*.json"))) == n_logs + 1)
    # 局终 final 覆盖同一文件（草稿/终态共用一份，不产第二份）
    sess.on_end_of_game("PreEndOfGame")
    check("终态：覆盖草稿同一文件",
          len(list(games_dir.glob("*.json"))) == n_logs + 1)
    doc2 = _json.loads(draft_path.read_text(encoding="utf-8"))
    check("终态：文件仍可读且含复盘字段",
          doc2["hero"] == draft_hero and doc2.get("clicks") == [])

    # -- P3b：终局监听兜底（数据流停摆；20260929 人马案：局终退回桌面
    #    本应 2s 内落盘，却因 phase 查询失败 prev 永卡 InProgress，
    #    on_end_of_game 永不触发直到关机）--
    from hexa.core.lcu import PhaseMachine as _PM
    n_logs2 = len(list(games_dir.glob("*.json")))
    sess2 = GameSession(kb, rules, settings=FakeSettings())
    pm = _PM(None, callbacks={"on_end_of_game": sess2.on_end_of_game,
                              "check_stall": sess2.report_data_stall})
    check("stall：idle 不判定", not sess2.report_data_stall())
    sess2.on_game_start({"ally": ["殇之木乃伊"], "enemy": [hero],
                         "my_hero": "殇之木乃伊", "source": "test"})
    sess2.on_in_progress()
    check("stall：数据新鲜不判定", not sess2.report_data_stall())
    sess2.on_phase("GameStart")   # phase 轨迹留痕（本局复盘可查）
    check("stall：phase 回调进 timeline",
          any(e[1] == "phase" and e[2] == "GameStart"
              for e in sess2.timeline))
    sess2.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                                my_gold=1000))
    check("stall：快照刷新不判定", not sess2.report_data_stall())
    sess2._last_data_ts -= 301.0   # 快照断流 5 分钟（游戏退了/接口死了/客户端不在）
    check("stall：断流超时判定", sess2.report_data_stall() is True)
    pm._check_stall()   # → fire on_end_of_game("data_stall") 落盘+收尾
    latest = sorted(games_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    check("stall：兜底触发落盘",
          len(list(games_dir.glob("*.json"))) == n_logs2 + 1)
    stall_doc = _json.loads(latest[-1].read_text(encoding="utf-8"))
    check("stall：落盘记录 data_stall",
          any(e["event"] == "end_of_game" and "data_stall" in e["detail"]
              for e in stall_doc["timeline"]),
          str([e["event"] for e in stall_doc["timeline"][-3:]]))
    check("stall：触发后 session 归位", sess2.stage == "idle")
    check("stall：收尾后不再判定", not sess2.report_data_stall())
    pm._check_stall()   # idle → 不再 fire
    sess2.on_end_of_game("PreEndOfGame")   # 模拟 phase 恢复后的重复终局事件
    check("stall：幂等门不产第二份文件",
          len(list(games_dir.glob("*.json"))) == n_logs2 + 1)


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
    # qwen3 2026-09-30 牛头案：low/high 档也一律强制关——服务商对思考
    # 请求的渐进输出绕过 read timeout，thinking=high 挂死 System2 12 分钟，
    # 期间 ctx 永远 pending、System1 全瘫、浮窗永久"生成中"
    body, mt, on = params("qwen3.8-max", "low")
    check("qwen3 low→强制关（档位不生效，防挂死）",
          body == {"enable_thinking": False} and mt == 800 and not on)
    body, mt, on = params("qwen3.8-max", "high")
    check("qwen3 high→强制关（防挂死）",
          body == {"enable_thinking": False} and mt == 800 and not on)
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


def test_system2_watchdog():
    """System2 挂死看门狗（2026-09-30 牛头案）：qwen3 思考请求的渐进输出
    绕过 read timeout，挂 12 分钟才失败——期间 ctx 永远 pending，System1
    全瘫、浮窗永久"生成中"。pending 超 180s 由 _ctx_ready 失败落位：
    stats 显示"生成失败"；真 ctx 迟到仍会 cache.set 正常生效。"""
    print("\n[11b] System2 挂死看门狗")
    import threading

    class GateClient(FakeClient):
        def __init__(self, script):
            super().__init__(script)
            self.gate = threading.Event()

        def chat(self, *a, **kw):
            self.gate.wait(timeout=5)
            return super().chat(*a, **kw)

    eng, _c1, _c2 = build_ai_engine()
    eng.client_s2 = GateClient(["{}"])
    eng.client_s1 = GateClient(["{}"])
    eng.start_game("暗裔剑魔", [], ["黑暗之女"])
    check("生成中 pending 置位", eng.cache.pending
          and eng.stats()["ctx_source"] == "生成中")
    # 模拟挂死：pending 起点拨回 181s 前，触发 _ctx_ready 的看门狗
    eng._pending_since = time.time() - 181
    check("pending 未就绪（180s 内不放行）", eng._ctx_ready() is False)
    check("看门狗已落位（不再 pending）", not eng.cache.pending)
    check("stats 显示生成失败", eng.stats()["ctx_source"] == "生成失败")
    # 真 ctx 迟到仍生效：放行 generate → 规则版 ctx set → source 覆盖
    eng.client_s2.gate.set()
    deadline = time.time() + 5
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.01)
    check("迟到 ctx 正常落缓存", eng.cache.get() is not None)
    check("ctx_source 转为实际来源", eng.stats()["ctx_source"] == "rule")


def test_grab_card_crop_no_deadlock():
    """点击抓帧重入死锁回归（2026-09-30 真机审查发现）：旧版
    _grab_card_crop 外层 with _grab_lock 后调 _grab()，_grab() 内层再
    with 同一把锁——threading.Lock 不可重入，同线程二次获取=永久死锁。
    点卡且海克斯证据在窗内即触发：MouseLedger 线程挂死、后续点击全部
    零痕迹。修复后同线程一次调用必须返回。"""
    print("\n[11c] 点击抓帧重入死锁回归")
    import numpy as _np
    from hexa.core.screen_detect import ScreenDetector

    class _Ocr:
        available = True

    class _Layout:
        window = None

        def to_pixels(self, w, h, canvas=False):
            return {"cards": [(10, 10, 50, 50)],
                    "title_bands": [], "rerolls": []}

    det = ScreenDetector(kb, _Ocr(), layout=_Layout())
    det._grab = lambda screen=None: _np.zeros((100, 100, 3), dtype=_np.uint8)
    det._screen_size = lambda: (100, 100)
    crop = det._grab_card_crop(0)   # 修复前：同线程重入拿锁，本行永久挂死
    check("抓帧返回（无重入死锁）",
          crop is not None and crop.shape[0] == 50)
    check("越界卡号安全返回 None", det._grab_card_crop(5) is None)


def test_mouse_ledger_hook():
    """点击捕获 v2（2026-09-30 牛头案）：GetAsyncKeyState 轮询被 OCR
    活跃期饿死——轮询间隙 100ms+ 叠 Windows sleep 精度，60-80ms 的快速
    选卡点击整次漏掉（两局 7 轮选卡零记账、所有捕获点击均在 OCR 空闲期
    的定向证据）。改 WH_MOUSE_LL 低级钩子：系统投递事件，回调只做
    非阻塞投队列（慢回调会拖慢全系统鼠标，游戏内不可接受），处理线程
    消费路由。断言：钩子安装成功、队列→路由端到端、stop 干净退出。

    2026-10-13 加固（门禁误报案，clicks_20261013_151845.txt 实证）：
    LL 钩子与捕获子进程都是全机的，本测试窗口期内屏幕上任何真实左键
    点击（用户/其他程序，含 GetAsyncKeyState LSB 启动沿收编的近端按压
    ——ready+21ms 落一条点击行）都会经钩子入队并被正确路由进 got。
    真实点击被记账 = 记账链工作正常，不是缺陷；原断言 len(got)==2 把
    "窗口期全机无左键点击"这个测试管不住的环境条件也当成了失败条件。
    改为按 (事件, x, y, ts) 精确配对注入事件 + 保序——顺带把 ts 透传
    也纳入断言（原版未验）。"""
    print("\n[11d] 点击捕获 v2（LL 钩子）")
    got = []
    ml = MouseLedger(
        on_click=lambda x, y, ts=None: got.append(("click", x, y, ts)),
        on_reroll=lambda x, y, ts=None: got.append(("reroll", x, y, ts)),
        reroll_rect_provider=lambda: [[900, 600, 100, 80]])
    ml.start()
    try:
        time.sleep(0.5)
        check("LL 钩子安装成功", ml.mode == "hook" and ml.is_alive(),
              f"mode={ml.mode}")
        # 模拟钩子投递 → 处理线程路由端到端（v3 队列带来源标记）。
        # 注入事件带精确 ts：回调原样透传（_route 不改值），据此配对，
        # 窗口期混入的真实点击条目不参与判定
        ts_reroll = time.time()
        ml._queue.put(("hook", 940, 640, ts_reroll))      # reroll 矩形
        ts_click = time.time()
        ml._queue.put(("hook", 100, 100, ts_click))       # 矩形外
        want = (("reroll", 940, 640, ts_reroll),
                ("click", 100, 100, ts_click))
        deadline = time.time() + 3
        while not all(w in got for w in want) and time.time() < deadline:
            time.sleep(0.01)
        # 单处理线程 FIFO：注入的两条必须按入队顺序到达回调（reroll 先
        # 于 click）；真实点击的条目夹在中间不影响结论
        ok = all(w in got for w in want) \
            and got.index(want[0]) < got.index(want[1])
        check("队列→路由端到端（reroll 命中/on_click 透传）", ok, str(got))
    finally:
        ml.stop()
        ml.join(timeout=3)
    check("stop 干净退出（消息循环收 WM_QUIT + unhook）", not ml.is_alive())


def test_mouse_ledger_ll_big_pointer():
    """LL 钩子 64 位指针溢出回归（2026-10-02 真机案）：CallNextHookEx 未设
    原型时 ctypes 缺省把 l_param 按 32 位 c_int 转换，而 64 位下它是
    MSLLHOOKSTRUCT 堆指针（普遍 > 2^31）——每个鼠标事件 OverflowError，
    被回调机制吞成"Exception ignored"刷屏。修复：私有 user32 实例 +
    钩子全家桶显式原型。断言：超 32 位 l_param 纯透传不炸（n_code≠
    HC_ACTION 不 deref，任何环境确定性复现）；真实堆指针完整链路
    （cast + 入队 + 透传）无异常、事件照常入队。"""
    print("\n[11e] LL 钩子 64 位指针原型回归")
    import ctypes as _ct
    from hexa.core.mouse_ledger import _MSLLHOOKSTRUCT, HC_ACTION, \
        WM_LBUTTONDOWN

    got = []
    ml = MouseLedger(on_click=lambda x, y, ts=None: got.append((x, y)))
    try:
        # ① 纯透传：合成超界指针不 deref，修复前此行确定性 OverflowError
        ret = ml._hook_proc(1, WM_LBUTTONDOWN, 0xFFFF80001234)
        check("超 32 位 l_param 纯透传不炸（返回 LRESULT）",
              isinstance(ret, int))
        # ② 真实事件：x64 堆地址本就 > 2^31（原案触发条件），完整走
        #    cast + 入队 + CallNextHookEx
        ev = _MSLLHOOKSTRUCT()
        ev.pt.x, ev.pt.y = 137, 579
        big = _ct.addressof(ev)
        check("x64 堆地址确超 32 位 c_int（原案触发条件）",
              big > 0x7FFFFFFF, hex(big))
        ret = ml._hook_proc(HC_ACTION, WM_LBUTTONDOWN, big)
        check("真实事件大指针 cast+透传不炸", isinstance(ret, int))
        src, x, y, _ts = ml._queue.get(timeout=2)
        check("事件照常入队（记账链不受影响）", src == "hook"
              and (x, y) == (137, 579) and not got)   # 处理线程未启动不路由
    finally:
        ml.stop()
    check("未启动线程直接 stop 干净收尾", not ml.is_alive())


def test_mouse_ledger_game_mode():
    """双通道兜底回归（2026-10-02 寒冰案）：v2 LL 钩子上线后首局实证——
    客户端窗口期正常收沿，游戏窗口焦点期整整一局零事件（[MOUSE] 全 0、
    台账空）。腾讯全家桶（wegame/tcls/LeagueClient*）实测 High 提权、
    本工具 Medium，UIPI/反作弊级拦截对低级钩子生效；GetAsyncKeyState 读
    全局异步键态不受限（牛头案轮询局内可见的定向证据）。v3：set_game_mode
    局内并行开轮询通道；钩子/轮询同次按压 50ms 同位去重；局内双通道全零
    连续 2 窗打前台窗口+双方 IL 现场告警。"""
    print("\n[11f] 局内轮询兜底 + 双通道去重 + 零沿告警")
    import io
    from contextlib import redirect_stdout
    from hexa.core.mouse_ledger import foreground_owner, self_il

    check("self_il 返回可读 IL 标签",
          isinstance(self_il(), str) and "0x" in self_il(), self_il())
    check("foreground_owner 返回可读现场标签",
          isinstance(foreground_owner(), str) and "IL=" in foreground_owner())

    got = []
    ml = MouseLedger(on_click=lambda x, y, ts=None: got.append(("click", x, y)))
    # ① 相位切换：进局内打印一次、同值重入静默（champ select 重置重入）
    buf = io.StringIO()
    with redirect_stdout(buf):
        ml.set_game_mode(True)
        ml.set_game_mode(True)
    check("进入局内打印一次（重入静默）",
          buf.getvalue().count("轮询通道启用") == 1
          and "退出局内" not in buf.getvalue(), buf.getvalue())
    # ② 去重：钩子沿先入，轮询沿 50ms 窗内同位 → 去重不路由；异位/超窗
    #    照常路由（钩子漏捕第一下时后续真点击不被误吞）
    with redirect_stdout(io.StringIO()):
        t0 = time.time()
        ml._feed("hook", 500, 600, t0)        # 钩子沿 → 路由
        ml._feed("poll", 503, 602, t0 + 0.01)   # 窗内同位 → 去重
        ml._feed("poll", 800, 600, t0 + 1.0)    # 异位 → 路由
        ml._feed("poll", 501, 599, t0 + 1.1)    # 异位 → 路由
    check("双通道去重（同次按压不双记，真点击不误吞）",
          [g[1:] for g in got] == [(500, 600), (800, 600), (501, 599)],
          str(got))
    check("分源计数 hook=1 poll=2 去重=1",
          ml._hook_edges == 1 and ml._poll_edges == 2
          and ml._dup_edges == 1,
          f"h={ml._hook_edges} p={ml._poll_edges} d={ml._dup_edges}")
    # ③ 局内 poll 沿正常 → 汇总行有数、不触发告警
    ml2 = MouseLedger(on_click=lambda x, y, ts=None: None)
    with redirect_stdout(io.StringIO()):
        ml2.set_game_mode(True)
        ml2._feed("poll", 100, 100, time.time())
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())
    check("局内有 poll 沿：汇总计数、不告警",
          "poll=1" in buf2.getvalue()
          and "[MOUSE][WARN]" not in buf2.getvalue(), buf2.getvalue())
    # ④ 局内双通道全零连续 2 窗 → 现场告警（前台属主 + 双方 IL），再重置
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())    # 第 1 窗全零 → 计数
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())    # 第 2 窗全零 → 告警
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())    # 重置后新窗只计数
    warn_n = buf3.getvalue().count("[MOUSE][WARN]")
    check("局内双通道全零 2 窗触发现场告警（仅一次）", warn_n == 1,
          buf3.getvalue())
    check("告警含前台属主与本进程 IL 现场",
          "前台 " in buf3.getvalue() and "IL=" in buf3.getvalue())
    # ⑤ 退出局内：轮询停用打印一次；局外全零不告警（非局内常态）
    with redirect_stdout(io.StringIO()):
        ml2.set_game_mode(True)      # 幂等（上面流程未改状态）
    buf4 = io.StringIO()
    with redirect_stdout(buf4):
        ml2.set_game_mode(False)
        ml2.set_game_mode(False)
    check("退出局内打印一次（重入静默）",
          buf4.getvalue().count("轮询通道停用") == 1, buf4.getvalue())
    buf5 = io.StringIO()
    with redirect_stdout(buf5):
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())
        ml2._stat_ts -= 61.0
        ml2._maybe_report_edges(time.monotonic())
    check("局外全零不触发局内告警",
          "[MOUSE][WARN]" not in buf5.getvalue(), buf5.getvalue())
    # ⑥ 统计窗历史（复盘 JSON mouse_stats 字段源）：③④⑤ 共 6 窗，
    # 告警窗恰 1 且 game=True；局外窗 game=False 永不标 warn
    hist = ml2.stats_history()
    check("统计窗历史随窗落账（6 窗全键齐全）",
          len(hist) == 6 and all(
              {"ts", "hook", "poll", "dup", "ev", "game", "helper",
               "warn"} == set(e)
              for e in hist), str(hist))
    check("告警窗恰 1 且局内标记", [e["warn"] for e in hist].count(True) == 1
          and next(e for e in hist if e["warn"])["game"] is True)
    check("局外窗 game=False 不标 warn",
          all(not e["warn"] for e in hist if not e["game"]))
    check("局内 poll 沿进历史（③窗 poll=1）",
          any(e["poll"] == 1 and e["game"] for e in hist))


def test_click_watcher_proc():
    """独立捕获子进程回归（20261002 虚空之女案）：v3 双通道首局实证——
    选卡瞬间钩子+轮询双双零捕获（轮询 50Hz 采样、LSB 位有按压必留痕，
    零留痕 = 采样线程在按压前后完全没跑到），而非选卡期点击（死后商店
    等）双双正常、主进程明明活着（复盘 timeline 数秒一条）——指向主进程
    内部 GIL/调度饥饿（选卡期 = 检测器逐秒全帧 OCR 风暴）或系统层过滤。
    v4 加 click_watcher 独立子进程：独立解释器独立 GIL、NORMAL 优先级
    （父进程 BELOW_NORMAL 会被子进程继承，必须显式抬回，否则白装），
    会话文件协议（ready 行 + "ts x y" 行）、tail 进统一管道按点击时刻
    去重、父进程死亡自动退出。它既是修复候选也是鉴别仪器（它也瞎 =
    系统层过滤的铁证）。"""
    print("\n[11g] 捕获子进程（click_watcher）+ tail 去重")
    import io
    import os
    import subprocess as _sp
    import tempfile
    from contextlib import redirect_stdout
    from pathlib import Path as _Path
    from hexa.core.mouse_ledger import MouseLedger as _ML

    script = (_Path(__file__).resolve().parent.parent
              / "hexa" / "core" / "click_watcher.py")
    check("click_watcher.py 就位（子进程脚本）", script.exists())
    ml_src = script.read_text(encoding="utf-8")
    check("子进程显式抬回 NORMAL 优先级（继承 BELOW_NORMAL 必修）",
          "SetPriorityClass" in ml_src and "NORMAL_PRIORITY_CLASS" in ml_src)
    check("子进程观察父进程死亡（看门狗自动收工）",
          "WaitForSingleObject" in ml_src and "SYNCHRONIZE" in ml_src)

    tmp = _Path(tempfile.mkdtemp(prefix="smoke_watcher_"))
    out1 = tmp / "c1.txt"
    # ① 真实拉起：ready 行落盘、terminate 收工
    p1 = _sp.Popen([sys.executable, str(script), str(os.getpid()), str(out1)],
                   stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    try:
        ready = None
        deadline = time.time() + 6
        while time.time() < deadline and ready is None:
            if out1.exists():
                lines = out1.read_text(encoding="ascii").splitlines()
                ready = next((ln for ln in lines if ln.startswith("ready")),
                             None)
            if ready is None:
                time.sleep(0.1)
        check("子进程拉起即写 ready 行（含墙钟时刻）",
              ready is not None and len(ready.split()) == 2, str(ready))
    finally:
        p1.terminate()
        p1.wait(timeout=5)
    check("terminate 收工", p1.poll() is not None)

    # ② 父进程死亡自动退出：短命"父进程"退场后子进程自行收工
    parent = _sp.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    out2 = tmp / "c2.txt"
    p2 = _sp.Popen([sys.executable, str(script), str(parent.pid), str(out2)],
                   stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    parent.wait(timeout=5)
    try:
        p2.wait(timeout=5)
        check("父进程死亡子进程自动退出（看门狗生效）",
              p2.poll() is not None)
    except _sp.TimeoutExpired:
        p2.terminate()
        check("父进程死亡子进程自动退出（看门狗生效）", False, "5s 未退出")

    # ③ 会话文件行解析 + 跨通道按点击时刻去重（迟到副本不双记）
    check("行解析：点击行/ready/坏行",
          _ML._parse_helper_line("1790948751.810 1755 956")
          == (1790948751.81, 1755, 956)
          and _ML._parse_helper_line("ready 1790948751.8") is None
          and _ML._parse_helper_line("garbage") is None)
    got = []
    ml = MouseLedger(on_click=lambda x, y, ts=None: got.append((x, y, ts)))
    t0 = time.time()
    with redirect_stdout(io.StringIO()):
        ml._feed("hook", 500, 600, t0)          # 钩子先到
        ml._feed("poll", 503, 602, t0 + 0.01)   # 子进程迟到副本 → 去重
        ml._feed("poll", 800, 600, t0 + 1.0)    # 真新点击 → 路由（带 ts 透传）
    check("迟到副本按点击时刻去重、真点击透传 ts",
          [(g[0], g[1]) for g in got] == [(500, 600), (800, 600)]
          and abs(got[1][2] - (t0 + 1.0)) < 1e-6, str(got))


def test_input_instrument_kit():
    """输入鉴别仪器回归（20261003 皮城女警案）：v4 子进程全程活着且采样
    节奏正常（CPU 开销吻合）仍看不到选卡按压，而客户端/商店/任务栏点击
    正常——"游戏画面焦点期的按压不进全局输入层"。仪器组：①钩子全事件
    计数（游戏内移动恒流，事件=0 即钩子连移动都没收到→输入整体消失）；
    ②全按键按下沿限频留痕（排除右/侧键选卡、驱动重映射等平凡解释）；
    ③子进程心跳/退出报文协议（hb 停滞=被挂起的直接证据，bye 区分自行
    退出）；④激活帧存档 + 丢弃点击全屏定格（坐标争议一锤定音）。"""
    print("\n[11h] 输入鉴别仪器（全事件计数/全按键留痕/心跳协议/帧存档）")
    import ctypes as _ct
    import io
    from contextlib import redirect_stdout
    from hexa.core.mouse_ledger import (HC_ACTION, WM_MBUTTONDOWN,
                                        WM_MOUSEMOVE, WM_RBUTTONDOWN,
                                        WM_XBUTTONDOWN, _MSLLHOOKSTRUCT,
                                        LLMHF_INJECTED)
    from hexa.core.screen_detect import ScreenDetector

    ml = MouseLedger(on_click=lambda x, y, ts=None: got.append((x, y)))
    got = []
    buf = io.StringIO()
    with redirect_stdout(buf):
        # ① 移动事件：计数、不入队不路由
        ret = ml._hook_proc(HC_ACTION, WM_MOUSEMOVE, 0)
        check("移动事件计入全事件数（不入队不路由）",
              isinstance(ret, int) and ml._event_count == 1 and not got)
        # ② 右键/中键/侧键：限频留痕投队列、不入账（排除非左键选卡解释）。
        #    20261013 改：回调在系统鼠标路径上，print 的控制台锁阻塞会拖慢
        #    全系统鼠标——留痕改投 ("log", 文案) 由处理线程打
        ev = _MSLLHOOKSTRUCT()
        ev.pt.x, ev.pt.y, ev.flags = 941, 395, LLMHF_INJECTED
        ml._hook_proc(HC_ACTION, WM_RBUTTONDOWN, _ct.addressof(ev))
        ml._btn_log_ts = 0.0   # 限频重置：下一条不受 2s 窗抑制
        ml._hook_proc(HC_ACTION, WM_MBUTTONDOWN, _ct.addressof(ev))
        import queue as _q
        notes = []
        try:
            while True:
                item = ml._queue.get_nowait()
                if item[0] == "log":
                    notes.append(item[1])
        except _q.Empty:
            pass
        check("非左键按键留痕（含注入标记、投队列由处理线程打）",
              any("按键 RB (941,395) 注入" in n for n in notes)
              and any("按键 MB" in n for n in notes) and not got
              and ml._event_count == 3, str(notes))
        # ③ 统计窗：事件数进汇总行与历史记录
        ml._stat_ts -= 61.0
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            ml._maybe_report_edges(time.monotonic())
        check("汇总行含事件数", "事件=3" in buf2.getvalue(),
              buf2.getvalue()[-200:])
        hist = ml.stats_history()
        check("历史记录含 ev 字段", hist and hist[-1]["ev"] == 3, str(hist[-1]))
    # ④ 子进程心跳/退出报文协议
    watcher_src = (Path(__file__).resolve().parent.parent
                   / "hexa" / "core" / "click_watcher.py").read_text(
                       encoding="utf-8")
    check("子进程心跳协议（hb 每 5s）",
          '"hb {now:.3f}' in watcher_src and "HB_INTERVAL = 5.0" in watcher_src)
    check("子进程退出报文（bye）+ Win32 显式原型（64 位句柄）",
          '"bye {time.time():.3f}' in watcher_src
          and "WaitForSingleObject.restype" in watcher_src)
    check("tail 识别心跳与退出报文",
          'parts[0] == "hb"' in Path(__file__).resolve().parent.parent.joinpath(
              "hexa/core/mouse_ledger.py").read_text(encoding="utf-8")
          and 'parts[0] == "bye"' in Path(__file__).resolve().parent.parent
          .joinpath("hexa/core/mouse_ledger.py").read_text(encoding="utf-8"))
    # ⑤ 激活帧存档 + 丢弃点击全屏定格（app 接线）
    app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    check("激活帧存档接线（frame_sink）",
          "detector.frame_sink = _frame_sink" in app_src
          and "round_{time.strftime('%Y%m%d_%H%M%S')}" in app_src)
    check("丢弃点击全屏定格接线（rect_miss/no_hex_evidence 带截图）",
          "_capture_full_drop(x, y)" in app_src
          and "drop_{time.strftime('%Y%m%d_%H%M%S')}" in app_src
          and app_src.count('"image": shot') >= 2)
    check("检测器激活帧走 frame_sink（错位争议一锤定音）",
          "frame_sink" in (Path(__file__).resolve().parent.parent
                           / "hexa" / "core" / "screen_detect.py")
          .read_text(encoding="utf-8"))


def test_auto_elevation():
    """自动提权回归（20261004 皮城女警案定案）：局内 ev=0 连鼠标移动都
    收不到、切出窗口瞬间涌进 7187 事件——游戏窗口提权(High)前台期输入
    对 Medium 进程完全不可见（UIPI），管理员实验未生效（用户 uv run 启动
    IL=Medium）。修复：启动检测 TokenElevation，未提权自动 ShellExecute
    runas 重启自身（UAC 一次），settings [diagnostics] elevate_auto 可关；
    重启目标必须用仓库 venv 的 pythonw（uv run 下 sys.executable 是
    anaconda 本体，直接重启丢虚拟环境）。附带修复：v3 的 lambda 吞掉了
    on_end_of_game 的 reason 参数（终局流程 TypeError 中断数日）。"""
    print("\n[11i] 自动提权 + on_end_of_game 参数透传")
    from hexa.core.mouse_ledger import is_elevated

    check("is_elevated 返回布尔（TokenElevation 探针）",
          isinstance(is_elevated(), bool), str(is_elevated()))
    app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    check("启动自动提权门控（settings 可关）",
          'settings.get("diagnostics", "elevate_auto"' in app_src
          and "is_elevated()" in app_src
          and 'ShellExecuteW(\n            None, "runas"' in app_src)
    check("提权重启用仓库 venv pythonw（uv run 兼容）",
          '.venv" / "Scripts" / "pythonw.exe' in app_src
          and "os.path.abspath(sys.argv[0])" in app_src)
    check("UAC 拒绝降级 WARN（不阻塞主流程）",
          "提权未确认" in app_src and "return" in app_src)
    check("on_end_of_game 参数透传（v3 lambda 吞参回归）",
          "session.on_end_of_game(*a)" in app_src)
    # 提权子进程 = 无控制台 pythonw：stdout/stderr 为 None，tee 之前的
    # 任何 print（知识库加载 [OK] 行等）都会 AttributeError 炸进程
    # （20261004 UAC 点"是"后无窗实锤）——垫流 + tee 提前到 KB 之前
    check("pythonw 垫流防护（stdout/stderr=None 兜底）",
          "if sys.stdout is None:" in app_src
          and "open(os.devnull" in app_src)
    check("tee 提前到知识库加载之前（早期打印有归处）",
          app_src.index("_tee_path = BASE_DIR")
          < app_src.index("kb = KnowledgeBase()"))
    # 递归保险 + 绕开 uv trampoline（20261004 提权递归案：跳板 spawn 真
    # 解释器时丢提权令牌 → 子进程仍 Medium → 再请求 → 8 分钟 1510 实例）
    check("提权递归保险（重启标记在身不再二次请求）",
          '"--elevated-relaunch" in sys.argv' in app_src
          and "提权重启未生效" in app_src)
    check("提权重启直启基础解释器（绕开 trampoline）+ PYTHONPATH 保依赖",
          'pyvenv.cfg' in app_src and "PYTHONPATH" in app_src
          and '--elevated-relaunch' in app_src)
    # 本机真值：pyvenv.cfg 能解析出存在的 pythonw（提权目标有效）
    import os as _os
    from hexa.config import BASE_DIR
    cfg = BASE_DIR / ".venv" / "pyvenv.cfg"
    if cfg.exists():
        home = None
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("home"):
                home = _os.path.dirname(line.split("=", 1)[1].strip())
                break
        check("pyvenv.cfg 解析出可用基础 pythonw（本机真值）",
              home and _os.path.exists(_os.path.join(home, "pythonw.exe")),
              str(home))
        check("venv site-packages 存在（PYTHONPATH 依赖保底）",
              (BASE_DIR / ".venv" / "Lib" / "site-packages").exists())


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


# ── 11. 点击链黑匣子（clicks 记录 + 定格截图落库） ────────────────

def test_click_blackbox():
    """台账排障黑匣子（2026-09-29 复发案二阶段）：每次卡片点击的终点
    记录（坐标/卡号/分支/结果/OCR 原文/截图文件名）进 session.clicks
    随复盘日志落盘；点击时刻定格帧经 capture_sink 写 log/captures/ PNG。
    台账缺记录时三方对账：clicks × 截图 × [LEDGER] 诊断日志。"""
    import json as _json
    import numpy as _np2

    print("\n[11] 点击链黑匣子（clicks 落盘 + 截图 sink）")

    s = GameSession(kb, rules)
    for i in range(310):
        s.log_click({"x": i, "y": i, "card": 1, "branch": "direct",
                     "result": "recorded", "name": "不祥契约"})
    check("log_click 环形上限 300（保尾部）", len(s.clicks) == 300
          and s.clicks[-1]["x"] == 309 and s.clicks[0]["x"] == 10)
    check("log_click 自动补 ts 且不改写调用方 dict",
          all("ts" in c for c in s.clicks))
    s.reset()
    check("reset 清空 clicks", s.clicks == [])

    # 纯点击留痕局（台账/建议双空、无 augment_unverified）也必须落盘：
    # "点击全被丢"的局 clicks 正是唯一现场（flush 非空门含 clicks）
    s2 = GameSession(kb, rules)
    s2._game_t0 = time.time() - 1000
    s2.stage = "in_game"
    s2.log_click({"x": 640, "y": 720, "card": 1, "branch": "drop",
                  "result": "dropped_rect_miss", "name": None,
                  "raw": None, "image": None})
    games_dir = _SMOKE_BASE / "log" / "games"
    n0 = len(list(games_dir.glob("*.json"))) if games_dir.exists() else 0
    s2.flush_pending_review()
    files = sorted(games_dir.glob("*.json"))
    want_prefix = time.strftime("%Y%m%d_%H%M%S", time.localtime(s2._game_t0))
    mine = [p for p in files if p.name.startswith(want_prefix)]
    check("纯点击留痕局 flush 兜底落盘（非空门含 clicks）",
          len(mine) == 1, f"n0={n0} files={[p.name for p in files]}")
    doc = _json.loads(mine[-1].read_text(encoding="utf-8"))
    check("复盘 doc 含 clicks 字段（记录完整字段）",
          len(doc.get("clicks", [])) == 1
          and doc["clicks"][0]["result"] == "dropped_rect_miss"
          and doc["clicks"][0]["x"] == 640
          and isinstance(doc["clicks"][0]["ts"], float), str(doc.get("clicks")))

    # app.py 源码断言：终点接线齐全
    app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    seg_diag = app_src[app_src.index("def _click_diag")
                       :app_src.index("def _on_click")]
    seg_click = app_src[app_src.index("def _on_click")
                        :app_src.index("def _record_click")]
    seg_rec = app_src[app_src.index("def _record_click")
                      :app_src.index("def _on_reroll")]
    seg_rr = app_src[app_src.index("def _on_reroll")
                     :app_src.index("ledger = MouseLedger")]
    check("_on_click 点击时刻 capture_click_evidence 定格+落盘，"
          "直记/补扫消费同一 crop（截图与识别同像素）",
          "capture_click_evidence(idx)" in seg_click
          and "crop=crop" in seg_click)
    check("_record_click 终点组装 clicks（ev 上下文/direct/rescan 分支）",
          "session.log_click" in seg_rec and '"branch": branch' in seg_rec
          and '_blackbox("recorded" if ok else "duplicate"' in seg_rec)
    check("_on_reroll 带坐标+点击时刻且终点 log_click（reroll_rescan）",
          "def _on_reroll(x, y, ts=None)" in seg_rr
          and '"result": "reroll_rescan"' in seg_rr)
    check("丢弃分支 clicks 记录（打印限频不限记录）",
          '"result": f"dropped_{key}"' in seg_diag
          and seg_diag.index("if first") < seg_diag.index("session.log_click"))
    check("capture_sink 接线 + 开关（diagnostics.click_capture 默认开）",
          "detector.capture_sink = _capture_sink" in app_src
          and 'settings.get("diagnostics", "click_capture"' in app_src)
    check("截图写 log/captures/ PNG（imencode 绕 ANSI 路径限制）",
          '"captures"' in app_src and "cv2.imencode" in app_src)

    # MouseLedger.on_reroll 带坐标回调（app._on_reroll 坐标来源）
    ml_src = (Path(__file__).resolve().parent.parent
              / "hexa" / "core" / "mouse_ledger.py").read_text(
                  encoding="utf-8")
    check("MouseLedger 路由 reroll 时传 (x, y, ts)",
          "self.on_reroll(x, y, ts)" in ml_src)

    # GameSession.review doc 的 clicks 序列化不含 numpy/不可 JSON 类型
    s3 = GameSession(kb, rules)
    s3.log_click({"x": 1, "y": 2, "card": 2, "branch": "rescan",
                  "result": "recorded", "name": "不祥契约",
                  "raw": None,
                  "image": "click_20260929_120000_000_card3.png",
                  "extra": _np2.zeros(3, dtype=_np2.float32).tolist()})
    try:
        blob = _json.dumps(
            [dict(c, ts=round(c["ts"], 3)) for c in s3.clicks],
            ensure_ascii=False)
        check("clicks 可 JSON 序列化（复盘落盘不炸）", "不祥契约" in blob)
    except TypeError as e:
        check("clicks 可 JSON 序列化（复盘落盘不炸）", False, repr(e))


# ── 12. UI 设计系统（Quiet Dark v3，2026-09-30 重构） ─────────────

def test_ui_design_system():
    """UI 层离线断言（无 Tk 窗口、无显示环境要求）：

    1. 调色板单源 —— `#rrggbb` 只允许出现在 theme.py（overlay 曾自带一套
       重复色值与 theme 分叉，UI 改色时最易两处各改一处）
    2. 档位色覆盖 S/A/B/C/? 且全部取自 Palette
    3. `fit_height` 纯函数：贴内容 / 夹屏内 / 收缩迟滞（overlay 死区与抖动）
    4. token 完整性：UI 里 `Palette["x"]` 的每个 x 都真实存在（typo 在
       运行时才炸，冒烟提前拦）
    5. emoji 清洗：剥掉彩色 emoji、保留 ①②③ 与 ✓✗（卡序/识别标记）
    6. 「路线」行不再出现在 UI（用户口径：路线不是 ad/ap/坦克三键）
    """
    import re as _re
    from hexa.ui import theme as _theme
    from hexa.ui.overlay import fit_height as _fit
    from hexa.ui.theme import GRADES, Metrics, Palette, Type, _is_emoji, strip_emoji

    print("\n[12] UI 设计系统（token / 档位色 / 贴内容高度 / 去 emoji）")
    ui_dir = Path(__file__).resolve().parent.parent / "hexa" / "ui"
    srcs = {p.name: p.read_text(encoding="utf-8")
            for p in sorted(ui_dir.glob("*.py"))}

    def literals(src, skip_names=()):
        """取"真正会被渲染/参与逻辑"的字符串字面量：跳过 docstring 与
        指定名字的赋值（emoji 剥离集合自身含被禁字符，须豁免）。
        注释里写旧色值/旧 emoji 做说明是合理的，不该被断言误伤。"""
        import ast as _ast
        tree = _ast.parse(src)
        skip = set()
        for node in _ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                                 _ast.AsyncFunctionDef)) and body:
                head = body[0]
                if (isinstance(head, _ast.Expr)
                        and isinstance(head.value, _ast.Constant)
                        and isinstance(head.value.value, str)):
                    skip.add(id(head.value))
            if isinstance(node, _ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, _ast.Name)]
                if any(n in skip_names for n in names):
                    for c in _ast.walk(node):
                        if isinstance(c, _ast.Constant):
                            skip.add(id(c))
        return [n.value for n in _ast.walk(tree)
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                and id(n) not in skip]

    # 1) 调色板单源
    hexpat = _re.compile(r"#[0-9a-fA-F]{6}\b")
    hits = {}
    for name, text in srcs.items():
        if name == "theme.py":
            continue
        found = []
        for lit in literals(text):
            found += hexpat.findall(lit)
        if found:
            hits[name] = found
    check("调色板单源：色值只出现在 theme.py（UI 改色不许就地写死）",
          not hits, str(hits))
    check("Palette 本身为合法 6 位 hex 且键数 >= 15",
          all(_re.fullmatch(r"#[0-9A-Fa-f]{6}", v) for v in Palette.values())
          and len(Palette) >= 15, f"{len(Palette)} keys")

    # 2) 档位色
    check("档位色覆盖 S/A/B/C/?", set(GRADES) >= {"S", "A", "B", "C", "?"},
          str(sorted(GRADES)))
    check("档位色全部取自 Palette（不另立色值）",
          all(v in Palette.values() for v in GRADES.values()))
    check("档位 S>A>B 各自不同色（避免整块同色回归）",
          len({GRADES["S"], GRADES["A"], GRADES["B"]}) == 3)

    # 3) fit_height：贴内容 / 夹屏内 / 收缩迟滞
    floor, screen = 150, 1440
    check("fit_height 内容不足时贴内容（不再强撑 560 下限）",
          _fit(430, floor, screen, 560) == 430, str(_fit(430, floor, screen, 560)))
    check("fit_height 内容超屏时夹到 屏-40",
          _fit(2000, floor, screen, 0) == screen - 40)
    check("fit_height 低于下限时保留下限",
          _fit(60, floor, screen, 0) == floor)
    check("fit_height 收缩迟滞 16px（内容忽长忽短不抖窗）",
          _fit(545, floor, screen, 560) == 560      # 差 15 → 不动
          and _fit(544, floor, screen, 560) == 544)  # 差 16 → 收缩
    check("fit_height 增长立即生效（内容多了马上看得到）",
          _fit(700, floor, screen, 560) == 700)

    # 4) token 完整性
    tokpat = _re.compile(r'\bP(?:alette)?\["(\w+)"\]')
    used = set()
    for text in srcs.values():
        used |= set(tokpat.findall(text))
    missing = sorted(used - set(Palette))
    check(f"token 完整性：UI 引用的 {len(used)} 个色 token 全部存在",
          not missing, str(missing))
    check("Type/Metrics 键齐全（字号与间距标尺被引用）",
          {"title", "body", "small", "ov_hero", "ov_body", "ov_meta"} <= set(Type)
          and {"pad_card", "rail_w", "badge"} <= set(Metrics),
          f"Type={sorted(Type)} Metrics={sorted(Metrics)}")

    # 5) emoji 清洗
    check("strip_emoji 剥掉彩色 emoji（逻辑层文案经渲染边界清洗）",
          strip_emoji("🔄 卖无尽 → 买星蚀") == "卖无尽 → 买星蚀"
          and strip_emoji("💰 距星蚀还差 400，可攒") == "距星蚀还差 400，可攒",
          repr(strip_emoji("🔄 卖无尽 → 买星蚀")))
    check("strip_emoji 保留卡序与识别标记（①②③ / ✓ / ✗）",
          strip_emoji("① [S] 贪欲束缚 ✓已选") == "① [S] 贪欲束缚 ✓已选")
    check("strip_emoji 空值安全", strip_emoji("") == "" and strip_emoji(None) == "")
    bad = []
    for name, text in srcs.items():
        for lit in literals(text, skip_names=("_EMOJI_BMP",)):
            hit = [c for c in lit if _is_emoji(c)]
            if hit:
                bad.append(f"{name}:{''.join(hit)}")
    check("UI 字符串无彩色 emoji（简约约束的永久守卫）", not bad, str(bad[:5]))

    # 6) 路线行已下线（用户口径：路线不是 ad/ap/坦克三键）
    route = [n for n in ("overlay.py", "main_window.py")
             if any("路线" in v for v in literals(srcs[n]))]
    check("UI 不再显示「路线」行（current_path 仍在引擎内供选候选/复盘）",
          not route, str(route))

    # 7) 应用版本号单一来源（hexa.__version__ ↔ pyproject 不得漂移）
    import tomllib as _toml
    from hexa import __version__
    from hexa.ui.theme import version_label as _vlabel
    root = Path(__file__).resolve().parent.parent
    try:
        pp = _toml.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        pp_ver = pp.get("project", {}).get("version", "")
    except Exception as e:  # noqa: BLE001
        pp_ver = f"<读不到: {e}>"
    check("应用版本号：hexa.__version__ 与 pyproject 一致",
          pp_ver == __version__, f"pyproject={pp_ver} __version__={__version__}")
    check("版本号格式为 vX.Y.Z", _vlabel() == f"v{__version__}"
          and __version__.count(".") == 2, _vlabel())
    titles = " ".join(v for n in ("main_window.py", "settings_window.py")
                      for v in literals(srcs[n]))
    check("两处窗口标题/角标都引用 version_label()（不各自硬写版本串）",
          "version_label()" in srcs["main_window.py"]
          and "version_label()" in srcs["settings_window.py"]
          and not [v for v in literals(srcs["main_window.py"])
                   + literals(srcs["settings_window.py"])
                   if __version__ in v],
          titles[:60])


def test_stability_batch():
    """稳定性批修回归（20261013 全项目盘点案）。样板是 is_elevated 的
    wt NameError：探针函数引用未定义名，NameError 被外层 except 吞成
    恒 False——旧断言 isinstance(bool) 测不出。本批对"恒返回默认值"类
    bug 一律补调用链/源级断言；同批含 C1 描述贯通 System2 与 C2 刷新
    数据按钮接线。"""
    print("\n[16] 稳定性批修（盘点 20261013）")
    import inspect
    import json as _json
    import threading as _threading

    root = Path(__file__).resolve().parent.parent

    def src_of(rel):
        return (root / rel).read_text(encoding="utf-8")

    # 1) is_elevated 未定义名（恒 False 回归：提权重启成功也报 WARN）
    from hexa.core import mouse_ledger
    fn_src = inspect.getsource(mouse_ledger.is_elevated)
    check("is_elevated 无未定义 wt 引用（恒 False 回归）",
          "wt." not in fn_src and "ctypes.wintypes." in fn_src)
    check("is_elevated 探针可用（返回布尔）",
          isinstance(mouse_ledger.is_elevated(), bool))

    # 2) LivePoller 单帧异常不杀轮询线程（脏数据 → 300s 误判终局回归）
    poller = LivePoller(kb, summoner_name_fn=lambda: "")
    poller.POLL_INTERVAL = 0.01
    hits = []

    def _boom_poll():
        hits.append(1)
        if len(hits) >= 3:
            poller._running = False
        raise RuntimeError("脏字段")

    poller._poll_once = _boom_poll
    poller._in_game.set()
    t = _threading.Thread(target=poller.run, daemon=True)
    t.start()
    t.join(timeout=10)
    check("Live 轮询单帧异常不杀线程（3 连炸后仍按需退出）",
          not t.is_alive() and len(hits) >= 3, f"hits={len(hits)}")

    # 3) overlay 刷新链防断（try/finally 续约，同主窗口 _poll_ui 口径）
    ov_src = src_of("hexa/ui/overlay.py")
    check("overlay _refresh 异常不冒出且 finally 续约",
          "def _refresh_once" in ov_src
          and "finally:\n            self.win.after(500, self._refresh)" in ov_src)

    # 4) settings 原子写 + 损坏备份（截断 → 配置蒸发回归）
    from hexa.config import DEFAULTS, Settings
    sdir = _SMOKE_BASE / "cfg_stability"
    sdir.mkdir(parents=True, exist_ok=True)
    sp = sdir / "settings.json"
    st = Settings(sp)
    st.set("ai", "enabled", True)
    check("settings 原子写（无 .tmp 残留）",
          sp.exists() and not sp.with_suffix(".json.tmp").exists())
    check("settings 写读一致",
          _json.loads(sp.read_text(encoding="utf-8"))["ai"]["enabled"] is True)
    sp.write_text("{broken", encoding="utf-8")
    st2 = Settings(sp)
    check("损坏 settings 备份为 .bak（配置可救）",
          sp.with_suffix(".json.bak").exists())
    check("损坏 settings 回退默认不崩",
          st2.get("ai", "enabled", default=False) is False)

    # 5) DEFAULTS 漂移键补齐（settings 全删重建后配置面不散落硬编码）
    check("DEFAULTS 补齐 ai.min_interval（成本守门 20s）",
          DEFAULTS["ai"].get("min_interval") == 20)
    check("DEFAULTS 补齐 fallback_model 两槽位",
          DEFAULTS["system1"].get("fallback_model") == ""
          and DEFAULTS["system2"].get("fallback_model") == "")
    check("DEFAULTS 补齐 diagnostics 开关",
          DEFAULTS["diagnostics"].get("elevate_auto") is True
          and DEFAULTS["diagnostics"].get("click_capture") is True)

    # 6) kb 损坏 JSON 不崩启动（champions/pinyin 降级为空）
    kdd = _SMOKE_BASE / "kb_corrupt_d"
    kdd.mkdir(parents=True, exist_ok=True)
    (kdd / "champions.json").write_text("{bad", encoding="utf-8")
    (kdd / "pinyin_map.json").write_text("[bad", encoding="utf-8")
    kb3 = KnowledgeBase(data_dir=kdd,
                        know_dir=_SMOKE_BASE / "kb_corrupt_k")
    check("kb 损坏 champions/pinyin 降级不崩启动",
          kb3.cn_to_en == {} and kb3.en_to_cn == {})

    # 7) OCR 初始化失败可重试（原 available=False 永不恢复回归）
    from hexa.core.ocr_engine import OcrEngine
    oe = OcrEngine(kb)
    boom_n = []

    def _boom_ocr():
        boom_n.append(1)
        raise RuntimeError("init fake fail")

    oe._get_ocr = _boom_ocr
    probe = object()
    check("OCR 初始化失败首帧降级且不改 available",
          oe.recognize(probe) == [] and oe.available and oe._init_fail_ts > 0)
    oe.recognize(probe)
    check("OCR 退避期内不重复初始化", len(boom_n) == 1)
    oe._init_fail_ts -= OcrEngine.INIT_RETRY_INTERVAL + 1

    def _fake_ocr(img):
        result = [([[0, 0], [5, 0], [5, 5], [0, 5]], "测试文本", 0.9)]
        return result, None

    oe._get_ocr = lambda: _fake_ocr
    out = oe.recognize(probe)
    check("OCR 退避期满自动重试恢复", len(out) == 1 and out[0][0] == "测试文本"
          and len(boom_n) == 1)

    # 8) 钩子回调零 print（系统鼠标路径不碰控制台锁）
    ml_src = src_of("hexa/core/mouse_ledger.py")
    hp = ml_src[ml_src.index("def _hook_proc"):ml_src.index("def _run_hook")]
    check("钩子回调不再 print（留痕投队列由处理线程打）",
          "print(" not in hp and '("log",' in hp)
    check("处理线程识别 log 投递",
          'item[0] == "log"' in ml_src)

    # 9) lcu lockfile 句柄 with 关闭
    check("lcu lockfile 句柄 with 关闭",
          "with open(lockfile)" in src_of("hexa/core/lcu.py"))

    # 10) C1 卡面描述贯通 System2 候选名单（隔离目录防污染真实知识库）
    from hexa.ai import system2 as _s2
    kd2 = _SMOKE_BASE / "s2desc_d"
    kk2 = _SMOKE_BASE / "s2desc_k"
    kd2.mkdir(parents=True, exist_ok=True)
    kk2.mkdir(parents=True, exist_ok=True)
    shutil.copy(kb.data_dir / "hero_augments.csv", kd2 / "hero_augments.csv")
    kb2 = KnowledgeBase(data_dir=kd2, know_dir=kk2)
    hero_cn = next((h for h, m in kb2.hero_augments.items() if m), "")
    check("C1 前置：先验英雄可用", bool(hero_cn), hero_cn)
    a0 = kb2.top_augments(hero_cn, 1)[0]
    kb2.record_augment_desc(a0, "测试描述原文：技能花费返还加强效果")
    out_line = _s2._fmt_augments(kb2, hero_cn)
    check("C1 卡面描述进 System2 候选名单（原文: 前缀）",
          "原文:测试描述原文" in out_line, out_line[:80])
    # 同一海克斯可出现在多英雄先验里，按"条目段"断言：只有已录描述的
    # a0 条目带原文段，其余条目不带
    segs = out_line.split("；")
    check("C1 仅已录描述条目带原文段",
          all("原文:" in s for s in segs if s.startswith(a0 + "（"))
          and all("原文:" not in s for s in segs if not s.startswith(a0 + "（")),
          out_line[:100])

    # 11) C2 刷新数据按钮接线（主窗口按钮 + app 后台脚本链）
    mw_src = src_of("hexa/ui/main_window.py")
    app_src = src_of("app.py")
    check("C2 刷新数据按钮接线",
          "on_refresh_data" in mw_src and "刷新数据" in mw_src
          and "_on_refresh_data" in mw_src
          and "_start_data_refresh" in app_src
          and "fetch_opgg_builds" in app_src
          and "gen_champ_profile" in app_src)


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_kb()
    test_rules()
    test_session()
    test_engine_late_start_net()
    test_phase_machine()
    test_live_me_match()
    test_runes_probe()
    test_live_facts_context()
    test_detect_and_layout()
    test_ai_engine()
    test_ai_build_pool()
    test_ai_inflight()
    test_system2_watchdog()
    test_grab_card_crop_no_deadlock()
    test_mouse_ledger_hook()
    test_mouse_ledger_ll_big_pointer()
    test_mouse_ledger_game_mode()
    test_click_watcher_proc()
    test_input_instrument_kit()
    test_auto_elevation()
    test_session_ai_serial()
    test_swap_mode()
    test_coach_save_gap()
    test_build_progress()
    test_review_fixes()
    test_thinking_params()
    test_click_diag_and_tee()
    test_click_blackbox()
    test_stability_batch()
    test_ui_design_system()
    print(f"\n{'='*40}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✅")
