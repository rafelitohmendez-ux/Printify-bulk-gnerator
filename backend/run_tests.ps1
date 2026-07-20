$BASE = "https://printify-bulk-gnerator.onrender.com/api"
$KEY  = "e69c365f90a594f5926beb9cd3d9734b6058833cac1f06d09847b381cb2833b7"
$HDR  = @{ "Content-Type" = "application/json"; "X-Admin-Key" = $KEY }
$pass = 0; $fail = 0; $skip = 0; $results = @()

function ok($name)         { $script:pass++; $script:results += "PASSED  $name" }
function fail($name, $msg) { $script:fail++; $script:results += "FAILED  $name -- $msg" }
function skip($name, $msg) { $script:skip++; $script:results += "SKIPPED $name -- $msg" }

function http_code($url, $method="Get", $hdrs=$null) {
    try {
        $a = @{ Uri=$url; Method=$method; TimeoutSec=15; ErrorAction="SilentlyContinue" }
        if ($hdrs) { $a.Headers = $hdrs }
        return [int](Invoke-WebRequest @a).StatusCode
    } catch [System.Net.WebException] {
        return [int]$_.Exception.Response.StatusCode.value__
    } catch { return -1 }
}

# ── TestHealth ────────────────────────────────────────────────────────────────
try {
    $r = Invoke-RestMethod "$BASE/" -TimeoutSec 10
    if ($r.service -eq "MidnightRotation" -and $r.status -eq "online") { ok "TestHealth::test_root" }
    else { fail "TestHealth::test_root" "service=$($r.service) status=$($r.status)" }
} catch { fail "TestHealth::test_root" $_ }

try {
    $r = Invoke-RestMethod "$BASE/capsules/stats" -TimeoutSec 10
    if ($null -ne $r.approved) { ok "TestHealth::test_stats_shape" }
    else { fail "TestHealth::test_stats_shape" "approved missing" }
} catch { fail "TestHealth::test_stats_shape" $_ }

# ── TestAuth — no key ─────────────────────────────────────────────────────────
$protectedRoutes = @(
    @{ m="POST"; p="/capsules/any-probe/approve" },
    @{ m="POST"; p="/capsules/any-probe/deny" },
    @{ m="POST"; p="/capsules/any-probe/push-printify" },
    @{ m="POST"; p="/capsules/any-probe/regenerate-image/front" },
    @{ m="PUT";  p="/settings" },
    @{ m="GET";  p="/printify/shops" },
    @{ m="GET";  p="/printify/print-providers" },
    @{ m="GET";  p="/capsules/next" },
    @{ m="POST"; p="/capsules/generate" }
)

foreach ($route in $protectedRoutes) {
    $name = "TestAuth::no_key_401 [$($route.m) $($route.p)]"
    $c = http_code "$BASE$($route.p)" $route.m
    if ($c -eq 401) { ok $name } else { fail $name "expected 401 got $c" }
}

foreach ($route in $protectedRoutes) {
    $name = "TestAuth::wrong_key_401 [$($route.m) $($route.p)]"
    $c = http_code "$BASE$($route.p)" $route.m @{ "X-Admin-Key" = "wrong-key-xyz" }
    if ($c -eq 401) { ok $name } else { fail $name "expected 401 got $c" }
}

foreach ($route in $protectedRoutes) {
    $name = "TestAuth::correct_key_not_401 [$($route.m) $($route.p)]"
    $c = http_code "$BASE$($route.p)" $route.m $HDR
    if ($c -ne 401) { ok $name } else { fail $name "correct key still got 401" }
}

# ── TestGenerate ──────────────────────────────────────────────────────────────
Write-Host "  [generating capsule — 20-90s]..."
$CID = $null
try {
    $cap = Invoke-RestMethod "$BASE/capsules/generate" -Method Post -Headers $HDR -TimeoutSec 200
    if ($cap.id -and $cap.status -eq "draft" -and $cap.capsule_name -and $cap.title -and $cap.description -and $cap.front_concept -and $cap.back_concept) {
        ok "TestGenerate::test_returns_valid_capsule"; $CID = $cap.id
    } else { fail "TestGenerate::test_returns_valid_capsule" "missing fields" }

    if ($cap.front_image_b64.Length -gt 100 -and $cap.back_image_b64.Length -gt 100) { ok "TestGenerate::test_images_present" }
    else { fail "TestGenerate::test_images_present" "b64 images missing or too short" }

    $lower = $cap.tags | ForEach-Object { $_.ToLower() }
    if ($cap.tags.Count -eq 13 -and $lower -contains "gothic streetwear" -and $lower -contains "back print shirt") { ok "TestGenerate::test_tags_13_with_required" }
    else { fail "TestGenerate::test_tags_13_with_required" "tags=$($cap.tags -join ',')" }

    if ($cap.description.StartsWith("THE GRIND //") -and $cap.description -match "Gildan 5000" -and $cap.description -match "Machine wash cold") { ok "TestGenerate::test_description_template" }
    else { fail "TestGenerate::test_description_template" "desc=$($cap.description.Substring(0,60))" }
} catch {
    fail "TestGenerate::test_returns_valid_capsule" "500/timeout: $($_.Exception.Message)"
    skip "TestGenerate::test_images_present" "no capsule"
    skip "TestGenerate::test_tags_13_with_required" "no capsule"
    skip "TestGenerate::test_description_template" "no capsule"
}

# ── TestImage ─────────────────────────────────────────────────────────────────
if ($CID) {
    foreach ($side in "front","back") {
        try {
            $wc = [System.Net.WebClient]::new(); $bytes = $wc.DownloadData("$BASE/capsules/$CID/image/$side")
            $ct = $wc.ResponseHeaders["Content-Type"]
            $ok = $ct -match "image/(jpeg|png)"
            if ($ct -match "image/jpeg") { $ok = $ok -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xD8 }
            elseif ($ct -match "image/png") { $ok = $ok -and $bytes[0] -eq 0x89 -and $bytes[1] -eq 0x50 }
            if ($ok) { ok "TestImage::test_image_$side" } else { fail "TestImage::test_image_$side" "ct=$ct" }
        } catch { fail "TestImage::test_image_$side" $_ }
    }
    $c = http_code "$BASE/capsules/$CID/image/middle"
    if ($c -eq 400) { ok "TestImage::test_invalid_side_400" } else { fail "TestImage::test_invalid_side_400" "expected 400 got $c" }
} else {
    skip "TestImage::test_image_front" "no capsule"
    skip "TestImage::test_image_back" "no capsule"
    skip "TestImage::test_invalid_side_400" "no capsule"
}

# ── TestApproveFlow ───────────────────────────────────────────────────────────
if ($CID) {
    try {
        $before = (Invoke-RestMethod "$BASE/capsules/stats" -TimeoutSec 10).approved
        $r = Invoke-RestMethod "$BASE/capsules/$CID/approve" -Method Post -Headers $HDR -TimeoutSec 30
        $after = (Invoke-RestMethod "$BASE/capsules/stats" -TimeoutSec 10).approved
        if ($r.id -eq $CID -and $r.status -eq "approved" -and $r.approved_at -and (-not $r.front_image_b64) -and $after -eq $before+1) { ok "TestApprove::test_approve_capsule" }
        else { fail "TestApprove::test_approve_capsule" "id=$($r.id) status=$($r.status) before=$before after=$after" }
    } catch { fail "TestApprove::test_approve_capsule" $_ }

    try {
        $list = Invoke-RestMethod "$BASE/capsules/approved" -TimeoutSec 15
        $found = $list | Where-Object { $_.id -eq $CID }
        if ($found -and $found.status -eq "approved" -and (-not $found.front_image_b64)) { ok "TestApprove::test_approved_list_contains" }
        else { fail "TestApprove::test_approved_list_contains" "not found or wrong state" }
    } catch { fail "TestApprove::test_approved_list_contains" $_ }

    $c = http_code "$BASE/capsules/nonexistent-id-xyz/approve" "Post" $HDR
    if ($c -eq 404) { ok "TestApprove::test_approve_unknown_404" } else { fail "TestApprove::test_approve_unknown_404" "expected 404 got $c" }
} else {
    skip "TestApprove::test_approve_capsule" "no capsule"
    skip "TestApprove::test_approved_list_contains" "no capsule"
    skip "TestApprove::test_approve_unknown_404" "no capsule"
}

# ── TestDenyFlow ──────────────────────────────────────────────────────────────
Write-Host "  [generating second capsule for deny test — 20-90s]..."
try {
    $cap2 = Invoke-RestMethod "$BASE/capsules/generate" -Method Post -Headers $HDR -TimeoutSec 200
    $r = Invoke-RestMethod "$BASE/capsules/$($cap2.id)/deny" -Method Post -Headers $HDR -TimeoutSec 15
    if ($r.ok -eq $true) { ok "TestDeny::test_deny_deletes_capsule" } else { fail "TestDeny::test_deny_deletes_capsule" "ok=$($r.ok)" }
    $c = http_code "$BASE/capsules/$($cap2.id)/image/front"
    if ($c -eq 404) { ok "TestDeny::test_deny_image_gone_404" } else { fail "TestDeny::test_deny_image_gone_404" "expected 404 got $c" }
    $c = http_code "$BASE/capsules/$($cap2.id)/approve" "Post" $HDR
    if ($c -eq 404) { ok "TestDeny::test_approve_after_deny_404" } else { fail "TestDeny::test_approve_after_deny_404" "expected 404 got $c" }
} catch {
    fail "TestDeny::test_deny_deletes_capsule" "generate failed: $($_.Exception.Message)"
    skip "TestDeny::test_deny_image_gone_404" "no capsule"
    skip "TestDeny::test_approve_after_deny_404" "no capsule"
}
$c = http_code "$BASE/capsules/nonexistent-id-xyz/deny" "Post" $HDR
if ($c -eq 404) { ok "TestDeny::test_deny_unknown_404" } else { fail "TestDeny::test_deny_unknown_404" "expected 404 got $c" }

# ── TestExportCSV ─────────────────────────────────────────────────────────────
try {
    $wc = [System.Net.WebClient]::new(); $bytes = $wc.DownloadData("$BASE/capsules/export.csv")
    $ct = $wc.ResponseHeaders["Content-Type"]; $cd = $wc.ResponseHeaders["Content-Disposition"]
    $body = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($ct -match "text/csv" -and $cd -match "attachment" -and $cd -match "midnightrotation_approved.csv" -and $body -match "capsule_name" -and $body -match "front_image_url") { ok "TestExportCSV::test_csv_export" }
    else { fail "TestExportCSV::test_csv_export" "ct=$ct cd=$cd" }
} catch { fail "TestExportCSV::test_csv_export" $_ }

# ── Summary ───────────────────────────────────────────────────────────────────
""
$results | ForEach-Object { Write-Host $_ }
""
Write-Host "================================"
Write-Host "$pass passed  |  $fail failed  |  $skip skipped  ($($pass+$fail+$skip) total)"
if ($fail -gt 0) { exit 1 }
