# Starts whatever is down for the Non-Sports Loan Builder. Idempotent - safe to
# run any time. Backend :8001, frontend :5174 (the athlete builder's
# credit-memo-builder uses :8000 / :5173 and is never touched by this script).
$root = Split-Path -Parent $PSScriptRoot

function Test-Port($port) {
  try { (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded }
  catch { $false }
}

# --- backend :8001 ---------------------------------------------------------
if (-not (Test-Port 8001)) {
  Start-Process -FilePath "$root\backend\.venv\Scripts\python.exe" `
    -ArgumentList "run_local.py" -WorkingDirectory "$root\backend" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$root\logs\backend.out.log" `
    -RedirectStandardError  "$root\logs\backend.err.log"
  "started backend :8001"
} else { "backend :8001 already up" }

# --- frontend :5174 --------------------------------------------------------
# Vite will NOT survive a plain Start-Process (the npm.cmd -> node chain is not
# truly detached and dies with the launching shell), and a plain WMI Create
# leaves a visible console window that is easy to close by accident - which
# kills Vite and makes the browser show "Failed to fetch". Hidden WMI Create.
if (-not (Test-Port 5174)) {
  $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[UInt16]0}
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "cmd.exe /c `"$root\logs\start-frontend.bat`""
    ProcessStartupInformation = $startup
  } | Out-Null
  "started frontend :5174"
} else { "frontend :5174 already up" }
