# Start or stop the local Elasticsearch used by the opt-in hybrid search profile.
#
#   .\scripts\windows\elasticsearch.ps1            start (creates the container the first time)
#   .\scripts\windows\elasticsearch.ps1 -Stop      stop it; the index is kept in a Docker volume
#   .\scripts\windows\elasticsearch.ps1 -Remove    remove the container and its data
#
# One node, reachable only from this machine (127.0.0.1:9200). Security is off because the port is
# not exposed to the network; never publish this container on another interface. Set -Image to the
# same major version the team's cluster runs, and install the matching Python client
# (pip install "elasticsearch>=8,<9" for an 8.x cluster).
# Needs Docker Desktop running. Then: $env:KB_LOCAL_HYBRID = '1'; .\scripts\windows\run.ps1
param([switch]$Stop, [switch]$Remove, [string]$Image = 'docker.elastic.co/elasticsearch/elasticsearch:8.19.3')

$Name = 'kbase-elasticsearch'
$Volume = 'kbase-elasticsearch-data'
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker is not running. Start Docker Desktop first.'; exit 1 }

if ($Remove) { docker rm -f $Name *> $null; docker volume rm $Volume *> $null; 'Removed the container and its data.'; exit 0 }
if ($Stop) { docker stop $Name *> $null; 'Stopped.'; exit 0 }

$existing = docker ps -a --filter "name=^$Name$" --format '{{.Names}}'
if ($existing) {
    docker start $Name | Out-Null
} else {
    docker run -d --name $Name -p 127.0.0.1:9200:9200 `
        -e 'discovery.type=single-node' -e 'xpack.security.enabled=false' `
        -e 'ES_JAVA_OPTS=-Xms1g -Xmx1g' -v "${Volume}:/usr/share/elasticsearch/data" $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error 'Could not start the Elasticsearch container.'; exit 1 }
}
foreach ($i in 1..60) {
    try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9200' -TimeoutSec 3; "Elasticsearch $($r.version.number) is up on http://127.0.0.1:9200"; exit 0 } catch { Start-Sleep -Seconds 2 }
}
Write-Error 'Elasticsearch did not answer within two minutes. Check: docker logs kbase-elasticsearch'; exit 1
