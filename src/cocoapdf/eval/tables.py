from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..ir.semantic import SemanticNode


@dataclass(frozen=True)
class TableMetrics:
    """Exact and tree-similarity metrics for one semantic table.

    ``cell_exact`` compares every occupied cell origin, role, span and
    normalized text. ``span_exact`` compares only origin/span topology.
    ``teds`` is an ordered labelled tree-edit similarity in [0, 1].
    """

    cell_exact: float
    span_exact: float
    teds: float
    expected_cells: int
    actual_cells: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cell_exact": round(self.cell_exact, 6),
            "span_exact": round(self.span_exact, 6),
            "teds": round(self.teds, 6),
            "expected_cells": self.expected_cells,
            "actual_cells": self.actual_cells,
        }


def evaluate_table(expected: SemanticNode | Dict[str, Any], actual: SemanticNode | Dict[str, Any]) -> TableMetrics:
    expected_tree = _table_tree(expected)
    actual_tree = _table_tree(actual)
    expected_cells = _cell_records(expected)
    actual_cells = _cell_records(actual)
    return TableMetrics(
        cell_exact=1.0 if expected_cells == actual_cells else 0.0,
        span_exact=1.0 if _span_records(expected_cells) == _span_records(actual_cells) else 0.0,
        teds=_tree_similarity(expected_tree, actual_tree),
        expected_cells=len(expected_cells),
        actual_cells=len(actual_cells),
    )


def teds_score(expected: SemanticNode | Dict[str, Any], actual: SemanticNode | Dict[str, Any]) -> float:
    """Return normalized ordered tree-edit similarity for semantic tables.

    The implementation is stdlib-only Zhang-Shasha tree edit distance. Table
    cell labels include ``th``/``td``, rowspan, colspan and normalized text, so
    both structure and content affect the result. No geometry is compared here;
    geometry is evaluated separately through provenance/cell exactness.
    """

    return _tree_similarity(_table_tree(expected), _table_tree(actual))


@dataclass(frozen=True)
class _Tree:
    label: str
    children: Tuple["_Tree", ...] = ()


def _table_tree(value: SemanticNode | Dict[str, Any]) -> _Tree:
    node = _node_dict(value)
    if str(node.get("kind")) != "table":
        raise ValueError("expected semantic table node")

    children: List[_Tree] = []
    for child in node.get("children", []):
        kind = str(child.get("kind", ""))
        if kind == "caption":
            children.append(_Tree("caption:" + _normalize_text(_node_text_dict(child))))
        elif kind == "table_note":
            children.append(_Tree("note:" + _normalize_text(_node_text_dict(child))))
        elif kind == "table_row":
            cells: List[_Tree] = []
            for cell in child.get("children", []):
                if str(cell.get("kind")) != "table_cell":
                    continue
                attrs = cell.get("attrs") or {}
                role = str(attrs.get("role") or "td").lower()
                rowspan = max(1, _integer(attrs.get("rowspan"), 1))
                colspan = max(1, _integer(attrs.get("colspan"), 1))
                text = _normalize_text(_node_text_dict(cell))
                cells.append(_Tree("%s:r%d:c%d:%s" % (role, rowspan, colspan, text), _block_children(cell)))
            children.append(_Tree("tr", tuple(cells)))
    return _Tree("table", tuple(children))


def _block_children(cell: Dict[str, Any]) -> Tuple[_Tree, ...]:
    blocks: List[_Tree] = []
    for child in cell.get("children", []):
        kind = str(child.get("kind", ""))
        if kind in {"paragraph", "list", "item", "code_block", "quote", "image"}:
            blocks.append(_semantic_subtree(child))
    return tuple(blocks)


def _semantic_subtree(node: Dict[str, Any]) -> _Tree:
    kind = str(node.get("kind", "unknown"))
    label = kind
    if kind in {"text", "code", "code_block", "paragraph", "item", "quote"}:
        label += ":" + _normalize_text(_node_text_dict(node))
    return _Tree(label, tuple(_semantic_subtree(child) for child in node.get("children", [])))


def _cell_records(value: SemanticNode | Dict[str, Any]) -> Tuple[Tuple[Any, ...], ...]:
    node = _node_dict(value)
    records: List[Tuple[Any, ...]] = []
    for row in node.get("children", []):
        if str(row.get("kind")) != "table_row":
            continue
        for cell in row.get("children", []):
            if str(cell.get("kind")) != "table_cell":
                continue
            attrs = cell.get("attrs") or {}
            records.append((
                _integer(attrs.get("row"), 0),
                _integer(attrs.get("col"), 0),
                max(1, _integer(attrs.get("rowspan"), 1)),
                max(1, _integer(attrs.get("colspan"), 1)),
                str(attrs.get("role") or "td").lower(),
                _normalize_text(_node_text_dict(cell)),
            ))
    return tuple(sorted(records))


def _span_records(records: Sequence[Tuple[Any, ...]]) -> Tuple[Tuple[int, int, int, int], ...]:
    return tuple(sorted((int(row), int(col), int(rowspan), int(colspan)) for row, col, rowspan, colspan, _role, _text in records))


def _node_dict(value: SemanticNode | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(value, SemanticNode):
        return value.to_dict()
    if not isinstance(value, dict):
        raise TypeError("semantic node must be a SemanticNode or dictionary")
    return value


def _node_text_dict(node: Dict[str, Any]) -> str:
    text = str(node.get("text") or "")
    if text:
        return text
    return "".join(_node_text_dict(child) for child in node.get("children", []))


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _integer(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _tree_similarity(left: _Tree, right: _Tree) -> float:
    left_index = _Postorder(left)
    right_index = _Postorder(right)
    distance = _zhang_shasha(left_index, right_index)
    normalizer = max(left_index.size, right_index.size, 1)
    return max(0.0, 1.0 - distance / normalizer)


class _Postorder:
    def __init__(self, root: _Tree) -> None:
        self.nodes: List[Optional[_Tree]] = [None]
        self.leftmost: List[int] = [0]
        self._append(root)
        self.size = len(self.nodes) - 1
        latest: Dict[int, int] = {}
        for index in range(1, self.size + 1):
            latest[self.leftmost[index]] = index
        self.keyroots = sorted(latest.values())

    def _append(self, node: _Tree) -> int:
        child_indices = [self._append(child) for child in node.children]
        index = len(self.nodes)
        self.nodes.append(node)
        self.leftmost.append(self.leftmost[child_indices[0]] if child_indices else index)
        return index


def _zhang_shasha(left: _Postorder, right: _Postorder) -> float:
    tree_distance = [[0.0] * (right.size + 1) for _ in range(left.size + 1)]
    for left_root in left.keyroots:
        for right_root in right.keyroots:
            _forest_distance(left, right, left_root, right_root, tree_distance)
    return tree_distance[left.size][right.size]


def _forest_distance(
    left: _Postorder,
    right: _Postorder,
    left_root: int,
    right_root: int,
    tree_distance: List[List[float]],
) -> None:
    left_start = left.leftmost[left_root]
    right_start = right.leftmost[right_root]
    rows = left_root - left_start + 2
    columns = right_root - right_start + 2
    forest = [[0.0] * columns for _ in range(rows)]
    for row in range(1, rows):
        forest[row][0] = forest[row - 1][0] + 1.0
    for column in range(1, columns):
        forest[0][column] = forest[0][column - 1] + 1.0
    for row in range(1, rows):
        left_node = left_start + row - 1
        for column in range(1, columns):
            right_node = right_start + column - 1
            deletion = forest[row - 1][column] + 1.0
            insertion = forest[row][column - 1] + 1.0
            if left.leftmost[left_node] == left_start and right.leftmost[right_node] == right_start:
                rename = forest[row - 1][column - 1] + _rename_cost(left.nodes[left_node], right.nodes[right_node])
                forest[row][column] = min(deletion, insertion, rename)
                tree_distance[left_node][right_node] = forest[row][column]
            else:
                prefix_row = left.leftmost[left_node] - left_start
                prefix_column = right.leftmost[right_node] - right_start
                substitution = forest[prefix_row][prefix_column] + tree_distance[left_node][right_node]
                forest[row][column] = min(deletion, insertion, substitution)


def _rename_cost(left: Optional[_Tree], right: Optional[_Tree]) -> float:
    if left is None or right is None:
        return 1.0
    if left.label == right.label:
        return 0.0
    left_head, _, left_text = left.label.partition(":")
    right_head, _, right_text = right.label.partition(":")
    if left_head != right_head:
        return 1.0
    if not left_text and not right_text:
        return 0.0
    return _normalized_levenshtein(left_text, right_text)


def _normalized_levenshtein(left: str, right: str) -> float:
    if left == right:
        return 0.0
    if not left or not right:
        return 1.0
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1] / max(len(left), len(right))
