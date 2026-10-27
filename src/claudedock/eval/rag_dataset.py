from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class RagEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str  # 评测 case 稳定 id，便于报告定位
    query: str  # 检索查询
    relevant_docs: list[str]  # 相关文档 doc_id（相对知识库根目录），供 Hit@K/MRR/Recall 判定
    ground_truth: str = ""  # 标准答案，供 RAGAs ContextRecall；检索层评测可为空
    filter: dict[str, object] = {}  # 可选元数据过滤条件：评测"带约束查询"时检索是否正确收窄


# 从 JSONL 逐行加载评测集；缺失文件返回空列表，非法行抛 ValueError 并带行号
def load_dataset(path: Path) -> list[RagEvalCase]:
    path = Path(path)
    if not path.is_file():
        return []
    cases: list[RagEvalCase] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(RagEvalCase.model_validate(json.loads(line)))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno} 非法评测行: {exc}") from exc
    return cases
