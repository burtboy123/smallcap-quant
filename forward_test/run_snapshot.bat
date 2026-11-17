@echo off
REM Wrapper for the isolated forward-test daily snapshot (scheduled task target).
REM Uses the project venv python, correct cwd + PYTHONPATH, appends timestamped
REM stdout/stderr to the ACTIVE period's snapshot_log.txt. Touches nothing outside
REM forward_test/.
REM
REM Active period is passed as %1 (default 2026Q4b — the live test).
REM 2026Q3_parallel CONCLUDED 2026-09-14 (evaluated/frozen). 2026Q4 was RETIRED
REM 2026-09-29 for tracking an un-investable book and is preserved as
REM 2026Q4_retired. Neither is snapshotted any more.
setlocal
set "ROOT=%~dp0.."
set PERIOD=%~1
if "%PERIOD%"=="" set PERIOD=2026Q4b
set LOG=%ROOT%\forward_test\%PERIOD%\snapshot_log.txt
cd /d "%ROOT%"
set PYTHONPATH=%ROOT%
>> "%LOG%" echo ======================================================================
>> "%LOG%" echo [%DATE% %TIME%] snapshot start (period %PERIOD%)
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\forward_test\snapshot.py" --period %PERIOD% >> "%LOG%" 2>&1
REM Capture python's exit code IMMEDIATELY. The redirected echo below runs a
REM command of its own and resets ERRORLEVEL, so reading %ERRORLEVEL% after it
REM reported 0 for a crashed snapshot -- Task Scheduler recorded LastTaskResult=0
REM while snapshot.py was exiting 1. That is how 9 days of silent failure went
REM unnoticed. Proven: with the trailing echo, python exit 1 -> wrapper exit 0.
set RC=%ERRORLEVEL%
>> "%LOG%" echo [%DATE% %TIME%] snapshot end (exit %RC%)
REM Early-warning risk watch on tonight's data (monitoring/risk_watch.py): emails
REM drawdowns, single-name drops and sector drift BEFORE a kill-switch fires. It runs
REM after RC is captured and writes to its own log, so it can never change this job's
REM exit code or the "snapshot end" line the health check reads.
if not exist "%ROOT%\monitoring\state" mkdir "%ROOT%\monitoring\state"
"%ROOT%\venv\Scripts\python.exe" -m monitoring.risk_watch >> "%ROOT%\monitoring\state\risk_watch_log.txt" 2>&1
REM Propagate it, so LastTaskResult is a signal rather than a constant.
endlocal & exit /b %RC%
