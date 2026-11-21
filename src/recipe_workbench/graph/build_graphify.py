"""Build or refresh the Graphify AST knowledge graph from ore_corpus/."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import graphify.build
import graphify.cluster
import graphify.export
import graphify.extract


def build_ore_graphify(
    corpus_dir: Path | str = Path("ore_corpus"),
    output_json: Path | str = Path("graphify-out/graph.json"),
) -> Tuple[int, int, Path]:
    corpus_path = Path(corpus_dir).resolve()
    out_path = Path(output_json).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    paths = sorted(p for p in corpus_path.rglob("*") if p.is_file())
    extraction = graphify.extract.extract(paths, root=corpus_path)
    graph = graphify.build.build_from_json(extraction, directed=True, root=corpus_path)
    communities = graphify.cluster.cluster(graph)
    graphify.export.to_json(graph, communities, str(out_path), force=True)
    return graph.number_of_nodes(), graph.number_of_edges(), out_path
