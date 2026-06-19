# TwelveHoursCO - Product Requirements Document

## Original Problem Statement
Build a web application for TwelveHoursCO Print-on-Demand business. A bulk product generator and listing approval dashboard where the only operator role is to Approve or Deny AI-generated shirt listings. Aesthetic: gothic, industrial grunge, dark alternative streetwear with stark white ink graphics on solid pure black tees.

## User Personas
- **Solo POD operator (single user, no auth)** — reviews AI-generated shirt design capsules and clicks Approve / Deny. Exports approved listings to CSV.

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + emergentintegrations
  - LLM text: `gemini-3-flash-preview` for capsule concept JSON
  - Image gen: `gemini-3.1-flash-image-preview` (Nano Banana) front + back
  - Two asyncio background workers: queue pre-warmer + draft TTL cleaner
  - Collections: `capsules` (drafts + approved), `settings` (singleton)
- **Frontend**: React 19 + Tailwind + Shadcn UI + Phosphor icons; Anton + IBM Plex Mono
- **Routes**: `/` Dashboard, `/history` Approved Archive

## Implemented

### v1 (2026-02)
- Generator + dashboard + approve/deny + history + CSV export

### v2 (2026-02) — Workflow Acceleration
- Pre-warm queue (5), theme seeds (built-in + custom), ban-words, inline editing, per-side image regen, TTL cleanup

### v3 (2026-02) — Printify Integration
- `printify_client.py`: shop discovery, print provider listing, base64 image upload, draft product creation for Gildan 5000 blueprint (#6)
- Auto-selects black variants from `list_variants`; falls back to first 5 of any color
- Front placement (left-chest, x=0.27 y=0.32 scale=0.18) + Back placement (full-back, x=0.5 y=0.5 scale=1.0)
- `GET /api/printify/shops`, `GET /api/printify/print-providers` for UI discovery
- `POST /api/capsules/{id}/push-printify` for manual on-demand push
- `POST /api/capsules/{id}/approve` auto-pushes when `printify_auto_push` is true + shop/provider are set
- Settings modal: shop dropdown, print provider dropdown, auto-push toggle
- History: "On Printify" badge with link, "Retry Push" on failure, manual "Push" button
- Token stored securely in `/app/backend/.env` (`PRINTIFY_API_TOKEN`)

### v2 Endpoints added
- `GET /api/capsules/next` (atomic queue pop)
- `GET /api/capsules/queue/status` (depth + target)
- `POST /api/capsules/{id}/regenerate-image/{side}`
- `POST /api/capsules/{id}/approve` now accepts `{title, tags, capsule_name}` body
- `GET /api/settings`, `PUT /api/settings`

## Deferred Backlog
- **P2 (deferred — user said "later")**: Direct Printify push (skip CSV). Needs `PRINTIFY_API_TOKEN` + `PRINTIFY_SHOP_ID`. Architecture sketch:
  - On approve, optionally call Printify Create Product API with description + tags + linked image URLs (need image hosting first — base64 → object storage)
  - Object storage integration required for image URLs to be public

## Backlog
- Printify direct push (requires user keys + image hosting)
- Bulk approve mode (approve N visible drafts in a grid)
- Per-theme analytics (which themes get approved most)
- Image hosting integration (object storage) — required for Printify push
EOF
