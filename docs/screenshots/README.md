# Screenshots

These images are embedded in the top-level `README.md`. To refresh one, replace
the file and keep its name — the README picks it up with no edits.

## What is shown

| File | Page | Must show |
|------|------|-----------|
| `live_text_origin.png` | Chat + Source tab | The hero image: an answer with reference numbers, one of them opened in the Source tab with the passage highlighted on the page. |
| `chat.png` | Chat + Results tab | A cited answer (tables read from a page work well) with the matching documents in the panel beside it. |
| `dashboard.png` | Dashboard | The deadlines / actions overview with a few upcoming items. |
| `document-detail.png` | Document detail dialog | Summary tab plus the PDF beside it. |
| `note-vault.png` | Note vault (`/brain`) | The folder tree with a note open and the Properties panel expanded. |
| `mobile.png` | Phone (PWA) | Three phone shots side by side — chat, document details, dashboard. Built from three single captures, see below. |

## How to shoot

- **Size:** capture at any HiDPI size, then scale to 2000 px wide — the README
  shows them at 820 px, so that is still sharp at 2×:
  `magick f.png -resize '2000x>' -strip f.png`
- **Theme:** light (the default), for all of them.
- **Language:** English UI (Settings → Language).
- **Content:** these ship in a public repo. Blur names, addresses, account and
  policy numbers — of other people especially — and check that the chat text
  does not name what the document view blurs.

## The phone strip

Three 1080 px phone captures, status bar cropped, rounded corners, transparent
gaps (works on GitHub's light and dark background):

```bash
magick -size 600x1244 xc:black -fill white -draw "roundrectangle 0,0,599,1243,36,36" mask.png
for i in 1 2 3; do
  magick shot$i.jpg -strip -crop 1080x2240+0+100 +repage -resize 600x1244! \
    mask.png -alpha off -compose CopyOpacity -composite p$i.png
done
magick p1.png p2.png p3.png -background none -splice 40x0 +append -chop 40x0 mobile.png
```
