<#
.SYNOPSIS
    Stops the Stage 1 legacy governance stack started by run_legacy.ps1.
.DESCRIPTION
    Finds headless python.exe processes running this repo's app.py under one
    of the legacy service folders and stops them.
#>

$root = $PSScriptRoot
$names = @('mcp-minierp-orders', 'mcp-minierp-finance', 'mcp-minierp-accounts', 'mcp-minierp-shipments', 'gateway')

$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object {
        $cmd = $_.CommandLine
        $cmd -and ($names | Where-Object { $cmd -like "*$root\$_\app.py*" })
    }

if (-not $procs) {
    Write-Host "No running legacy service processes found."
    return
}

foreach ($p in $procs) {
    Write-Host "Stopping python.exe (PID $($p.ProcessId)): $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "Legacy stack stopped."
