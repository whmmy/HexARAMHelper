"""临时诊断：对指定 PID 按线程采样 CPU，输出每线程 CPU 时间与名字。

用法: python experiments/cpu_monitor.py <pid> <秒数>
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

import psutil

THREAD_QUERY_INFORMATION = 0x0040
_GetThreadDescription = ctypes.windll.kernel32.GetThreadDescription
_GetThreadDescription.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
_GetThreadDescription.restype = ctypes.c_long


def thread_name(tid):
    """打开线程句柄读描述名（CPython 3.8+ 会 SetThreadDescription）"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return "?"
    try:
        name = wt.LPWSTR()
        hr = _GetThreadDescription(ctypes.c_void_p(h), ctypes.byref(name))
        return name.value if hr == 0 and name.value else "?"
    finally:
        k32.CloseHandle(h)


def main(pid, secs):
    p = psutil.Process(pid)
    t0 = {t.id: (t.user_time, t.system_time) for t in p.threads()}
    samples = []
    deadline = time.time() + secs
    while time.time() < deadline:
        time.sleep(1.0)
        samples.append(p.cpu_percent(None))
    t1 = {t.id: (t.user_time, t.system_time) for t in p.threads()}

    total_pct = sum(samples) / len(samples)
    ncpu = psutil.cpu_count()
    print(f"\n=== 进程平均 CPU: {total_pct:.1f}% （逻辑核 {ncpu}，峰值 {max(samples):.0f}%）===")

    rows = []
    for tid, (u, s) in t1.items():
        u0, s0 = t0.get(tid, (0.0, 0.0))
        busy = (u - u0) + (s - s0)
        rows.append((busy, tid, thread_name(tid)))
    rows.sort(reverse=True)
    print(f"{'线程名':<20} {'tid':>8} {'窗口内CPU时间s':>12} {'≈占总CPU':>9}")
    for busy, tid, name in rows[:15]:
        pct = busy / secs / ncpu * 100
        print(f"{name:<20} {tid:>8} {busy:>14.2f} {pct:>8.1f}%")


if __name__ == "__main__":
    main(int(sys.argv[1]), float(sys.argv[2]))
