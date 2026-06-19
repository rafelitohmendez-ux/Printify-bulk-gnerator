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
- **Background pre-warm queue (P1)**: `queue_worker` keeps N (default 5) unconsumed drafts in MongoDB; `GET /api/capsules/next` atomically pops one with `find_one_and_update` so approve loads instantly. Synchronous fallback when queue is empty.
- **Queue depth indicator** in dashboard counter strip (pip bar + n/target text), polls every 3s.
- **Theme seed selector (P1)**: 18 built-in gothic/industrial themes + custom themes (CRUD list with name + seed prompt). Saved settings flush the pre-warmed queue.
- **Ban-words list (P1)**: User-curated banned words injected into LLM system prompt; flush queue on update.
- **Inline editing pre-approve (P2)**: capsule name, SEO title, and 13 tags are all inline-editable on the dashboard; edits POST as body to `/approve` and override the persisted record.
- **Single-side image regeneration (P2)**: Per-side Regen button on each mockup; `POST /api/capsules/{id}/regenerate-image/{side}`; cache-busted image URL.
- **TTL cleanup (P2)**: `cleanup_worker` runs every 5 min and deletes drafts older than 1 hour to keep MongoDB clean.

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
