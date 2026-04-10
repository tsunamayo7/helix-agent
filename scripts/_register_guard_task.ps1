$action = New-ScheduledTaskAction -Execute 'C:\Program Files\Python312\pythonw.exe' -Argument '"C:\Development\tools\helix-agent\scripts\critical_files_guard.py"' -WorkingDirectory 'C:\Development\tools\helix-agent'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 365)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName 'Helix-CriticalGuard' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
(Get-ScheduledTask -TaskName 'Helix-CriticalGuard').State
