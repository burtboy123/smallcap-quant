@echo off
REM Scheduled-task wrapper for the pipeline health check.
REM
REM Runs WEEKLY, Monday 15:30, after the 15:00 price refresh (it was nightly 23:50 until
REM 2026-10-04). Order runs execute the same checks themselves before trading. See
REM monitoring/README.md for the schedule rationale.
REM
REM EXIT CODE IS THE POINT. This wrapper captures python's exit code immediately and
REM re-raises it through endlocal, so Task Scheduler's LastTaskResult is a real signal.
REM The snapshot wrapper got this wrong: it read %ERRORLEVEL% after a redirected echo,
REM which resets it, and reported LastTaskResult=0 while snapshot.py was exiting 1 --
REM that is how nine days of silent failure went unnoticed. Do not add a command
REM between the python call and `set RC`.
REM
REM This file must stay CRLF. cmd.exe mis-parses an LF-only .bat and every REM line
REM above would emit "'M' is not recognized" to a stderr nothing captures.
REM .gitattributes pins it.
setlocal
set "ROOT=%~dp0.."
set LOG=%ROOT%\monitoring\state\health_log.txt
cd /d "%ROOT%"
set PYTHONPATH=%ROOT%
REM A scheduled task inherits no console codepage; the report contains em-dashes.
set PYTHONIOENCODING=utf-8
if not exist "%ROOT%\monitoring\state" mkdir "%ROOT%\monitoring\state"
>> "%LOG%" echo ======================================================================
>> "%LOG%" echo [%DATE% %TIME%] backup start
REM Weekly backup first, so backup_fresh below sees today's zip. Its failure shows up
REM as a backup_fresh FAIL in the check, so it does not need to touch RC here.
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\scripts\backup.py" >> "%LOG%" 2>&1
>> "%LOG%" echo [%DATE% %TIME%] health check start
"%ROOT%\venv\Scripts\python.exe" -m monitoring.health_check -v --weekly-report >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
>> "%LOG%" echo [%DATE% %TIME%] health check end (exit %RC%)
endlocal & exit /b %RC%
