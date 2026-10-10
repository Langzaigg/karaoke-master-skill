#!/usr/bin/env python
"""karaoke-master command line — every step the agent (or the web page) runs.

Run with the skill venv interpreter (``km.py setup`` prints its path)::

    python km.py new <media-or-title> [--title ..]   create a job, open nothing
    python km.py serve <job>                        start the local web page
    python km.py status <job>                       compact state for the agent
    python km.py wait <job> [--timeout 540]         block until the user acts
    python km.py say <job> "message"                post an agent message

See SKILL.md for the full workflow.  Every sub command is import-safe (the
MP4 exporter uses spawn multiprocessing, which re-imports this file).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _CleanStdout:
    """QFluentWidgets (imported with the SUG engine) prints an advert to stdout;
    drop it so command output stays parseable JSON."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        if "QFluentWidgets Pro" in text:
            return len(text)
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _CleanStdout(sys.stdout)


def _store(job: str):
    from kmlib.jobstore import JobStore

    return JobStore(job)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _commands():
    from kmlib import commands

    return commands


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="km.py", description="karaoke-master skill CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="安装向导：创建/检查运行环境")
    p.add_argument("--check", action="store_true", help="只检查不安装")
    p.add_argument("--target", help="安装位置：user（默认，用户目录）/ project（当前目录下 .karaoke-master）/ 任意路径")
    p.add_argument("--reinstall-ai", action="store_true", help="重装 AI 环境（CPU 基线）")
    p.add_argument("--engine-ref", default=None, help="技能不在 Lin-K Lyrics 仓库内时克隆的引擎版本（tag）")
    p.add_argument("--with-models", action="store_true", help="同时下载对齐模型")

    p = sub.add_parser("accel", help="硬件加速：探测显卡与 AI 运行环境能力，或写入加速设置")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="ai_python / align_device / onnx / dml_device / asr_device / asr_compute / encoder / align_jobs；"
                        "值为 default 时恢复默认")
    p.add_argument("--quick", action="store_true", help="不探测 AI 运行环境（只列显卡与设置）")

    p = sub.add_parser("new", help="创建 KTV 工程（每个工程一个文件夹、一个网页）")
    p.add_argument("source", help="视频/音频文件路径，或歌名")
    p.add_argument("--name", help="工程名（文件夹为 <名称>_<日期>）")
    p.add_argument("--title")
    p.add_argument("--artist")
    p.add_argument("--dir", "--jobs-dir", dest="dir", help="工程存放目录（缺省：安装目录下的 projects）")
    p.add_argument("--out", help="成品输出目录（缺省：工程内 export）")
    p.add_argument("--like", metavar="PROJECT",
                   help="沿用同一素材的另一个工程的歌曲信息、歌词、分析与时间轴（同一首歌换一种背景再做一版）")

    p = sub.add_parser("serve", help="启动本工程的网页（同一工程只会有一个网页服务）")
    p.add_argument("job")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--daemon", action="store_true", help="后台运行并立即返回网址")

    p = sub.add_parser("stop", help="关闭本工程的网页服务（任务全部结束后）")
    p.add_argument("job")

    p = sub.add_parser("status", help="任务状态摘要")
    p.add_argument("job")
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("wait", help="等待网页上的用户操作")
    p.add_argument("job")
    p.add_argument("--timeout", type=float, default=540)
    p.add_argument("--peek", action="store_true", help="只查看不消费")

    p = sub.add_parser("say", help="以 Agent 身份发言")
    p.add_argument("job")
    p.add_argument("text")
    p.add_argument("--reply-to")

    p = sub.add_parser("set", help="写入任务字段（JSON 合并）")
    p.add_argument("job")
    p.add_argument("--song", help="JSON：title/artist/work/lyricist/composer/arranger/notes/sources/singers")
    p.add_argument("--segment", help="start,end（秒）")
    p.add_argument("--options", help="JSON：合并进 options")
    p.add_argument("--stage", type=int)
    p.add_argument("--status")
    p.add_argument("--await-user", action="store_true", help="状态设为等待用户确认")

    p = sub.add_parser("analyze", help="阶段一：提取音频、分离人声、语音识别")
    p.add_argument("job")
    p.add_argument("--skip-asr", action="store_true")
    p.add_argument("--asr-model", default="large-v3-turbo")

    p = sub.add_parser("lyrics-search", help="检索歌词候选")
    p.add_argument("job")
    p.add_argument("--query", action="append", help="检索词（可多次）")
    p.add_argument("--utaten-query", action="append")

    p = sub.add_parser("lyrics-use", help="选用歌词候选")
    p.add_argument("job")
    p.add_argument("candidate", help="候选 id，或 uploads/ 下的文本文件")
    p.add_argument("--ruby-from", help="从另一个候选（如 utaten）迁移注音")
    p.add_argument("--split-long", type=float, default=0, help="超过 N 个全角宽度的行自动拆分")

    p = sub.add_parser("lines", help="歌词行操作")
    p.add_argument("job")
    p.add_argument("--list", action="store_true")
    p.add_argument("--split", action="append", help="行号:字符位置（行号从 1 开始）")
    p.add_argument("--merge", action="append", help="把第 N 行与下一行合并")
    p.add_argument("--include", help="入选行，如 1-12,15")
    p.add_argument("--exclude", help="排除行")
    p.add_argument("--dup", action="append", help="复制第 N 行（MAD 重复段）到其后")
    p.add_argument("--delete", action="append", help="删除第 N 行")
    p.add_argument("--singer", action="append", help="行范围=歌手id，如 1-4=s1")
    p.add_argument("--ruby", action="append", help="行:起-止=读音，如 3:0-1=きおく（字符位置从 0 开始）")
    p.add_argument("--reannotate", action="store_true", help="重新自动补全缺失注音")
    p.add_argument("--fix-char", action="append",
                   help="行:位置=文字，把该位置的一个字符换成 0–3 个字符（位置从 0 开始）：20:32=’ 修正撇号误作逗号；13=\"e \" 补空格；等号后留空则删除")
    p.add_argument("--split-long", type=float, help="拆分超过 N 个全角宽度的行（英文字母按半个计；优先在逗号处断开）")

    p = sub.add_parser("match", help="把歌词行与语音识别结果对齐，建议入选行与歌曲区间")
    p.add_argument("job")
    p.add_argument("--apply", action="store_true", help="直接写入入选建议")
    p.add_argument("--ref", help="参考时间轴的歌词候选 id（默认自动选时长最接近的同步歌词）")

    p = sub.add_parser("previews", help="渲染模板/特效/歌手样式预览")
    p.add_argument("job")
    p.add_argument("--only", choices=["templates", "effects", "singers"], action="append")

    p = sub.add_parser("mv", aliases=["spectrum"], help="生成背景设计：AMV（mv）/ 图片混剪（montage）")
    p.add_argument("job")
    p.add_argument("--kind", choices=["mv", "montage"],
                   help="操作哪个设计（缺省：预设 / 场景自身的类型，否则当前背景）")
    p.add_argument("--list-presets", action="store_true", help="列出主题预设（可配合 --kind）")
    p.add_argument("--preset", help="以主题预设为起点（会覆盖当前设计）")
    p.add_argument("--spec", help="写入自定义场景 JSON（@文件）；只给 preset 字段时以该预设补全")
    p.add_argument("--show", action="store_true", help="打印当前设计")
    p.add_argument("--stills", "--still", action="store_true", help="渲染 3 张关键时刻静帧（前奏 / 主歌 / 副歌）")
    p.add_argument("--gallery", action="store_true", help="为每个预设渲染一张副歌静帧（阶段一画廊）")
    p.add_argument("--video", action="store_true", help="渲染完整 MV 背景视频（含原唱音轨）")
    p.add_argument("--seconds", type=float, help="只渲染开头 N 秒（试看）")
    p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("hires-source", help="阶段一：登记 Hi-Res / 无损音源（对齐到素材时间轴，打轴与成品都用它）")
    p.add_argument("job")
    p.add_argument("--on", help="原唱无损音频")
    p.add_argument("--off", action="append", help="伴奏无损音频，可多次")
    p.add_argument("--no-align", action="store_true")
    p.add_argument("--clear", action="store_true", help="取消登记")

    p = sub.add_parser("mv-assets", help="MV 混剪素材池：添加图片（本地 / 网址）、从视频抽场景、查看剪辑计划")
    p.add_argument("job")
    p.add_argument("--add", action="append",
                   help="图片 / 文件夹 / .zip 图包 / http(s) 网址，可多次（网络图片需先征得用户同意）")
    p.add_argument("--origin", choices=["user", "web", "video"],
                   help="素材来源（缺省：网址 = web，本地文件 = user）")
    p.add_argument("--source", help="来源页面网址（记录出处）")
    p.add_argument("--credit", help="署名 / 出处说明")
    p.add_argument("--tags", help="逗号分隔标签，如 chorus,character")
    p.add_argument("--from-video", type=int, metavar="N", help="从素材视频抽取 N 个不同场景")
    p.add_argument("--remove", action="append", help="删除素材 id（all = 全部）")
    p.add_argument("--tag", action="append", help="事后打标：id[+id…]=标签,标签（支持 sheet 编号 #3 / U5）")
    p.add_argument("--list", action="store_true")
    p.add_argument("--credits", action="store_true", help="列出出处 / 署名")
    p.add_argument("--plan", action="store_true", help="按当前 MV 设计计算节拍剪辑计划（需要多少张图）")

    p = sub.add_parser("mv-video", help="网络 MV 背景：搜索 / 下载 YouTube·B站 MV 并对齐到歌曲（下载前须征得用户同意）")
    p.add_argument("job")
    p.add_argument("--search", help="搜索关键词（YouTube），如「歌手 歌名 MV」")
    p.add_argument("--n", type=int, default=8, help="搜索结果数量")
    p.add_argument("--info", metavar="URL|#k", help="查看视频信息（标题 / 频道 / 时长 / 将下载的格式与大小）")
    p.add_argument("--use", metavar="URL|#k", help="下载并设为背景（自动对齐到歌曲）")
    p.add_argument("--local", metavar="PATH", help="改用本机视频文件作 MV 背景（同样自动对齐）")
    p.add_argument("--max-height", type=int, default=1080)

    p = sub.add_parser("oped", help="OP/ED 拼接背景：多段 OP/ED 视频各自波形对齐到歌曲时间轴，按添加顺序优先级拼接成整首歌的背景")
    p.add_argument("job")
    p.add_argument("--add", action="append", metavar="PATH|URL",
                   help="添加拼接源（本地路径直接引用，http(s) 网址下载；可多次，先添加的优先）")
    p.add_argument("--label", action="append", help="与 --add 按出现顺序配对的段名")
    p.add_argument("--credit", action="append", help="与 --add 按出现顺序配对的署名 / 出处")
    p.add_argument("--section", action="append", metavar="A-B",
                   help="与 --add 按出现顺序配对的源区间（视频内时间，秒或 m:ss；缺省 = 整段视频）")
    p.add_argument("--speed", action="append", metavar="X",
                   help="与 --add 按出现顺序配对的播放速度（缺省 1.0；<1 = 慢放拉长，如 0.55 铺满间奏空缺）")
    p.add_argument("--shift", action="append", metavar="sN=SEC", help="手动覆盖某段的对齐偏移（对齐失败时的逃生门）")
    p.add_argument("--remove", action="append", metavar="sN", help="移除某段")
    p.add_argument("--clear", action="store_true", help="清空全部拼接段")
    p.add_argument("--list", action="store_true", help="列出各段的对齐与歌曲时间轴覆盖区间")
    p.add_argument("--render", action="store_true", help="拼接生成 media/oped_bg.mp4 并设为当前背景")
    p.add_argument("--fade", type=float, help="段间交叉淡化秒数（默认 0.7）")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重渲")

    p = sub.add_parser("mv-clips", help="视频片段混剪：下载游戏 / 动画素材视频（YouTube·B站 / 本地），切成镜头加入混剪素材池")
    p.add_argument("job")
    p.add_argument("--search", help="搜索 YouTube（PV / 实况 / 过场 / 剪辑）")
    p.add_argument("--n", type=int, default=15, help="搜索结果数量")
    p.add_argument("--info", metavar="URL", help="查看视频信息（标题 / 频道 / 时长 / 章节 / 最高画质），不下载")
    p.add_argument("--add", action="append", metavar="URL|PATH",
                   help="下载（只要画面，≤1080p）或登记本地视频，切成镜头加入素材池；可多次")
    p.add_argument("--sections", help="长视频只下载这些区间，如 60-180,7:30-9:00（对本次所有 --add 生效）")
    p.add_argument("--max-height", type=int, default=1080)
    p.add_argument("--credit", help="署名（作品 / 频道）")
    p.add_argument("--crop", help="画面裁切 x,y,w,h（0–1，去黑边 / 角标 / 摄像头框）；缺省自动去黑边")
    p.add_argument("--library", help="素材库：名称或目录（缺省：工程目录旁的 _footage，多个工程共用）")
    p.add_argument("--min-score", type=float, default=0.35, help="镜头质量下限 0–1（亮度 / 色彩 / 细节 / 动感）")
    p.add_argument("--max-clips", type=int, default=80, help="每个来源最多加入的镜头数")
    p.add_argument("--tags", help="逗号分隔标签")
    p.add_argument("--from-library", action="store_true", help="把素材库里已下载的来源全部加入本工程")
    p.add_argument("--sheet", action="store_true", help="生成带编号的镜头缩略图总览（previews/clip_sheet_N.jpg）供审核")
    p.add_argument("--sheet-used", action="store_true",
                   help="按当前剪辑计划，为每个用到的片段截取实际出镜部分的首 / 中 / 尾三帧（previews/clip_used_N.jpg，编号 U1…）供二次审核；已标记 checked 的片段跳过")
    p.add_argument("--mark-checked", action="store_true",
                   help="二次审核后：把当前计划用到的片段标记为 checked（先 --exclude 不合格的；可与 --sheet-used 同用，先标记再出新图）")
    p.add_argument("--exclude", help="从素材池移除镜头（逗号分隔 id，或最近一次 --sheet 上的黄色编号 #N）")
    p.add_argument("--tag", action="append", help="id[+id…]=标签,标签")
    p.add_argument("--avoid-from", metavar="PROJECT", help="另一个工程已用过的镜头排到最后（同一素材库的多首歌少重复画面）")
    p.add_argument("--list", action="store_true")
    p.add_argument("--credits", action="store_true", help="列出素材来源署名")
    p.add_argument("--enhance", action="store_true",
                   help="AI 增强素材池里的低清源视频（RIFE 补帧 + Real-ESRGAN 超分）：在素材库生成 <原名>.enhanced.mp4 "
                        "并把素材池指过去；串行执行，一次只处理一个源")
    p.add_argument("--ids", help="只增强这些镜头 id（逗号分隔）涉及的源文件；时长 >5 分钟的保护对显式指定的源不生效")
    p.add_argument("--only-used", action="store_true", help="只增强当前剪辑计划（montage_plan.json）实际用到的镜头涉及的源")
    p.add_argument("--no-sr", action="store_true", help="不做超分（只补帧）")
    p.add_argument("--no-rife", action="store_true", help="不做补帧（只超分）")
    p.add_argument("--sr-scale", type=int, default=2, help="超分倍率 2/3/4（默认 2）")
    p.add_argument("--rife-multi", type=int, default=2, help="补帧倍率，2 的幂（默认 2）")

    p = sub.add_parser("versions", help="on vocal / off vocal 双版本（复制视频流，只换音轨）")
    p.add_argument("job")
    p.add_argument("--video", help="母版视频（缺省：已导出的成品 MP4）")
    p.add_argument("--on", help="原唱音频（缺省：母版自带音轨）")
    p.add_argument("--off", action="append", help="伴奏音频，可多次（缺省：人声分离得到的伴奏）")
    p.add_argument("--no-align", action="store_true")

    p = sub.add_parser("timing", help="阶段二：自动打轴")
    p.add_argument("job")
    p.add_argument("--device", help="cpu / cuda / xpu / xpu:1 …（缺省：安装时选定的加速方案）")
    p.add_argument("--no-chunk", action="store_true", help="整首一次对齐（默认按间奏分段）")
    p.add_argument("--no-words", action="store_true", help="不用逐字歌词（YRC/KRC 单词时间）校正对齐结果")
    p.add_argument("--aligner", action="store_true",
                   help="英文歌也用强制对齐模型（缺省：英文歌按逐字歌词 / 英文识别定时，同 realign --english）")

    p = sub.add_parser("realign", help="重新对齐部分行")
    p.add_argument("job")
    p.add_argument("--lines", required=True, help="如 12-15")
    p.add_argument("--window", help="start,end（秒）；缺省按前后行自动推断")
    p.add_argument("--english", action="store_true", help="英文行：用英文语音识别确定每个单词的时间")
    p.add_argument("--no-words", action="store_true", help="不用逐字歌词（YRC/KRC 单词时间）校正")
    p.add_argument("--asr-first", action="store_true",
                   help="--english：即使逐字歌词可靠也以语音识别的单词时间为主")
    p.add_argument("--fresh-asr", action="store_true",
                   help="--english：重新识别这些行（缺省：英文歌复用阶段一的整首英文识别）")
    p.add_argument("--device")

    p = sub.add_parser("edit", help="时间轴编辑（JSON 操作列表）")
    p.add_argument("job")
    p.add_argument("ops", help='JSON，如 [{"op":"shift_lines","lines":[3],"ms":-200}]（行号从 0 开始）')

    p = sub.add_parser("singers", help="从字幕文件 / 歌割り资料导入每个字的演唱者（打轴之后）")
    p.add_argument("job")
    p.add_argument("--from", dest="source", required=True,
                   help="字幕或资料：.ass/.ssa/.lrc/.txt/.html/.json 文件或网页 URL")
    p.add_argument("--map", action="append", default=[],
                   help="键=歌手（键：颜色 #rrggbb、ASS 的 Name/Style、LRC 前缀、none=无颜色文字；歌手写 - 表示忽略）")
    p.add_argument("--ignore-unmapped", action="store_true", help="忽略没有 --map 的键")
    p.add_argument("--dry-run", action="store_true", help="只报告覆盖率，不修改")

    p = sub.add_parser("style", help="修改渲染样式（JSON 合并）")
    p.add_argument("job")
    p.add_argument("patch", help='JSON，如 {"template":"neon","overrides":{"font_size_px":110}}')
    p.add_argument("--no-preview", action="store_true")

    p = sub.add_parser("qa", help="打印质检摘要")
    p.add_argument("job")

    p = sub.add_parser("frame", help="用渲染引擎输出某一时刻的画面")
    p.add_argument("job")
    p.add_argument("t", type=float)
    p.add_argument("--out")

    p = sub.add_parser("export", help="导出工程 / 成品")
    p.add_argument("job")
    p.add_argument("--kinds", default="sug,yurika,mp4,onoff", help="sug,yurika,lrc,mp4,onoff,mv,hires")
    p.add_argument("--clip", type=float, help="只渲染开头 N 秒的试看版（<名称> (preview).mp4）")
    p.add_argument("--cast-delay", type=int, metavar="MS",
                   help="投屏延迟：画面比声音提前的毫秒数（缺省 +200，保存到工程设置）")

    p = sub.add_parser("hires", help="Hi-Res 混流：成品视频 + 无损原唱/伴奏 → MKV")
    p.add_argument("job")
    p.add_argument("--on", help="原唱无损音频（缺省：素材原音频）")
    p.add_argument("--off", action="append", help="伴奏无损音频，可多次（缺省：人声分离得到的伴奏）")
    p.add_argument("--video", help="视频轨（缺省：已导出的成品 MP4）")
    p.add_argument("--no-align", action="store_true", help="不做波形对齐，按原样混流")

    p = sub.add_parser("open-app", help="在 Lin-K Lyrics 主程序中打开导出的工程（最后一步可选）")
    p.add_argument("job")
    p.add_argument("--file", help="yurika（默认）/ sug / 文件路径")
    p.add_argument("--exe", help="已安装的 Lin-K Lyrics.exe（缺省：从引擎源码启动）")

    p = sub.add_parser("ui-action", help=argparse.SUPPRESS)
    p.add_argument("--job", required=True)
    p.add_argument("--action-file", required=True)

    args = ap.parse_args(argv)
    cmd = args.cmd

    if cmd == "setup":
        from kmlib import setup_env

        return setup_env.main(check=args.check, with_models=args.with_models, target=args.target,
                              engine_ref=args.engine_ref or setup_env.ENGINE_REF, reinstall_ai=args.reinstall_ai)
    if cmd == "accel":
        from kmlib import accel, paths, setup_env

        home = paths.km_home()
        if args.set:
            accel.set_values(home, args.set)
        _print(accel.report(home, setup_env._py(home / "ai_venv"), deep=not args.quick))
        return 0
    if cmd == "serve":
        from kmlib import server

        if args.daemon:
            _print(server.serve_daemon(args.job, port=args.port, open_browser=not args.no_browser))
            return 0
        server.serve(args.job, port=args.port, open_browser=not args.no_browser)
        return 0
    if cmd == "stop":
        from kmlib import server

        _print(server.stop(args.job))
        return 0
    if cmd == "status":
        _print(_commands().status(args.job, full=args.full))
        return 0
    if cmd == "wait":
        return _wait(args)
    if cmd == "say":
        _store(args.job).chat("agent", args.text, reply_to=args.reply_to)
        return 0
    return _commands().dispatch(args)


def _wait(args) -> int:
    store = _store(args.job)
    deadline = time.monotonic() + args.timeout
    store.update(lambda st: st.setdefault("agent", {}).update(listening=True, listening_since=time.time()))
    try:
        while True:
            pending = store.pending_actions()
            # only wake the agent for things it must handle; server-handled
            # actions (nudges, exports...) are returned alongside as context
            if any(a.get("handled_by", "agent") != "server" for a in pending):
                items = pending if args.peek else store.consume_actions()
                _print({"actions": items})
                return 0
            if time.monotonic() > deadline:
                _print({"actions": [], "timeout": True})
                return 0
            time.sleep(0.5)
    finally:
        try:
            store.update(lambda st: st.setdefault("agent", {}).update(listening=False))
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
