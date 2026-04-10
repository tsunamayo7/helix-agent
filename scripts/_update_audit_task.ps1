$action = New-ScheduledTaskAction -Execute 'C:\Development\tools\helix-agent\scripts\run_audit_and_dispatch.bat' -WorkingDirectory 'C:\Development\tools\helix-agent'
Set-ScheduledTask -TaskName 'Helix-SystemAudit' -Action $action | Out-Null
(Get-ScheduledTask -TaskName 'Helix-SystemAudit').Actions | Format-List Execute,Arguments
