from __future__ import annotations

import unittest
from typing import Callable, Dict, List, Tuple

from cocoapdf.ir.evidence import Evidence
from cocoapdf.ir.semantic import NodeFactory, SemanticNode, SourceRef
from cocoapdf.semantics.graph import _project_table_alignments
from cocoapdf.semantics.tables import _prune_vacuous_narrow_edge_columns


def _left_edge_rows(
    edge_width: float = 10.0,
    table_width: float = 400.0,
) -> Tuple[List[SemanticNode], Tuple[float, float, float, float]]:
    factory = NodeFactory("strict-prune")
    xs = (0.0, edge_width, table_width / 2.0, table_width)
    rows: List[SemanticNode] = []
    for row_index in range(2):
        cells: List[SemanticNode] = []
        for column in range(3):
            box = (xs[column], row_index * 20.0, xs[column + 1], (row_index + 1) * 20.0)
            geometry_only = column == 0
            cells.append(
                factory.make(
                    "table_cell",
                    text="" if geometry_only else "value",
                    attrs={
                        "row": row_index,
                        "col": column,
                        "rowspan": 1,
                        "colspan": 1,
                        "role": "td",
                        "bbox": box,
                        "rotation": 0,
                    },
                    evidence=[
                        Evidence(
                            "lattice_cell",
                            0.98,
                            page=1,
                            data={
                                "bbox": box,
                                **({"geometry_only_empty": True} if geometry_only else {}),
                            },
                        )
                    ],
                    sources=[SourceRef(page=1, bbox=box)],
                )
            )
        rows.append(factory.make("table_row", children=cells, attrs={"row": row_index}))
    return rows, (0.0, 0.0, table_width, 40.0)


class StrictGeometryMarkerTests(unittest.TestCase):
    def test_absolute_width_limit_rejects_twelve_point_zero_one_sliver(self) -> None:
        rows, bbox = _left_edge_rows(edge_width=12.01, table_width=500.0)

        semantic_bbox, records, _ = _prune_vacuous_narrow_edge_columns(rows, bbox)

        self.assertEqual(semantic_bbox, bbox)
        self.assertEqual(records, [])

    def test_marker_contract_rejects_missing_or_extended_evidence(self) -> None:
        def evidence(cell: SemanticNode, *, kind: str = "lattice_cell", detail: str = "", **extra: object) -> Evidence:
            return Evidence(
                kind,
                0.98,
                detail=detail,
                page=1,
                data={
                    "bbox": cell.attrs["bbox"],
                    "geometry_only_empty": True,
                    **extra,
                },
            )

        mutations: Dict[str, Callable[[SemanticNode], None]] = {
            "missing_marker": lambda cell: setattr(
                cell,
                "evidence",
                [Evidence("lattice_cell", 0.98, page=1, data={"bbox": cell.attrs["bbox"]})],
            ),
            "extra_data": lambda cell: setattr(
                cell,
                "evidence",
                [evidence(cell, actual_text="")],
            ),
            "detail": lambda cell: setattr(
                cell,
                "evidence",
                [evidence(cell, detail="inferred")],
            ),
            "non_cell_kind": lambda cell: setattr(
                cell,
                "evidence",
                [evidence(cell, kind="actual_text_resolution")],
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                rows, bbox = _left_edge_rows()
                mutate(rows[0].children[0])

                semantic_bbox, records, _ = _prune_vacuous_narrow_edge_columns(rows, bbox)

                self.assertEqual(semantic_bbox, bbox)
                self.assertEqual(records, [])


class ResidualAlignmentProjectionTests(unittest.TestCase):
    @staticmethod
    def _table(records: List[Dict[str, object]]) -> SemanticNode:
        return SemanticNode(
            id="table-1",
            kind="table",
            attrs={
                "column_count": 2,
                "pruned_vacuous_edge_columns": records,
            },
        )

    def test_already_post_prune_vector_is_unchanged(self) -> None:
        table = self._table([{"column": 0, "side": "left"}])
        self.assertEqual(
            _project_table_alignments(["right", "center"], table),
            ["right", "center"],
        )

    def test_left_then_right_records_are_applied_sequentially(self) -> None:
        table = self._table(
            [
                {"column": 0, "side": "left"},
                {"column": 2, "side": "right"},
            ]
        )
        self.assertEqual(
            _project_table_alignments(["", "left", "right", ""], table),
            ["left", "right"],
        )


if __name__ == "__main__":
    unittest.main()
