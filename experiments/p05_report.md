# P0.5 翻译层验证报告

生成时间: 2026-09-24 21:35:36
模型: qwen3.8-flash | 判定标准: 零幻觉 + top10≥7 一致 + 无过时装备


## 暗裔剑魔 —— ✅ 通过

- ✅ ①零幻觉：推荐字段装备名全部命中 DD 库　命中 11 个装备名
- ✅ ①零幻觉：海克斯名全部命中 229 名单　全部命中
- ✅ ②海克斯亲和 top10 方向一致（交集 10/10 ≥7）　model: ['贪欲束缚', '升级：死亡之舞', '渴血', '升级：焚天', '大力']... csv: ['贪欲束缚', '升级：死亡之舞', '渴血', '升级：焚天', '大力']...
- ✅ 观测：理由文字无未登记装备词（不计入通过线）　干净
- ✅ 观测：加点顺序与 OP.GG 一致（不计入通过线）　模型: 主Q副E / OP.GG: 主Q副E / 一致

<details><summary>模型输出 JSON</summary>

```json
{
 "augment_affinity": [
  {
   "name": "贪欲束缚",
   "bias": "ad",
   "note": "排名第一，强化吸血与持续作战能力，契合剑魔被动机制。",
   "is_upgrade": false
  },
  {
   "name": "升级：死亡之舞",
   "bias": "tank",
   "note": "核心防御装升级选项，提供减伤与回血，提升容错率。",
   "is_upgrade": true
  },
  {
   "name": "渴血",
   "bias": "functional",
   "note": "增强残血反杀能力，配合Q技能二段伤害效果显著。",
   "is_upgrade": false
  },
  {
   "name": "升级：焚天",
   "bias": "ad",
   "note": "主要输出装升级路线，增加攻速与特效伤害。",
   "is_upgrade": true
  },
  {
   "name": "大力",
   "bias": "ad",
   "note": "基础属性增益，提升前期对线压制力。",
   "is_upgrade": false
  },
  {
   "name": "最终形态",
   "bias": "functional",
   "note": "棱彩海克斯，大幅改变英雄机制，需特定装备支撑。",
   "is_upgrade": false
  },
  {
   "name": "质变：棱彩阶",
   "bias": "functional",
   "note": "通用强力海克斯，提升整体数值上限。",
   "is_upgrade": false
  },
  {
   "name": "升级：贪欲九头蛇",
   "bias": "ad",
   "note": "清线与AOE伤害升级选项，适合多目标环境。",
   "is_upgrade": true
  },
  {
   "name": "飞身踢",
   "bias": "functional",
   "note": "提供额外位移与控制，弥补剑魔机动性不足。",
   "is_upgrade": false
  },
  {
   "name": "惊惧",
   "bias": "functional",
   "note": "控制类海克斯，增强留人能力。",
   "is_upgrade": false
  },
  {
   "name": "无尽大杀四方",
   "bias": "ad",
   "note": "击杀后刷新技能或提供增益，滚雪球利器。",
   "is_upgrade": false
  },
  {
   "name": "循环往复",
   "bias": "functional",
   "note": "减少技能冷却，提升连招频率。",
   "is_upgrade": false
  },
  {
   "name": "坦克引擎",
   "bias": "tank",
   "note": "基于最大生命值的伤害加成，半肉流首选。",
   "is_upgrade": false
  },
  {
   "name": "自适应防护",
   "bias": "tank",
   "note": "根据敌方伤害类型自动调整抗性，泛用性强。",
   "is_upgrade": false
  },
  {
   "name": "黎明使者的坚决",
   "bias": "tank",
   "note": "提供护盾与治疗增强，提升生存能力。",
   "is_upgrade": false
  }
 ],
 "opening_items": [
  {
   "combo": [
    "长剑",
    "考尔菲德的战锤"
   ],
   "when": "标准出门，兼顾攻击力与生命值，平滑过渡至星蚀/焚天。"
  },
  {
   "combo": [
    "鞋子",
    "考尔菲德的战锤"
   ],
   "when": "对面poke较多或需要移速拉扯时，优先保证走位空间。"
  }
 ],
 "build_paths": {
  "ad": {
   "items": [
    "星蚀",
    "焚天",
    "死亡之舞"
   ],
   "skill_order": "主Q副E",
   "note": "主流AD战士出装，利用星蚀的护盾与焚天的特效快速叠加层数，死亡之舞提供续航。"
  }
 },
 "component_rules": [
  {
   "item": "考尔菲德的战锤",
   "upgrade_to": "星蚀",
   "hold_when": "第一件大件合成前，作为核心组件持有。"
  },
  {
   "item": "长剑",
   "upgrade_to": "星蚀",
   "hold_when": "若选择先出小件过渡，保留长剑直至合成完整装备。"
  }
 ],
 "augment_item_links": [
  {
   "augment": "升级：死亡之舞",
   "shift": "优先做出死亡之舞而非其他防御装",
   "reason": "该海克斯直接强化核心防御装，使其成为必做项，改变出装顺序优先级。"
  },
  {
   "augment": "升级：焚天",
   "shift": "在星蚀之后立即合成焚天",
   "reason": "海克斯强化了焚天的特效收益，使其成为中期爆发与持续输出的关键节点。"
  },
  {
   "augment": "贪欲束缚",
   "shift": "偏向高吸血与持续作战装备",
   "reason": "海克斯机制鼓励长时间战斗，因此死亡之舞和渴血类效果的价值被放大。"
  }
 ],
 "skill_order": "主Q副E"
}
```
</details>

## 黑暗之女 —— ✅ 通过

- ✅ ①零幻觉：推荐字段装备名全部命中 DD 库　命中 13 个装备名
- ✅ ①零幻觉：海克斯名全部命中 229 名单　全部命中
- ✅ ②海克斯亲和 top10 方向一致（交集 10/10 ≥7）　model: ['仆从大师', '循环往复', '巫师式思考', '魔法飞弹', '尤里卡']... csv: ['仆从大师', '循环往复', '巫师式思考', '魔法飞弹', '尤里卡']...
- ✅ 观测：理由文字无未登记装备词（不计入通过线）　干净
- ✅ 观测：加点顺序与 OP.GG 一致（不计入通过线）　模型: 主Q副W / OP.GG: 主Q副W / 一致

<details><summary>模型输出 JSON</summary>

```json
{
 "augment_affinity": [
  {
   "name": "仆从大师",
   "bias": "functional",
   "note": "安妮被动晕眩配合召唤师技能或海克斯效果，增强控制链",
   "is_upgrade": false
  },
  {
   "name": "循环往复",
   "bias": "ap",
   "note": "高频Q技能重置冷却，极大提升爆发频率",
   "is_upgrade": false
  },
  {
   "name": "巫师式思考",
   "bias": "ap",
   "note": "增加法强收益，契合AP出装路线",
   "is_upgrade": false
  },
  {
   "name": "魔法飞弹",
   "bias": "ap",
   "note": "强化普攻附带魔法伤害，适合短手法师补刀与消耗",
   "is_upgrade": false
  },
  {
   "name": "尤里卡",
   "bias": "ap",
   "note": "提供额外法力值与技能急速，优化施法体验",
   "is_upgrade": false
  },
  {
   "name": "杀戮时间到了",
   "bias": "ad",
   "note": "击杀后获得属性，适合滚雪球打法",
   "is_upgrade": false
  },
  {
   "name": "超凡邪恶",
   "bias": "ap",
   "note": "直接提升法术强度，简单粗暴的增伤",
   "is_upgrade": false
  },
  {
   "name": "珠光护手",
   "bias": "ap",
   "note": "技能可暴击，安妮高倍率技能受益巨大",
   "is_upgrade": false
  },
  {
   "name": "术士果汁盒",
   "bias": "ap",
   "note": "提供治疗与护盾加成，增强生存能力",
   "is_upgrade": false
  },
  {
   "name": "三重射击",
   "bias": "ad",
   "note": "普攻特效，虽为AP英雄但部分流派可尝试混合输出",
   "is_upgrade": false
  },
  {
   "name": "质变：棱彩阶",
   "bias": "functional",
   "note": "升级已购装备至棱彩阶，需先有核心大件",
   "is_upgrade": true
  },
  {
   "name": "缩小引擎",
   "bias": "functional",
   "note": "参与击杀减少终极技能冷却，配合R刷新机制",
   "is_upgrade": false
  },
  {
   "name": "家园卫士",
   "bias": "functional",
   "note": "脱战加速，便于游走支援",
   "is_upgrade": false
  },
  {
   "name": "升级：中娅",
   "bias": "tank",
   "note": "将中娅沙漏升级为棱彩阶，提供更强保命能力",
   "is_upgrade": true
  },
  {
   "name": "纯粹主义者 - 术师",
   "bias": "ap",
   "note": "限制移动速度换取高额法强，适合站桩输出环境",
   "is_upgrade": false
  }
 ],
 "opening_items": [
  {
   "combo": [
    "复用型药水",
    "遗失的章节"
   ],
   "when": "标准开局，兼顾续航与法强成长"
  },
  {
   "combo": [
    "遗失的章节"
   ],
   "when": "对面poke较少或自信走位，最大化前期法强"
  }
 ],
 "build_paths": {
  "ap": {
   "items": [
    "残疫",
    "风暴狂涌",
    "影焰"
   ],
   "skill_order": "主Q副W",
   "note": "核心爆发流，利用残疫叠加层数，风暴狂涌提供穿透与移速，影焰补足斩杀线"
  },
  "ap_mobility": {
   "items": [
    "残疫",
    "海克斯科技火箭腰带",
    "影焰"
   ],
   "skill_order": "主Q副W",
   "note": "机动性流，火箭腰带提供进场手段，弥补安妮腿短缺陷，后续接影焰收割"
  }
 },
 "component_rules": [
  {
   "item": "遗失的章节",
   "upgrade_to": "残疫",
   "hold_when": "前期对线期，作为合成残疫的核心组件"
  },
  {
   "item": "残疫",
   "upgrade_to": "风暴狂涌",
   "hold_when": "中期团战前，若敌方魔抗较高则优先合成风暴狂涌"
  }
 ],
 "augment_item_links": [
  {
   "augment": "循环往复",
   "shift": "优先合成【残疫】并快速过渡到【风暴狂涌】",
   "reason": "循环往复大幅降低Q技能冷却，配合残疫的持续伤害和风暴狂涌的法穿，能实现极高频率的技能轰炸"
  },
  {
   "augment": "珠光护手",
   "shift": "在【残疫】后尽快补充【影焰】",
   "reason": "珠光护手使技能可暴击，安妮的大招和Q技能基础伤害高，配合影焰的斩杀效果能瞬间秒杀脆皮"
  },
  {
   "augment": "升级：中娅",
   "shift": "在【影焰】之后选择合成【中娅沙漏】并立即升级",
   "reason": "该海克斯专门用于升级中娅，因此必须确保出装有中娅沙漏这一件，以触发其棱彩阶的强力保命效果"
  }
 ],
 "skill_order": "主Q副W"
}
```
</details>

## 暴走萝莉 —— ✅ 通过

- ✅ ①零幻觉：推荐字段装备名全部命中 DD 库　命中 10 个装备名
- ✅ ①零幻觉：海克斯名全部命中 229 名单　全部命中
- ✅ ②海克斯亲和 top10 方向一致（交集 10/10 ≥7）　model: ['暴击飞弹', '更万用的瞄准镜', '升级：无尽之刃', '暴击律动', '双刀流']... csv: ['暴击飞弹', '更万用的瞄准镜', '升级：无尽之刃', '暴击律动', '双刀流']...
- ✅ 观测：理由文字无未登记装备词（不计入通过线）　干净
- ✅ 观测：加点顺序与 OP.GG 一致（不计入通过线）　模型: 主Q副W / OP.GG: 主Q副W / 一致

<details><summary>模型输出 JSON</summary>

```json
{
 "augment_affinity": [
  {
   "name": "暴击飞弹",
   "bias": "ad",
   "note": "核心输出海克斯，强化普攻与技能联动",
   "is_upgrade": false
  },
  {
   "name": "更万用的瞄准镜",
   "bias": "functional",
   "note": "提升射程与攻击速度，优化走A手感",
   "is_upgrade": false
  },
  {
   "name": "升级：无尽之刃",
   "bias": "ad",
   "note": "直接升级已购装备，大幅提升暴击伤害",
   "is_upgrade": true
  },
  {
   "name": "暴击律动",
   "bias": "ad",
   "note": "提供额外暴击率或攻速收益",
   "is_upgrade": false
  },
  {
   "name": "双刀流",
   "bias": "ad",
   "note": "棱彩级强力海克斯，显著增强持续输出能力",
   "is_upgrade": false
  },
  {
   "name": "灵魂虹吸",
   "bias": "functional",
   "note": "提供续航或吸血效果，增加生存能力",
   "is_upgrade": false
  },
  {
   "name": "最万用的瞄准镜",
   "bias": "functional",
   "note": "顶级射程/攻速加成，极大改善输出环境",
   "is_upgrade": false
  },
  {
   "name": "灵巧",
   "bias": "functional",
   "note": "提供韧性或移速，减少被控时间",
   "is_upgrade": false
  },
  {
   "name": "踢踏舞",
   "bias": "functional",
   "note": "移动时积累层数或提供特殊位移/加速效果",
   "is_upgrade": false
  },
  {
   "name": "台风",
   "bias": "functional",
   "note": "提供击退或控制效果，增强自保能力",
   "is_upgrade": false
  },
  {
   "name": "万用瞄准镜",
   "bias": "functional",
   "note": "基础射程/攻速加成，性价比高的前期选择",
   "is_upgrade": false
  },
  {
   "name": "双发快射",
   "bias": "ad",
   "note": "可能涉及多段射击或攻速爆发机制",
   "is_upgrade": false
  },
  {
   "name": "连拨击锤",
   "bias": "ad",
   "note": "连续攻击触发额外伤害或特效",
   "is_upgrade": false
  },
  {
   "name": "罪恶快感",
   "bias": "functional",
   "note": "击杀或助攻后提供增益，适合滚雪球",
   "is_upgrade": false
  },
  {
   "name": "易损",
   "bias": "functional",
   "note": "对敌人施加易损状态，提升团队整体伤害",
   "is_upgrade": false
  }
 ],
 "opening_items": [
  {
   "combo": [
    "暴风之剑",
    "生命药水"
   ],
   "when": "标准出门装，兼顾攻击力与基础续航"
  },
  {
   "combo": [
    "暴风之剑"
   ],
   "when": "激进换血或配合特定高爆发海克斯（如暴击飞弹）时省略药水以最大化属性"
  }
 ],
 "build_paths": {
  "ad": {
   "items": [
    "育恩塔尔荒野箭",
    "卢安娜的飓风",
    "无尽之刃"
   ],
   "skill_order": "主Q副W",
   "note": "基于OP.GG数据的核心AD路线，依赖暴击与分裂箭输出。鞋子推荐狂战士胫甲或暴食胫甲。"
  }
 },
 "component_rules": [
  {
   "item": "暴风之剑",
   "upgrade_to": "育恩塔尔荒野箭",
   "hold_when": "作为核心神话/传说装备的合成组件优先购买"
  },
  {
   "item": "暴风之剑",
   "upgrade_to": "无尽之刃",
   "hold_when": "若先出无尽路线，需保留暴风之剑作为合成件"
  }
 ],
 "augment_item_links": [
  {
   "augment": "升级：无尽之刃",
   "shift": "优先做出无尽之刃并立即使用海克斯升级",
   "reason": "该海克斯专门用于升级已购装备，能瞬间将普通无尽转化为强化版，大幅提前强势期"
  },
  {
   "augment": "暴击飞弹",
   "shift": "偏向攻速与暴击混合出装（如卢安娜的飓风+无尽）",
   "reason": "暴击飞弹通常依赖高频攻击触发，配合飓风的多目标攻击和无尽的暴击伤害可最大化DPS"
  },
  {
   "augment": "双刀流",
   "shift": "确保拥有足够的暴击率和攻速支撑双刀流机制",
   "reason": "作为棱彩海克斯，其收益往往与攻击频率或暴击次数挂钩，需通过育恩塔尔荒野箭等装备快速成型"
  }
 ],
 "skill_order": "主Q副W"
}
```
</details>