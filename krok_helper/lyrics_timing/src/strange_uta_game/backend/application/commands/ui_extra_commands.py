"""界面附加撤销命令。

供不经过 TimingService 的界面（演唱者管理、导出页等）把整体操作纳入
CommandManager 撤销栈使用。与 :class:`SentenceSnapshotCommand` 同思路，
但快照对象是 ``project.singers``——演唱者操作（增删改/重排/启停/分组）
此前全部绕过撤销栈。
"""

from __future__ import annotations

from copy import deepcopy
from typing import List, Optional

from strange_uta_game.backend.application.commands.base import Command
from strange_uta_game.backend.domain import Project, Sentence, Singer


class SingersSnapshotCommand(Command):
    """基于 ``project.singers`` 前后快照的演唱者操作撤销命令。

    多数演唱者操作（添加/重命名/改色/重排/启停/分组）只修改 ``singers``
    列表，``before/after_sentences`` 传 None 即可（拖放重排等高频操作
    免去整表歌词快照的开销）。删除演唱者会把句子转移/级联到其他演唱者，
    此时必须同时传入句子快照，撤销才能完整还原。
    """

    def __init__(
        self,
        project: Project,
        before_singers: List[Singer],
        after_singers: List[Singer],
        description: str,
        before_sentences: Optional[List[Sentence]] = None,
        after_sentences: Optional[List[Sentence]] = None,
    ):
        self._project = project
        self._before_singers = before_singers
        self._after_singers = after_singers
        self._before_sentences = before_sentences
        self._after_sentences = after_sentences
        self._description = description

    def _apply(self, singers: List[Singer], sentences: Optional[List[Sentence]]) -> None:
        self._project.singers = deepcopy(singers)
        if sentences is not None:
            self._project.sentences = deepcopy(sentences)
        self._project._update_timestamp()

    def execute(self) -> None:
        self._apply(self._after_singers, self._after_sentences)

    def undo(self) -> None:
        self._apply(self._before_singers, self._before_sentences)

    @property
    def description(self) -> str:
        return self._description


__all__ = ["SingersSnapshotCommand"]
