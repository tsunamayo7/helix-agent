' run_hidden.vbs - Run Python scripts without terminal window
' Usage: wscript run_hidden.vbs <script.py> [args...]
'
' Task Scheduler:
'   wscript C:\Development\tools\helix-agent\scripts\run_hidden.vbs supervisor.py

Dim objShell, scriptDir, scriptName, args, cmd
Set objShell = CreateObject("WScript.Shell")

scriptDir = "C:\Development\tools\helix-agent\scripts"

' Get script name from arguments
If WScript.Arguments.Count > 0 Then
    scriptName = WScript.Arguments(0)
Else
    WScript.Quit 1
End If

' Build additional arguments
args = ""
Dim i
For i = 1 To WScript.Arguments.Count - 1
    args = args & " " & WScript.Arguments(i)
Next

' Run with pythonw.exe (no window)
cmd = "pythonw """ & scriptDir & "\" & scriptName & """" & args

' 0 = vbHide, True = wait for completion
objShell.Run cmd, 0, True

Set objShell = Nothing
