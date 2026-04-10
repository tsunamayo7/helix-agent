$action = New-ScheduledTaskAction -Execute 'C:\Program Files\Python312\pythonw.exe' -Argument '"C:\Development\tools\helix-agent\scripts\dept_ft_advisor.py" --export' -WorkingDirectory 'C:\Development\tools\helix-agent'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "5:00"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName 'Helix-FTAdvisor' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
(Get-ScheduledTask -TaskName 'Helix-FTAdvisor').State
