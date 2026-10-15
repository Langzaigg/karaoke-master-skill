# 手工剪辑段 (hand-built segments: 图片幻灯片 / 混合来源)

A background can need pieces the automatic pipelines do not make: a stills slideshow over part
of the song (story CGs, key visuals, event stills), a prepared clip, or a slideshow cut to the
lyrics. Build such a piece as an **ordinary video file** in the project's `media/`, then stitch
it with `KM oped` (SKILL.md "OP/ED 拼接背景"). This is the production chain for one segment:
cut plan → smooth motion → optional segment-scoped visualiser → stitching → verification.

When to hand-build:

| want | use |
|---|---|
| chosen shots, fixed order, one duration per shot, filling one passage **exactly** | hand-built video segment + `KM oped` |
| the whole song paced automatically from a pool (section-aware, repeats) | 混剪 (montage layer) |
| long takes / official PV pieces, no cutting inside | plain OPED |
| the visualiser on **part** of the background only | composite it into that segment (§3); the `visualize` switch always covers the whole background |

Keep the producing script with the job — the plan numbers (image, seconds) are the design record,
and shots can be re-rendered one at a time.

## 1. 卡点计划 — beat grid, phrase boundaries, exact fill

1. **Grids.** Beat/tempo detection on just that song range (librosa `beat_track`, or reuse the
   engine's own features: `kmlib.mv.job_features` / `beat_grid` — the same bar grid the montage
   planner uses). Phrase boundaries come from the job's timing: lyric-line start times
   (`timing/timed.json` / `timing/project.sug`) and section changes (chorus in, interlude).
2. **Candidates.** Cut points are bar downbeats (every 2 or 4 beats — respect the felt tempo; a
   ballad often half-times, so verify against the vocal) **and** phrase starts. A cut lands on a
   phrase start when one is within ~0.3 s, otherwise on the nearest bar.
3. **Assign.** One image per phrase (or per N bars). Keep shots in a comfortable range (≈3.5–10 s
   for slow material; a single long shot may hold an instrumental passage).
4. **Exact fill.** Convert to frames at the output fps and make the total exact:
   `sum(round(d*fps)) == round(segment_seconds*fps)` — absorb the ±1–2 frame drift in the
   longest shot. Encode every shot with identical settings, then
   `ffmpeg -f concat -safe 0 -i list.txt -c copy seg.mp4`.
5. Sanity-check: cut times monotonic; total == the song interval the segment covers; the last
   cut sits on a boundary the following segment starts at.

```python
plan = [('img_01', 3.6), ('img_02', 5.2), ('img_03', 4.4)]   # (image, seconds) — snapped above
n = [round(d * fps) for _, d in plan]
n[n.index(max(n))] += round(total * fps) - sum(n)            # exact fill: drift to the longest shot
```

## 2. 平滑慢速推拉 — float per-frame rendering, never `zoompan`

**Root cause of the judder.** ffmpeg `zoompan` (and `crop`-expression chains) quantise the
sampling window to **integer pixels** every frame. A slow move (~5 % over 5 s) therefore advances
the window in a regular 0–1 px staircase — the eye reads it as twitching. Supersampling + eased
curves only shrink it; the step is still in the window.

**Fix.** Render every frame yourself with float coefficients and pipe raw frames into the encoder:

```python
"""One shot, slow push/pull, no integer stepping.
base: the source at cover size × (1+zoom) (cover-fitted to the frame aspect) —
      for every 1080p frame it is larger than the frame, so AFFINE never samples outside it.
"""
import subprocess
from PIL import Image

def render_shot(base, out, dur, *, w=1920, h=1080, fps=60, zoom=0.055, push=True):
    n = round(dur * fps)
    cmd = ['ffmpeg', '-y', '-v', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-',
           '-frames:v', str(n), '-c:v', 'libx264', '-preset', 'medium', '-crf', '10', out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    d = max(n - 1, 1)
    for k in range(n):
        u = k / d
        e = u * u * (3 - 2 * u)                        # ease in/out
        z = 1 + zoom * (e if push else 1 - e)          # z >= 1 always (zoom in from 1.0)
        a = (base.width / w) / z;  c = base.width / 2 - (w / 2) * a
        ee = (base.height / h) / z; f = base.height / 2 - (h / 2) * ee
        p.stdin.write(base.transform((w, h), Image.AFFINE, (a, 0, c, 0, ee, f),
                                     resample=Image.BICUBIC).tobytes())
    p.stdin.close(); p.wait()
```

- Alternate the direction per shot (push on even shots, pull on odd); amplitude ≈4–6 % reads as
  calm; add a small deterministic pan if wanted. Deterministic > random — renders must be
  reproducible.
- Performance ≈0.1 s per 1080p frame (BICUBIC): a 2-min 60 fps segment ≈ 13 min — run in the
  background; one file per shot lets you resume/redo a single shot and re-concat with `-c copy`.
- **Verify smoothness**: take 6–8 consecutive frames, align each pair (small mean-shift /
  correlation on a strip) and look at the frame-to-frame displacement — it should vary a few %,
  not 10–20 % (an integer-stepped render shows the regular staircase).
- The engine's own montage renderer draws the same way (float transforms, Qt
  `SmoothPixmapTransform`, per frame) — this is the house style: slow moves are **rendered per
  frame**, not delegated to ffmpeg zoom/crop filters.

## 3. 段内频谱 — visualiser on one segment only

`options.background.visualize` applies to the whole background. For "visualiser here only" (e.g.
a slideshow passage), composite it into that segment file:

- `kmlib.mv.render_overlay(store, st, out)` renders the visualiser **alone on black** at the
  output size/fps → `render/visual_overlay.mp4` (cached by spec + size + fps + audio features;
  the same file the whole-background `visualize` pass would use). Design it with
  `KM mv <job> --overlay @spec.json` — any layer set, any position/size/shape.
- Blend it over the segment, both trimmed to the song window, **in RGB** — screen-blend in YUV
  also adds the chroma planes and tints the frame; black contributes nothing under screen.

```python
off, dur = 84.0, 96.0                    # song seconds this segment covers
fc = ('[0:v]format=gbrp[t];[1:v]format=gbrp[o];'
      '[t][o]blend=all_mode=screen:shortest=1,format=yuv420p[out]')
cmd = [ffmpeg, '-i', seg, '-ss', f'{off}', '-i', overlay, '-ss', f'{off}', '-i', song_wav,
       '-filter_complex', fc, '-map', '[out]', '-map', '2:a', '-t', f'{dur}',
       '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-c:a', 'aac', '-b:a', '192k', out]
```

The output is a normal video file again — nothing downstream knows about the visualiser.

## 4. 拼接 (KM oped)

- `KM oped <job> --add <segment.mp4> --label "…" --credit "…" --section 0-<dur>` — a segment
  carrying the song's own audio **auto-aligns** (shift/confidence printed). A silent segment
  cannot → always place it manually with `--shift sN=<song second where the file's 0.0 lands>`
  (a slideshow cut against the song is planned at absolute song times already — use the plan's
  start).
- `--list` → covered intervals, gaps, overlap priority (earlier `--add` wins).
- **Cache:** `--render` caches on the segment *configuration* (path, shift, section, speed) +
  fade + output format — changing a segment **file's content** is invisible to it. After
  re-generating/replacing a file, `--render --force`. (Same for `visual_overlay.mp4` and the
  engine's `bg_vis_*` composites: they compare spec/size/fps/stamps — delete or regenerate when
  a recipe changes.)
- `--render` writes `media/oped_bg.mp4` (song length, output res/fps; cross-fade between parts,
  freeze frames over gaps — see SKILL.md).

## 5. 导出前验证

- ffprobe every shot / segment: duration == planned, frames == `sum(round(d*fps))`,
  resolution/fps/codec as intended.
- Contact sheet of the segments: one frame per shot in order → check selection and order.
- Motion check on 1–2 shots (§2).
- ffprobe `media/oped_bg.mp4`: duration == song length; spot-check the seam (cross-fade), start
  and end.
- Records: keep the producing script + plan with the job; set `--label` / `--credit` on each oped
  segment so what was built (and from what source) stays recorded.
