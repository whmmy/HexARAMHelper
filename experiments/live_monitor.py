"""临时真机监控：轮询 Live API，把对局事件化记录到 log/_monitor.log

- 我方锚定：activePlayer.summonerName/riotIdGameName → allPlayers 匹配（国服 activePlayer 缺英雄名/装备，用 allPlayers 行补全）
- 记录：开局阵容、我方/敌方 KDA/等级/装备变化、我方加点与金币、周期心跳
- 失联：视为局终，持续观察 150s 后退出（配合局终复盘日志核对）
"""
import datetime
import json
import os
import ssl
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGF = os.path.join(BASE, "log", "_monitor.log")
URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

HEARTBEAT_S = 120
POLL_S = 15


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch():
    with urllib.request.urlopen(URL, timeout=6, context=CTX) as r:
        return json.load(r)


def champ(p):
    return p.get("championName") or p.get("rawChampionName") or "?"


def item_names(p):
    out = []
    for it in p.get("items") or []:
        if it.get("itemID"):
            out.append(it.get("displayName") or str(it.get("itemID")))
    return out


def kda(s):
    return (s.get("kills") or 0, s.get("deaths") or 0, s.get("assists") or 0)


def fmt_t(sec):
    return f"T+{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def main():
    log("监控启动（15s 轮询 / 120s 心跳）")
    prev = {}          # champion -> {kda, level, items}
    prev_me = {}       # 我的快照
    prev_abil = {}
    last_hb = 0.0
    fail = 0
    opened = False

    while True:
        try:
            d = fetch()
            fail = 0
        except Exception as e:
            fail += 1
            if fail == 1:
                log(f"Live API 失联（{type(e).__name__}）—— 局终或游戏退出，尾声观察中")
            if fail >= 10:
                log("Live API 持续失联 ~150s，监控结束")
                return
            time.sleep(POLL_S)
            continue

        gd = d.get("gameData", {})
        gt = gd.get("gameTime") or 0
        ap = d.get("activePlayer", {})
        players = d.get("allPlayers", [])

        if not opened:
            opened = True
            order = "、".join(champ(p) for p in players if p.get("team") == "ORDER")
            chaos = "、".join(champ(p) for p in players if p.get("team") == "CHAOS")
            log(f"开局确认 mode={gd.get('gameMode')} 时长{fmt_t(gt)} | ORDER: {order} | CHAOS: {chaos}")

        # 锚定"我"
        me = None
        key = (ap.get("riotIdGameName") or ap.get("summonerName") or "").strip()
        if key:
            for p in players:
                if key in (p.get("riotIdGameName"), p.get("summonerName"), p.get("riotId")):
                    me = p
                    break
        if me is None:
            log(f"警告：未能锚定本地玩家（key={key!r}），本轮回退用等级+金币模糊匹配")
            cands = [p for p in players if p.get("level") == ap.get("level")]
            me = cands[0] if len(cands) == 1 else None

        # 我的事件
        if me is not None:
            snap = {
                "kda": kda(me.get("scores", {})),
                "level": me.get("level"),
                "items": item_names(me),
                "gold": int(ap.get("currentGold") or 0),
                "dead": me.get("isDead"),
            }
            if prev_me:
                if snap["kda"] != prev_me["kda"]:
                    log(f"{fmt_t(gt)} 我({champ(me)}) KDA {prev_me['kda']} → {snap['kda']}")
                if snap["level"] != prev_me["level"]:
                    log(f"{fmt_t(gt)} 我 升级 lv{prev_me['level']}→{snap['level']}")
                if snap["items"] != prev_me["items"]:
                    add = [x for x in snap["items"] if x not in prev_me["items"]]
                    rem = [x for x in prev_me["items"] if x not in snap["items"]]
                    log(f"{fmt_t(gt)} 我 装备变化 +{add} -{rem} | 现有: {snap['items']}")
                if snap["gold"] - prev_me["gold"] >= 400:
                    log(f"{fmt_t(gt)} 我 金币 {prev_me['gold']}→{snap['gold']}")
                if snap["dead"] and not prev_me.get("dead"):
                    log(f"{fmt_t(gt)} 我 阵亡（{champ(me)}，等待复活）")
            abil = {k: v.get("abilityLevel") for k, v in (ap.get("abilities") or {}).items()}
            if abil != prev_abil and prev_abil:
                diff = {k: (prev_abil.get(k), v) for k, v in abil.items() if prev_abil.get(k) != v}
                if diff:
                    log(f"{fmt_t(gt)} 我 加点变化 {diff} | 当前 {abil}")
            prev_abil = abil
            prev_me = snap

        # 全员事件
        for p in players:
            name = champ(p)
            s = p.get("scores", {})
            cur = {"kda": kda(s), "level": p.get("level"), "items": item_names(p)}
            old = prev.get(name)
            if old:
                if cur["kda"] != old["kda"]:
                    ok, od, oa = old["kda"]
                    k, de, a = cur["kda"]
                    ev = []
                    if k > ok:
                        ev.append("击杀")
                    if de > od:
                        ev.append(f"被击杀{'' if not p.get('isDead') else '(阵亡中)'}")
                    if a > oa:
                        ev.append("助攻")
                    if ev:
                        log(f"{fmt_t(gt)} {name} {'/'.join(ev)} → K{k}/D{de}/A{a}")
                if cur["level"] != old["level"]:
                    log(f"{fmt_t(gt)} {name} 升级 lv{old['level']}→{cur['level']}")
                if cur["items"] != old["items"]:
                    add = [x for x in cur["items"] if x not in old["items"]]
                    if add:
                        log(f"{fmt_t(gt)} {name} 购买 +{add}")
            prev[name] = cur

        # 心跳
        if time.time() - last_hb >= HEARTBEAT_S:
            last_hb = time.time()
            me_txt = "?"
            if me is not None:
                s = me.get("scores", {})
                me_txt = (
                    f"{champ(me)} {s.get('kills')}/{s.get('deaths')}/{s.get('assists')} "
                    f"lv{me.get('level')} 金币{prev_me.get('gold', '?')} 装备{len(prev_me.get('items', []))}件"
                )
            board = " | ".join(
                f"{champ(p)} {p.get('scores', {}).get('kills')}/{p.get('scores', {}).get('deaths')}"
                f"/{p.get('scores', {}).get('assists')} lv{p.get('level')}"
                for p in players
            )
            log(f"心跳 {fmt_t(gt)} 我: {me_txt} || 全场: {board}")

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
