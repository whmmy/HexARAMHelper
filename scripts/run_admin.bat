@echo off
rem 以管理员身份启动 HexARAMHelper（20261003 皮城女警案实验）：
rem 游戏画面焦点期间的左键按压对三通道（LL 钩子/捕获子进程/局内轮询）
rem 全部不可见，而客户端/商店/任务栏点击正常——游戏进程提权(High)运行、
rem 本工具 Medium，UIPI/驱动级输入过滤是最后候选。本启动器把本工具
rem 提权到 High 再观察一局：台账有记录 = 提权隔离实锤并解决。
cd /d "%~dp0.."
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0..\.venv\Scripts\pythonw.exe' -ArgumentList 'app.py' -WorkingDirectory '%~dp0..' -Verb RunAs"
