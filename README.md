<div align="center">

# Karaoke Master · 卡拉OK制作 Agent Skill

**给 AI Agent 用的 KTV 字幕视频制作技能**：交给它一首歌名、一段音频或一个 MV / MAD 视频，
它会在本机打开一个专属于这个工程的网页，带你走完「素材确认 → 自动打轴 → 审阅导出」三个阶段，
最后交出逐字走字的卡拉OK 成品视频（on vocal / off vocal 双版本，可选 Hi-Res 无损混流）和可继续精修的工程文件。

[![License: GPL v3](https://img.shields.io/badge/License-GPL_v3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10–3.13-3776AB)
![Platform](https://img.shields.io/badge/Windows-主要验证平台-0078D6)
![Agent Skill](https://img.shields.io/badge/Agent-Skill-ff5fa2)

<img src="images/hero.jpg" width="100%" alt="四种背景模式的成品画面：原视频、网络 MV、AMV 频谱、图片混剪">

<sub>样例成品画面（左起：原视频 / 网络下载的 MV / AMV 频谱 / 图片混剪），文档截图中的歌词均已模糊处理。</sub>

</div>

---

## 它能做什么

| 你给 Agent 的 | 它交还给你的 |
|---|---|
| 歌名 / 音频文件 / 视频（MV、MAD、切片） | 逐字走字的卡拉OK MP4（30 / 60 fps，1080p 起，按歌割り给每位歌手上色） |
| 可选：无损原唱 FLAC（Hi-Res 音源） | **on vocal** 与 **off vocal**（伴奏）两个版本，伴奏由人声分离自动生成 |
| 可选：你自己的图包 | Hi-Res 混流 MKV（FLAC 32bit 无损原唱 / 伴奏轨） |
| 一句话的修改意见 | `.sug` 打轴工程、`.yurika` 字幕工程，可在 Lin-K Lyrics 桌面版里继续精修 |
| | 透明字幕层 MOV（ProRes 4444，叠加到 PR / AE / 达芬奇） |
| | 投屏延迟补偿：导出时画面比声音提前（默认 +200 ms，可调） |

日文与英文歌曲均可：日文用 UtaTen 振假名 + 强制对齐模型；英文用网易云 / 酷狗逐字歌词与英文识别打轴。
导出默认使用硬件编码（QSV / NVENC / AMF / VideoToolBox，可选 HEVC，同画质体积更小），失败自动回退 CPU。

## 背景模式

<table>
<tr>
<td width="25%"><img src="images/mode_video.jpg" alt="视频模式"></td>
<td width="25%"><img src="images/mode_mvvideo.jpg" alt="网络 MV 模式"></td>
<td width="25%"><img src="images/mode_amv.jpg" alt="AMV 频谱模式"></td>
<td width="25%"><img src="images/mode_montage.jpg" alt="图片混剪模式"></td>
</tr>
<tr>
<td><b>视频</b><br>直接用素材视频作背景。</td>
<td><b>网络 MV</b><br>从 YouTube / B 站找来这首歌的 MV 或动画 OP·ED，按波形对齐到你的音频，只取画面。</td>
<td><b>AMV（频谱可视化）</b><br>只有音频时，Agent 按歌曲主题设计画面：配色、意象、封面、频谱与光效，随音乐律动。</td>
<td><b>图片混剪</b><br>官方宣传图 / 你的图包，按节拍在小节强拍上切换；默认交叉淡化 + 缓慢推拉。</td>
</tr>
</table>

- **视频片段混剪（游戏 / 动画素材卡点剪辑）**：Agent 在 YouTube / B 站找游戏的预告、实况与结局录像，按字节范围只下载需要的分段，
  扫描切镜后按音乐节拍硬切卡点；同一游戏的多首歌共享素材库且镜头互不重复。
- **仅 KTV 字幕**：纯色底（黑底 / 绿幕）+ 透明字幕层 MOV，叠加到你自己剪的视频里。
- **低清素材 AI 增强（可选）**：`mv-clips --enhance` 用 RIFE 补帧 ×2 + Real-ESRGAN 超分 ×2 处理 720p / 24fps 以下的素材（如 VR 单眼录像），
  串行执行、可断点续跑，只增强剪辑计划实际用到的源。

## 三阶段网页

每个 KTV 工程都是一个独立文件夹，并有自己的本地网页（类似 [ppt-master](https://github.com/hugohe3/ppt-master) 的工程 + 实时预览页）。

### ① 素材分析与制作确认

<img src="images/stage1.jpg" width="100%" alt="阶段一：素材、歌曲信息、歌词与注音、模板与特效预览">

- 歌曲信息由 Agent 联网补全，歌词优先采用 UtaTen（带振假名），网易云 / QQ / 酷狗 / LRCLIB 的同步歌词作时间参考；
- 语音识别 + 参考时间轴推断视频里实际唱了哪些句子（MAD 剪掉的段落、括号里的和声会被标出）；
- 字幕模板、特效、多人合唱配色全部由**真实渲染引擎**在你的素材上出图；
- 背景、帧率（30 / 60）、导出内容、Hi-Res 无损音源都在这里确定。

### ② 自动打轴

<img src="images/stage2.jpg" width="100%" alt="阶段二：流水线进度与实时时间轴">

人声分离（UVR-MDX-NET）→ 注音转写 → 强制对齐（按间奏切块，避免误差漂移）→ 行首 / 行尾 / 句中换气修正 → Agent 质检复核。时间轴随对齐进度实时出现。

### ③ 审阅、微调与导出

<img src="images/stage3.jpg" width="100%" alt="阶段三：与引擎逐像素一致的预览、逐行微调与导出">

- 网页预览与渲染引擎逐像素一致；逐行 ±0.02 / 0.1 秒微调、拖动时间轴、一键「引擎帧预览」；
- 歌手分工条：点选行（Shift 范围选）+ 点歌手即可批量改配色，多人合唱自动拼色；「平滑走字」让走字速度均匀；所有手动改动可撤销；
- 导出区默认折叠，按需展开；也可以直接用自然语言告诉 Agent：「第 12 行晚了 0.2 秒」「副歌换成红色描边」。

<img src="images/mv_video.jpg" width="100%" alt="网络 MV：搜索结果、对齐结果与本地视频">

网络 MV：搜索结果附缩略图、时长与播放量，选中「使用」后下载并对齐（这里 -4.84 秒、置信 100%）；也可以改用本机视频。

<table>
<tr>
<td width="50%"><img src="images/amv_design.jpg" alt="AMV 设计卡片"></td>
<td width="50%"><img src="images/montage_plan.jpg" alt="混剪素材与节拍剪辑计划"></td>
</tr>
<tr>
<td>AMV 设计：前奏 / 主歌 / 副歌三张关键帧 + 预设画廊，配色与意象可直接让 Agent 改。</td>
<td>图片混剪：节拍剪辑胶片条、素材池（图包 / 网络 / 视频来源标记），悬停即可移除不想要的图。</td>
</tr>
</table>

## 安装（最小安装）

本分支只包含 Agent 完成 KTV 制作所需的代码：技能本体（`skills/karaoke-master/`）与它调用的渲染 / 对齐引擎源码（`krok_helper/` 的必要模块，内含 StrangeUtaGame 的打轴后端）。
不含桌面版程序、安装包、exe / dll、构建脚本和测试；运行环境由安装向导按需展开到本机。

```bash
git clone https://github.com/Langzaigg/karaoke-master-skill.git
```

1. **让 Agent 发现技能**：把 `skills/karaoke-master` 链接或复制到 Agent 的技能目录，例如 Claude Code 的
   `~/.claude/skills/karaoke-master`（Windows 可用 `mklink /J`），或在项目里放到 `.claude/skills/`。
2. **准备 FFmpeg**：`ffmpeg` / `ffprobe` 需在 PATH 中（或设置 `KM_FFMPEG_DIR`）。
3. **第一次使用时**，Agent 会自己运行安装向导（也可以手动）：

   ```bash
   python skills/karaoke-master/scripts/km.py setup --check   # 只检查，任意 Python 3.10–3.13
   python skills/karaoke-master/scripts/km.py setup           # 建虚拟环境、装依赖（约 3–5 GB）
   ```

   依赖清单见 `skills/karaoke-master/requirements.txt`（应用运行时）与 `requirements-ai.txt`（AI 运行时，默认 CPU 版）。
   模型首次使用时下载：人声分离约 60 MB、语音识别约 0.8 GB、对齐模型约 1.2 GB（可设 `HF_ENDPOINT` 使用镜像）。
4. **硬件加速（可选）**：`km.py accel` 会列出显卡与可用的推理后端；技能只引导 Agent 查找适合你显卡的 PyTorch / ONNX Runtime 版本并用一小段真实任务验证，不做压力测试。

## 怎么用

装好后直接对 Agent 说话即可，例如：

> 用 karaoke-master 把 `D:\ktv\ALIVE ～祈りの唄～［ MAD ］マクロス Δ.mp4` 做成卡拉OK，原唱无损在 `D:\ktv\ワルキューレ - ALIVE～祈りの唄～.flac`，成品放到 `D:\ktv\output`。

> 只有这首歌的 FLAC，帮我做一个星空主题的频谱 MV 版卡拉OK，30 fps。

> 用这个游戏（ALTDEUS）的实况和预告素材，给它的六首歌都做卡点混剪版卡拉OK。

> 我只有 `RTB.flac`，去油管找这首歌的片尾动画当背景，导出 on / off vocal，投屏延迟设成 400 ms。

Agent 会创建工程、打开网页，并在每个阶段等你确认；渲染与导出在后台进行，Agent 可以同时推进后续歌曲的打轴与剪辑。
所有命令都在 `km.py` 里，完整说明见 [`skills/karaoke-master/SKILL.md`](skills/karaoke-master/SKILL.md)，常用操作与图层参数见 [`references/recipes.md`](skills/karaoke-master/references/recipes.md)。

```text
projects/ALIVE_视频模式_20261008/      ← 一个 KTV 工程（km.py new 创建）
├── job.json / inbox.jsonl / chat.jsonl   网页与 Agent 共享的状态
├── live_preview/lock.json                这个工程的网页地址（km.py serve --daemon / km.py stop）
├── media/  analysis/  timing/  render/   素材、人声分离、时间轴、背景渲染
└── export/                               成品（或用 --out 指定到你的目录）
```

## 目录结构

```text
skills/karaoke-master/
├── SKILL.md                 Agent 工作流说明（技能入口）
├── requirements*.txt        依赖清单（安装向导使用）
├── scripts/km.py            命令行入口：setup / new / serve / analyze / timing / export …
├── scripts/kmlib/           歌词、分析、打轴桥接、渲染、MV 设计、混剪、素材增强、版本、Hi-Res
├── scripts/ai/              人声分离与语音识别（在 AI 运行时中执行）
├── web/                     三阶段网页
└── references/recipes.md    修改指令、样式字段、MV 图层参考
krok_helper/                 Lin-K Lyrics 引擎的必要模块（字幕渲染、波形对齐、歌词检索、混流）
krok_helper/lyrics_timing/   StrangeUtaGame 打轴后端（项目模型、自动注音、AI 强制对齐）
```

## 版权与来源

**Fork 来源**：本仓库 fork 自 [karaoke-studio/karaoke-studio](https://github.com/karaoke-studio/karaoke-studio)（Lin-K Lyrics / 凛K）。
Lin-K Lyrics 由 [Myosotis11037](https://github.com/Myosotis11037)（原 [karaoke-helper](https://github.com/Myosotis11037/karaoke-helper) 作者）与
[Xuan-cc（Hoshiro）](https://github.com/Xuan-cc)（原 [StrangeUtaGame](https://github.com/Xuan-cc/StrangeUtaGame) 作者）于 2026 年合并而成；
`skill` 分支在其基础上加入了 Agent 技能 `skills/karaoke-master`，并只保留技能运行所需的引擎源码：
`krok_helper/` 取自 Lin-K Lyrics v4.3.7.3，`krok_helper/lyrics_timing/` 是 StrangeUtaGame（提交 `bbd93eac`）中打轴后端等必要模块的**未修改副本**（不含 BASS 等二进制库）。
引擎部分的作者与模块归属见 [AUTHORS.md](AUTHORS.md) 与 [NOTICE](NOTICE)。

**协议**：整体沿用 **GNU GPL v3.0**（见 [LICENSE](LICENSE)）。修改与再分发请保留版权声明并以相同协议开源。

**第三方组件与模型**（运行时按需下载，不随仓库分发）：

| 组件 | 用途 | 协议 / 说明 |
|---|---|---|
| [NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn](https://huggingface.co/NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn) | 强制对齐打轴 | **CC BY-NC-SA 4.0，仅限非商业用途** |
| UVR-MDX-NET-Inst_HQ_3（[python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator)） | 人声 / 伴奏分离 | 见上游项目 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + Whisper large-v3-turbo | 识别唱了哪些句子 | MIT |
| [rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan) / [Real-ESRGAN-ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN) | 素材补帧 / 超分增强（`--enhance` 使用时下载） | 见上游项目 |
| PyQt6 / Qt 6 | 离屏渲染 | GPL v3 / LGPL |
| FFmpeg | 解码、编码、混流 | LGPL / GPL（由用户自行安装） |

**素材版权**：歌词、歌曲、画面等素材的版权归各自权利人所有；技能只面向个人学习与非商业的卡拉OK制作。
网络素材的出处会被记录（`mv-assets` / `mv-clips --credits`），下载视频前 Agent 会说明来源并征得同意。

**文档样例素材**：本文截图使用ワルキューレ「ALIVE～祈りの唄～」（《劇場版マクロスΔ 絶対LIVE!!!!!!》插入歌，画面 ©2021 BIGWEST/MACROSS DELTA PROJECT）
与ムッシュかまやつ「RTB」（OVA《戦闘妖精・雪風》片尾曲）制作，仅作功能演示；截图中的歌词均已模糊处理，样例成品不包含在仓库中。
