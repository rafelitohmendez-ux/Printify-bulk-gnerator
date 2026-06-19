# TwelveHoursCO - Product Requirements Document

## Original Problem Statement
Build a web application for TwelveHoursCO Print-on-Demand business. A bulk product generator and listing approval dashboard where the only operator role is to Approve or Deny AI-generated shirt listings. Aesthetic: gothic, industrial grunge, dark alternative streetwear with stark white ink graphics on solid pure black tees.

## User Personas
- **Solo POD operator (single user, no auth)** — reviews AI-generated shirt design capsules one at a time and clicks Approve / Deny. Exports approved listings to CSV for upload to Printify / Printful.

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + emergentintegrations
  - LLM text: `gemini-3-flash-preview` for capsule concept JSON (name, title, front/back concepts, 13 tags)
  - Image gen: `gemini-3.1-flash-image-preview` (Nano Banana) for both front (left-chest) and back (oversized) shirt prints — stark white on pure black
  - Mongo collection `capsules` stores drafts; status flips to `approved` or row deleted on `denied`
- **Frontend**: React 19 + Tailwind + Shadcn UI + Phosphor icons; Anton (heading) + IBM Plex Mono (body)
- **Routes**: `/` Dashboard, `/history` Approved Archive

## Core Requirements (Static)
1. Generate AI capsule (text + 2 images) per request
2. Single-design dashboard with mockup viewer (front + back tee silhouettes)
3. Display SEO data: Title (keyword formula), THE GRIND description template, 13 tags
4. Approve / Deny buttons → discard or persist + load next
5. History page with CSV export of approved capsules

## Implemented (2026-02 - v1)
- Backend endpoints:
  - `GET /api/` health
  - `POST /api/capsules/generate` — full AI pipeline (text + 2 images in parallel)
  - `POST /api/capsules/{id}/approve` — mark approved
  - `POST /api/capsules/{id}/deny` — delete draft
  - `GET /api/capsules/approved` — list (no base64)
  - `GET /api/capsules/stats` — approved count
  - `GET /api/capsules/{id}/image/{side}` — PNG bytes
  - `GET /api/capsules/export.csv` — CSV download
- Frontend:
  - Dashboard with mockup viewer (SVG tee + AI graphic overlay using mix-blend-screen), SEO panel, large Approve/Deny buttons (white + blood-red)
  - History page with archive grid + CSV export button
  - Counter strip (approved / denied / reviewed)
  - Loading state with scanline + spinner
  - Brutalist industrial typography (Anton + IBM Plex Mono), zero border-radius, grain overlay

## Backlog
- **P1**: Bulk-generation queue (generate N capsules in background)
- **P1**: Variant controls (theme seed selector, ban-words list, regeneration counter)
- **P2**: Direct Printify/Printful API push instead of CSV
- **P2**: Editable fields before approve (tweak title/tags inline)
- **P2**: Image regeneration (regenerate just the back graphic)
- **P2**: Approval comments / notes per capsule
EOF
echo "PRD written"