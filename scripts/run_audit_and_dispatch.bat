@echo off
chcp 65001 >nul 2>&1
REM System Audit -> Anomaly Dispatch -> Self-Heal chain execution
REM Called from Helix-SystemAudit task (every 6 hours)

set "PY=C:\Program Files\Python312\python.exe"
set "SCRIPTS=C:\Development\tools\helix-agent\scripts"

cd /d C:\Development\tools\helix-agent
REM 1. Audit system (6 layers + 3 extended layers)
"%PY%" "%SCRIPTS%\system_auditor.py" --quick >nul 2>&1
REM 2. Push HIGH/CRITICAL findings to queue + Discord notify
"%PY%" "%SCRIPTS%\anomaly_dispatcher.py" >nul 2>&1
REM 3. Auto-heal fixable anomalies (service/task/daemon/file)
"%PY%" "%SCRIPTS%\env_self_heal.py" >nul 2>&1
