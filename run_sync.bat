@echo off
REM ATC Race Sync - nightly wrapper invoked by Windows Task Scheduler.
REM
REM %~dp0 is this file's folder (the repo root), so the task works no matter
REM where it's launched from or where the repo was cloned.
REM
REM The sync writes its own structured history to logs\sync.log (normal runs and
REM handled errors). This wrapper additionally tees console + tracebacks to
REM logs\scheduler.out so catastrophic failures (wrong Python, uncaught errors)
REM are still captured for a headless, unattended run.

cd /d "%~dp0"
if not exist logs mkdir logs

REM Prefer the Windows 'py' launcher; fall back to 'python' on PATH.
where /q py
if %errorlevel%==0 (
    py src\sync_races.py >> logs\scheduler.out 2>&1
) else (
    python src\sync_races.py >> logs\scheduler.out 2>&1
)

exit /b %errorlevel%
