# CadenceOS Static Assets

Drop your files into this folder (`Golf dashboard/static/`) with the exact names below.
All images degrade gracefully to emoji fallbacks when missing.

---

## ✅ Already in place

| File | Used in | Notes |
|------|---------|-------|
| `cadenceos-logo.png` | Sidebar (top), Landing nav | Transparent background recommended. Displayed at 64×64px in sidebar, 44px tall in nav |
| `golf-god.png` | Golf God button, Chat header, Round analysis comment | Circular crop. Displayed at 32×32px (button), 52×52px (chat), 40×40px (round analysis) |
| `icon-simulator.png` | Sidebar nav — Simulator tab | Displayed at 26×26px. Dark or transparent background |
| `icon-realcourse.png` | Sidebar nav — Real Course tab | Displayed at 26×26px |
| `icon-mybag.png` | Sidebar nav — My Bag tab | Displayed at 26×26px ✅ (copied from App icons folder) |

---

## ❌ Still missing — add these to unlock all icons

| File | Used in | Fallback | Suggested image |
|------|---------|----------|-----------------|
| `icon-allrounds.png` | Sidebar nav — All Rounds tab | 🏠 | Dashboard/home icon, or golf scorecard |
| `icon-tournament.png` | Sidebar nav — Tournaments tab | 🏆 | Trophy or tournament bracket icon |

---

## Optional / future

| File | Used in | Notes |
|------|---------|-------|
| `og-image.png` | Landing page Open Graph meta tag | 1200×630px, used when sharing link on social |
| `favicon.ico` | Browser tab | 32×32px or 64×64px |
| `apple-touch-icon.png` | iOS home screen bookmark | 180×180px |

---

## Sizing guide

- **Logo**: transparent PNG works best. The sidebar will show it at 64×64px (scales down to 32×32 when sidebar collapses).
- **Nav icons**: 64×64px source recommended (displayed at 26×26px). Transparent or dark background. White/light artwork looks best on the dark theme.
- **Golf God**: Square or portrait. Circular crop applied automatically. Minimum 128×128px recommended for crispness at retina.
