# Start or stop the local OpenSearch used by the opt-in hybrid search profile.
#
#   .\scripts\windows\opensearch.ps1                start the node (creates the container the first time)
#   .\scripts\windows\opensearch.ps1 -Dashboards    also start OpenSearch Dashboards on http://127.0.0.1:5601
#   .\scripts\windows\opensearch.ps1 -Stop          stop both; the index is kept in a Docker volume
#   .\scripts\windows\opensearch.ps1 -Remove        remove both containers and the index data
#
# One node, reachable only from this machine (127.0.0.1:9200; Dashboards 127.0.0.1:5601). Security
# is off because the ports are not exposed to the network; never publish these containers on another
# interface. Dashboards must be the same version as the node.
# Needs Docker Desktop running. Then: $env:KB_LOCAL_HYBRID = '1'; .\scripts\windows\run.ps1
param([switch]$Stop, [switch]$Remove, [switch]$Dashboards, [string]$Version = '3.9.0')

$Name = 'kbase-opensearch'
$DashName = 'kbase-opensearch-dashboards'
$Volume = 'kbase-opensearch-data'
$Network = 'kbase-search'   # lets Dashboards reach the node by its container name
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker is not running. Start Docker Desktop first.'; exit 1 }

if ($Remove) {
    docker rm -f $DashName *> $null; docker rm -f $Name *> $null; docker volume rm $Volume *> $null
    'Removed the containers and the index data.'; exit 0
}
if ($Stop) { docker stop $DashName *> $null; docker stop $Name *> $null; 'Stopped.'; exit 0 }

function Test-Container($n) { [bool](docker ps -a --filter "name=^$n$" --format '{{.Names}}') }

docker network inspect $Network *> $null
if ($LASTEXITCODE -ne 0) { docker network create $Network | Out-Null }

if (Test-Container $Name) {
    docker start $Name | Out-Null
} else {
    docker run -d --name $Name --network $Network -p 127.0.0.1:9200:9200 `
        -e 'discovery.type=single-node' -e 'DISABLE_SECURITY_PLUGIN=true' -e 'DISABLE_INSTALL_DEMO_CONFIG=true' `
        -e 'OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g' -v "${Volume}:/usr/share/opensearch/data" "opensearchproject/opensearch:$Version" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error 'Could not start the OpenSearch container.'; exit 1 }
}
# A node created before this script used a network is joined to it here; already joined is fine.
docker network connect $Network $Name *> $null

$up = $false
foreach ($i in 1..60) {
    try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9200' -TimeoutSec 3; "OpenSearch $($r.version.number) is up on http://127.0.0.1:9200"; $up = $true; break } catch { Start-Sleep -Seconds 2 }
}
if (-not $up) { Write-Error 'OpenSearch did not answer within two minutes. Check: docker logs kbase-opensearch'; exit 1 }
if (-not $Dashboards) { exit 0 }

if (Test-Container $DashName) {
    docker start $DashName | Out-Null
} else {
    # OPENSEARCH_HOSTS is a plain URL, not a JSON array: Windows PowerShell 5.1 strips the inner quotes.
    docker run -d --name $DashName --network $Network -p 127.0.0.1:5601:5601 `
        -e "OPENSEARCH_HOSTS=http://${Name}:9200" -e 'DISABLE_SECURITY_DASHBOARDS_PLUGIN=true' `
        "opensearchproject/opensearch-dashboards:$Version" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error 'Could not start the Dashboards container.'; exit 1 }
}
foreach ($i in 1..60) {
    if (-not (docker ps --filter "name=^$DashName$" --format '{{.Names}}')) {
        Write-Error 'Dashboards stopped while starting. Check: docker logs kbase-opensearch-dashboards'; exit 1
    }
    try {
        $s = Invoke-RestMethod -Uri 'http://127.0.0.1:5601/api/status' -TimeoutSec 3
        if ($s.status.overall.state -eq 'green') { 'OpenSearch Dashboards is up on http://127.0.0.1:5601'; exit 0 }
    } catch { }
    Start-Sleep -Seconds 2
}
Write-Error 'Dashboards did not report ready within three minutes. Check: docker logs kbase-opensearch-dashboards'; exit 1
