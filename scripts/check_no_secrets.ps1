$ErrorActionPreference = "Stop"

$files = git ls-files --cached --others --exclude-standard -z
$found = @()
$assignment = '(?m)^\s*(DEEPSEEK_API_KEY|GLM_API_KEY|JWT_SIGNING_KEY|PROVIDER_CREDENTIAL_ENCRYPTION_KEY)[ \t]*[:=][ \t]*["'']?([^\r\n\s"''$<>{}]+)'
$token = '(?i)\bsk-(?!demo-not-real-)[A-Za-z0-9_-]{12,}\b'

foreach ($path in ($files -split "`0")) {
    if ([string]::IsNullOrWhiteSpace($path) -or
        $path -match '(^|[\\/])(?:\.git|\.venv|node_modules)([\\/]|$)' -or
        $path -match '\.txt\.example$' -or
        $path -match '(^|[\\/])backend[\\/]tests([\\/]|$)' -or
        $path -match '(^|[\\/])frontend[\\/]tests([\\/]|$)' -or
        $path -match '(^|[\\/])demo_cases([\\/]|$)') {
        continue
    }
    $text = Get-Content -Raw -LiteralPath $path -ErrorAction SilentlyContinue
    if ($null -eq $text) { continue }
    foreach ($match in [regex]::Matches($text, $assignment)) {
        $value = $match.Groups[2].Value
        if ($value -notmatch '^(?i)(None|null|true|false|str|test-|CHANGE_ME|your-|<|\$\{)') {
            $found += "${path}: secret-like assignment"
        }
    }
    if ($text -match $token) { $found += "${path}: token-like value" }
}

if ($found.Count -gt 0) {
    $found | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Output "secret scan: clean"
