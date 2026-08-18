"""协议 4：lesson schema —— 错题本。

只沉淀"非业务 + 高概率复现 + 解法非平凡"的坑。
开工前按 keywords 召回；解决后满足条件才写。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "lesson"


@dataclass
class Lesson:
    title: str
    symptom: str
    root_cause: str
    fix: str
    keywords: List[str] = field(default_factory=list)
    scope: str = "general"
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            day = time.strftime("%Y%m%d", time.localtime(self.created_at))
            self.id = f"lesson-{day}-{_slug(self.title)}"

    def to_dict(self) -> dict:
        return asdict(self)


class LessonBook:
    """错题本：JSONL 落盘，关键词召回。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def add(self, lesson: Lesson) -> Lesson:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return lesson

    def all(self) -> List[Lesson]:
        if not self.path.exists():
            return []
        out: List[Lesson] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(Lesson(**json.loads(line)))
        return out

    def search(self, query: str) -> List[Lesson]:
        q = query.lower()
        hits = []
        for lesson in self.all():
            hay = " ".join([lesson.title, *lesson.keywords]).lower()
            if any(term and term in hay for term in q.split()) or q in hay:
                hits.append(lesson)
        return hits
