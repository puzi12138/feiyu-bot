param(
    [Parameter(Mandatory=$true)][string]$LiteralRoot,
    [Parameter(Mandatory=$true)][string]$ItemsBase64
)
$ErrorActionPreference='Stop'
try {
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -Force
    $currentSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed=@($currentSid,'S-1-5-18','S-1-5-32-544')
    $root=[IO.Path]::GetFullPath($LiteralRoot).TrimEnd('\','/')
    if ($root -notmatch '^[A-Za-z]:\\' -or $root.Substring(3).Contains(':')) { throw 'invalid' }
    $itemsJson=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ItemsBase64))
    if (!$itemsJson.Trim().StartsWith('[') -or !$itemsJson.Trim().EndsWith(']')) { throw 'invalid' }
    $decoded=$itemsJson|ConvertFrom-Json
    $items=if($itemsJson.Trim() -eq '[]'){@()}else{@($decoded)}
    if ($items.Count -gt 20) { throw 'invalid' }
    $checkPaths=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $null=$checkPaths.Add($root)
    foreach($itemPath in $items) {
        if ($itemPath -isnot [string]) { throw 'invalid' }
        $full=[IO.Path]::GetFullPath($itemPath)
        if ($full -ne $root -and !$full.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'invalid' }
        $cursor=$full
        while ($cursor) {
            $item=Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'invalid' }
            $parent=[IO.Directory]::GetParent($cursor)
            $cursor=if($parent){$parent.FullName}else{$null}
        }
        $cursor=$full
        while ($cursor -and ($cursor -eq $root -or $cursor.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase))) {
            $null=$checkPaths.Add($cursor)
            $parent=[IO.Directory]::GetParent($cursor)
            $cursor=if($parent){$parent.FullName}else{$null}
        }
    }
    foreach($full in $checkPaths) {
        $item=Get-Item -LiteralPath $full -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'invalid' }
        $acl=Get-Acl -LiteralPath $full
        if ($full -eq $root -and !$acl.AreAccessRulesProtected) { throw 'invalid' }
        $owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) { throw 'invalid' }
        $hasCurrent=$false
        foreach($rule in $acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow') {
                if ($rule.IdentityReference.Value -notin $allowed) { throw 'invalid' }
                if ($rule.IdentityReference.Value -eq $currentSid) { $hasCurrent=$true }
            }
        }
        if (!$hasCurrent) { throw 'invalid' }
    }
    '{"ok":true}'
} catch {
    '{"ok":false}'
    exit 1
}
