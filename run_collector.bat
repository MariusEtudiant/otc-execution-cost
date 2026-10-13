@echo off
rem Keeps the collector alive: restarts it if it ever exits. Create data\STOP to stop both.
cd /d "%~dp0"
if not exist data mkdir data
:loop
if exist data\STOP goto end
python collector\collect.py >> data\supervisor.log 2>&1
echo %date% %time% collector exited, restarting in 10s >> data\supervisor.log
ping -n 11 127.0.0.1 >nul
goto loop
:end
echo %date% %time% STOP file found, supervisor exiting >> data\supervisor.log
