@echo off
rem Starts the ZK Device Manager agent and restarts it if it ever stops.
rem Put a shortcut to this in shell:startup, or run it from Task Scheduler
rem "At log on" / "At startup". The PC must be on the devices' network and
rem must not run a VPN that blocks the local network.
rem
rem Every stop is written to logs\agent.log with its exit code, so a crash
rem shows up there rather than as a silent gap. -1073741819 (C0000005) is a
rem crash inside native code, such as the ZKTeco SDK: see logs\crash.log.
cd /d %~dp0
if not exist logs mkdir logs
:loop
python zk_agent.py
echo %date% %time%  WARNING  Agent process stopped with exit code %errorlevel%; restarting in 30 seconds>> logs\agent.log
echo Agent stopped (exit %errorlevel%). Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
