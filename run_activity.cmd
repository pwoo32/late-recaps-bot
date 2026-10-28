@echo off
rem Evening activity run for Task Scheduler: schedule check, comment replies, first comments, community drafts.
cd /d "%~dp0"
set PYTHONUTF8=1
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\Python\Python314\Scripts;%PATH%"
python channel_activity_agent.py --post --notify >> activity.log 2>&1
