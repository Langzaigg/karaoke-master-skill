# Review recipes

How to turn stage-3 requests into `km.py` calls. JSON goes into a temp file passed as `@file`.

## Timing edit ops (`KM edit <job> @ops.json`)

Line indices are **0-based timing lines** (stage-3 table number − 1); times are seconds (`t`) or
milliseconds (`ms`). Several ops can go in one list; checkpoints are kept monotonic afterwards.

| op | fields | use for |
|---|---|---|
| `shift_all` | `ms` | whole song early/late |
| `shift_lines` | `lines: [i…]` or `from`,`to`, `ms` | one line / a passage early/late |
| `set_line_span` | `line, start, end` | stretch/squeeze a line into a new range (linear) |
| `set_char` | `line, char, cp?, t` | move one checkpoint (char 0-based, cp = mora index) |
| `set_line_end` | `line, t` | when the last syllable is released |
| `set_pause` | `line, char, t` (`t: "auto"` = last voiced moment from the vocal stem; `t: null` removes) | breath / release after a syllable inside the line |
| `set_singer` | `lines`, `singer` (SUG singer id from `timing/timed.json → singers`) | duet re-assignment after timing |
| `set_ruby` | `line, ruby: [[start, end, "かな"]]` | fix a reading; then `KM realign <job> --lines i+1` |

Payload of a page `prompt` includes `t` (playhead seconds) and `active_line` (0-based line being
sung at the playhead) — use them for "这句 / 这里 / 刚才那句".

| user says | do |
|---|---|
| 第 12 行晚了 0.2 秒 / 第 12 句慢了 | `[{"op":"shift_lines","lines":[11],"ms":-200}]` |
| 整体字幕慢半拍 | `[{"op":"shift_all","ms":-150}]` (ask how much if unclear; 0.1–0.2 s typical) |
| 这句结尾拖太长 | `set_line_end` at the last voiced moment (look at `qa` / vocal waveform) |
| 副歌第二句走字太快 | find the line, `realign --lines N` with a wider `--window` |
| 「明日」应该读 あした | `set_ruby` on that line, then `realign` |
| 第 3–6 行是美雲唱的 | before timing: `lines --singer 3-6=mikumo`; after: `set_singer` with the SUG id |

## Style patch (`KM style <job> @patch.json`)

```json
{"template": "neon", "effect": "glow", "ruby": true, "title": true,
 "overrides": {"font_size_px": 110, "letter_spacing_px": 6, "line_y_margin_px": 80},
 "singer_styles": {"mikumo": {"color": "#8E7CFF"}},
 "background": {"type": "color", "color": "#101426"}, "resolution": "1920x1080", "fps": 60}
```

`style` merges `overrides` / `singer_styles`, rewrites the page's overlay style and keeps the job's
other options. Re-run `KM previews <job>` only when the user wants to compare galleries again.

Templates (`template`): `classic` 经典卡拉OK · `tactic` TACTIC 蓝白发光 · `sakura` 樱色偶像 ·
`neon` 霓虹夜 · `mincho` 明朝·抒情 · `anime_op` 动画 OP 风 · `minimal` 简洁白.

Effects (`effect`): `classic` 淡入淡出 · `utopia` 柔光擦色 · `char_fade` 逐字浮现 · `glow` 辉光 ·
`sparkle` 星光闪烁 · `petal` 花瓣飘落 · `scanline` 扫描光 · `zoom_pulse` 跳动放大 · `notes` 音符飘出 ·
`wave` 波浪 + 粒子消散.

Useful `overrides` (any `krok_helper.subtitle_render.domain.models.Style` field is accepted; values
are at 1080p and rescaled to the output height):

| field | meaning |
|---|---|
| `font_family`, `font_weight`, `font_size_px`, `letter_spacing_px` | main text font |
| `stroke_width_px`, `stroke2_enabled`, `stroke2_width_px` | outline / second outline |
| `decoration_kind` (`none`/`shadow`/`glow`), `glow_radius_px`, `glow_concentration_level` | decoration |
| `ruby_font_size_px`, `ruby_gap_px` | furigana size / distance |
| `line_y_position` (`bottom`/`center`/`top`), `line_y_margin_px`, `line_gap_px`, `horizontal_margin_px` | layout |
| `line_lead_in_ms` (default 1800), `line_tail_ms` (1000) | how early lines appear / linger |
| `entry_anim`, `exit_anim`, `karaoke_anim`, `sing_fx` | per-song effects (see `effects` above) |
| `vertical` | vertical (縦書き) layout |

Colours per state live in the template; for a one-off colour change prefer switching template or
`singer_styles`. Background types: `source` (the job's video), `video` (`path`: another local
video, picture only), `mv` (the AMV design; audio-only default — `spectrum` is an old alias),
`montage` (the image montage design), `subs` (subtitles only, `color`), plus `color` and `image`
(`path` relative to the job, e.g. an upload). `fps`: 30 or 60.

## QA flags (`KM qa`)

| note | meaning | usual fix |
|---|---|---|
| 没有时间戳 | line not aligned (e.g. after `set_ruby`) | `realign` |
| 人声能量很低，疑似错位 | line sits in silence | `realign` (maybe `--window` from `match` estimates) |
| 与语音识别位置相差 ±X 秒 | disagrees with ASR; ASR can be ±1 s off — check the synced reference | realign if > 2.5 s |
| 语速过快 | line squeezed into too little time | realign with neighbours |
| 行持续偏长 | slow line; fine for ballads | listen |
| 第 k 字…持续 X 秒，其中 Y% 无人声 | breath inside a syllable | `set_pause` / realign |
| 与下一行重叠 | end overlaps next start | `set_line_end` or realign both |
| N 个字时长不足 45ms | squeezed syllables | realign |

## MV design

Two designs per job: the AMV (`render/mv_spec.json`) and the image montage
(`render/montage_spec.json`); a spec with a `montage` layer is saved as the montage design. Write
one with `KM mv <job> --spec @spec.json` (`--kind` to force), print it with `KM mv <job> --show
[--kind montage]`. A spec with only `"preset"` (+ `theme`/`notes`/`palette`) is completed from that
preset. Without `fps` the background renders at the output frame rate (30 / 60).

```json
{"preset": "starry_prayer", "theme": "祈愿 · 星空 · 生命", "notes": "歌词围绕祈祷与生命……",
 "palette": "auto", "fps": 30, "seed": 7,
 "layers": [{"type": "gradient", "stops": [[0, "$0"], [1, "$1"]]}, {"type": "stars", "count": 220}]}
```

Rules: layers draw bottom → top. Colours are hex or palette refs `"$0".."$4"` (`"auto"` derives
the palette from the cover: dark → light accents). Positions / sizes are fractions of the frame
(`x` of width, `y`/sizes of height). Any layer can react to the music with `"react": "bass" |
"mid" | "high" | "energy" | "onset" | "flux"` and strength `"k"`. `"optional": true` drops a
cover-based layer when there is no cover. Keep the lower ~35 % calm for the karaoke lines (dark
ground, a `lyrics_band`), put busy motifs above. Presets (`--list-presets`): `starry_prayer` 星空祈愿,
`sakura_spring` 樱花, `rainy_night` 雨夜, `neon_city` 霓虹都市, `ocean_summer` 海边夏日, `winter_snow`
冬雪, `sunset_nostalgia` 夕阳怀旧, `cosmic_battle` 宇宙战歌, `anime_montage` 动画 / 游戏混剪,
`spectrum_classic` 经典频谱; montage presets `anime_montage` 标准混剪, `montage_cinematic` 电影感混剪,
`montage_beat` 高燃踩点混剪.

| layer `type` | key fields |
|---|---|
| `gradient` | `stops [[pos, colour]…]` vertical sky / base |
| `cover_bg` | blurred cover fill: `blur`, `dim` |
| `nebula` | soft colour clouds: `colors`, `react` |
| `stars` | `count`, `y1` (lowest star), `twinkle`, `color` |
| `aurora` | `colors`, `y`, `amp` |
| `moon` / `sun` | `x`, `y`, `r`, `color`, `color2`, `glow`, `stripes` (sun, retro) |
| `clouds` | `count`, `y0`, `y1`, `color`, `alpha` (drifting) |
| `light_rays` | `x`, `y`, `count`, `color` (god rays, react recommended) |
| `silhouette` | `shape`: `hills` / `mountains` (`snow`) / `city` (`windows` colour) / `tree` (`x`, `size`) / `waves`; `y`, `color`, `layers` |
| `grid` | retro perspective floor: `horizon`, `color`, `speed` |
| `image` | `src` (`"cover"` or a file path), `shape` `circle` / `rounded`, `x`, `y`, `size`, `spin` (deg/s), `ring` colour |
| `montage` | beat-synced image montage from `mv-assets` (below) |
| `spectrum` | `style` `bars` / `mirror` / `ring` (`x`, `y`, `radius`) / `horizon` / `wave`; `x0`, `x1`, `y`, `height`, `bands`, `colors`, `alpha` |
| `waveform` | oscilloscope line: `x0`, `x1`, `y`, `height`, `color`, `alpha` |
| `pulse` | radial glow on the beat: `x`, `y`, `r`, `color`, `react` |
| `flash` | full-frame flash on hits: `color`, `react: "onset"`, `k` |
| `particles` | `kind` `rain` / `petals` / `sparkles` / `fireflies` / `snow` / `bokeh` / `embers` / `dust` / `bubbles`; `count`, `color`, `y0`, `y1` |
| `lyrics_band` | darkening band for the lyrics: `from` (y), `alpha` |
| `letterbox` | cinema bars top and bottom: `size`, `color` |
| `vignette` (`strength`), `grain` (`amount`), `scanlines` (`alpha`) | finishing |
| `progress` | thin progress bar: `color` |
| `title` | `text` / `sub` (`{title}`, `{artist}`), `show [t0, t1]`, `y`, `size`, `font`, `color` |

### Montage layer

```json
{"type": "montage", "bars": 2, "min_shot": 1.5, "max_shot": 10, "transitions": "auto",
 "trans": 0.5, "dim": 0.08, "fit": "cover",
 "exclude": ["a1b2c3"], "pins": [{"t": 92.5, "asset": "d4e5f6"}]}
```

| field | meaning |
|---|---|
| `bars` | bars per shot at normal energy (halved when loud, doubled when quiet); raised automatically when the pool is too small |
| `min_shot`, `max_shot` | shot length limits (s) |
| `transitions` | `auto` (default, smooth: a crossfade at every cut, longer at section starts and in quiet parts) · `beat` (energetic: snap zoom at section starts, white flash on strong hits, hard cuts) · or one of `cut` / `crossfade` / `zoom` / `flash`; `trans` = crossfade seconds |
| `punch` | zoom kick on every onset, default 0 — on still images it looks like twitching, so only for explicitly "燃" edits and only when the user wants it |
| `dim` | darken the images so lyrics stay readable |
| `fit` | `cover` (crop to fill) or `contain` (whole image on a blurred copy; used automatically for very tall / wide images) |
| `prefer` | origin priority when the pool has more images than needed, default `["user", "web", "video"]` (the user's packs first); `"even"` = proportional. Chosen images are interleaved by origin |
| `exclude`, `images` | skip these asset ids / use only these, in this order |
| `pins` | force an image onto a moment (a character's solo line, the title drop) |

Every shot gets a slow, deterministic Ken Burns move. Pool entries carry `origin` (`user` image
pack / `web` / `video` scene) and the original file `name`. `render/montage_plan.json` has the full
plan (each shot: `t0`, `t1`, `asset`, `why` = `new` / `repeat` / `pin` / `fallback`), `KM mv-assets
<job> --plan` the summary. Image sourcing rules are in SKILL.md (ask before downloading, record
`--source` / `--credit`).
