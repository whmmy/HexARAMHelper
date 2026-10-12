"""一次性诊断：查询指定进程与自身的完整性级别（TokenIntegrityLevel）。

用法：python query_il.py <pid> [<pid> ...]
不传 pid 则列出候选进程（LoL 全家桶 + pythonw）+ 自身。
只读查询（PROCESS_QUERY_LIMITED_INFORMATION + TOKEN_QUERY），不注入不改状态。
"""
import ctypes
import ctypes.wintypes as wt
import sys

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64 位下凡返回指针的 API 必须显式 restype，默认 c_int 截断指针 = 段错误
# （与 LL 钩子 CallNextHookEx 溢出同类病）
advapi32.GetSidSubAuthorityCount.restype = ctypes.c_void_p
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthority.restype = ctypes.c_void_p
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [
    wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetCurrentProcessId.restype = wt.DWORD
advapi32.OpenProcessToken.restype = wt.BOOL
advapi32.OpenProcessToken.argtypes = [
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.HANDLE)]


def integrity_level(pid: int):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return f"OpenProcess失败 err={ctypes.get_last_error()}"
    try:
        tok = wt.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return f"OpenProcessToken失败 err={ctypes.get_last_error()}"
        try:
            ret_len = wt.DWORD()
            advapi32.GetTokenInformation(tok, TokenIntegrityLevel, None, 0,
                                         ctypes.byref(ret_len))
            n = ret_len.value
            buf = ctypes.create_string_buffer(n)
            if not advapi32.GetTokenInformation(tok, TokenIntegrityLevel,
                                                buf, n, ctypes.byref(ret_len)):
                return f"GetTokenInformation失败 err={ctypes.get_last_error()}"
            # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label; }
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            count = advapi32.GetSidSubAuthorityCount(ctypes.c_void_p(sid_ptr))
            count = ctypes.cast(count, ctypes.POINTER(ctypes.c_ubyte)).contents.value
            rid_ptr = advapi32.GetSidSubAuthority(ctypes.c_void_p(sid_ptr), count - 1)
            rid = ctypes.cast(rid_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
            rid = ctypes.c_uint32(rid).value  # 高位存的是 0xXXXX0000
            level = rid >> 16
            names = {0x0: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
                     0x2100: "Medium Plus", 0x3000: "High", 0x4000: "System"}
            return f"IL=0x{rid:08x} ({names.get(level, '?')}, rid={level})"
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(h)


if __name__ == "__main__":
    me = kernel32.GetCurrentProcessId()
    print(f"self(pid={me}): {integrity_level(me)}")
    pids = [int(a) for a in sys.argv[1:]]
    if not pids:
        import re
        out = ctypes.create_unicode_buffer(4096)
        # 枚举所有进程名→pid，筛 LoL 全家桶 + pythonw
        PSAPI = ctypes.WinDLL("psapi")
        need = wt.DWORD()
        arr = (wt.DWORD * 4096)()
        PSAPI.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr),
                            ctypes.byref(need))
        n = need.value // ctypes.sizeof(wt.DWORD)
        for i in range(n):
            pid = arr[i]
            if not pid:
                continue
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                continue
            try:
                if kernel32.QueryFullProcessImageNameW(
                        h, 0, out, ctypes.byref(wt.DWORD(4096))):
                    name = out.value.rsplit("\\", 1)[-1].lower()
                    if re.match(r"^(league|tcls|wegame|laclient|riot|pythonw?\.exe)",
                                name):
                        print(f"{name}(pid={pid}): {integrity_level(pid)}")
            finally:
                kernel32.CloseHandle(h)
    else:
        for pid in pids:
            print(f"pid={pid}: {integrity_level(pid)}")
