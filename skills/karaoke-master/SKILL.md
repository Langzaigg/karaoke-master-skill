---
name: karaoke-master
description: >-
  Make karaoke (NicoKara-style 卡拉OK / 走字 / 逐字字幕) videos and timing projects from a song
  title, an audio file or a video (MV / MAD / 切片) through a local three-stage web page:
  ① gather song info, lyrics + furigana, find which part of the media is sung, and let the user
  confirm template / effects / multi-singer styles with engine-rendered previews;
  ② auto-time the lyrics (vocal separation + ASR + forced alignment) with live progress;
  ③ review in the browser, apply natural-language tweaks, export .sug / .yurika / LRC / MP4,
  on vocal + off vocal versions, a transparent subtitle MOV and optional Hi-Res (lossless FLAC)
  MKVs, at 30 or 60 fps. Backgrounds: the source video, the song's MV downloaded from YouTube /
  Bilibili and aligned to the user's audio, an AMV designed for the song (themed scene
  + audio visualizers), a beat-synced montage of images (found online, the user's image packs, or
  both) or of video clips cut from a game's / anime's footage on YouTube (trailers, gameplay
  recordings — 游戏素材卡点混剪), or subtitles only. Japanese and English songs (word-level
  NetEase / Kugou lyrics are used for English timing). Use whenever the user wants 卡拉OK字幕、
  打轴、歌词时间轴、走字视频、伴奏版 / off vocal、歌曲 MV / AMV、游戏 / 动画素材混剪卡点的 KTV,
  karaoke subtitles, or to turn a
  song / MV / MAD into a karaoke video. Built on the Lin-K Lyrics repo (krok_helper) and its
  StrangeUtaGame (SUG) submodule.
---

# Karaoke Master

You (the agent) drive every step with `scripts/km.py`; the user watches and decides in a local
web page served by `km.py serve`. The page and you talk only through the job directory:
`job.json` (state you write) and `inbox.jsonl` (actions the user posts, read with `km.py wait`).

```
user ─▶ web page ──POST──▶ inbox.jsonl ──km.py wait──▶ you ──km.py <cmd>──▶ job.json ──SSE──▶ web page
```

Deterministic page actions (nudges, style previews, engine frames, exports, applying the stage-1
form) are executed by the server itself; `km.py wait` only wakes you for things that need judgement.

## Hard rules

1. Lyrics come from `lyrics-search` results or text the user provides (page "粘贴歌词" →
   `uploads/`). Change lines with index-based tools (`lines`, `edit`) so word timings shift along.
2. All user-facing text (page messages via `say`, `--status`, song notes) is **Chinese**.
3. JSON arguments: write the JSON to a temp file and pass `@path` (PowerShell mangles inline quotes).
4. Line numbers: `lines`/`--include`/`--singer` use **1-based stage-1 lyric lines** (all candidate
   lines). `edit` ops use **0-based timing lines** (only included lines, as shown in stage 3).
   `realign --lines` and the stage-3 table use **1-based timing lines**.
5. Keep the page informed: post a `say` message after every meaningful step, and set
   `--status` while long jobs run. Don't leave the user staring at a silent page.
6. Do not edit `krok_helper/` (incl. the StrangeUtaGame code in `krok_helper/lyrics_timing`); the skill only calls it.

## Throughput — heavy jobs in the background, keep working

Never sit idle waiting for a render. Start heavy jobs in the background (no timeout — an export
can legitimately take 10-30+ min, page servers run for hours) and use the time for the next
song's timing / editing work:

- **Can run alongside an export** (render workers + hardware encoder): `analyze`, `timing` /
  `realign` (English mode), `match`, `qa`, `edit`, `style` / `mv --spec`, footage downloads and
  scans, lyrics search, song info, typo scans, page / docs edits.
- **Serialize these** (they fight for the same resource): two exports at once (render processes
  + encoder), an export next to `--enhance` (encoder + Vulkan compute), a Japanese forced
  alignment next to an export (heavy CPU/GPU). When in doubt, one heavy job at a time; light
  steps always fit in the gaps.
- The same applies to `--enhance` and footage downloads: queue them in the background and
  continue with stage 1-3 work on other projects.

## Stage 0 — environment and project

The skill folder only holds the install wizard, requirement lists, scripts and the page. The
wizard expands everything else into an **install home**:

```bash
python <skill>/scripts/km.py setup --check          # any Python 3.10–3.13, stdlib only; prints JSON
python <skill>/scripts/km.py setup [--target user|project|<dir>]
```

- `--target user` (default) → `%LOCALAPPDATA%\KaraokeMaster` / `~/.karaoke-master`;
  `project` → `<cwd>/.karaoke-master` (ask the user which they prefer on first install).
  The home holds `venv/` (app runtime), `ai_venv/` (AI runtime), `models/`, `km_config.json`,
  and `engine/` when needed (below); KTV projects live elsewhere (next sections). ~3–5 GB, 5–20 min → run in the background.
- **Engine** = the Lin-K Lyrics code the skill calls (`krok_helper` + the StrangeUtaGame backend). If
  the skill sits inside a checkout (the minimal `skill` branch or a full Lin-K Lyrics repo) it is used
  in place (a missing SUG submodule is initialised); otherwise setup clones the `skill` branch of this
  fork into `<home>/engine` (falling back to the pinned Lin-K Lyrics release).
- The AI runtime is a **CPU baseline**; it works everywhere. If `setup --check` lists GPUs, try
  hardware acceleration (next section) once per machine — optional.
- From now on `KM` means the `km` value it printed: `"<home venv python>" "<skill>/scripts/km.py"`.

### Hardware acceleration (try it, don't assume it)

`KM accel` prints the GPUs, the DirectML adapter order (Windows), ffmpeg's compiled-in hardware
encoders and what the active AI runtime can actually use (torch devices, ONNX Runtime providers,
CTranslate2 CUDA). Four independent things can be accelerated:

| stage | engine | knob (`KM accel --set key=value`) |
|---|---|---|
| forced alignment (biggest win) | PyTorch inside SUG's worker; any `torch.device` string is passed through | `align_device` (`cuda`, `xpu`, `mps`, `cuda:1` …) |
| vocal separation | ONNX Runtime via audio-separator | `onnx` (`cuda` / `dml` / `auto`), `dml_device` (adapter index, `igpu`, `dgpu`) |
| ASR | faster-whisper / CTranslate2 | `asr_device`, `asr_compute` |
| MP4 encode | ffmpeg via the renderer | `encoder` (`nvenc`, `qsv`, `amf_qvbr`, `videotoolbox`, `auto`), `codec` (`h264` / `hevc`) |

How to approach it: identify the vendor/generation from `KM accel`, look up (web search) which
PyTorch build and ONNX Runtime execution provider support that GPU and this Python version, and
install them into a **separate** venv under the install home (keep `ai_venv` as the working CPU
fallback); then `KM accel --set ai_python=<new venv python>` and the device knobs, re-run
`KM accel` to verify the device is visible, then check it on **one short real step** (e.g. `realign`
of a few lines on a test job) and compare with the CPU time already in the job log. Never run stress
tests, benchmarks or several GPU workloads at once — consumer machines (laptop iGPU + dGPU, other
GPU apps running) can hang or crash; ask the user before any long GPU trial. Things to know: only one `onnxruntime*` flavour may be
installed in a venv (faster-whisper pulls the CPU one; reinstall the GPU flavour afterwards); an
encoder listed by ffmpeg may still fail on this machine (test a short encode); integrated GPUs are
worth trying too (DirectML adapter choice via `dml_device`). Every GPU path already falls back to
CPU on failure and logs it; `KM_DEVICE` / `KM_ONNX` / `KM_ENCODER` / `KM_AI_PYTHON` override one run.
`align_jobs` (parallel alignment processes) is opt-in for machines with headroom. Tell the user
what you enabled and how you verified it.

### One project = one folder + one page (like ppt-master)

Every KTV edit is its own **project**: a folder `projects/<name>_<YYYYMMDD>` (in the current working
folder; `--dir` / `KM_PROJECTS` to put it elsewhere) holding the media copies, analysis, timing,
renders and state, plus its own page server. Below, `<job>` = the project folder path.

```bash
KM new "<video|audio path or song title>" [--name N] [--title T --artist A] [--out <output folder>]
KM serve <job> --daemon   # starts (or reuses) this project's page in the background, prints {"url": ...}
...                       # stages 1–3
KM stop <job>             # at the very end: closes this project's page server
```

- `--out` sends finished files to a folder of the user's choice (e.g. `<media folder>/output`);
  otherwise they go to `<job>/export/`. Change later with `KM set <job> --options @f` →
  `{"output_dir": "..."}`.
- The page server's URL / port / pid are in `<job>/live_preview/lock.json`; `serve` never starts a
  second server for the same project. Tell the user the URL.
- **Closing**: when everything is delivered, run `KM stop <job>` (the page shows that the project
  has ended). The user can also click "完成并关闭" in the page: the server stops and `wait`
  returns a `page_closed` action — wrap up (final `say` is no longer visible; answer in the chat)
  and don't restart the page unless the user asks.
- Same song, another version (e.g. an AMV and a montage from the same FLAC):
  `KM new <same source file> --like <project>` copies song info, lyrics, analysis and timing into a
  new project — go straight to the background design and export.
- A title-only project has no media: ask the user for an audio/video file (or a URL they
  downloaded) before stage 2; you can still search lyrics.

## Stage 1 — material analysis and confirmation

Run these in this order; start the slow one first.

0. **Hi-Res / lossless source (optional, decide first).** Ask whether the user has a lossless
   release of the song (FLAC / WAV, often Hi-Res). If yes, register it before anything else:
   `KM hires-source <job> --on "<原唱 file>" [--off "<伴奏 file>" …]`. It is waveform-aligned to the
   media timeline (prints the shift and confidence; a MAD with cuts → warning, then it is not
   used), stored as `media/hires_on.wav`, and becomes the **timing audio**: separation, ASR,
   alignment, the off-vocal track and the Hi-Res MKVs all use it instead of the lossy media audio.
   The page's output card has the same fields (changing them re-registers; the server handles it).
   `--clear` reverts to the media audio. For audio-only jobs a lossless input file is already used
   as timing audio automatically.
1. **Analysis (background, 3–6 min on CPU):** `KM analyze <job>` — vocal separation (SUG's
   UVR-MDX-NET-Inst_HQ_3 model, ONNX) + faster-whisper ASR on
   the vocal stem of the timing audio. Progress shows on the page. ASR runs in `song.language`
   (default `ja`): for an English song set it first — `KM set <job> --song @f` with
   `{"language": "en"}` — or re-run `analyze` (the separation is reused).
2. **Identify the song** from the file name, `KM status <job>` → `media.tags`, and what the user
   said; use web search to complete it: official title notation, artist, tie-in work, 作詞/作曲/編曲,
   language, singers (for groups: each member; for anime/idol units the characters + CV), and
   **歌割り** (who sings which line) when a reliable source exists. Write `song.json` and run
   `KM set <job> --song @song.json`. Fields: `title artist work lyricist composer arranger language
   confidence(0–1) notes(Chinese: how you identified it, caveats) sources[{label,url}]
   singers[{id,name,color,colors?}]` (`colors` = 拼色 bands, see 5). Single artist → one singer (the artist). Group → first singer is the
   whole group ("全員"/unit name) used as default, then members.
3. **Lyrics:** `KM lyrics-search <job> --query "<title> <artist>" --utaten-query "<title> <artist>"`
   (UtaTen misses decorated titles such as `ALIVE～祈りの唄～`; simpler variants like `ALIVE ワルキューレ`
   are tried automatically — if there is still no `utaten-*` candidate, try the main title word + artist yourself).
   Choose with `KM lyrics-use <job> <candidate-id>`:
   - Japanese → prefer `utaten-*` (authoritative furigana). Synced versions (`qm/ne/kg/lrclib`) are
     used automatically as timing reference by `match`.
   - No UtaTen → the synced candidate whose duration matches the media. Missing readings are
     filled by SUG's analyzer (shown dimmer, marked `auto`).
   - Want LRC line breaks but UtaTen readings → `lyrics-use <lrc-id> --ruby-from <utaten-id>`.
   - **Word-level (逐字) lyrics** — candidates with `has_verbatim` (NetEase YRC `ne-*`, Kugou KRC
     `kg-*`) keep every word's start and length (`line.words`). Prefer one whose duration matches
     the media: English songs are timed from it (stage 2), other songs use it to correct the
     aligner. Compare its text with the other candidates; where they disagree on a word, check
     `analysis/asr.json` around that time. It is usually finer than an LRC the
     user brings — search even when the user gives one.
   - **The user's .lrc**: `lyrics-use <job> <file.lrc>` keeps its line times as the `match`
     reference (candidate `user-lrc`); NetEase credit lines (`{"t":0,"c":…}`, `作词 : …`) are not
     lyrics — they are printed in `notes` (useful for `song.json`).
   - Typos in the source — synced lyric sites have them (`it,ll`, `all.,`, `word ,`, a missing
     space `becomeour`, misspellings `Commenmorate`): scan the lines with regexes for punctuation
     slips and, for English, every word against a word list (SUG ships CMU / e2k lists under
     `krok_helper/lyrics_timing/src/strange_uta_game`), and report the offending word / position.
     Fix with `KM lines <job> --fix-char N:pos=TEXT` — the character at 0-based `pos` becomes
     `TEXT` (0–3 chars: `’` replaces, `"e "` inserts a space after an `e`, empty deletes); word
     times and readings shift along. Then re-run `timing` (English: seconds).
   - Lines wider than ~17 full-width chars won't fit: add `--split-long 17` (or `lines --split-long 17`
     later; Latin letters count as half, a comma near the middle is preferred), or `lines --split N:pos`.
     After timing, `qa` also flags lines the engine draws wider than the frame (「超出画面宽度」, from
     the review page's layout): `edit` `[{"op":"split_line","line":i,"char":k}]` splits the timed
     line at character `k` (0-based, e.g. the space after a comma) keeping every timestamp, and
     splits the stage-1 line the same way.
4. **Locate the sung lines** (after `analyze` finished): `KM match <job> --apply`. Output per line:
   `✓/·  score  est-time  source(asr|lrc)  ref=reference line  note`. ASR anchors + the synced
   reference give a piecewise offset (handles MAD cuts); lines predicted outside vocal activity are
   dropped. Review it:
   - "参考歌词中没有这一行" = usually parenthesised backing chants / spoken parts → keep excluded
     unless clearly lead vocals; mention them to the user.
   - A run of consecutive missing lines in the middle = the MAD cut that part. Repeated chorus in
     the edit = `lines --dup N` then re-run `match --apply`.
   - Check the printed song region; fix with `KM set <job> --segment start,end` if needed.
5. **Singers (歌割り)** — take them from a source, never from listening (harmonies and unison
   parts cannot be told apart by ear or by the vocal stem). Sources in this order:
   1. a subtitle / lyric file the user gives (ASS with per-singer styles, actors or colours; LRC/TXT
      with `名前：` / `【名前】` prefixes);
   2. a reliable 歌割り source: official booklet or karaoke data, fan パート分け pages that
      colour-code members (`F&M&K`-style markers = those members together), fansub ASS;
   3. nothing found → keep the default singer and tell the user.
   After timing, import it with **`KM singers <job> --from <file|URL> --map KEY=歌手 …`**: it reads
   the runs (ASS Name/Style/inline colour, LRC prefix, HTML colour, or a JSON list you write), aligns
   them to the timed characters and sets every character's singer (mid-line switches included). Run
   it once without `--map` to see the keys (`need_map`: colours / names with character counts); map
   unwanted keys (legends, translations) to `-`. Check `coverage` (≈1.0) and the `uncovered` /
   `weak` line numbers; tell the user which source you used.
   **Fewer colours, fewer mistakes:** one colour per lead singer, and parts sung together become
   one 「和声」 singer drawn in **拼色** — `{"name":"和声","color":"<blend>","colors":["#lead1","#lead2"]}`
   in `song.singers` splits every glyph into horizontal bands of those colours (a singer named
   「A＆B」 after existing singers gets their bands automatically). Members who only sing backing
   chorus need no colour of their own; ask before showing more than ~4 colours. Backing lines that
   overlap the lead (e.g. "(ha～)") or whose position the source does not give stay excluded —
   mention them. Before timing, whole lines can be pre-assigned with `KM lines <job> --singer 1-4=<id>`.
6. **Background and output format** (page card "输出设置"; you set defaults, the user changes them):
   - **视频** — the source video (default for video jobs).
   - **MV 视频**（audio projects）— the song's MV downloaded from YouTube / Bilibili with Lin-K
     Lyrics' video downloader, or a local video; only its picture is used, aligned to the song (see
     "MV from the web" below). Typical for "I have the lossless song, use the official MV / anime OP·ED".
   - **AMV** — a themed scene you design for the song (default for audio-only jobs), see
     "AMV design" below.
   - **混剪** — a beat-synced montage of images (found online, the user's image packs, or both;
     see "Image montage") and / or of **video clips** cut from a game's / anime's own footage —
     trailers, PVs, gameplay recordings found on YouTube / Bilibili or local videos (see
     "Video-clip montage"). Typical for "用这个游戏的实况 / PV 卡点剪辑做背景".
   - **仅 KTV 字幕** — subtitles only on a plain colour (`{"type": "subs", "color": "#000000"}`;
     green `#00B140` for keying); pair it with export kind `alpha` = a transparent ProRes 4444 MOV
     to lay over the user's own footage in an editor.
   - Frame rate **30 or 60 fps** (`options.fps`), resolution, export kinds.
   Set it with `KM style <job> @patch.json` → `{"background": {...}}` (validates a video path and
   prepares AMV / montage stills) or let the user click in the page (`set_background` action).
7. **Previews:** AMV / montage jobs first make the design (below) so previews have a background.
   Then `KM previews <job>` (~10–30 s): template, effect (8-frame sprites) and per-singer cards
   rendered by the real subtitle engine on the user's own media with the song's first lines.
8. Post a summary with `KM say` (song identified, lyric source, N lines located / excluded and
   why, region, anything uncertain, the background idea; mention the optional Hi-Res 混流 if a
   lossless release exists), then `KM set <job> --await-user --status "请确认制作方案"`.
9. **Wait loop:** `KM wait <job> --timeout 540` (repeat until something arrives; it prints
   `{"actions": [...]}`). Handle:

| action `type` | meaning | what you do |
|---|---|---|
| `confirm_stage1` | user confirmed (server already wrote it into job.json) | go to stage 2 |
| `use_lyrics` `{candidate}` | switch lyric source | `lyrics-use`, `match --apply`, `previews`, `say` |
| `custom_lyrics` `{path}` | user pasted lyrics (supports `漢字(かんじ)`, `{漢字|かな}`, `[歌手]`) | `lyrics-use <job> <path>`, `match --apply`, `previews`, `say` |
| `set_background` `{background}` | user picked a background (already applied; default stills rendered) | AMV not designed for this song yet → design it; 图片混剪 → ask where the images should come from unless `montage_source` is set; `previews`, `say` |
| `montage_source` `{source: web\|user\|mixed}` | where montage images come from (saved in `options.montage_source`) | `web` / `mixed`: search and import images (the choice is the user's go-ahead to download; still say which sites); `user`: wait for the pack, say how many images the plan wants |
| `mv_assets_import` `{paths}` | the user uploaded an image pack (already imported, stills re-rendered) | check `mv-assets --plan`; say how many were used; top up with web images if `mixed` and short |
| `mv_video_use` / `mv_video_local` `{url}` / `{path}` | the user picked an MV in the page (already downloaded, aligned, previews refreshed) | read `media.mv_video.notes` (`KM status --full`); a bad alignment → suggest another candidate; `say` |
| `prompt` `{text, stage, t, active_line}` | free-text request | interpret, act, reply with `say` |
| `handled_by: "server"` items | nudges, previews, exports, Hi-Res registration, preset / stills / gallery, image removal / re-plan — already done | context only (react if the user's choices change your plan) |

### MV from the web (audio projects)

`KM mv-video <job> --search "<artist> <title> MV"` lists YouTube results (also stored for the page).
Pick the real MV: an official channel or the anime OP / ED footage, duration close to the song,
**motion video** — "… - Topic" uploads and many fan uploads are a still cover (tiny file for its
length is a hint). `KM mv-video <job> --info "#k"` prints title / channel / duration / format / size:
**ask the user before downloading**, naming that video (a click on 「使用」 in the page is the user's
own choice). Then `KM mv-video <job> --use "#k"` (or a URL; Bilibili URLs work too):

- downloads ≤1080p through `krok_helper.video_download` (yt-dlp, the desktop app's step 1; proxy
  from `HTTPS_PROXY` / `KM_PROXY`) into `media/mv_download/`;
- aligns the MV's audio to the song and renders `media/mv_bg.mp4` — picture only, start trimmed or
  padded with black, cut to the song length — and sets `background: {"type": "video", "path": …}`;
- prints notes: confidence < 20 % = a different version (live, TV size, cover) → pick another;
  "无法用单一偏移对齐" = an edited MV whose picture will drift in places → tell the user.

All audio (karaoke MP4, on / off vocal, Hi-Res) still comes from the song file. `--local <path>`
does the same alignment for a video the user already has.

### AMV design (static MV for a song without a video)

The AMV design is the background when `background.type` is `mv`; it is also exported alone as
`<name> (MV).mp4` (export kind `mv`). **Design it for this song** — don't just take the first preset:

1. Read the song: lyrics themes, tie-in work, cover art,
   tempo / mood from `analyze`. Pick imagery, palette and motion that fit (e.g. 祈り / 星空 →
   night sky, slow drifting light; summer idol song → bright sky, petals, bouncy bars).
2. `KM mv <job> --list-presets --kind mv`, start from the closest one (`KM mv <job> --preset <id>`),
   then write your own spec `KM mv <job> --spec @spec.json`: `theme` (short Chinese title of the
   idea), `notes` (Chinese, why these visuals), `palette` (`"auto"` = from the cover, or 5
   colours), `layers` (catalog + rules in [references/recipes.md](references/recipes.md#mv-design)).
   Keep the lower third calm (karaoke lines live there): dark ground / `lyrics_band`.
3. `--spec`/`--preset` render 3 stills (intro / verse / loudest chorus) into the page's design
   card; `KM mv <job> --gallery` renders one chorus still per preset for comparison. Iterate on the
   user's feedback (`prompt` actions). View the stills yourself before presenting them.
4. The full background video renders at export time at the output frame rate (or `KM mv <job>
   --video`, `--seconds 20` for a trial); it runs as one process — start it in the background and
   don't run other GPU / render jobs at the same time.

### Image montage (图片混剪)

`background.type` = `montage`; its design is separate from the AMV (`KM mv <job> --kind montage`
…; presets `anime_montage` 标准混剪 / `montage_cinematic` 电影感 / `montage_beat` 高燃踩点 — the
montage layer at the bottom, visualizer effects on top; adapt them to the song like an AMV).
Exported alone as `<name> (混剪).mp4`.

- **Where images come from** (`options.montage_source`, the page asks the user): `web` — you
  search; `user` — the user's image packs; `mixed` — both. User packs arrive through the page
  (upload images / a .zip, or type a local folder / .zip path) or from you:
  `KM mv-assets <job> --add "<folder|.zip|image>"` (folders are read recursively in natural file
  order; GBK zip names are handled). In a mixed pool the user's images are used first and spread
  evenly between the others (layer option `prefer`).
- **How many**: `KM mv-assets <job> --plan` prints the BPM, shot count, `needed_unique` (images
  for the current plan), `ideal_unique` (for the designed pacing) and `by_origin`. Aim for
  `ideal_unique`; with fewer, shots get longer automatically.
- **Finding images online** (`web` / `mixed`): web-search official key visuals, promotional art,
  episode / event stills and character art related to the song (the scene it plays in, the
  singers' characters, the story the lyrics refer to). Prefer official sources and large images
  (short side ≥ 720 px; < 480 is rejected), no watermarks / UI / subtitles burned in, varied shots
  (characters, scenery, close-ups). If the user has not chosen `web` / `mixed` in the page, **ask
  before downloading** (which sites, roughly how many). Always record the origin:
  `KM mv-assets <job> --add <url> [--add …] --source <page url> --credit "<作品 / 出处说明>" --tags chorus,character`.
- Exact and near-duplicates are rejected (output says why). Video jobs can also use
  `--from-video N` (distinct scenes from the source video). `--remove <id|all>`, `--list`,
  `--credits` (put them in the video description and tell the user).
- What the planner does: cuts land on bar downbeats of the detected beat grid; section starts and
  repeated passages force a cut; loud parts cut faster, quiet ones slower; on top of that the plan
  is **section-aware** — a repeated passage (and its source) or the hottest sung block is the
  chorus and cuts fastest with the most dynamic clips, verses run at normal pace, and instrumental
  gaps / the intro slow down with the calmest clips (`section_pace` / `section_motion` /
  `section_tags` tune or disable this). Every image is used once,
  **except** that a repeated lyric passage (e.g. the last chorus) reuses, in order, the images shown
  the first time; if the pool is still too small, the least-used images repeat and the plan says
  so. One pool feeds the montage: images (user packs, web finds, video stills) and video clips mix
  freely; `sources: ["image"]` / `["clip"]` restricts it to one kind. Knobs on the layer: `bars`,
  `min_shot`, `max_shot`, `transitions`, `punch`, `dim`, `fit`,
  `prefer`, `exclude`, `images`, `pins` (recipes.md).
- Keep montages **smooth** by default: crossfades and slow pans only (`transitions: "auto"`, no `punch`,
  no `flash` layer). Beat zooms / white flashes on still images look like twitching — use the `beat`
  transitions, `punch` or the `montage_beat` preset only when the user explicitly wants a 燃 / 踩点冲击 edit.
- The page shows the cut plan as a filmstrip plus the image pool (badges: 图包 / 网络 / 视频 / 片段); the
  user can remove images (re-plans and re-renders stills), upload more, or ask you for others.

### Video-clip montage (视频片段混剪 / 游戏素材卡点剪辑)

The same `montage` background, filled with **video clips** instead of (or next to) images — for
"use this game's / anime's footage". Start from the preset `game_montage` (游戏素材卡点混剪:
hard cuts on the beat, short crossfades in quiet parts, a lyrics band, no particles — game footage
is busy enough) and adapt it to the song.

1. **Find footage** that fits the song's role: concert scenes for an in-game idol song, the ending
   for an ED theme, battles for the loud parts, the story's key places / characters. Search with
   `KM mv-clips <job> --search "<game> no commentary"` (also `実況なし` / `全編` / `walkthrough` /
   `trailer` / `PV` / `all cutscenes`); `--info URL` prints duration, best height and the uploader's
   chapters (they often name every scene). Prefer, in this order: official trailers / PVs / opening
   movies (only their caption-free ranges — most have feature captions, logo and release cards),
   full **no-commentary** playthroughs in high resolution (no facecam, flat 16:9 mirror view; VR
   lens views and Twitch layouts are poor), then let's plays whose picture is clean. Look before
   choosing ranges: the thumbnail, the storyboard or a few single frames.
2. **Download** (ask first unless the user already told you to search and use footage; name the
   videos): `KM mv-clips <job> --add <url> [--sections "a-b,c-d"] --credit "<频道 · 作品>"`. Picture
   only, ≤1080p (`--max-height`), into the **footage library** `<projects>/_footage` shared by sibling
   projects (each source downloaded once; `--library` for another one). Long videos: only
   `--sections` (seconds or m:ss; ~1–3 min each, 10–15 min of good footage per song) — fetched as
   byte ranges of the stream through yt-dlp's own session, so no full download. Short trailers:
   whole. One `--add` per video when the sections differ; run them one after another in the
   background (each is a download plus a quick scan, not a render).
3. **Shots**: every file is scanned once (cuts at 6 fps; long continuous shots split into ≤ 7 s
   pieces) and each shot scored for brightness, colour, detail and motion; black bars are cropped
   automatically (`--crop x,y,w,h` also cuts a logo / facecam corner off a source); near-identical
   shots from different trailers are dropped. `--min-score` (0.35), `--max-clips` per source (80).
   Only the files of the given `--sections` go into this project's pool, so each song gets its
   own footage from a shared library.
4. **Review**: the planner already rejects black bars, low scores and duplicates, and battle HUD
   (FIRE / DANGER / radar / the full-screen TOUCH hexagons) is part of the show — keep it. When the
   user asks for a careful review (or the footage is new and untrusted), do two rounds:
   - *Pool*: `KM mv-clips <job> --sheet` writes `previews/clip_sheet_N.jpg` (one middle frame per
     clip, yellow numbers `#N`; `previews/clip_sheet_N.json` maps them to ids). Look at every sheet
     (several sheets → fan them out to sub-agents, one sheet each, answering by number) and
     `--exclude "#3,#17,…"` shots with burned-in captions / marketing text, credits, menus,
     logos, facecams, livestream overlays, black / white / blurred frames — they clash with the
     karaoke lines. Shots whose only problem is an in-game dialogue subtitle can stay as a last
     resort: put their ids in the layer's `avoid`. Tag the rest (`--tag id+id=concert,best`);
     `best` shots are preferred, `prefer_tags` on the layer boosts a theme.
   - *Used*: after the plan exists, `KM mv-clips <job> --sheet-used` shows start / middle / end of
     the part of each **used** clip that will actually be on screen (`clip_used_N.jpg`, numbers
     `U1`…) — subtitles, UI pop-ups and fades that appear mid-clip only show up here.
     `--exclude "U5,U8" --mark-checked`, re-plan (`mv --spec` or `--sheet-used` again, which
     only shows the newly used clips) until a round comes back clean.
   - Footage can lie about its content: a "game longplay" may end in another game or a stream
     outro, trailers hide release-date / name cards in their last seconds, a streamer's overlay
     or camera can drift over the game picture mid-video. Spot-check a frame per section
     (`ffmpeg -ss`) before trusting the pool; exclude the whole off-topic section or source, and
     delete it from the library so sibling projects never import it. In-game dialogue subtitles
     of a visual novel are part of the show; keep those shots unless the user objects, but prefer
     them last (`avoid`).
5. **Design and plan**: `KM mv <job> --kind montage --preset game_montage`, then a spec of your own
   (theme / notes in Chinese). Then **lay out the shot order yourself**: `KM mv-clips <job> --list`
   prints every pool entry (id, source, `t_in`, duration, motion, score, tags) — order the ids by
   the rules (video clips first, calm sources early, hottest for the chorus, one source's shots in
   `t_in` order, images last) and write them into the montage layer's `images` array via
   `mv --spec`. The planner then plays them in that order (length-fit and source-order guards still
   apply; an entry too short for the current shot is skipped and picked up by a later shorter one).
   Without `images`, the planner fills each shot with an unused clip whose length fits and whose
   motion suits the music there, alternates sources, reuses the first
   occurrence's clips for a repeated chorus, and slows a short clip down (≥ 0.7×) instead of
   repeating one. `transitions: "auto"` on clips = hard cut exactly on the beat (卡点), short
   crossfade in quiet parts and at calm section starts; `clip_zoom` adds a slow push-in; `dim`
   darkens. A second song from the same library: `KM mv-clips <job2> --avoid-from <job1>` puts the
   first song's shots last. `mv-assets --plan` / `mv-clips` print `clips`, `unique_used`,
   `ideal_unique`, `slow_motion`; aim for a pool of ~1.5× `ideal_unique` after review. A clip is
   never given a shot it cannot fill at ≥ 0.7× speed (it would freeze on its last frame); when the
   clean pool is small, `max_scale: 1.0` keeps the designed pacing and repeats the least-used long
   clips (`fallback_repeats`) instead of stretching every shot — prefer that over subtitled shots,
   and tell the user how many repeats there are. Tempo: if `bpm` comes out half of the felt tempo
   (a ballad at ~78), `bars: 1` gives ~3 s shots; `bars: 2` suits ~110–130 BPM.
6. Check `KM mv <job> --stills` (and the karaoke `frame`s) before exporting. The background video is
   rendered once at export (clips decoded with ffmpeg while drawing, one process).
7. **Credits**: `KM mv-clips <job> --credits` lists every source if you need a record.
8. **Low-quality footage → AI enhance (optional)**: sources below 1080p or 30 fps (VR eye-mirror
   recordings are often 720×720@24) look soft after the cover-crop to 16:9. `KM mv-clips <job>
   --enhance` re-encodes each such source in the library with **RIFE frame interpolation**
   (2× fps) and **Real-ESRGAN super-resolution** (2×, `realesr-animevideov3` — great on cel-shaded
   game footage) and repoints the pool at the enhanced copies. `--only-used` limits it to the
   sources the current plan actually uses (the cheap default), `--no-sr` / `--no-rife` skip one
   stage, `--ids` names clips explicitly. It runs strictly serially and resumes across runs.
   Cost on an Arc A770M: RIFE ≈ 11 fps of 720² frames, Real-ESRGAN ≈ 4 fps — a few minutes per
   source with `--only-used`, hours for whole longplays (don't). First run needs the tools under
   `<home>/tools/`; the command prints the download links when they are missing.

## Stage 2 — automatic timing

1. `KM timing <job>` (background). Steps shown live: prepare → separate (reused from `analyze`;
   redone automatically when the timing audio changed, e.g. a Hi-Res source registered later) → pronounce
   (SUG project + readings) → align (SUG forced-alignment worker, model
   `NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn`, split into blocks at instrumental breaks so
   errors cannot drift) → refine (energy-based line-head / tail / breath correction) → QA.
   About 1.5–3 min per 4-min song on CPU. It prints `bad_lines` / `warn_lines` (1-based).
   - With **word-level lyrics** (`line.words`), aligned words that land more than 0.2 s away from
     the lyric source's time (after its constant offset) are moved there afterwards — the log says
     「逐字歌词校正」; nothing changes when the source mostly disagrees (another version). `--no-words`
     skips it.
   - **English songs** (`song.language` = `en`): the aligner (a Japanese romaji model) is skipped —
     on English it was off by 3–5 s on whole lines. Every word is timed from the word-level lyrics
     when they are reliable (≥ 60 % of their words within 0.25 s of the English recognizer after
     their offset; they lead because the recognizer's word starts after a held note are pulled
     into the previous word), otherwise from the stage-1 English recognition (`analysis/asr.json`)
     with the word-level lyrics filling gaps — the same code as `realign --english`, in seconds.
     Line ends that run < 0.3 s into the next line are trimmed; stale mid-line pauses are dropped.
     `--aligner` forces the aligner anyway. It prints `mode`, `lyrics_offset`, `lyrics_agree`.
2. **Review with your own judgement** — `KM qa <job>` lists every line with flags. Fix, then re-run
   `qa`; stop after ~3 rounds and report what is left:
   - "人声能量很低，疑似错位" / "与语音识别位置相差" → `KM realign <job> --lines N[-M]` (auto
     window = previous end … next start; pass `--window a,b` from `match` estimates if the
     neighbours are wrong too).
   - "第 k 字…持续 X 秒，其中 Y% 无人声" → a breath inside a syllable: `edit` `set_pause`
     (release at the last voiced moment), or realign with a tighter window. A long held **final**
     note is normal.
   - "语速过快" (squeezed) / "与下一行重叠" → realign that line together with its neighbour.
   - **English lines in a Japanese song** (refrains, English verses): the aligner is built for
     Japanese and QA flags miss its mistakes — a word stretched over an interlude, the next words
     squeezed into a few frames, neighbours shifted. Compare every English line and its neighbours
     with the synced reference (`match` maps English lines by their letters; NetEase `ne-*` line
     starts are usually within ±0.3 s): for a line off by more than ~0.4 s run
     `KM realign <job> --lines N --window a,b` with `a` = its reference start − 0.3 and `b` = the
     next line's reference start (one line per call; the vocal-stem energy shows where a held note
     ends). Without a synced reference, `realign --english` (English recognizer word times) helps,
     but its word starts often swallow the silence before a phrase (~1 s early) — check them
     against the energy before keeping them. Then check with `qa` that no character is shorter than
     a frame or longer than a held note. When the chosen lyrics are **word-level** (`ne-*` / `kg-*`
     with `has_verbatim`), `realign --lines N --english` uses their word starts and lengths
     (`--asr-first` to prefer the recognizer, `--fresh-asr` to recognize the windows again).
   - Line missing entirely / sung lyrics differ → back to stage 1 (`lines`, then `timing`).
3. `KM say` a summary (lines timed, fixes made, lines the user should listen to), then
   `KM set <job> --stage 3 --await-user --status "打轴完成，请审阅"`.

## Stage 3 — review, tweaks, export

The page plays the media with a live karaoke overlay (engine sprites), a draggable timeline,
per-line ±0.02/0.1 s nudges and "⇤ 播放头", a singer bar (歌割り by hand), 「平滑走字」, 「↶ 撤销」,
an "引擎帧预览" button (real renderer frame) and export buttons. Those are handled by the server.
You run the wait loop and handle `prompt`s:

- Timing: "第 12 行晚了 0.2 秒" → `edit` `[{"op":"shift_lines","lines":[11],"ms":-200}]`;
  "整体提前 0.1 秒" → `shift_all`; "这句" uses `active_line` / `t` from the payload.
- Readings: `edit` `set_ruby` then `realign --lines N`.
- Singers on the page: click line numbers to select (Shift = range; none = the playing line),
  click one or more singers in the bar (several = chorus 「A＆B」, drawn in 拼色), then 「设为…」 or
  press 1–9; a singer's colour dot recolours it, 「＋ 歌手」 adds one. In chat use `KM singers` /
  `edit` `set_singer` (tell the user the page can do it in bulk).
- "走字不够平滑 / 速度忽快忽慢" → `edit` `[{"op":"smooth"}]` (or the page's 「平滑走字」; `lines` to
  limit it, run again for more): evens the wipe speed between neighbouring characters, keeps phrase
  starts, pauses and held notes, moves no checkpoint more than `max_ms` (80). Every manual change
  on the page can be undone (「↶ 撤销」 / ui-action `undo`, last 30 changes).
- Look: `KM style <job> @patch.json` with `template`, `effect`, `overrides` (Style fields),
  `singer_styles {id: {color}}`, `ruby`, `title`, `background`, `resolution`, `fps` (30 / 60).
  English lyrics: `"ruby": false` and a Latin font in `overrides` — `font_family_latin`
  (`Segoe UI`, `Georgia` …; the templates' Japanese fonts are wide for Latin), `space_width_percent`
  ≈ 28, `font_size_px` 86–88 so a 34-letter line fits; check the longest line with `KM frame`.
- Export: `KM export <job> --kinds sug,yurika,mp4,onoff` (the default; add `lrc`, `alpha`, `mv`,
  `hires`). MP4 ≈ 1–1.5× song length on 8 CPU cores, at the chosen 30 / 60 fps. `--clip 25` first
  renders a 25-second trial `<name> (preview).mp4` (and `(透明字幕 preview).mov` with `alpha`) —
  offer it before a long full render (the page has "试看 25 秒").
- `alpha` = `<name> (透明字幕).mov`: subtitles only, ProRes 4444 with alpha + PCM audio, for PR / AE /
  DaVinci (a whole song is several GB; ordinary players may show it on black — that is normal).
- **投屏延迟** (`options.cast_delay_ms`, default **+200 ms**): how far the picture leads the sound in
  every exported video — karaoke MP4, on / off vocal, Hi-Res MKV, transparent MOV (the audio is
  delayed, the frames are unchanged). Casting to a TV shows the picture late; ask the user if they
  watch that way and adjust. `KM export <job> --cast-delay 400 …` (saved with the project) or the
  page fields (stage 1 输出设置 / stage 3 导出). `0` = no offset.
- Back to stage 1: `KM set <job> --stage 1 --await-user`.

Effects whose animation enlarges the sung characters (Utopia 柔光擦色 / 辉光 / 跳动放大) get extra
furigana spacing automatically, so the popping glyph never runs into the reading above it.

Always confirm what you changed with `say` (e.g. "已将第 12 行整体提前 0.2 秒"). Prompt patterns,
Style fields and edit-op schemas: [references/recipes.md](references/recipes.md).

Outputs land in the output folder (`--out`) or `<job>/export/`: `<artist> - <title>.sug` (open in Lin-K Lyrics / SUG for manual
fine timing), `.yurika` (Lin-K Lyrics step-5 subtitle project; media paths are absolute), `.lrc`
(word-level), `.mp4` (1080p60 H.264 by default, title card at the start; hardware encoder from the
`encoder` setting, falls back to libx264).

**on vocal / off vocal** (export kind `onoff`, on by default): the same video stream with the
audio swapped (AAC 320k). `<name> (on vocal).mp4` carries the best 原唱 (registered Hi-Res source /
lossless original / the user's file) and replaces the master `<name>.mp4`, so a finished project
has exactly the two versions; `<name> (off vocal).mp4` carries the 伴奏 —
the user's instrumental files when given, else the instrumental separated from the timing audio
(the registered Hi-Res source if any). Several instrumentals → `(off vocal 2)` …. Redo with other
files: `KM versions <job> [--video V] [--on F] [--off F …] [--no-align]` (files are aligned to the
video's audio first). Audio-only jobs with kind `mv` also get the plain MV in both versions.

**Hi-Res 混流 (optional, off by default)** — when the user ticks it or asks: `KM hires <job>`
after the MP4 exists (or `export --kinds …,mp4,hires`). Uses the workbench's step-6 pipeline:
video stream copied, every track → FLAC 32-bit ≥48 kHz, outputs `<name> (Hi-Res on vocal).mkv` /
`<name> (Hi-Res off vocal).mkv`. It uses the sources registered in stage 1 (`hires-source`) automatically;
`--on` / `--off` override them (waveform-aligned to the video, shift reported, warning when an
edited MAD has no single offset). Without an 伴奏 file the instrumental is separated from the
lossless 原唱. If the user only now brings a lossless file, prefer registering it with
`hires-source` and re-running `timing` so the timing uses it too; mention that a MAD's own audio is
lossy.

**Open in Lin-K Lyrics (optional last step)** — if the user wants to keep editing in the full app:
`KM open-app <job> [--file yurika|sug] [--exe "…/Lin-K Lyrics.exe"]` (page button
"在 Lin-K Lyrics 中打开"). Without `--exe`/`KM_LINK_EXE` it starts the engine checkout from source
with the skill venv and loads the exported project.

## Command reference

| command | purpose |
|---|---|
| `setup [--check] [--target user\|project\|DIR] [--reinstall-ai] [--engine-ref TAG] [--with-models]` | install wizard / status |
| `accel [--set KEY=VALUE …] [--quick]` | probe GPUs / AI runtime, set acceleration knobs (`encoder`, `codec`, `align_device`, `onnx`, `asr_device` …) |
| `new SRC [--name N] [--title --artist] [--dir D] [--out D] [--like PROJECT]` | create a project folder (probe, extract audio, peaks, browser proxy) |
| `serve JOB [--daemon] [--port --no-browser]` · `stop JOB` | the project's page server (stdlib HTTP + SSE) / close it |
| `status JOB [--full]` / `qa JOB` | compact state / per-line QA table |
| `wait JOB [--timeout S] [--peek]` | block until the user acts; prints actions JSON |
| `say JOB TEXT` | agent message in the page |
| `set JOB [--song @f] [--segment a,b] [--options @f] [--stage N] [--status T] [--await-user]` | write state |
| `analyze JOB [--skip-asr] [--asr-model M]` | separation + ASR |
| `lyrics-search JOB --query Q [--utaten-query Q]` · `lyrics-use JOB ID\|file [--ruby-from ID] [--split-long N]` | lyrics (a `.lrc` file → candidate `user-lrc`; `has_verbatim` = word-level) |
| `lines JOB [--list] [--split N:pos] [--merge N] [--dup N] [--delete N] [--include R] [--exclude R] [--singer R=id] [--ruby N:a-b=かな] [--reannotate] [--fix-char N:pos=X] [--split-long N]` | stage-1 line ops |
| `match JOB [--apply] [--ref ID]` | locate lines via ASR + synced reference |
| `hires-source JOB [--on F] [--off F …] [--no-align] [--clear]` | stage-1 lossless source → timing audio |
| `mv JOB [--kind mv\|montage] [--list-presets] [--preset ID] [--spec @f] [--show] [--stills] [--gallery] [--video] [--seconds N]` | AMV / montage designer (alias `spectrum`) |
| `mv-video JOB [--search Q] [--n N] [--info URL\|#k] [--use URL\|#k] [--local PATH] [--max-height H]` | MV background from YouTube / Bilibili or a local video, aligned to the song |
| `mv-clips JOB [--search Q] [--info URL] [--add URL\|PATH … [--sections a-b,…] [--credit C] [--crop x,y,w,h] [--max-height H]] [--library L] [--from-library] [--min-score S] [--max-clips N] [--sheet] [--sheet-used] [--mark-checked] [--exclude IDS\|#N\|UN] [--tag ID=t] [--avoid-from PROJECT] [--enhance [--only-used] [--ids IDS] [--no-sr] [--no-rife] [--sr-scale N] [--rife-multi N]] [--list] [--credits]` | video-clip montage: footage library → shots in the montage pool; review sheets; AI enhance (RIFE + Real-ESRGAN) for low-quality sources |
| `mv-assets JOB [--add URL\|IMAGE\|FOLDER\|ZIP …] [--origin user\|web\|video] [--source U] [--credit C] [--tags a,b] [--from-video N] [--remove ID] [--list] [--credits] [--plan]` | montage image pool / cut plan |
| `previews JOB [--only templates\|effects\|singers]` | engine-rendered galleries |
| `timing JOB [--device DEV] [--no-chunk] [--no-words] [--aligner]` · `realign JOB --lines R [--window a,b] [--english [--asr-first] [--fresh-asr]] [--no-words]` | alignment (English songs: word-level lyrics / English recognizer instead of the aligner) |
| `singers JOB --from FILE\|URL [--map KEY=歌手 …] [--ignore-unmapped] [--dry-run]` | 歌割り from a subtitle file / part-distribution source (ASS, LRC, TXT, colour-coded HTML, JSON runs) |
| `edit JOB @ops.json` · `style JOB @patch.json` · `frame JOB T` | review |
| `export JOB [--kinds sug,yurika,lrc,mp4,alpha,onoff,mv,hires] [--clip N] [--cast-delay MS]` | output (default `sug,yurika,mp4,onoff`; 投屏延迟 default +200 ms) |
| `versions JOB [--video V] [--on F] [--off F …] [--no-align]` | on / off vocal versions |
| `hires JOB [--on F] [--off F …] [--video V] [--no-align]` · `open-app JOB [--file yurika\|sug] [--exe EXE]` | optional extras |

## Troubleshooting

- Page shows "等待 Agent" forever → you are not in `KM wait`; run it.
- Tofu boxes in previews → Qt platform lacks fonts; on Windows the native platform is used, on
  headless Linux set `QT_QPA_FONTDIR`. `KM_QT_PLATFORM` overrides.
- First `analyze` / `timing` downloads models (separation ~60 MB from GitHub, ASR ~0.8 GB and
  alignment ~1.2 GB from Hugging Face). Set `HF_ENDPOINT` for a mirror; proxies come from
  `HTTPS_PROXY`.
- Port busy → the server picks the next free port and writes it to `<job>/live_preview/lock.json`.
- GPU problems → the log says "<device> 推理失败…改用 CPU"; force a device for one run with
  `KM_DEVICE` (alignment), `KM_ONNX` (separation), `KM_ENCODER` (MP4), or reset with
  `KM accel --set align_device=default`.
- YouTube downloads: 403 → the other player clients are tried automatically (as Lin-K Lyrics'
  downloader does). Behind a proxy whose exit address changes, stream URLs are bound to the
  address that asked for them, so never hand them to ffmpeg — `mv-clips --sections` fetches byte
  ranges through yt-dlp's own session for that reason (a 403 mid-download re-extracts a fresh URL;
  "Sign in to confirm you're not a bot" moves on to the next player client). If a source still
  fails, re-run that one `--add` later — finished sections are kept and skipped.
- Command output must stay JSON: `km.py` drops QFluentWidgets' advert from stdout.
- Logs: `<job>/logs/events.log`, page-triggered actions in `<job>/logs/ui_actions.log`.
