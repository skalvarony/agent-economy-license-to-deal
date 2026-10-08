---
workflow: general-video
flow: automation
storyboard: no
format: 1920x1080, 30 fps, H.264
duration: 100-115 s (hard limit 120 s)
audio: silent
---

# License to Deal: the demo film

**Message.** What was actually built, shown as the real product: a personal
shopping agent that proposes and cannot pay until you approve; the shop that
verifies the agent and records who bought; the marketplace console; the venue;
both records cross-checked.

**Rules.** Demo mode, not a presentation: only real screen recordings of the
running stack (record/record.mjs), framed with slow punch-ins and pans, hard
cuts on action, one short caption at a time (Inter, #0d0d0d, bottom-left),
product colours only, no diagrams, tiles, shapes or music. Close on the
product mark, "License to Deal", "licensetodeal.app".

**Footage.** `record/reset.sh` (cleans the local stack only), then
`record/record.mjs`, then `record/encode.mjs` -> `project/assets/<take>.mp4`
+ `<take>.markers.json` (cut points). The composition is `project/index.html`.

**How an iteration works.** The shots, their order, timing and the exact
captions are in `SCRIPT.md`; the feedback on each version and what changes
next is in `NOTES.md` (newest entry first). Read both before touching
anything, change only what the latest note asks, render to
`renders/demo-v<N>.mp4` with eight stills in `renders/demo-v<N>-stills/`,
and add the version's line to `NOTES.md`. Never commit renders, frames or
the encoded takes; the scripts and the composition are what the repo keeps.
