# Screenshots

These images are embedded in the top-level `README.md`. The files currently
here are **placeholders** — replace each with a real capture, keeping the exact
filename so the README picks it up with no edits.

## What to capture

| File | Page | Must show | Notes |
|------|------|-----------|-------|
| `chat.png` | Chat | The archive chat with a cited answer — the hero image at the top of the README. | Static screenshot, ~820px+ wide. |
| `dashboard.png` | Dashboard | The deadlines / actions overview populated with a few upcoming items. | |
| `document-detail.png` | Document detail dialog | The per-document dialog open: vision-read page text, an extracted table, actions/deadlines, cross-references. | Pick a document with a table and at least one action so the extraction depth shows. |
| `demo.gif` | Deep research (recording) | A short clip of a multi-step research run — sub-tasks streaming, then the synthesized result. Full-width tile in the gallery. | See "Recording the GIF" below. Keep it ~10–15 s and under ~5 MB. |

## How to shoot

- **Size:** ~1280×800 (16:10) or wider. The README scales them down; higher res
  looks crisp on HiDPI.
- **Theme:** be consistent — pick dark **or** light for all four, don't mix.
- **Content:** use demo / non-sensitive documents. No real invoices, names,
  addresses, account numbers, or anything you would not put on the public
  internet. These ship in the public repo.
- **Format:** PNG, same filename as the placeholder.
- **Language:** English UI (Settings → Language) so the screenshots match the
  default the README describes.

After replacing the files, just view `README.md` on GitHub — no code changes
needed.

## Recording the GIF (KDE Wayland)

1. **Record** with Spectacle (built-in): open Spectacle → *Record Screen* →
   *Rectangular Region* → drag the chat area → record ~10–15 s of one query
   answering (tool calls streaming in, then the cited reply) → stop → save as
   `demo.webm` (or `.mp4`).
2. **Convert to GIF** with the two-pass palette method (crisp, small):

   ```bash
   # tune fps/scale to taste; 12 fps + 900px wide keeps size down
   ffmpeg -i demo.webm -vf "fps=12,scale=900:-1:flags=lanczos,palettegen" -y palette.png
   ffmpeg -i demo.webm -i palette.png \
     -lavfi "fps=12,scale=900:-1:flags=lanczos[x];[x][1:v]paletteuse" \
     -y docs/screenshots/demo.gif
   rm palette.png
   ```

3. **Check the size** — aim for under ~5 MB so the README loads fast. If it's
   too big: lower `fps` (10), narrow `scale` (800), or trim the clip shorter.

GitHub autoplays GIFs in the README, so no click-to-play is needed.
