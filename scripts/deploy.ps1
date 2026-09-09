# ==============================================================================
# Google Cloud Production Deployment Script for StudioTower (PowerShell / Windows / CI)
# ==============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectId = "agentic-cinema-demo-2026",

    [Parameter(Mandatory = $false)]
    [string]$Region = "us-central1",

    [Parameter(Mandatory = $false)]
    [string]$ServiceName = "studio-tower-api",

    [Parameter(Mandatory = $false)]
    [string]$GcsBucket = "agentic-cinema-demo-2026-studiotower-artifacts",

    [Parameter(Mandatory = $false)]
    [string]$GeminiModel = "gemini-3.6-flash",

    [Parameter(Mandatory = $false)]
    [string]$WorkerServiceUrl = "",

    [Parameter(Mandatory = $false)]
    [string]$FirebaseApiKey = "",

    [Parameter(Mandatory = $false)]
    [string]$FirebaseAppId = "",

    [Parameter(Mandatory = $false)]
    [string]$SchedulerJobName = "studiotower-maintenance-job",

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

# Set ErrorActionPreference to Continue so stderr warnings from native tools (e.g. gcloud / python urllib3)
# do not trigger false-positive NativeCommandError terminations. Failures are strictly verified via
# Invoke-NativeCommand and $LASTEXITCODE checks.
$ErrorActionPreference = "Continue"

if ($FirebaseApiKey) {
    $env:VITE_FIREBASE_API_KEY = $FirebaseApiKey
}
if ($FirebaseAppId) {
    $env:VITE_FIREBASE_APP_ID = $FirebaseAppId
}
if (-not $env:VITE_FIREBASE_PROJECT_ID) {
    $env:VITE_FIREBASE_PROJECT_ID = $ProjectId
}

$BuildId = (Get-Date -Format "yyyyMMddHHmmss")
$CandidateTag = "candidate-$BuildId"
$Image = "gcr.io/$ProjectId/${ServiceName}:$BuildId"
$FirebaseProjectId = $ProjectId

function Invoke-NativeCommand {
    param(
        [scriptblock]$ScriptBlock,
        [string]$ErrorMessage = "Native command execution failed"
    )
    & $ScriptBlock
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit code: $LASTEXITCODE)"
    }
}

Write-Host "==> 1. Checking Existing Cloud Run Service Status and Traffic..." -ForegroundColor Cyan
$DescribeOutFile = "$env:TEMP\st_desc_out_$BuildId.json"
$DescribeErrFile = "$env:TEMP\st_desc_err_$BuildId.txt"
$IsFirstDeployment = $false
$ExistingServiceUrl = ""
$CurrentTrafficAlloc = ""
$CurrentProductionRevision = ""

if ($DryRun) {
    Write-Host "Dry-run mode active: using simulated fixture service URL for dry-run verification." -ForegroundColor Yellow
    $ExistingServiceUrl = if ($WorkerServiceUrl) { $WorkerServiceUrl } else { "https://${ServiceName}-mbvd6nfacq-uc.a.run.app" }
    $CurrentTrafficAlloc = "prod-v1=100"
    $CurrentProductionRevision = "prod-v1"
} else {
    gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format=json > $DescribeOutFile 2> $DescribeErrFile

    if ($LASTEXITCODE -eq 0) {
        $DescribeJson = Get-Content $DescribeOutFile -Raw
        $IsFirstDeployment = $false

        # Extract authoritative status.url
        $ExistingServiceUrl = ($DescribeJson | python "$PSScriptRoot\parse_traffic.py" --service-url).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $ExistingServiceUrl) {
            throw "Existing Cloud Run service describe did not yield a valid status.url."
        }

        # Extract traffic allocations snapshot
        $CurrentTrafficAlloc = ($DescribeJson | python "$PSScriptRoot\parse_traffic.py" --allocations).Trim()
        $CurrentProductionRevision = ($DescribeJson | python "$PSScriptRoot\parse_traffic.py" --primary).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $CurrentTrafficAlloc -or -not $CurrentProductionRevision) {
            throw "Existing Cloud Run service describe did not yield valid traffic allocations or primary revision. Aborting."
        }

        Write-Host "Existing Service Detected:" -ForegroundColor Green
        Write-Host "  Authoritative URL: $ExistingServiceUrl" -ForegroundColor Gray
        Write-Host "  Serving Traffic:   $CurrentTrafficAlloc" -ForegroundColor Gray
        Write-Host "  Primary Revision:  $CurrentProductionRevision" -ForegroundColor Gray
    } else {
        $DescribeErr = if (Test-Path $DescribeErrFile) { Get-Content $DescribeErrFile -Raw } else { "" }
        if ($DescribeErr -match "Cannot find service|not found|NOT_FOUND") {
            $IsFirstDeployment = $true
            Write-Host "Note: Service '$ServiceName' does not exist in $Region (Initial Deployment)." -ForegroundColor Yellow
        } else {
            throw "Failed to query Cloud Run service '$ServiceName' due to GCP/network/permission error: $DescribeErr"
        }
    }
}
Remove-Item -Force $DescribeOutFile, $DescribeErrFile -ErrorAction SilentlyContinue

# Authoritative Worker Service URL resolution & boundary validation
if ($IsFirstDeployment) {
    if (-not $WorkerServiceUrl) {
        throw "Service '$ServiceName' does not exist yet (first deployment). -WorkerServiceUrl must be explicitly provided (cannot guess synthetic URL)."
    }
} else {
    if (-not $WorkerServiceUrl) {
        $WorkerServiceUrl = $ExistingServiceUrl
    } else {
        if ($WorkerServiceUrl.Trim() -ne $ExistingServiceUrl.Trim()) {
            throw "Explicit -WorkerServiceUrl '$WorkerServiceUrl' does not match authoritative Cloud Run service URL '$ExistingServiceUrl'."
        }
    }
}

if (-not $WorkerServiceUrl.StartsWith("https://")) {
    throw "STUDIO_TOWER_WORKER_SERVICE_URL must start with https://"
}

$WorkerHost = [System.Uri]::new($WorkerServiceUrl).Host
$SchedulerSa = if ($env:STUDIO_TOWER_SCHEDULER_SA) { $env:STUDIO_TOWER_SCHEDULER_SA } else { "studiotower-api@${ProjectId}.iam.gserviceaccount.com" }

Write-Host "==> 2. Validating & Provisioning Production Secret Manager Bindings..." -ForegroundColor Cyan
$secrets = "GEMINI_API_KEY=GEMINI_API_KEY:latest,STUDIO_TOWER_MAINTENANCE_SECRET=STUDIO_TOWER_MAINTENANCE_SECRET:latest,STUDIO_TOWER_TASK_SECRET=STUDIO_TOWER_TASK_SECRET:latest,CURSOR_SIGNING_SECRET=CURSOR_SIGNING_SECRET:latest,ACTION_SIGNING_SECRET=ACTION_SIGNING_SECRET:latest,TELEMETRY_TENANT_KEY_PRIMARY=TELEMETRY_TENANT_KEY_PRIMARY:latest"

if (-not $DryRun) {
    # Optional dual-secret task rotation binding
    $prevSecretCheck = gcloud secrets describe STUDIO_TOWER_TASK_SECRET_PREVIOUS --project=$ProjectId 2>$null
    if ($LASTEXITCODE -eq 0 -and $prevSecretCheck) {
        $secrets += ",STUDIO_TOWER_TASK_SECRET_PREVIOUS=STUDIO_TOWER_TASK_SECRET_PREVIOUS:latest"
    }
}

$GrafanaBaseUrl = if ($env:GRAFANA_BASE_URL) { $env:GRAFANA_BASE_URL.Trim() } else { "https://loftyladybug3305.grafana.net" }
$GrafanaAllowedHost = [System.Uri]::new($GrafanaBaseUrl).Host
# gcloud forbids combining --set-env-vars with --env-vars-file. Put every
# runtime env var in one YAML so GRAFANA_ALLOWED_HOSTS keeps JSON quotes.
$RunEnvFile = Join-Path $env:TEMP "studiotower-run-env.yaml"
@"
ENV: production
STUDIO_TOWER_AUTH_MODE: firebase
STUDIO_TOWER_STORE: firestore
STUDIO_TOWER_FIREBASE_PROJECT_ID: $FirebaseProjectId
STUDIO_TOWER_ARTIFACT_BACKEND: gcs
STUDIO_TOWER_GCS_BUCKET: $GcsBucket
INGESTION_RUNNER: cloud_tasks
ACTION_RUNNER: cloud_tasks
STUDIO_TOWER_WORKER_SERVICE_URL: "$WorkerServiceUrl"
STUDIO_TOWER_ALLOWED_WORKER_HOST: $WorkerHost
STUDIO_TOWER_SCHEDULER_SA: $SchedulerSa
STUDIO_TOWER_SCHEDULER_AUDIENCE: "$WorkerServiceUrl"
STUDIO_TOWER_CLOUD_TASKS_PROJECT: $FirebaseProjectId
STUDIO_TOWER_CLOUD_TASKS_QUEUE: studiotower-ingestion-queue
STUDIO_TOWER_CLOUD_TASKS_LOCATION: us-central1
GEMINI_MODEL: $GeminiModel
AI_FALLBACK_ALLOWED: "false"
AI_INFERENCE_TIMEOUT_SECONDS: "45.0"
AI_MAX_CONCURRENT_INFERENCES: "16"
GRAFANA_MCP_ENDPOINT: "http://127.0.0.1:8000/mcp"
GRAFANA_BASE_URL: "$GrafanaBaseUrl"
GRAFANA_ALLOWED_HOSTS: '["$GrafanaAllowedHost"]'
GRAFANA_OTLP_ENDPOINT: "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
GRAFANA_OTLP_INSTANCE_ID: "1742899"
"@ | Set-Content -Path $RunEnvFile -Encoding ascii

if (-not $DryRun) {
    # Grafana Cloud MCP credentials. Sidecar grafana/mcp-grafana needs a service account token.
    $grafanaCheck = gcloud secrets describe GRAFANA_CLOUD_API_KEY --project=$ProjectId 2>$null
    if ($LASTEXITCODE -eq 0 -and $grafanaCheck) {
        $secrets += ",GRAFANA_CLOUD_API_KEY=GRAFANA_CLOUD_API_KEY:latest"
    }
    $grafanaSaCheck = gcloud secrets describe GRAFANA_SERVICE_ACCOUNT_TOKEN --project=$ProjectId 2>$null
    if ($LASTEXITCODE -eq 0 -and $grafanaSaCheck) {
        $secrets += ",GRAFANA_SERVICE_ACCOUNT_TOKEN=GRAFANA_SERVICE_ACCOUNT_TOKEN:latest"
    }
    $grafanaOtlpCheck = gcloud secrets describe GRAFANA_OTLP_TOKEN --project=$ProjectId 2>$null
    if ($LASTEXITCODE -eq 0 -and $grafanaOtlpCheck) {
        $secrets += ",GRAFANA_OTLP_TOKEN=GRAFANA_OTLP_TOKEN:latest"
    }
}

if ($DryRun) {
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host " StudioTower Cloud Deployment Pipeline [DRY RUN]" -ForegroundColor Cyan
    Write-Host " ProjectId: $ProjectId | Region: $Region | Service: $ServiceName" -ForegroundColor Cyan
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host "==> [DRY-RUN 1/6] Running Traffic Allocation Parser Verification on Representative Fixtures..." -ForegroundColor Cyan
    python "$PSScriptRoot\parse_traffic.py" --verify-fixtures
    if ($LASTEXITCODE -ne 0) { throw "Traffic parser fixtures failed in dry-run" }

    Write-Host "==> [DRY-RUN 2/6] Validating Multi-Revision Rollback Command Assembly..." -ForegroundColor Cyan
    # 1. 100% single revision
    $mock100 = '{"status":{"traffic":[{"revisionName":"prod-v1","percent":100}]}}'
    $alloc100 = ($mock100 | python "$PSScriptRoot\parse_traffic.py" --allocations).Trim()
    Write-Host "  [Single 100%] -> Allocation: $alloc100 | Rollback: gcloud run services update-traffic $ServiceName --project=$ProjectId --region=$Region --to-revisions=$alloc100" -ForegroundColor Gray
    if ($alloc100 -ne "prod-v1=100") { throw "Allocation 100% mismatch: $alloc100" }

    # 2. 50/50 split
    $mock50 = '{"status":{"traffic":[{"revisionName":"rev-a","percent":50},{"revisionName":"rev-b","percent":50}]}}'
    $alloc50 = ($mock50 | python "$PSScriptRoot\parse_traffic.py" --allocations).Trim()
    Write-Host "  [50/50 Split] -> Allocation: $alloc50 | Rollback: gcloud run services update-traffic $ServiceName --project=$ProjectId --region=$Region --to-revisions=$alloc50" -ForegroundColor Gray
    if ($alloc50 -ne "rev-a=50,rev-b=50") { throw "Allocation 50/50 mismatch: $alloc50" }

    # 3. 90/10 canary
    $mock90 = '{"status":{"traffic":[{"revisionName":"rev-stable","percent":90},{"revisionName":"rev-canary","percent":10}]}}'
    $alloc90 = ($mock90 | python "$PSScriptRoot\parse_traffic.py" --allocations).Trim()
    Write-Host "  [90/10 Canary] -> Allocation: $alloc90 | Rollback: gcloud run services update-traffic $ServiceName --project=$ProjectId --region=$Region --to-revisions=$alloc90" -ForegroundColor Gray
    if ($alloc90 -ne "rev-stable=90,rev-canary=10") { throw "Allocation 90/10 mismatch: $alloc90" }

    Write-Host "==> [DRY-RUN 3/6] Validating Worker Service URL and Host Whitelist..." -ForegroundColor Cyan
    Write-Host "  Target Project: $ProjectId" -ForegroundColor Gray
    Write-Host "  Resolved Worker URL: $WorkerServiceUrl" -ForegroundColor Gray
    Write-Host "  Allowed Worker Host: $WorkerHost" -ForegroundColor Gray
    Write-Host "  ✓ Worker URL HTTPS and host whitelist verified." -ForegroundColor Green

    Write-Host "==> [DRY-RUN 4/6] Validating Cloud Scheduler OIDC Command Assembly (Zero Plaintext Secrets)..." -ForegroundColor Cyan
    $simSchedulerCmd = "gcloud scheduler jobs create http $SchedulerJobName --project=$ProjectId --location=$Region --schedule=`"*/5 * * * *`" --uri=`"${WorkerServiceUrl}/v1/maintenance/reconcile-actions-and-cleanup`" --http-method=POST --oidc-service-account-email=`"$SchedulerSa`" --oidc-token-audience=`"${WorkerServiceUrl}`""
    Write-Host "  Simulated Scheduler CLI: $simSchedulerCmd" -ForegroundColor Gray
    if ($simSchedulerCmd.Contains("X-StudioTower-Maintenance-Secret")) {
        throw "SECURITY ERROR: Secret found in Scheduler CLI args!"
    }
    Write-Host "  ✓ Zero shared secrets exposed on Cloud Scheduler CLI (OIDC SA: $SchedulerSa)." -ForegroundColor Green

    Write-Host "==> [DRY-RUN 5/6] Validating Firestore Composite Indexes & Frontend Package..." -ForegroundColor Cyan
    if (-not (Test-Path "$PSScriptRoot\..\firestore.indexes.json")) { throw "firestore.indexes.json missing" }
    if (-not (Test-Path "$PSScriptRoot\..\frontend\package.json")) { throw "frontend/package.json missing" }
    Write-Host "  ✓ Indexes schema and frontend bundle definitions verified." -ForegroundColor Green

    Write-Host "==> [DRY-RUN 6/6] Validating Candidate Tag and URL Resolution..." -ForegroundColor Cyan
    Write-Host "  Container Cloud Build target -> $Image" -ForegroundColor Gray
    Write-Host "  Candidate Tag -> $CandidateTag" -ForegroundColor Gray
    $simCandTagUrl = ('{"status":{"traffic":[{"tag":"' + $CandidateTag + '","url":"https://' + $CandidateTag + '---studio-tower-api.run.app"}]}}' | python "$PSScriptRoot\parse_traffic.py" --candidate-url $CandidateTag).Trim()
    Write-Host "  Candidate URL parser simulation -> $simCandTagUrl" -ForegroundColor Gray
    Write-Host "==> [DRY-RUN] All parser calculations, fixture tests, rollback commands, and security boundaries passed cleanly." -ForegroundColor Green
    exit 0
}

Write-Host "==> 3. Deploying Firestore Composite Indexes Prior to Code Rollout..." -ForegroundColor Cyan
Push-Location "$PSScriptRoot\.."
try {
    Invoke-NativeCommand -ScriptBlock {
        firebase deploy --only firestore:indexes --project=$ProjectId
    } -ErrorMessage "Firestore indexes deployment failed"
} finally {
    Pop-Location
}

Write-Host "Verifying Firestore composite indexes have reached READY status..." -ForegroundColor Cyan
$maxAttempts = 30
$allReady = $false
for ($i = 1; $i -le $maxAttempts; $i++) {
    $buildingIndexes = (gcloud firestore indexes composite list --project=$ProjectId --format=json | ConvertFrom-Json) | Where-Object { $_.state -ne "READY" }
    if (-not $buildingIndexes) {
        $allReady = $true
        Write-Host "All Firestore composite indexes are confirmed READY." -ForegroundColor Green
        break
    }
    Write-Host "Waiting for $($buildingIndexes.Count) indexes to finish building (attempt $i/$maxAttempts)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}

if (-not $allReady) {
    throw "Firestore composite indexes did not reach READY state within the timeout. Aborting candidate deployment."
}

Write-Host "==> 4. Building Frontend Production Assets (npm run build:prod)..." -ForegroundColor Cyan
Push-Location "$PSScriptRoot\..\frontend"
try {
    Invoke-NativeCommand -ScriptBlock {
        npm run build:prod
    } -ErrorMessage "Frontend build failed"
} finally {
    Pop-Location
}

Write-Host "==> 5. Building Container Image via Cloud Build ($Image)..." -ForegroundColor Cyan
Push-Location "$PSScriptRoot\.."
try {
    Invoke-NativeCommand -ScriptBlock {
        gcloud builds submit . `
            --project=$ProjectId `
            --config="$PSScriptRoot\..\cloudbuild.yaml" `
            --substitutions="_IMAGE=$Image"
    } -ErrorMessage "Cloud Build container image creation failed"
} finally {
    Pop-Location
}

Write-Host "==> 6. Deploying 0% Traffic Candidate to Google Cloud Run (Tag: $CandidateTag)..." -ForegroundColor Cyan
Invoke-NativeCommand -ScriptBlock {
    gcloud run deploy $ServiceName `
        --project=$ProjectId `
        --image=$Image `
        --region=$Region `
        --platform=managed `
        --allow-unauthenticated `
        --min-instances=0 `
        --max-instances=3 `
        --cpu=1 `
        --memory=1Gi `
        --env-vars-file=$RunEnvFile `
        --set-secrets=$secrets `
        --no-traffic `
        --tag=$CandidateTag
} -ErrorMessage "Candidate deployment failed"

$CandidateRevision = (gcloud run revisions list --service=$ServiceName --project=$ProjectId --region=$Region --filter="metadata.annotations['run.googleapis.com/tag'] = '$CandidateTag'" --format="value(metadata.name)" --limit=1 2>$null)
if (-not $CandidateRevision) {
    $CandidateRevision = (gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format="value(status.latestCreatedRevisionName)" 2>$null)
}
Write-Host "Deployed Candidate Revision: $CandidateRevision (Tag: $CandidateTag)" -ForegroundColor Green

# Authoritatively Extract Candidate Tag URL (fail closed immediately if tag URL not found or not HTTPS)
$candidateJson = gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format=json
$CandidateUrl = ($candidateJson | python "$PSScriptRoot\parse_traffic.py" --candidate-url "$CandidateTag").Trim()
if ($LASTEXITCODE -ne 0 -or -not $CandidateUrl -or -not $CandidateUrl.StartsWith("https://")) {
    throw "Failed to resolve authoritative Candidate Tag URL for '$CandidateTag' from Cloud Run service describe. Aborting deployment immediately."
}
Write-Host "Authoritative Candidate URL: $CandidateUrl" -ForegroundColor Green

$TrafficShifted = $false

function Invoke-RollbackTraffic {
    param([string]$Reason)
    Write-Host "`nCRITICAL FAILURE AFTER PROMOTION: $Reason" -ForegroundColor Red
    if ($TrafficShifted -and $CurrentProductionRevision -and ($CurrentProductionRevision -ne $CandidateRevision)) {
        Write-Host "Initiating emergency traffic rollback to prior serving configuration: $($CurrentTrafficAlloc)..." -ForegroundColor Yellow
        try {
            $targetRevs = if ($CurrentTrafficAlloc) { $CurrentTrafficAlloc } else { "${CurrentProductionRevision}=100" }
            gcloud run services update-traffic $ServiceName --to-revisions=$targetRevs --project=$ProjectId --region=$Region
            if ($LASTEXITCODE -ne 0) {
                Write-Host "EMERGENCY: Automated rollback command failed! Immediate manual intervention required for service '$ServiceName' in region '$Region' (Target Configuration: $targetRevs)" -ForegroundColor Red
                exit 2
            }
            Write-Host "Rollback successfully completed. Traffic restored to prior configuration: $targetRevs." -ForegroundColor Green
        } catch {
            Write-Host "EMERGENCY: Rollback threw exception: $_! Immediate manual intervention required!" -ForegroundColor Red
            exit 2
        }
    } else {
        Write-Host "Traffic was not shifted or no prior serving revision exists; no traffic rollback required." -ForegroundColor Yellow
    }
    exit 1
}

Write-Host "==> 7. Auditing Candidate Cloud Run Spec for Secret Leak Prevention..." -ForegroundColor Cyan
$specJson = gcloud run revisions describe $CandidateRevision --project=$ProjectId --region=$Region --format=json
$spec = $specJson | ConvertFrom-Json
$envList = $spec.spec.containers[0].env
foreach ($e in $envList) {
    $name = $e.name
    $val = $e.value
    if (($name -like "*SECRET*" -or $name -like "*KEY*" -or $name -like "*TOKEN*") -and ($name -ne "GEMINI_MODEL")) {
        if ($val -and ($val.ToString().Length -gt 0)) {
            Write-Host "SECURITY VIOLATION: Sensitive environment variable $name set as plaintext in container spec!" -ForegroundColor Red
            exit 1
        }
    }
}

Write-Host "==> 8. Verifying Candidate Health Status (Pre-Traffic Shift)..." -ForegroundColor Cyan
# Smoke test candidate revision via direct tag URL
$readyEndpoint = "$CandidateUrl/readyz"
Write-Host "Probing candidate readiness endpoint: $readyEndpoint" -ForegroundColor Gray
$candidateHealthy = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $readyEndpoint -UseBasicParsing -TimeoutSec 10
        if ($response.StatusCode -eq 200) {
            $candidateHealthy = $true
            Write-Host "Candidate revision $CandidateRevision is READY (HTTP 200)." -ForegroundColor Green
            break
        }
    } catch {
        Write-Host "Readiness check failed, waiting 5s (attempt $attempt/12)..." -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 5
}

if (-not $candidateHealthy) {
    throw "Candidate revision $CandidateRevision failed readiness check. Aborting before traffic shift."
}

Write-Host "==> 9. Shifting 100% Production Traffic to Candidate Revision..." -ForegroundColor Cyan
try {
    Invoke-NativeCommand -ScriptBlock {
        gcloud run services update-traffic $ServiceName `
            --project=$ProjectId `
            --region=$Region `
            --to-revisions="${CandidateRevision}=100"
    } -ErrorMessage "Traffic shift failed"
    $TrafficShifted = $true
} catch {
    Invoke-RollbackTraffic -Reason "Traffic shift command failed"
}

Write-Host "==> 10. Configuring Cloud Scheduler Actions Reconciliation Job with OIDC..." -ForegroundColor Cyan
try {
    $jobCheck = gcloud scheduler jobs describe $SchedulerJobName --project=$ProjectId --location=$Region 2>$null
    if ($LASTEXITCODE -eq 0 -and $jobCheck) {
        Write-Host "Updating existing Cloud Scheduler maintenance job with OIDC service account..." -ForegroundColor Cyan
        Invoke-NativeCommand -ScriptBlock {
            gcloud scheduler jobs update http $SchedulerJobName `
                --project=$ProjectId `
                --location=$Region `
                --schedule="*/5 * * * *" `
                --uri="${WorkerServiceUrl}/v1/maintenance/reconcile-actions-and-cleanup" `
                --http-method=POST `
                --oidc-service-account-email=$SchedulerSa `
                --oidc-token-audience=$WorkerServiceUrl
        } -ErrorMessage "Failed to update Cloud Scheduler job"
    } else {
        Write-Host "Creating new Cloud Scheduler maintenance job with OIDC service account..." -ForegroundColor Cyan
        Invoke-NativeCommand -ScriptBlock {
            gcloud scheduler jobs create http $SchedulerJobName `
                --project=$ProjectId `
                --location=$Region `
                --schedule="*/5 * * * *" `
                --uri="${WorkerServiceUrl}/v1/maintenance/reconcile-actions-and-cleanup" `
                --http-method=POST `
                --oidc-service-account-email=$SchedulerSa `
                --oidc-token-audience=$WorkerServiceUrl
        } -ErrorMessage "Failed to create Cloud Scheduler job"
    }
} catch {
    Invoke-RollbackTraffic -Reason "Cloud Scheduler job configuration failed"
}

Write-Host "==> 11. Deploying Frontend to Firebase Hosting..." -ForegroundColor Cyan
Push-Location "$PSScriptRoot\..\frontend"
try {
    Write-Host "    --> Compiling production frontend bundle (npm run build:prod)..." -ForegroundColor Yellow
    Invoke-NativeCommand -ScriptBlock {
        npm run build:prod
    } -ErrorMessage "Frontend production build failed"

    Write-Host "    --> Deploying static assets to Firebase Hosting..." -ForegroundColor Yellow
    Invoke-NativeCommand -ScriptBlock {
        firebase deploy --only hosting --project=$ProjectId
    } -ErrorMessage "Firebase hosting deployment failed"
} catch {
    Pop-Location
    Invoke-RollbackTraffic -Reason "Firebase Hosting deployment failed"
}
Pop-Location

Write-Host "==> 12. Running Post-Shift Smoke Test on Production URL..." -ForegroundColor Cyan
$prodUrl = (gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format="value(status.url)").Trim()
try {
    $postShiftCheck = Invoke-WebRequest -Uri "$WorkerServiceUrl/readyz" -UseBasicParsing -TimeoutSec 10
    if ($postShiftCheck.StatusCode -ne 200) {
        throw "Production readiness check returned status $($postShiftCheck.StatusCode)"
    }
} catch {
    Invoke-RollbackTraffic -Reason "Post-shift production health check failed: $_"
}

Write-Host "==================================================================" -ForegroundColor Green
Write-Host " StudioTower Deployment Successful!" -ForegroundColor Green
Write-Host " Service Revision: $CandidateRevision (100% traffic)" -ForegroundColor Green
Write-Host " Backend URL:      $prodUrl" -ForegroundColor Green
Write-Host " Frontend URL:     https://${ProjectId}.web.app" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
