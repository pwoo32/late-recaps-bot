@echo off
rem Daily streamer run for Task Scheduler. YOUTUBE_API_KEY comes from the user environment; no keys here.
cd /d "%~dp0"
set PYTHONUTF8=1
set CUDA_VISIBLE_DEVICES=-1
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\Python\Python314\Scripts;%PATH%"
rem Review yesterday's clips first so today's picks and scripts use what it learned.
python review_agent.py >> review.log 2>&1
python channel_builder.py run --publish --niche streamer >> builder.log 2>&1
