$action = New-ScheduledTaskAction -Execute 'C:\Program Files\Python312\pythonw.exe' -Argument '"C:\Users\tomot\.claude\hooks\timeout_auto_approve.py" --periodic-check'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration (New-TimeSpan -Days 365)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName 'Helix-TimeoutChecker' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
(Get-ScheduledTask -TaskName 'Helix-TimeoutChecker').State
