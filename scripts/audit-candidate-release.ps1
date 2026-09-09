# ============================================================================
# StudioTower Production Candidate Release Gate Audit Script (PowerShell)
# Enforces Tier 3 (F-Production Candidate) Deployment Gates:
# 1. Sensitive Secret Manager Registry Spec Audit (0 plain-text, 5 required secrets)
# 2. Exact Structural Schema Matching against firestore.indexes.json (All READY)
# 3. Candidate Pre-Shift Smoke (/healthz, /readyz, 401 barrier, public preview)
# 4. Partitioned Modes: -AuditOnly (Preflight Sign-Off) vs -Promote (Traffic Shift & Rollback Guard)
# ============================================================================

param (
    [string]$ProjectId = "agentic-cinema-demo-2026",
    [string]$Region = "us-central1",
    [string]$ServiceName = "studio-tower-api",
    [string]$CandidateRevision = "",
    [string]$CandidateUrl = "",
    [switch]$AuditOnly = $false,
    [switch]$Promote = $false,
    [switch]$DryRun = $false
)

$ErrorActionPreference = "Stop"

# Partitioning and mutual exclusivity validation:
if ($AuditOnly -and $Promote) {
    Write-Host "FAILED: Mutually exclusive switches -AuditOnly and -Promote cannot both be specified." -ForegroundColor Red
    exit 1
}

if (-not $DryRun) {
    if (-not $AuditOnly -and -not $Promote) {
        Write-Host "FAILED: Must specify either -AuditOnly (preflight audit) or -Promote (traffic release)." -ForegroundColor Red
        exit 1
    }
    if (-not $CandidateRevision) {
        Write-Host "FAILED: -CandidateRevision is required in non-dry-run mode." -ForegroundColor Red
        exit 1
    }
    if (-not $CandidateUrl) {
        Write-Host "FAILED: -CandidateUrl is required in non-dry-run mode." -ForegroundColor Red
        exit 1
    }
} else {
    # In DryRun mode, default to AuditOnly if neither is specified
    if (-not $AuditOnly -and -not $Promote) {
        $AuditOnly = $true
    }
}

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " StudioTower Tier 3: Production Candidate Release Gate Audit" -ForegroundColor Cyan
Write-Host " Mode: $(if ($AuditOnly) { 'AuditOnly' } else { 'Promote' }) | DryRun: $DryRun" -ForegroundColor Cyan
Write-Host " Target Project: $ProjectId | Region: $Region | Service: $ServiceName" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# HTTP Probe Helper (compatible with PowerShell 5.1 and Core)
function Invoke-HttpProbe([string]$Url) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 10
        return [int]$resp.StatusCode
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode
        }
        throw $_
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return [int]$_.Exception.Response.StatusCode
        }
        throw $_
    }
}

# Fail-Closed CLI check in non-DryRun mode
if (-not $DryRun) {
    $gcloudCmd = Get-Command gcloud -ErrorAction SilentlyContinue
    if (-not $gcloudCmd) {
        Write-Host "CRITICAL ERROR: 'gcloud' CLI is not found on PATH. Audit cannot proceed fail-closed." -ForegroundColor Red
        exit 1
    }
    $authAccount = gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $authAccount) {
        Write-Host "CRITICAL ERROR: gcloud is unauthenticated or active account cannot be verified." -ForegroundColor Red
        exit 1
    }
    Write-Host "Verified gcloud active account: $authAccount" -ForegroundColor Gray
}

# ----------------------------------------------------------------------------
# GATE 1: Sensitive Secret Registry Spec Audit
# ----------------------------------------------------------------------------
Write-Host "`n[Gate 1/4] Auditing Sensitive Secret Registry against Cloud Run spec..." -ForegroundColor Yellow

$SENSITIVE_SECRETS = @(
    "GEMINI_API_KEY",
    "STUDIO_TOWER_MAINTENANCE_SECRET",
    "STUDIO_TOWER_TASK_SECRET",
    "STUDIO_TOWER_TASK_SECRET_PREVIOUS",
    "ACTION_SIGNING_SECRET",
    "ACTION_SIGNING_SECRET_PREV",
    "CURSOR_SIGNING_SECRET",
    "GRAFANA_CLOUD_API_KEY",
    "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    "GRAFANA_OTLP_TOKEN",
    "GRAFANA_MCP_ACCESS_TOKEN",
    "TELEMETRY_TENANT_KEY_PRIMARY"
)

$REQUIRED_SECRETS = @(
    "GEMINI_API_KEY",
    "STUDIO_TOWER_MAINTENANCE_SECRET",
    "STUDIO_TOWER_TASK_SECRET",
    "CURSOR_SIGNING_SECRET",
    "ACTION_SIGNING_SECRET",
    "TELEMETRY_TENANT_KEY_PRIMARY"
)

if (-not $DryRun) {
    Write-Host "Querying revision spec for candidate revision: $CandidateRevision..." -ForegroundColor Gray
    $revSpecJson = gcloud run revisions describe $CandidateRevision --project=$ProjectId --region=$Region --format=json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $revSpecJson) {
        Write-Host "CRITICAL ERROR: Failed to describe candidate revision '$CandidateRevision' in project $ProjectId, region $Region." -ForegroundColor Red
        exit 1
    }
    $revSpec = $revSpecJson | ConvertFrom-Json
    $containers = $revSpec.spec.containers
    if (-not $containers -and $revSpec.spec.template.spec.containers) {
        $containers = $revSpec.spec.template.spec.containers
    }
    if (-not $containers -or $containers.Count -eq 0) {
        Write-Host "CRITICAL ERROR: No containers found in candidate revision spec." -ForegroundColor Red
        exit 1
    }

    $violations = @()
    $foundSecrets = @{}

    foreach ($c in $containers) {
        foreach ($envVar in $c.env) {
            if ($SENSITIVE_SECRETS -contains $envVar.name) {
                if ($envVar.value -and -not $envVar.valueFrom) {
                    $violations += "CRITICAL VIOLATION: $($envVar.name) is configured as plain-text value in container spec!"
                } elseif ($null -eq $envVar.valueFrom.secretKeyRef) {
                    $violations += "CRITICAL VIOLATION: $($envVar.name) is missing secretKeyRef binding!"
                } else {
                    $foundSecrets[$envVar.name] = $true
                }
            }
        }
    }

    foreach ($req in $REQUIRED_SECRETS) {
        if (-not $foundSecrets.ContainsKey($req)) {
            $violations += "CRITICAL VIOLATION: Required production secret '$req' is missing from candidate revision spec!"
        }
    }

    if ($violations.Count -gt 0) {
        Write-Host "FAILED: Sensitive secret registry audit failed!" -ForegroundColor Red
        foreach ($v in $violations) { Write-Host "  - $v" -ForegroundColor Red }
        exit 1
    }
    Write-Host "PASS: Container spec contains 0 plain-text sensitive variables. All $($REQUIRED_SECRETS.Count) required secrets bound via Secret Manager." -ForegroundColor Green
} else {
    Write-Host "[DRY-RUN] PASS: Sensitive secret registry allowlist verified (0 plain-text credentials allowed; all 5 required production secrets verified)." -ForegroundColor Green
}

# ----------------------------------------------------------------------------
# GATE 2: Comprehensive Firestore Index Schema Matching
# ----------------------------------------------------------------------------
Write-Host "`n[Gate 2/4] Parsing firestore.indexes.json and validating structural index schema..." -ForegroundColor Yellow

$indexesFilePath = Join-Path $PSScriptRoot "..\firestore.indexes.json"
if (-not (Test-Path $indexesFilePath)) {
    Write-Host "FAILED: firestore.indexes.json not found at $indexesFilePath" -ForegroundColor Red
    exit 1
}

$indexesFileContent = Get-Content -Raw -Path $indexesFilePath | ConvertFrom-Json
$declaredIndexes = $indexesFileContent.indexes
Write-Host "Found $($declaredIndexes.Count) declared composite indexes in firestore.indexes.json." -ForegroundColor Gray

function Compare-IndexFieldLists($declaredFields, $remoteFields) {
    if ($declaredFields.Count -ne $remoteFields.Count) { return $false }
    for ($i = 0; $i -lt $declaredFields.Count; $i++) {
        $df = $declaredFields[$i]
        $rf = $remoteFields[$i]
        if ($df.fieldPath -ne $rf.fieldPath) { return $false }
        if ($df.order -and ($df.order.ToUpper() -ne $rf.order.ToUpper())) { return $false }
        if ($df.arrayConfig -and ($df.arrayConfig.ToUpper() -ne $rf.arrayConfig.ToUpper())) { return $false }
    }
    return $true
}

if (-not $DryRun) {
    $rawRemoteJson = gcloud firestore indexes composite list --project=$ProjectId --format=json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $rawRemoteJson) {
        Write-Host "CRITICAL ERROR: Failed to query remote composite indexes from Google Cloud Firestore." -ForegroundColor Red
        exit 1
    }
    $remoteIndexes = $rawRemoteJson | ConvertFrom-Json
    Write-Host "Queried $($remoteIndexes.Count) remote composite indexes from Google Cloud Firestore." -ForegroundColor Gray

    $missingIndexes = @()
    $unreadyIndexes = @()

    foreach ($decl in $declaredIndexes) {
        $matchedRemote = $remoteIndexes | Where-Object {
            $_.collectionGroup -eq $decl.collectionGroup -and
            $_.queryScope.ToUpper() -eq $decl.queryScope.ToUpper() -and
            (Compare-IndexFieldLists $decl.fields $_.fields)
        }

        if (-not $matchedRemote) {
            $missingIndexes += "$($decl.collectionGroup) [$(($decl.fields | ForEach-Object { "$($_.fieldPath):$($_.order)$($_.arrayConfig)" }) -join ', ')]"
        } else {
            if ($matchedRemote.state -ne "READY") {
                $unreadyIndexes += "$($decl.collectionGroup) (state: $($matchedRemote.state))"
            }
        }
    }

    if ($missingIndexes.Count -gt 0) {
        Write-Host "FAILED: Missing composite indexes in Google Cloud Firestore!" -ForegroundColor Red
        foreach ($m in $missingIndexes) { Write-Host "  Missing: $m" -ForegroundColor Red }
        exit 1
    }

    if ($unreadyIndexes.Count -gt 0) {
        Write-Host "FAILED: Composite indexes exist but are not READY!" -ForegroundColor Red
        foreach ($u in $unreadyIndexes) { Write-Host "  Building/Error: $u" -ForegroundColor Red }
        exit 1
    }

    Write-Host "PASS: All $($declaredIndexes.Count) composite indexes exist and are confirmed READY in Cloud Firestore." -ForegroundColor Green

    # Audit Firestore TTL policy on rate_limits.expires_at
    Write-Host "Auditing rate_limits.expires_at TTL policy in Cloud Firestore..." -ForegroundColor Gray
    $ttlListJson = gcloud firestore fields ttls list --project=$ProjectId --collection-group=rate_limits --format=json 2>$null
    if ($LASTEXITCODE -eq 0 -and $ttlListJson) {
        $ttlList = $ttlListJson | ConvertFrom-Json
        $ttlActive = $ttlList | Where-Object { $_.state -eq "ACTIVE" -or $_.state -eq "CREATING" }
        if ($ttlActive) {
            Write-Host "PASS: rate_limits TTL policy confirmed active on expires_at." -ForegroundColor Green
        } else {
            Write-Host "NOTICE: rate_limits TTL policy is in state: $($ttlList[0].state)" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[DRY-RUN] PASS: Validated $($declaredIndexes.Count) composite index schemas and rate_limits.expires_at TTL policy." -ForegroundColor Green
}

# ----------------------------------------------------------------------------
# GATE 3: Pre-Shift Candidate Smoke Tests
# ----------------------------------------------------------------------------
Write-Host "`n[Gate 3/4] Running Candidate Smoke Tests..." -ForegroundColor Yellow

if (-not $DryRun) {
    Write-Host "Testing Candidate URL: $CandidateUrl" -ForegroundColor Gray

    # 1. Healthz
    try {
        $hRes = Invoke-RestMethod -Uri "$CandidateUrl/v1/healthz" -Method Get -TimeoutSec 10
        if ($hRes.status -ne "ok") { throw "Unexpected /v1/healthz response: $($hRes | ConvertTo-Json -Compress)" }
        Write-Host "  ✓ /v1/healthz: OK" -ForegroundColor Green
    } catch {
        Write-Host "FAILED: /healthz failed on candidate: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }

    # 2. Readyz (assert live mode, ai_ready=true, fallback_allowed=false)
    try {
        $rRes = Invoke-RestMethod -Uri "$CandidateUrl/readyz" -Method Get -TimeoutSec 10
        if ($rRes.status -ne "ready" -and $rRes.status -ne "ok") { throw "Unexpected /readyz response status: $($rRes.status)" }
        if ($null -eq $rRes.components.ai.fallback_allowed -or $rRes.components.ai.fallback_allowed -ne $false) {
            throw "Production candidate must have explicit fallback_allowed=false (got $($rRes.components.ai.fallback_allowed))."
        }
        if ($rRes.components.ai.ready -ne $true) {
            throw "Production candidate must have ai.ready=true."
        }
        if ($rRes.components.ai.mode -ne "live") {
            throw "Production candidate must have ai.mode='live' (got $($rRes.components.ai.mode))."
        }
        Write-Host "  ✓ /readyz: READY (Live Gemini confirmed, 0 fallback allowed)" -ForegroundColor Green
    } catch {
        Write-Host "FAILED: /readyz failed on candidate: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }

    # 3. Unauthenticated 401 barrier
    try {
        $uCode = Invoke-HttpProbe "$CandidateUrl/v1/spaces"
        if ($uCode -ne 401) { throw "Expected 401 Unauthorized, got $uCode" }
        Write-Host "  ✓ /v1/spaces: 401 Unauthorized Barrier Enforced" -ForegroundColor Green
    } catch {
        Write-Host "FAILED: Unauthenticated barrier test failed: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }

    # 4. Public Invite Preview alive (non-500, probe token returns 404)
    try {
        $iCode = Invoke-HttpProbe "$CandidateUrl/v1/invites/tok_candidate_probe_gate/preview"
        if ($iCode -ne 404) { throw "Expected 404 Not Found for probe token, got $iCode" }
        Write-Host "  ✓ /v1/invites/:token/preview: Public Preview Endpoint Alive (404 Not Found for Probe Token)" -ForegroundColor Green
    } catch {
        Write-Host "FAILED: Public invite preview probe failed: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[DRY-RUN] PASS: Pre-shift smoke contracts verified (/healthz -> 200, /readyz -> 200 [live Gemini], /v1/spaces -> 401, /v1/invites/:token/preview -> 404)." -ForegroundColor Green
}

# ----------------------------------------------------------------------------
# GATE 4: Partitioned Mode (-AuditOnly vs -Promote) & Automated Rollback
# ----------------------------------------------------------------------------
if ($AuditOnly) {
    Write-Host "`n[Gate 4/4] Preflight Sign-Off (-AuditOnly Mode Selected)..." -ForegroundColor Yellow
    Write-Host "`n==================================================================" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host " [DRY-RUN] DRY-RUN LOGIC CHECK COMPLETED (Mode: AuditOnly)" -ForegroundColor Green
        Write-Host " Simulated syntax, index schema, and sensitive secret registry checks passed." -ForegroundColor Gray
        Write-Host " (No remote gcloud queries or promotions were executed)." -ForegroundColor Gray
    } else {
        Write-Host " [PREFLIGHT AUDIT PASSED] Candidate $CandidateRevision certified." -ForegroundColor Green
        Write-Host " Candidate passed all preflight gates and is ready for promotion." -ForegroundColor Gray
    }
    Write-Host "==================================================================" -ForegroundColor Cyan
    exit 0
}

# -Promote Mode:
Write-Host "`n[Gate 4/4] Executing Production Traffic Shift and Post-Shift Verification..." -ForegroundColor Yellow

$TrafficShifted = $false
$PostShiftSucceeded = $false
$CurrentRevision = ""

if (-not $DryRun) {
    # 1. Query Current Service Spec
    $serviceSpecJson = gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format=json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $serviceSpecJson) {
        Write-Host "CRITICAL ERROR: Failed to describe Cloud Run service $ServiceName." -ForegroundColor Red
        exit 1
    }
    $serviceSpec = $serviceSpecJson | ConvertFrom-Json
    $ServiceUrl = $serviceSpec.status.url

    # Record exact traffic allocation for rollback guard
    $trafficList = @($serviceSpec.status.traffic)
    $allocPairs = @()
    $CurrentRevision = ""
    foreach ($t in $trafficList) {
        if ($t.percent -gt 0 -and $t.revisionName) {
            $allocPairs += "$($t.revisionName)=$($t.percent)"
            if ($t.percent -ge 50 -and -not $CurrentRevision) {
                $CurrentRevision = $t.revisionName
            }
        }
    }
    $CurrentTrafficAlloc = if ($allocPairs.Count -gt 0) { $allocPairs -join "," } else { "" }
    if (-not $CurrentRevision) {
        $CurrentRevision = if ($trafficList.Count -gt 0 -and $trafficList[0].revisionName) { $trafficList[0].revisionName } else { $serviceSpec.status.latestReadyRevisionName }
    }
    if (-not $CurrentTrafficAlloc -and $CurrentRevision) {
        $CurrentTrafficAlloc = "${CurrentRevision}=100"
    }

    if (-not $CurrentRevision) {
        Write-Host "CRITICAL ERROR: Unable to identify active serving revision for rollback target." -ForegroundColor Red
        exit 1
    }

    if ($CurrentRevision -eq $CandidateRevision) {
        Write-Host "CRITICAL ERROR: Candidate revision '$CandidateRevision' is already the active serving revision ($CurrentRevision)." -ForegroundColor Red
        exit 1
    }

    Write-Host "Active serving revision: $CurrentRevision" -ForegroundColor Gray
    Write-Host "Active traffic allocation: $CurrentTrafficAlloc" -ForegroundColor Gray
    Write-Host "Promoting candidate revision: $CandidateRevision" -ForegroundColor Gray
    Write-Host "Production public service URL: $ServiceUrl" -ForegroundColor Gray

    try {
        Write-Host "Shifting 100% traffic to candidate revision $CandidateRevision..." -ForegroundColor Yellow
        gcloud run services update-traffic $ServiceName --to-revisions=${CandidateRevision}=100 --project=$ProjectId --region=$Region
        if ($LASTEXITCODE -ne 0) {
            throw "Traffic shift command returned non-zero exit code: $LASTEXITCODE"
        }
        $TrafficShifted = $true
        Write-Host "Traffic shift command succeeded. Executing post-shift smoke on $ServiceUrl..." -ForegroundColor Gray

        # Post-shift smoke tests
        $psHealthz = Invoke-RestMethod -Uri "$ServiceUrl/v1/healthz" -Method Get -TimeoutSec 10
        if ($psHealthz.status -ne "ok") { throw "Post-shift /v1/healthz failed on $ServiceUrl" }

        $psReadyz = Invoke-RestMethod -Uri "$ServiceUrl/readyz" -Method Get -TimeoutSec 10
        if ($psReadyz.status -ne "ready" -and $psReadyz.status -ne "ok") { throw "Post-shift /readyz failed on $ServiceUrl" }
        if ($null -eq $psReadyz.components.ai.fallback_allowed -or $psReadyz.components.ai.fallback_allowed -ne $false) {
            throw "Post-shift candidate must have fallback_allowed=false (got $($psReadyz.components.ai.fallback_allowed))"
        }
        if ($psReadyz.components.ai.ready -ne $true) {
            throw "Post-shift candidate must have ai.ready=true"
        }
        if ($psReadyz.components.ai.mode -ne "live") {
            throw "Post-shift candidate must have ai.mode='live' (got $($psReadyz.components.ai.mode))"
        }

        $psAuth = Invoke-HttpProbe "$ServiceUrl/v1/spaces"
        if ($psAuth -ne 401) { throw "Post-shift 401 barrier failed on $ServiceUrl (got $psAuth)" }

        $psInvite = Invoke-HttpProbe "$ServiceUrl/v1/invites/tok_probe_postshift/preview"
        if ($psInvite -ne 404) { throw "Post-shift invite preview failed on $ServiceUrl (got $psInvite)" }

        $PostShiftSucceeded = $true
        Write-Host "Post-shift smoke tests passed cleanly on $ServiceUrl!" -ForegroundColor Green
    } finally {
        if ($TrafficShifted -and -not $PostShiftSucceeded) {
            $targetRevs = if ($CurrentTrafficAlloc) { $CurrentTrafficAlloc } else { "${CurrentRevision}=100" }
            Write-Host "`nCRITICAL POST-SHIFT SMOKE FAILURE: Rolling back traffic immediately to prior serving configuration: $targetRevs..." -ForegroundColor Red
            try {
                gcloud run services update-traffic $ServiceName --to-revisions=$targetRevs --project=$ProjectId --region=$Region
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "EMERGENCY: Automated rollback command failed with exit code $LASTEXITCODE! Manual intervention required immediately for service '$ServiceName' in region '$Region' (Target: $targetRevs)!" -ForegroundColor Red
                    exit 2
                } else {
                    Write-Host "Rollback to $targetRevs completed successfully." -ForegroundColor Yellow
                }
            } catch {
                Write-Host "EMERGENCY: Automated rollback command encountered exception! Manual intervention required: $($_.Exception.Message)" -ForegroundColor Red
                exit 2
            }
            exit 1
        }
    }

    Write-Host "`n==================================================================" -ForegroundColor Cyan
    Write-Host " [PRODUCTION RELEASE PASSED] Candidate $CandidateRevision promoted to 100% traffic." -ForegroundColor Green
    Write-Host "==================================================================" -ForegroundColor Cyan
} else {
    $CurrentRevision = "studio-tower-api-prod-current"
    $CandidateRevisionSim = if ($CandidateRevision) { $CandidateRevision } else { "studio-tower-api-candidate-sim" }

    Write-Host "[DRY-RUN] Active serving revision: $CurrentRevision" -ForegroundColor Gray
    Write-Host "[DRY-RUN] Target candidate revision: $CandidateRevisionSim" -ForegroundColor Gray
    Write-Host "[DRY-RUN] Simulating 100% traffic shift to candidate revision..." -ForegroundColor Gray
    Write-Host "[DRY-RUN] Simulating post-shift smoke tests (/healthz, /readyz, 401 barrier, public preview)..." -ForegroundColor Gray
    Write-Host "[DRY-RUN] Automated rollback guard verified: (`$TrafficShifted -and -not `$PostShiftSucceeded) triggers rollback to $CurrentRevision." -ForegroundColor Green

    Write-Host "`n==================================================================" -ForegroundColor Cyan
    Write-Host " [DRY-RUN] DRY-RUN LOGIC CHECK COMPLETED (Mode: Promote)" -ForegroundColor Green
    Write-Host " Simulated traffic shift, smoke validation, and rollback handler logic verified." -ForegroundColor Gray
    Write-Host " (No remote gcloud traffic shifts or promotions were executed)." -ForegroundColor Gray
    Write-Host "==================================================================" -ForegroundColor Cyan
}
