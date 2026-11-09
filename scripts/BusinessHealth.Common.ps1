# Local metadata only. This helper never starts/restarts business services.
function Invoke-BusinessHealthCheck([string]$Root,[switch]$Manual) {
    $process=$null
    try {
        $python=Join-Path $Root 'runtime\astrbot-venv\Scripts\python.exe'
        $checker=Join-Path $Root 'scripts\CheckBusinessHealth.py'
        if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $checker -PathType Leaf)) {
            Write-Warning 'Business health check unavailable; QQ connection alone does not verify Heybox or Bilibili.'
            return
        }
        $mode=if($Manual){'--check'}else{'--after-restart'}
        $info=New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName=$python
        $info.Arguments='-B "'+$checker+'" --root "'+$Root+'" '+$mode
        $info.WorkingDirectory=$Root
        $info.UseShellExecute=$false
        $info.CreateNoWindow=$true
        $info.RedirectStandardOutput=$true
        $info.RedirectStandardError=$true
        $info.StandardOutputEncoding=[Text.UTF8Encoding]::new($false)
        $info.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
        $info.EnvironmentVariables['PYTHONUTF8']='1'
        $process=New-Object System.Diagnostics.Process
        $process.StartInfo=$info
        if (!$process.Start()) { throw 'business_health_child_not_started' }
        $outputTask=$process.StandardOutput.ReadToEndAsync()
        $errorTask=$process.StandardError.ReadToEndAsync()
        if (!$process.WaitForExit(20000)) {
            # Only terminate the exact checker handle created above, never QQ.
            $process.Kill()
            $null=$process.WaitForExit(2000)
            Write-Warning 'Business health check timed out; state remains unverified. No business action was retried.'
            return
        }
        $output=$outputTask.GetAwaiter().GetResult()
        $null=$errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            Write-Warning 'Business health check could not finish. No function was resumed or message retried.'
            return
        }
        if ([string]::IsNullOrWhiteSpace($output)) {
            Write-Warning 'Business health check returned no result; state remains unverified.'
            return
        }
        if ($output.Length -gt 65536) { throw 'business_health_output_too_large' }
        $result=$output | ConvertFrom-Json -ErrorAction Stop
        if (!$Manual -and $result.skipped -eq $true) { return }
        if ($result.summary -is [string] -and $result.summary.Length -le 1500) { Write-Host $result.summary }
        else { Write-Warning 'Business health report saved; inspect logs/business-health for the verified scope.' }
    } catch {
        Write-Warning 'Business health check needs attention. Core connection state is unchanged; no business was retried.'
    } finally {
        if ($process) { $process.Dispose() }
    }
}
