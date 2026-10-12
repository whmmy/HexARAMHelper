import ctypes, os
import ctypes.wintypes as wt
out = open('log/elev_probe.txt', 'w', encoding='utf-8')
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
a32 = ctypes.WinDLL("advapi32", use_last_error=True)
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcessToken = a32.OpenProcessToken
a32.OpenProcessToken.restype = wt.BOOL
a32.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
a32.GetTokenInformation.restype = wt.BOOL
a32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                    wt.DWORD, ctypes.POINTER(wt.DWORD)]
h = k32.OpenProcess(0x1000, False, k32.GetCurrentProcessId())
tok = wt.HANDLE()
a32.OpenProcessToken(h, 0x0008, ctypes.byref(tok))
elev = wt.DWORD(0)
n = wt.DWORD(0)
ok1 = a32.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(n))
# 完整性级别
k32.GetSidSubAuthorityCount.restype = ctypes.c_void_p
k32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
k32.GetSidSubAuthority.restype = ctypes.c_void_p
k32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]
need = wt.DWORD(0)
a32.GetTokenInformation(tok, 25, None, 0, ctypes.byref(need))
buf = ctypes.create_string_buffer(need.value)
a32.GetTokenInformation(tok, 25, buf, need.value, ctypes.byref(need))
sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
cnt = ctypes.cast(k32.GetSidSubAuthorityCount(sid), ctypes.POINTER(ctypes.c_ubyte)).contents.value
rid = ctypes.cast(k32.GetSidSubAuthority(sid, cnt-1), ctypes.POINTER(wt.DWORD)).contents.value
out.write(f"TokenElevation call ok={ok1} elev={elev.value} | IL=0x{rid:x} | exe={sys_exec if (sys_exec:=os.environ.get('VIRTUAL_ENV')) else 'no-venv-env'}\n")
out.close()
