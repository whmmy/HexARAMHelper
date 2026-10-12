"""局内点击捕获子进程（20261002 虚空之女案）。

主进程内两条通道（LL 钩子 + GetAsyncKeyState 轮询）在海克斯选卡瞬间
双双零捕获，而非选卡期点击（死后商店点击等）双双正常；且选卡瞬间主
进程明明活着（复盘 timeline 数秒一条）。同时解释"钩子丢事件+轮询零
采样+进程存活"的候选只剩：主进程内部 GIL/调度饥饿（检测器 OCR 风暴
期钩子回调拿不到 GIL 被系统按超时跳过、轮询线程排不上队），或系统层
在选卡期过滤输入。本子进程 = 修复候选 + 鉴别仪器：独立解释器独立
GIL，NORMAL 优先级（父进程 BELOW_NORMAL 会被子进程继承，必须显式抬
回——否则它会在选卡期和主进程一起饿死，白装），~0 CPU 的
GetAsyncKeyState 边沿采样，把 (ts, x, y) 追加到会话文件由主进程 tail。
若它能看到选卡点击 → 记账修复成立；若它也看不到 → 系统层过滤，
复盘 JSON/日志再添铁证。

20261003 皮城女警案升级：子进程全程活着且采样节奏正常（CPU 开销吻合）
仍看不到选卡按压——加心跳行（每 5s 一条 "hb <ts>"，tail 测停滞=子进程
被挂起的直接证据）与退出报文（"bye <ts>"，区分自行退出/被杀）。
Win32 调用显式原型（默认 c_int 截断 64 位句柄/指针 = 同 CallNextHookEx
溢出类病）。

只读约束不变：观察输入、不注入、不改鼠标行为。协议：首行
"ready <ts>"；心跳行 "hb <ts>"；点击行 "<ts> <x> <y>"（time.time()
墙钟，主进程换算后参与统一去重）；退出行 "bye <ts>"。父进程死亡
（WaitForSingleObject）即自动退出。
用法：python click_watcher.py <parent_pid> <out_path>
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

NORMAL_PRIORITY_CLASS = 0x00000020
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
HB_INTERVAL = 5.0   # 心跳周期（s）：tail 据此判"子进程被挂起"

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.WaitForSingleObject.restype = wt.DWORD
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
_k32.SetPriorityClass.restype = wt.BOOL
_k32.SetPriorityClass.argtypes = [ctypes.c_void_p, wt.DWORD]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.GetCurrentProcess.argtypes = []
_u32.GetAsyncKeyState.restype = ctypes.c_short
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetCursorPos.restype = wt.BOOL
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]


def main():
    parent_pid = int(sys.argv[1])
    out_path = sys.argv[2]
    # 抬回 NORMAL：捕获线程要与系统输入路径赛跑，BELOW_NORMAL（继承自
    # 父进程）在游戏 High 优先级满载时会被整段饿过按压窗。采样本身
    # ~0 CPU（50 次/秒微秒级 API），不违背"突发计算不与游戏抢核"。
    _k32.SetPriorityClass(_k32.GetCurrentProcess(), NORMAL_PRIORITY_CLASS)
    h_parent = _k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not h_parent:
        return   # 父进程已不在，无需值守
    with open(out_path, "a", encoding="ascii", buffering=1) as out:
        out.write(f"ready {time.time():.3f}\n")
        prev_down = False
        last_hb = 0.0
        pt = wt.POINT()
        try:
            while True:
                # 等父进程死亡或 20ms 超时——看门狗与轮询节拍共用一次系统调用
                if _k32.WaitForSingleObject(h_parent, 20) != WAIT_TIMEOUT:
                    return
                now = time.time()
                if now - last_hb >= HB_INTERVAL:
                    last_hb = now
                    out.write(f"hb {now:.3f}\n")
                st = _u32.GetAsyncKeyState(0x01)
                down = bool(st & 0x8000) or bool(st & 0x0001)
                if down and not prev_down:
                    if _u32.GetCursorPos(ctypes.byref(pt)):
                        out.write(f"{now:.3f} {pt.x} {pt.y}\n")
                prev_down = down
        finally:
            try:
                out.write(f"bye {time.time():.3f}\n")
            except Exception:
                pass


if __name__ == "__main__":
    main()
