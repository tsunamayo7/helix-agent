foreach ($n in @('Helix-ContradictionCheck','Helix-QdrantDedup')) {
    $i = Get-ScheduledTaskInfo -TaskName $n
    Write-Output "$n : LastRun=$($i.LastRunTime) Result=$($i.LastTaskResult)"
}
