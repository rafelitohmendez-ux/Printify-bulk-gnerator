$BASE = "https://printify-bulk-gnerator.onrender.com/api"
$KEY  = "e69c365f90a594f5926beb9cd3d9734b6058833cac1f06d09847b381cb2833b7"
$HDR  = @{ "Content-Type" = "application/json"; "X-Admin-Key" = $KEY }
$PRINTIFY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIzN2Q0YmQzMDM1ZmUxMWU5YTgwM2FiN2VlYjNjY2M5NyIsImp0aSI6IjM5ODlhOTdhMzI3YjFiM2RiMDlhNTA5OTVkNTRlMWRlODVmNDE5YTZmMmNmMGQ1ZTAzNGI4MzhlZjQ3ZjVlMzczODU0MTQ4ZTEyNzJiN2UzIiwiaWF0IjoxNzgyMzQxNjQ5LjM4NzUyLCJuYmYiOjE3ODIzNDE2NDkuMzg3NTIyLCJleHAiOjE4MTM4Nzc2NDkuMzgwNzgyLCJzdWIiOiI4MzcxMTMzIiwic2NvcGVzIjpbInNob3BzLm1hbmFnZSIsInNob3BzLnJlYWQiLCJjYXRhbG9nLnJlYWQiLCJvcmRlcnMucmVhZCIsIm9yZGVycy53cml0ZSIsInByb2R1Y3RzLnJlYWQiLCJwcm9kdWN0cy53cml0ZSIsIndlYmhvb2tzLnJlYWQiLCJ3ZWJob29rcy53cml0ZSIsInVwbG9hZHMucmVhZCIsInVwbG9hZHMud3JpdGUiLCJwcmludF9wcm92aWRlcnMucmVhZCIsInVzZXIuaW5mbyJdfQ.qChXyUvsE-6XykthqLtavKlPwoDrnlz88zhAMS7BnvqcilOfbtZ-bUPCb2dJV7sisYeA1fEeSQYshL7vAEVJFEX_rJh7nnlEJN7BTUle6vK6mKos4Gn05CEGYW3-52MXpzlwoVwPnAs4Fz5MJKESwrLz3m18OWa3lHx5I2HCBtp4bX3oLNdpUDqZrNDZFdU3MoHhAUBPoXDHPM2xb6_jbb_YI9TpEzQ7XBL_ct5Ub_NdKkoigzWpozKySah2qwCZzuNE0fhImt8cQJqDTldOw_u1xSUizq01BMxj-EfgfO4yn_biyCX3XQ-Rm3c5yXIfRLowLWkg03PQdOGUKtHjWEh9hhokzzQ2HrTkBespNuzKWrjtL_BHd1Q9uCYjnZyNtAqrGxQFKt8_xHAAoqYBbqzXGZiLp76dd49SVgXvR3_KoCNCOtO4P7PIDX1B4xG_jNbbKbR6cU6C4CoadWl9NDFF0qyMdLvMkRlyBdPra09uXkm21iU8UlA8DMZP-qsivGZWpvMTh5vtCy0Y4mwKgDAUG7AHSvWHqot9VBfwuoKAev7u4V2WWhZmmw6xuzsullNCoh2T8tnJzI-K026fkElms0gGnVCr33OYhUBcSnagZO5D9KcNuQtfDcw8SGn0nRKTFESofPdEIbWZEFOtu728L5fIpy5vudQ_oSygyqY"
$SHOP_ID = 3121777
$PRINTIFY_BASE = "https://api.printify.com/v1"
$PHDRS = @{ "Authorization" = "Bearer $PRINTIFY_TOKEN"; "User-Agent" = "MidnightRotation-Dashboard/1.0" }

# ── Step 1: Use already-approved capsule (skip re-generating) ─────────────────
$CAPSULE_ID = "5c64fa7d-0287-4fee-ba4d-b4c3c475d35b"
Write-Host "Using approved capsule: $CAPSULE_ID (Ruined Hymnal)"

# ── Step 2: Push to Printify ──────────────────────────────────────────────────
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Pushing to Printify (120s timeout)..."
try {
    $push = Invoke-RestMethod "$BASE/capsules/$CAPSULE_ID/push-printify" `
        -Method Post -Headers $HDR -TimeoutSec 120
    $productId = $push.printify_product_id
    Write-Host "  ok=$($push.ok)  printify_product_id=$productId"
} catch {
    $resp = $_.Exception.Response
    if ($resp) {
        $body = [System.IO.StreamReader]::new($resp.GetResponseStream()).ReadToEnd()
        Write-Host "  Push FAILED $($resp.StatusCode.value__): $body"
    } else {
        Write-Host "  Push FAILED: $_"
    }
    exit 1
}

if (-not $productId) { Write-Host "No product_id returned. Aborting."; exit 1 }

# ── Step 3: Fetch product from Printify directly ──────────────────────────────
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Fetching product $productId from Printify..."
$product = Invoke-RestMethod "$PRINTIFY_BASE/shops/$SHOP_ID/products/$productId.json" `
    -Method Get -Headers $PHDRS -TimeoutSec 30

# ── a) Variant prices ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "── a) VARIANT PRICES ────────────────────────────────────"
$prices = $product.variants | Select-Object -ExpandProperty price -Unique
foreach ($p in $prices) {
    $dollars = [math]::Round($p / 100, 2)
    Write-Host "  price=$p  (USD $dollars)"
}
$badPrices = @($prices | Where-Object { $_ -ne 4499 })
if ($badPrices.Count -eq 0) {
    Write-Host "  PASS: all variants priced at 4499 (`$44.99)"
} else {
    Write-Host "  FAIL: unexpected prices found: $($badPrices -join ', ')"
}

# ── b) Image default / selected-for-publishing ────────────────────────────────
Write-Host ""
Write-Host "── b) IMAGE DEFAULT / SELECTED-FOR-PUBLISHING ───────────"
foreach ($img in $product.images) {
    $src = ($img.src -split "\?")[0].Split("/")[-1]
    Write-Host "  position=$($img.position)  is_default=$($img.is_default)  is_selected_for_publishing=$($img.is_selected_for_publishing)  file=$src"
}
$defaultImg = @($product.images | Where-Object { $_.is_default -eq $true })
Write-Host ""
if ($defaultImg.Count -eq 0) {
    Write-Host "  FAIL: no image has is_default=true"
} else {
    $defaultPos = $defaultImg[0].position
    if ($defaultPos -eq "back") {
        Write-Host "  PASS: default image is position=back"
    } else {
        Write-Host "  FAIL: default image is position=$defaultPos, expected back"
    }
}
