Get-Process python,pythonw -ErrorAction SilentlyContinue | ForEach-Object {
    $proc = $_
    try {
        $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)").CommandLine
        if ($cmdline -like "*dashboard_server*") {
            Stop-Process -Id $proc.Id -Force
            Write-Output "stopped PID $($proc.Id)"
        }
    } catch {}
}
