@echo off
rem Starts the ZK Device Manager agent and restarts it if it ever stops.
rem Put a shortcut to this in shell:startup, or run it from Task Scheduler
rem "At log on" / "At startup". The PC must be on the devices' network and
rem must not run a VPN that blocks the local network.
cd /d %~dp0
:loop
python zk_agent.py
echo Agent stopped (exit %errorlevel%). Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
