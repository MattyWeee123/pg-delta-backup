"""Positional comparison of a source tree against basis digests (page i against page i).

A node or page is reusable only if its byte range has the same, non-zero
length in both files and its digests are equal. Nodes are compared first;
only nodes that differ, or that straddle the end of the shorter file, are
descended into page by page.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pgdelta.hashing import DIGEST_SIZE
from pgdelta.tree import page_count, span_length


@dataclass
class NodePlan:
    matched: list = field(default_factory=list)
    descend: list = field(default_factory=list)
    whole: list = field(default_factory=list)
    mismatched: int = 0
    high_churn: bool = False


def classify_nodes(tree, basis_size, basis_nodes, high_churn_node_fraction=None):
    node_bytes = tree.block * tree.node_blocks
    plan, boundary = NodePlan(), []
    for node in range(tree.node_count):
        source_length = span_length(node, node_bytes, tree.size)
        basis_length = span_length(node, node_bytes, basis_size)
        if basis_length == 0:
            plan.whole.append(node)
        elif source_length != basis_length:
            boundary.append(node)
        elif tree.node(node) == basis_nodes[node * DIGEST_SIZE:(node + 1) * DIGEST_SIZE]:
            plan.matched.append(node)
        else:
            plan.descend.append(node)
    plan.mismatched = len(plan.descend)
    comparable = len(plan.matched) + plan.mismatched
    if (high_churn_node_fraction is not None and plan.mismatched
            and plan.mismatched / comparable > high_churn_node_fraction):
        plan.high_churn = True
        plan.whole = sorted(plan.whole + plan.descend + boundary)
        plan.descend = []
    else:
        plan.descend = sorted(plan.descend + boundary)
    return plan


def node_pages(tree, node):
    first = node * tree.node_blocks
    return range(first, min(first + tree.node_blocks, tree.page_count))


def basis_leaf_count(node, basis_size, block, node_blocks):
    first = node * node_blocks
    return max(0, min(node_blocks, page_count(basis_size, block) - first))


def changed_pages(tree, node, basis_size, basis_leaves):
    """Pages of `node` that must be sent, given the basis leaf digests of that node."""
    first = node * tree.node_blocks
    pages = []
    for page in node_pages(tree, node):
        offset = (page - first) * DIGEST_SIZE
        if (span_length(page, tree.block, tree.size) != span_length(page, tree.block, basis_size)
                or tree.leaf(page) != basis_leaves[offset:offset + DIGEST_SIZE]):
            pages.append(page)
    return pages


def runs(pages, max_pages):
    """Coalesce sorted page numbers into (first, count) runs of at most max_pages."""
    result = []
    for page in pages:
        if result and result[-1][0] + result[-1][1] == page and result[-1][1] < max_pages:
            result[-1][1] += 1
        else:
            result.append([page, 1])
    return [(first, count) for first, count in result]
