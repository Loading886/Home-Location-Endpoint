[CmdletBinding()]
param(
    [string]$VlessUri = $env:HLE_TEST_VLESS_URI,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedEgressIp,
    [string]$XrayVersion = "v26.3.27",
    [int]$SocksPort = 19080
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($VlessUri)) {
    throw "Provide -VlessUri or set HLE_TEST_VLESS_URI."
}
if (-not $VlessUri.StartsWith("vless://", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "VlessUri must use the vless:// scheme."
}

function ConvertFrom-QueryString {
    param([string]$Query)

    $values = @{}
    foreach ($item in $Query.TrimStart("?").Split("&", [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $pair = $item.Split("=", 2)
        $name = [Uri]::UnescapeDataString($pair[0])
        $value = if ($pair.Count -eq 2) { [Uri]::UnescapeDataString($pair[1]) } else { "" }
        $values[$name] = $value
    }
    return $values
}

function Wait-LocalTcpPort {
    param(
        [int]$Port,
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 10
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            return $false
        }
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $client.Connect("127.0.0.1", $Port)
            return $true
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
        finally {
            $client.Dispose()
        }
    }
    return $false
}

$toolRoot = Join-Path $env:LOCALAPPDATA "Home-Location-Endpoint\test-tools\$XrayVersion"
$xrayPath = Join-Path $toolRoot "xray.exe"
$archivePath = Join-Path $toolRoot "Xray-windows-64.zip"

if (-not (Test-Path -LiteralPath $xrayPath)) {
    New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/XTLS/Xray-core/releases/tags/$XrayVersion" `
        -Headers @{ "User-Agent" = "Home-Location-Endpoint-test" }
    $asset = $release.assets | Where-Object { $_.name -eq "Xray-windows-64.zip" }
    if ($null -eq $asset -or [string]::IsNullOrWhiteSpace($asset.digest)) {
        throw "The Xray release does not expose the expected Windows asset and SHA-256 digest."
    }
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archivePath
    $actualDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
    $expectedDigest = $asset.digest.Substring("sha256:".Length).ToLowerInvariant()
    if ($actualDigest -ne $expectedDigest) {
        throw "Xray archive SHA-256 mismatch."
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $toolRoot -Force
}

$uri = [Uri]$VlessUri
$query = ConvertFrom-QueryString -Query $uri.Query
foreach ($required in @("flow", "sni", "fp", "pbk", "sid", "packetEncoding")) {
    if ([string]::IsNullOrWhiteSpace($query[$required])) {
        throw "VLESS URI is missing required query parameter: $required"
    }
}

$config = @{
    log = @{ loglevel = "info" }
    inbounds = @(
        @{
            listen = "127.0.0.1"
            port = $SocksPort
            protocol = "socks"
            settings = @{ udp = $true }
        }
    )
    outbounds = @(
        @{
            protocol = "vless"
            settings = @{
                vnext = @(
                    @{
                        address = $uri.Host
                        port = $uri.Port
                        users = @(
                            @{
                                id = $uri.UserInfo
                                encryption = "none"
                                flow = $query.flow
                                packetEncoding = $query.packetEncoding
                            }
                        )
                    }
                )
            }
            streamSettings = @{
                network = "raw"
                security = "reality"
                realitySettings = @{
                    serverName = $query.sni
                    fingerprint = $query.fp
                    publicKey = $query.pbk
                    shortId = $query.sid
                    spiderX = "/"
                }
            }
        }
    )
}

$runId = [Guid]::NewGuid().ToString("N")
$configPath = Join-Path $env:TEMP "hle-xray-client-$runId.json"
$stdoutPath = Join-Path $env:TEMP "hle-xray-client-$runId.stdout.log"
$stderrPath = Join-Path $env:TEMP "hle-xray-client-$runId.stderr.log"
$curlErrorPath = Join-Path $env:TEMP "hle-xray-client-$runId.curl.log"
$process = $null

try {
    $config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $configPath -Encoding utf8
    $process = Start-Process `
        -FilePath $xrayPath `
        -ArgumentList @("run", "-c", $configPath) `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath

    if (-not (Wait-LocalTcpPort -Port $SocksPort -Process $process)) {
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "Xray client did not start: $stderr"
    }

    $proxy = "127.0.0.1:$SocksPort"
    $egressOutput = & curl.exe --silent --show-error --max-time 20 `
        --stderr $curlErrorPath --socks5-hostname $proxy https://api.ipify.org
    $egressExit = $LASTEXITCODE
    if ($egressExit -ne 0) {
        $curlError = if (Test-Path -LiteralPath $curlErrorPath) {
            Get-Content -LiteralPath $curlErrorPath -Raw
        } else {
            ""
        }
        $xrayError = if (Test-Path -LiteralPath $stderrPath) {
            Get-Content -LiteralPath $stderrPath -Raw
        } else {
            ""
        }
        $xrayOutput = if (Test-Path -LiteralPath $stdoutPath) {
            Get-Content -LiteralPath $stdoutPath -Raw
        } else {
            ""
        }
        throw "Proxy request failed (curl $egressExit): $curlError`nXray stdout:`n$xrayOutput`nXray stderr:`n$xrayError"
    }
    $egressIp = ([string]$egressOutput).Trim()
    $httpCode = & curl.exe --silent --show-error --max-time 20 `
        --output NUL --write-out "%{http_code}" `
        --socks5-hostname $proxy https://cp.cloudflare.com/generate_204

    if ($egressIp -ne $ExpectedEgressIp) {
        throw "Unexpected egress IP: $egressIp"
    }
    if ($httpCode -ne "204") {
        throw "Unexpected Cloudflare status: $httpCode"
    }

    Write-Output "VLESS + REALITY end-to-end: OK"
    Write-Output "Observed egress IP: $egressIp"
    Write-Output "Cloudflare connectivity: HTTP $httpCode"
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
    foreach ($path in @($configPath, $stdoutPath, $stderrPath, $curlErrorPath)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}
