' Runs the cloud watchlist sync with no window. Started by Windows Task Scheduler.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\DrShah\cloud"
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\DrShah\cloud\sync_to_cloud.ps1""", 0, True)
