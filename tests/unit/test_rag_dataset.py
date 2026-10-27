from __future__ import annotations

from pathlib import Path

import pytest

from claudedock.eval.rag_dataset import load_dataset


# 功能：验证 load_dataset 从 JSONL 加载合法行，缺失文件返回空列表
# 设计：写两条合法行 + 一条空行，断言解析数量、缺省字段与缺失文件行为
def test_load_dataset_valid(tmp_path: Path) -> None:
    f = tmp_path / "rag_queries.jsonl"
    f.write_text(
        '{"id":"q1","query":"怎么配置？","relevant_docs":["a.md"]}\n'
        "\n"
        '{"id":"q2","query":"怎么测试？","relevant_docs":["b.md"],"ground_truth":"..."}\n',
        encoding="utf-8",
    )
    cases = load_dataset(f)
    assert len(cases) == 2
    assert cases[0].id == "q1"
    assert cases[0].ground_truth == ""
    assert cases[0].filter == {}
    assert cases[1].ground_truth == "..."
    assert load_dataset(tmp_path / "missing.jsonl") == []


# 功能：验证 load_dataset 解析可选的 filter 字段（元数据过滤条件）
# 设计：带 filter 的行断言解析为 dict，缺省行 filter 为空 dict
def test_load_dataset_filter_field(tmp_path: Path) -> None:
    f = tmp_path / "rag_queries.jsonl"
    f.write_text(
        '{"id":"q1","query":"怎么配置？","relevant_docs":["a.md"],'
        '"filter":{"department":"研发"}}\n',
        encoding="utf-8",
    )
    cases = load_dataset(f)
    assert len(cases) == 1
    assert cases[0].filter == {"department": "研发"}


# 功能：验证 load_dataset 对非法行（坏 JSON / 缺必填字段）抛 ValueError
# 设计：坏 JSON 与缺字段两行分别断言抛错，覆盖解析失败与 schema 校验失败两条路径
def test_load_dataset_invalid(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text(
        '{"id":"q1","query":"x","relevant_docs":["a"]}\nnot json\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_dataset(bad_json)

    missing_field = tmp_path / "missing.jsonl"
    missing_field.write_text('{"id":"q1"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(missing_field)
