@echo off
REM Scheduled-task wrapper for the daily price refresh (QuantPricesUpdate).
REM
REM Runs data/prices_updater.py for the full universe so the prices table -- which
REM feeds ADV and 20-day sigma into order sizing -- never drifts stale again. It went
REM 8 sessions behind in September because nothing ran it automatically.
REM
REM Scheduled MONDAYS 15:00 Finland time (weekly since 2026-10-04; Monday's run
REM picks up Friday's close): 08:00 New York, after the previous close is
REM final and before the open, so an execution run that day sizes off current data.
REM
REM EXIT CODE IS THE POINT. prices_updater exits 1 when every batch came back empty.
REM Capture it immediately and re-raise it through endlocal so LastTaskResult is a real
REM signal. Do not add a command between the python call and `set RC`: a redirected
REM echo resets ERRORLEVEL, which is how the snapshot wrapper hid nine days of failure.
REM
REM This file must stay CRLF (.gitattributes pins it).
setlocal
set "ROOT=%~dp0.."
set LOG=%ROOT%\monitoring\state\prices_update_log.txt
cd /d "%ROOT%"
set PYTHONPATH=%ROOT%
set PYTHONIOENCODING=utf-8
if not exist "%ROOT%\monitoring\state" mkdir "%ROOT%\monitoring\state"
>> "%LOG%" echo ======================================================================
>> "%LOG%" echo [%DATE% %TIME%] prices update start
"%ROOT%\venv\Scripts\python.exe" -m data.prices_updater --lookback-days 35 >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
>> "%LOG%" echo [%DATE% %TIME%] prices update end (exit %RC%)
endlocal & exit /b %RC%
