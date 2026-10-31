# 本地调试入口（Windows PowerShell）
# 用法：
#   .\run.ps1                  # 正常跑一次（有 token 会真推送）
#   .\run.ps1 --dry-run        # 只打印消息、生成本地看板，不推送
#
# 密钥可写在同目录的 .env.local（参考 .env.example），本脚本会自动加载。

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$envFile = Join-Path $root ".env.local"
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $idx = $line.IndexOf("=")
            if ($idx -gt 0) {
                $name = $line.Substring(0, $idx).Trim()
                $value = $line.Substring($idx + 1).Trim()
                if ($value) { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
            }
        }
    }
}
$env:PYTHONPATH = Join-Path $root "src"
python -m rainwatch @args
