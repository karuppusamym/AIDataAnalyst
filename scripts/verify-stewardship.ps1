param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$UiUrl = "http://localhost:3000"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-AidaJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        [object]$Body
    )
    $parameters = @{ Uri = $Uri; Method = $Method; Headers = $Headers }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = $Body | ConvertTo-Json -Depth 12
    }
    Invoke-RestMethod @parameters
}

function Approve-Review {
    param([string]$ReviewId)
    Invoke-AidaJson -Uri "$BaseUrl/v1/governance/reviews/$ReviewId/decision" `
        -Method "POST" -Headers $script:checkerHeaders -Body @{
            decision = "APPROVE"
            reason = "Independent stewardship verification approval."
        }
}

$platformHeaders = @{
    "X-Principal-Id" = "stewardship-verifier"
    "X-Roles" = "PlatformAdmin,MetadataAdmin,SemanticAdmin,DataSteward,Analyst,Viewer"
}
$organizations = Invoke-AidaJson -Uri "$BaseUrl/v1/organizations?limit=500" `
    -Method "GET" -Headers $platformHeaders
$selectedOrganization = $null
$selectedSource = $null
$selectedTable = $null
$selectedAnnotation = $null
foreach ($organization in $organizations.items) {
    $candidateHeaders = $platformHeaders.Clone()
    $candidateHeaders["X-Organization-Id"] = $organization.id
    $sources = Invoke-AidaJson `
        -Uri "$BaseUrl/v1/organizations/$($organization.id)/datasources?limit=500" `
        -Method "GET" -Headers $candidateHeaders
    foreach ($source in $sources.items) {
        $annotations = Invoke-AidaJson `
            -Uri "$BaseUrl/v1/datasources/$($source.id)/business-annotations?limit=500" `
            -Method "GET" -Headers $candidateHeaders
        if ($annotations.total -gt 0) {
            $selectedOrganization = $organization
            $selectedSource = $source
            $selectedAnnotation = $annotations.items[0]
            $tables = Invoke-AidaJson -Uri "$BaseUrl/v1/datasources/$($source.id)/tables?limit=500" `
                -Method "GET" -Headers $candidateHeaders
            $selectedTable = $tables.items | Where-Object { $_.id -eq $selectedAnnotation.table_id } `
                | Select-Object -First 1
            break
        }
    }
    if ($null -ne $selectedTable) { break }
}
if ($null -eq $selectedTable) {
    throw "No catalog table with an approved business annotation is available"
}

$makerHeaders = $platformHeaders.Clone()
$makerHeaders["X-Organization-Id"] = $selectedOrganization.id
$checkerHeaders = @{
    "X-Principal-Id" = "stewardship-checker"
    "X-Roles" = "PlatformAdmin,Reviewer,DataSteward,Auditor"
    "X-Organization-Id" = $selectedOrganization.id
}
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$coverageBefore = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/stewardship/coverage" `
    -Method "GET" -Headers $makerHeaders

$category = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-categories" `
    -Method "POST" -Headers $makerHeaders -Body @{
        category_key = "verified_context_$suffix"
        display_name = "Verified context $suffix"
        description = "Terms verified by the complete stewardship lifecycle."
        parent_id = $null
    }
$term = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-terms" `
    -Method "POST" -Headers $makerHeaders -Body @{
        term_key = "verified_term_$suffix"
        display_name = $selectedAnnotation.business_name
        definition = "Authoritative verified definition for $($selectedAnnotation.business_name)."
        category_id = $category.id
        synonyms = @("Verified $suffix", "VT$suffix", "Collision $suffix")
        owner_principal = "verified-steward"
    }
$termReview = Invoke-AidaJson -Uri "$BaseUrl/v1/glossary-term-versions/$($term.id)/submit" `
    -Method "POST" -Headers $makerHeaders
Approve-Review -ReviewId $termReview.id | Out-Null

$rule = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/ownership-rules" `
    -Method "POST" -Headers $makerHeaders -Body @{
        rule_key = "verified_owner_$suffix"
        display_name = "Verified owner $suffix"
        match_field = "TABLE_NAME"
        match_pattern = $selectedTable.name
        owner_type = "GROUP"
        owner_principal = "verified-data-stewards"
    }
$ruleOperation = Invoke-AidaJson -Uri "$BaseUrl/v1/ownership-rules/$($rule.id)/apply" `
    -Method "POST" -Headers $makerHeaders
Approve-Review -ReviewId $ruleOperation.governance_review_id | Out-Null

$individualOperation = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/stewardship/bulk-operations" `
    -Method "POST" -Headers $makerHeaders -Body @{
        operation_type = "ASSIGN_OWNERSHIP"
        subject_type = "TABLE"
        subject_ids = @($selectedTable.id)
        owner_type = "INDIVIDUAL"
        owner_principal = "verified-individual-steward"
        term_id = $null
        rationale = "Assign an individual accountable steward."
        expires_at = $null
        source_rule_id = $null
    }
Approve-Review -ReviewId $individualOperation.governance_review_id | Out-Null

$certificationOperation = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/stewardship/bulk-operations" `
    -Method "POST" -Headers $makerHeaders -Body @{
        operation_type = "CERTIFY_ASSET"
        subject_type = "TABLE"
        subject_ids = @($selectedTable.id)
        owner_type = $null
        owner_principal = $null
        term_id = $null
        rationale = "Verified against the approved semantic and ownership contracts."
        expires_at = [DateTimeOffset]::UtcNow.AddDays(90).ToString("o")
        source_rule_id = $null
    }
Approve-Review -ReviewId $certificationOperation.governance_review_id | Out-Null

$proposals = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-link-proposals/generate" `
    -Method "POST" -Headers $makerHeaders -Body @{ minimum_confidence = 0.75; limit = 200 }
$proposal = $proposals.items | Where-Object {
    $_.term_id -eq $term.term_id -and $_.table_id -eq $selectedTable.id
} | Select-Object -First 1
if ($null -eq $proposal) { throw "Expected inferred glossary link proposal was not generated" }
$proposalReview = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/glossary-link-proposals/$($proposal.id)/submit" `
    -Method "POST" -Headers $makerHeaders
Approve-Review -ReviewId $proposalReview.id | Out-Null

$alternateTerm = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-terms" `
    -Method "POST" -Headers $makerHeaders -Body @{
        term_key = "alternate_verified_term_$suffix"
        display_name = "Alternate verified meaning $suffix"
        definition = "A deliberately competing definition retained for conflict verification."
        category_id = $category.id
        synonyms = @("Collision $suffix")
        owner_principal = "verified-steward"
    }
$alternateReview = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/glossary-term-versions/$($alternateTerm.id)/submit" `
    -Method "POST" -Headers $makerHeaders
Approve-Review -ReviewId $alternateReview.id | Out-Null
$detected = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-conflicts/detect" `
    -Method "POST" -Headers $makerHeaders
$conflict = $detected.items | Where-Object {
    ($_.position_a.term_id -eq $term.term_id -and $_.position_b.term_id -eq $alternateTerm.term_id) -or
    ($_.position_b.term_id -eq $term.term_id -and $_.position_a.term_id -eq $alternateTerm.term_id)
} | Select-Object -First 1
if ($null -eq $conflict) { throw "Expected synonym collision was not detected" }
$conflictReview = Invoke-AidaJson -Uri "$BaseUrl/v1/glossary-conflicts/$($conflict.id)/resolution" `
    -Method "POST" -Headers $makerHeaders -Body @{
        resolution = "MERGE"
        resolved_definition = "Merged governed definition retaining both source positions."
        rationale = "The glossary and semantic annotation describe complementary scopes."
    }
Approve-Review -ReviewId $conflictReview.id | Out-Null

$coverageAfter = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/stewardship/coverage" `
    -Method "GET" -Headers $makerHeaders
Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/stewardship/coverage/snapshots" `
    -Method "POST" -Headers $makerHeaders | Out-Null
$assignments = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/ownership-assignments?subject_type=TABLE&subject_id=$($selectedTable.id)&limit=500" `
    -Method "GET" -Headers $makerHeaders
$links = Invoke-AidaJson -Uri "$BaseUrl/v1/metadata/tables/$($selectedTable.id)/glossary-links" `
    -Method "GET" -Headers $makerHeaders
$resolvedConflicts = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-conflicts?status=RESOLVED&limit=500" `
    -Method "GET" -Headers $makerHeaders
if (
    $assignments.total -lt 2 -or
    ($assignments.items.owner_type -notcontains "GROUP") -or
    ($assignments.items.owner_type -notcontains "INDIVIDUAL") -or
    ($links.items | Where-Object { $_.term_id -eq $term.term_id }).link_type -ne "INFERRED" -or
    ($resolvedConflicts.items.id -notcontains $conflict.id) -or
    $coverageAfter.dimensions.owned.covered -lt 1 -or
    $coverageAfter.dimensions.certified.covered -lt 1
) {
    throw "Stewardship readback did not contain the approved ownership, link, conflict, or coverage evidence"
}

$deprecation = Invoke-AidaJson -Uri "$BaseUrl/v1/glossary-terms/$($term.term_id)/deprecate" `
    -Method "POST" -Headers $makerHeaders -Body @{
        reason = "Verification lifecycle completed; retire this synthetic governed term."
    }
Approve-Review -ReviewId $deprecation.governance_review_id | Out-Null
$alternateDeprecation = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/glossary-terms/$($alternateTerm.term_id)/deprecate" `
    -Method "POST" -Headers $makerHeaders -Body @{
        reason = "Conflict verification completed; retire the alternate synthetic term."
    }
Approve-Review -ReviewId $alternateDeprecation.governance_review_id | Out-Null
$terms = Invoke-AidaJson `
    -Uri "$BaseUrl/v1/organizations/$($selectedOrganization.id)/glossary-terms?limit=500" `
    -Method "GET" -Headers $makerHeaders
$deprecatedTerm = $terms.items | Where-Object { $_.term_id -eq $term.term_id } | Select-Object -First 1
if (
    $deprecatedTerm.lifecycle_status -ne "DEPRECATED" -or
    $deprecatedTerm.category_id -ne $category.id -or
    $deprecatedTerm.synonyms.Count -ne 3
) {
    throw "Governed term category, synonym, or deprecation state was not retained"
}

$ui = Invoke-WebRequest -Uri $UiUrl -UseBasicParsing
foreach ($marker in @(
    "STEWARDSHIP CONTROL CENTER",
    "Infer term links",
    "Detect conflicts",
    "Request bulk stewardship action",
    "Certification expiry"
)) {
    if ($ui.Content -notmatch [regex]::Escape($marker)) {
        throw "Atlas UI is missing stewardship marker: $marker"
    }
}

[pscustomobject]@{
    organization = $selectedOrganization.name
    datasource = $selectedSource.name
    table = $selectedTable.name
    category = $category.display_name
    synonyms = $deprecatedTerm.synonyms.Count
    ownership_assignments = $assignments.total
    inferred_link_type = ($links.items | Where-Object { $_.term_id -eq $term.term_id }).link_type
    resolved_conflict = $conflict.id
    coverage_before = $coverageBefore.overall_score
    coverage_after = $coverageAfter.overall_score
    term_lifecycle = $deprecatedTerm.lifecycle_status
} | ConvertTo-Json -Compress
