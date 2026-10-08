"""领域层相关的具体命令实现。

所有可撤销操作均通过 Command 模式实现。
命令操作的对象是新层次化领域模型：Sentence → Character → Ruby。
"""

from copy import deepcopy
from typing import Optional, Dict, Any, List, Tuple
from strange_uta_game.backend.domain import (
    Project,
    Sentence,
    Character,
    Ruby,
)
from .base import Command
from .sentence_snapshot import SentenceSnapshotCommand


class AddTimeTagCommand(Command):
    """添加时间标签命令

    在指定句子的指定字符上添加一个时间戳。
    """

    def __init__(
        self,
        project: Project,
        sentence_id: str,
        char_idx: int,
        timestamp_ms: int,
        checkpoint_idx: int = -1,
    ):
        self.project = project
        self.sentence_id = sentence_id
        self.char_idx = char_idx
        self.timestamp_ms = timestamp_ms
        self.checkpoint_idx = checkpoint_idx
        self._old_timestamps: Optional[list] = None
        self._old_sentence_end_ts: Optional[int] = None
        # 光标追踪：撤销/重做后应恢复的全局 checkpoint 索引
        self.undo_cp_idx: Optional[int] = None
        self.redo_cp_idx: Optional[int] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")
        char = sentence.get_character(self.char_idx)
        if not char:
            raise ValueError(f"字符索引 {self.char_idx} 超出范围")

        self._old_timestamps = list(char.timestamps)
        self._old_sentence_end_ts = char.sentence_end_ts
        if char.is_sentence_end and self.checkpoint_idx >= char.check_count:
            char.set_sentence_end_ts(self.timestamp_ms)
        else:
            char.add_timestamp(self.timestamp_ms, self.checkpoint_idx)

    def undo(self) -> None:
        if self._old_timestamps is not None:
            sentence = self.project.get_sentence(self.sentence_id)
            if sentence:
                char = sentence.get_character(self.char_idx)
                if char:
                    char.timestamps = list(self._old_timestamps)
                    char.sentence_end_ts = self._old_sentence_end_ts
                    char._update_offset_timestamps()
                    char.push_to_ruby()

    @property
    def description(self) -> str:
        return f"添加时间标签 [{self.timestamp_ms}ms]"


class RemoveTimeTagCommand(Command):
    """删除时间标签命令

    移除指定字符上指定 checkpoint 位置的时间戳。
    """

    def __init__(
        self,
        project: Project,
        sentence_id: str,
        char_idx: int,
        checkpoint_idx: int,
    ):
        self.project = project
        self.sentence_id = sentence_id
        self.char_idx = char_idx
        self.checkpoint_idx = checkpoint_idx
        self._removed_ts: Optional[int] = None
        self._removed_sentence_end_ts: Optional[int] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")
        char = sentence.get_character(self.char_idx)
        if not char:
            raise ValueError(f"字符索引 {self.char_idx} 超出范围")

        if char.is_sentence_end and self.checkpoint_idx >= char.check_count:
            self._removed_sentence_end_ts = char.sentence_end_ts
            char.clear_sentence_end_ts()
        else:
            self._removed_ts = char.remove_timestamp_at(self.checkpoint_idx)

    def undo(self) -> None:
        if self._removed_ts is not None or self._removed_sentence_end_ts is not None:
            sentence = self.project.get_sentence(self.sentence_id)
            if sentence:
                char = sentence.get_character(self.char_idx)
                if char:
                    if self._removed_ts is not None:
                        char.add_timestamp(self._removed_ts, self.checkpoint_idx)
                    elif self._removed_sentence_end_ts is not None:
                        char.set_sentence_end_ts(self._removed_sentence_end_ts)

    @property
    def description(self) -> str:
        ts = (
            self._removed_ts
            if self._removed_ts is not None
            else self._removed_sentence_end_ts
        )
        ts = ts if ts is not None else "?"
        return f"删除时间标签 [{ts}ms]"


class ClearLineTimeTagsCommand(Command):
    """清空整行时间标签命令

    清除指定句子所有字符的时间戳。
    """

    def __init__(self, project: Project, sentence_id: str):
        self.project = project
        self.sentence_id = sentence_id
        self._old_timestamps: Optional[Dict[int, list]] = None
        self._old_sentence_end_ts: Optional[Dict[int, Optional[int]]] = None
        # 光标追踪：撤销/重做后应恢复的全局 checkpoint 索引
        self.undo_cp_idx: Optional[int] = None
        self.redo_cp_idx: Optional[int] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")

        # 保存所有字符的时间戳
        self._old_timestamps = {
            i: list(c.timestamps) for i, c in enumerate(sentence.characters)
        }
        self._old_sentence_end_ts = {
            i: c.sentence_end_ts for i, c in enumerate(sentence.characters)
        }
        sentence.clear_all_timestamps()

    def undo(self) -> None:
        if self._old_timestamps is not None:
            sentence = self.project.get_sentence(self.sentence_id)
            if sentence:
                for i, timestamps in self._old_timestamps.items():
                    char = sentence.get_character(i)
                    if char:
                        char.timestamps = list(timestamps)
                        if self._old_sentence_end_ts is not None:
                            char.sentence_end_ts = self._old_sentence_end_ts.get(i)
                        char._update_offset_timestamps()
                        char.push_to_ruby()

    @property
    def description(self) -> str:
        return "清空行时间标签"


class UpdateCharacterCommand(Command):
    """更新字符属性命令

    修改指定字符的属性（check_count、is_line_end、is_rest、
    linked_to_next、singer_id）。通过 kwargs 传入要修改的属性。
    """

    ALLOWED_ATTRS = {
        "check_count",
        "is_line_end",
        "is_rest",
        "linked_to_next",
        "singer_id",
        "needs_guide",
    }

    def __init__(
        self,
        project: Project,
        sentence_id: str,
        char_idx: int,
        **kwargs: Any,
    ):
        self.project = project
        self.sentence_id = sentence_id
        self.char_idx = char_idx
        # 过滤非法属性
        self.updates = {k: v for k, v in kwargs.items() if k in self.ALLOWED_ATTRS}
        self._old_values: Dict[str, Any] = {}
        # check_count 走权威 setter 时联动的 timestamps / ruby 原状态（undo 还原用）
        self._old_check_count_state: Optional[Dict[str, Any]] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")
        char = sentence.get_character(self.char_idx)
        if not char:
            raise ValueError(f"字符索引 {self.char_idx} 超出范围")

        # 保存旧值
        for key in self.updates:
            self._old_values[key] = getattr(char, key)

        # check_count 必须走权威 setter（直接 setattr 会绕过 timestamps 截断
        # 与 ruby.parts 合并/重分段的不变式维护）
        if "check_count" in self.updates:
            self._old_check_count_state = {
                "check_count": char.check_count,
                "timestamps": list(char.timestamps),
                "ruby": deepcopy(char.ruby),
            }
            char.set_check_count(self.updates["check_count"])

        # 应用其余新值
        for key, value in self.updates.items():
            if key == "check_count":
                continue
            setattr(char, key, value)

        # check_count 变更可能使选中 cp 越界，自动顺延
        if "check_count" in self.updates:
            self.project.shift_selected_checkpoint_if_lost()

    def undo(self) -> None:
        if self._old_values:
            sentence = self.project.get_sentence(self.sentence_id)
            if sentence:
                char = sentence.get_character(self.char_idx)
                if char:
                    # 先还原 check_count 联动状态（timestamps/ruby 逐位还原，
                    # 不走 set_check_count——重分段无法保证还原出原分段）
                    if self._old_check_count_state is not None:
                        char.check_count = self._old_check_count_state["check_count"]
                        char.timestamps = list(self._old_check_count_state["timestamps"])
                        char.ruby = deepcopy(self._old_check_count_state["ruby"])
                        char._update_offset_timestamps()
                        char.push_to_ruby()
                    for key, value in self._old_values.items():
                        if key == "check_count":
                            continue
                        setattr(char, key, value)

    @property
    def description(self) -> str:
        attrs = ", ".join(f"{k}={v}" for k, v in self.updates.items())
        return f"更新字符属性 (char_idx={self.char_idx}, {attrs})"


class AddRubyCommand(Command):
    """添加注音命令

    为指定字符设置注音。
    """

    def __init__(
        self,
        project: Project,
        sentence_id: str,
        char_idx: int,
        ruby: Ruby,
    ):
        self.project = project
        self.sentence_id = sentence_id
        self.char_idx = char_idx
        self.ruby = ruby
        self._old_ruby: Optional[Ruby] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")
        char = sentence.get_character(self.char_idx)
        if not char:
            raise ValueError(f"字符索引 {self.char_idx} 超出范围")

        self._old_ruby = char.ruby
        char.set_ruby(self.ruby)

    def undo(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if sentence:
            char = sentence.get_character(self.char_idx)
            if char:
                char.set_ruby(self._old_ruby)

    @property
    def description(self) -> str:
        return f"添加注音 [{self.ruby.text}]"


class RemoveRubyCommand(Command):
    """移除注音命令

    移除指定字符的注音。
    """

    def __init__(self, project: Project, sentence_id: str, char_idx: int):
        self.project = project
        self.sentence_id = sentence_id
        self.char_idx = char_idx
        self._removed_ruby: Optional[Ruby] = None

    def execute(self) -> None:
        sentence = self.project.get_sentence(self.sentence_id)
        if not sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")

        self._removed_ruby = sentence.remove_ruby_from_char(self.char_idx)

    def undo(self) -> None:
        if self._removed_ruby:
            sentence = self.project.get_sentence(self.sentence_id)
            if sentence:
                char = sentence.get_character(self.char_idx)
                if char:
                    char.set_ruby(self._removed_ruby)

    @property
    def description(self) -> str:
        text = self._removed_ruby.text if self._removed_ruby else "?"
        return f"移除注音 [{text}]"


class AddSentenceCommand(Command):
    """添加句子命令"""

    def __init__(
        self,
        project: Project,
        sentence: Sentence,
        after_sentence_id: Optional[str] = None,
    ):
        self.project = project
        self.sentence = sentence
        self.after_sentence_id = after_sentence_id
        self._added = False

    def execute(self) -> None:
        self.project.add_sentence(self.sentence, self.after_sentence_id)
        self._added = True

    def undo(self) -> None:
        if self._added:
            try:
                self.project.remove_sentence(self.sentence.id)
            except Exception:
                pass

    @property
    def description(self) -> str:
        return f"添加歌词行 [{self.sentence.text[:10]}...]"


class RemoveSentenceCommand(Command):
    """删除句子命令"""

    def __init__(self, project: Project, sentence_id: str):
        self.project = project
        self.sentence_id = sentence_id
        self._sentence: Optional[Sentence] = None
        self._index: int = -1

    def execute(self) -> None:
        self._sentence = self.project.get_sentence(self.sentence_id)
        if not self._sentence:
            raise ValueError(f"句子 {self.sentence_id} 不存在")

        self._index = self.project.sentences.index(self._sentence)
        self.project.remove_sentence(self.sentence_id)

    def undo(self) -> None:
        if self._sentence:
            if 0 <= self._index <= len(self.project.sentences):
                self.project.sentences.insert(self._index, self._sentence)
            else:
                self.project.add_sentence(self._sentence)

    @property
    def description(self) -> str:
        return "删除歌词行"


class AddSingerCommand(Command):
    """添加演唱者命令"""

    def __init__(self, project: Project, singer):
        self.project = project
        self.singer = singer

    def execute(self) -> None:
        self.project.add_singer(self.singer)

    def undo(self) -> None:
        try:
            self.project.remove_singer(self.singer.id)
        except Exception:
            pass

    @property
    def description(self) -> str:
        return f"添加演唱者 [{self.singer.name}]"


class RemoveSingerCommand(Command):
    """删除演唱者命令"""

    def __init__(
        self, project: Project, singer_id: str, transfer_to: Optional[str] = None
    ):
        self.project = project
        self.singer_id = singer_id
        self.transfer_to = transfer_to
        self._singer = None
        # 被删除（或被改写句级 singer_id）的句子 → (删除前索引, sentence)
        self._sentences: List[Tuple[int, Sentence]] = []
        # 保留句子中被改写的逐字 singer_id → {sentence_id: [(char_idx, 原值)]}
        self._char_singer_ids: Dict[str, List[Tuple[int, str]]] = {}

    def execute(self) -> None:
        self._singer = self.project.get_singer(self.singer_id)
        if self._singer:
            # 记录删除前状态，undo 按位还原句子顺序与逐字 singer_id
            self._sentences = [
                (i, s)
                for i, s in enumerate(self.project.sentences)
                if s.singer_id == self.singer_id
            ]
            # 逐字 singer_id：转移场景下 _sentences 内的句子保留但逐字被改写，
            # 级联场景下保留句子的逐字也可能被改写——统一全量记录
            self._char_singer_ids = {}
            for s in self.project.sentences:
                hits = [
                    (ci, ch.singer_id)
                    for ci, ch in enumerate(s.characters)
                    if ch.singer_id == self.singer_id
                ]
                if hits:
                    self._char_singer_ids[s.id] = hits
            self.project.remove_singer(self.singer_id, self.transfer_to)

    def undo(self) -> None:
        if self._singer:
            self.project.add_singer(self._singer)
            for idx, sentence in self._sentences:
                # 还原句级 singer_id（级联删除场景原值即本演唱者）
                sentence.singer_id = self.singer_id
                if sentence not in self.project.sentences:
                    if 0 <= idx <= len(self.project.sentences):
                        self.project.sentences.insert(idx, sentence)
                    else:
                        self.project.sentences.append(sentence)
            # 还原保留句子中的逐字 singer_id
            for sentence in self.project.sentences:
                for ci, old_singer_id in self._char_singer_ids.get(sentence.id, []):
                    char = sentence.get_character(ci)
                    if char:
                        char.singer_id = old_singer_id
            self.project._update_timestamp()

    @property
    def description(self) -> str:
        return "删除演唱者"


class TagAndDeleteNextCommand(SentenceSnapshotCommand):
    """「打轴并删除下一节奏点」的窄快照撤销命令。

    只快照受影响的一到两个 Character（写入者 + 被删节奏点所属字符，可能为
    同一字符），避免每次按键对整个 ``project.sentences`` 做 2 次 deepcopy。

    刻意继承 SentenceSnapshotCommand：前端按 ``isinstance(cmd,
    SentenceSnapshotCommand)`` 走结构化刷新路径，undo_position /
    redo_position / move_cp 光标恢复语义保持不变。
    """

    def __init__(
        self,
        project: Project,
        entries: List[Tuple[str, int, Dict[str, Any], Dict[str, Any]]],
        description: str,
    ):
        self._project = project
        # [(sentence_id, char_idx, before_state, after_state)]
        self._entries = entries
        self._description = description
        self.undo_position: Optional[Tuple[int, int]] = None
        """撤销后应恢复的光标位置 ``(line_idx, char_idx)``。"""
        self.redo_position: Optional[Tuple[int, int]] = None
        """重做后应恢复的光标位置 ``(line_idx, char_idx)``。"""
        self.move_cp: bool = True
        """撤销/重做后是否需要调用 timing_service.move_to_checkpoint 同步打轴位置。"""

    @staticmethod
    def _capture_state(char: Character) -> Dict[str, Any]:
        """捕获单个字符的打轴相关状态（check_count / 时间戳 / 停顿点 / ruby）。"""
        return {
            "check_count": char.check_count,
            "timestamps": list(char.timestamps),
            "sentence_end_ts": char.sentence_end_ts,
            "is_sentence_end": char.is_sentence_end,
            "ruby": deepcopy(char.ruby),
        }

    @classmethod
    def _apply_state(
        cls, project: Project, sentence_id: str, char_idx: int, state: Dict[str, Any]
    ) -> None:
        sentence = project.get_sentence(sentence_id)
        if not sentence:
            return
        char = sentence.get_character(char_idx)
        if not char:
            return
        char.check_count = state["check_count"]
        char.timestamps = list(state["timestamps"])
        char.sentence_end_ts = state["sentence_end_ts"]
        char.is_sentence_end = state["is_sentence_end"]
        char.ruby = deepcopy(state["ruby"])
        char._update_offset_timestamps()
        char.push_to_ruby()

    def execute(self) -> None:
        for sentence_id, char_idx, _before, after in self._entries:
            self._apply_state(self._project, sentence_id, char_idx, after)
        self._project._update_timestamp()

    def undo(self) -> None:
        for sentence_id, char_idx, before, _after in reversed(self._entries):
            self._apply_state(self._project, sentence_id, char_idx, before)
        self._project._update_timestamp()
