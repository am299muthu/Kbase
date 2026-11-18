"""Turn graphify's per-folder graphs into one KBase graph with permissions.

What it adds on top of graphify:
  * folder_id on every node and link, taken from the folder the graph was built for
  * evidence on every link: source file, section, how it was found, confidence
  * a KBase type for every node and a KBase predicate for every link
    (ontology/kbase-ontology.yaml); graphify cannot be told our types, so these
    are mapped here and marked kbase_type_source = "heuristic"
  * a lifecycle status: links graphify marks EXTRACTED at or above the approval
    threshold start as "approved", everything else as "proposed" for a checker

Usage:
  python scripts/graph/import_graph.py                    # write output/graph/kbase-graph.json
  python scripts/graph/import_graph.py --as E005          # also show what employee E005 can see
  python scripts/graph/import_graph.py --approve-threshold 0.9
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRAPHS = ROOT / 'output' / 'graph'
ACCESS = ROOT / 'mockdata' / 'access-control.json'
TARGET = GRAPHS / 'kbase-graph.json'

RULE_WORDS = re.compile(r'\b(rule|threshold|requires?|required|must|deadline|within|limit|approval|escalat\w*|sla)\b', re.I)
POLICY_WORDS = re.compile(r'\b(policy|procedure|standard)\b', re.I)


def kbase_type(node: dict) -> str:
    if node.get('file_type') == 'document':
        return 'Document'
    label = node.get('label', '')
    if POLICY_WORDS.search(label):
        return 'Policy'
    if RULE_WORDS.search(label):
        return 'Rule'
    return 'Term'


def predicate(relation: str, subject_type: str, object_type: str) -> str:
    if relation == 'references':
        if subject_type == 'Document':
            return {'Rule': 'states', 'Term': 'defines', 'Policy': 'evidenced_by'}.get(object_type, 'related_to')
        if object_type == 'Rule':
            return 'states'
    return 'related_to'


def status(link: dict, threshold: float) -> str:
    if link.get('confidence') == 'EXTRACTED' and float(link.get('confidence_score') or 0) >= threshold:
        return 'approved'
    return 'proposed'


def load(threshold: float) -> dict:
    nodes, links = [], []
    for graph_file in sorted(GRAPHS.glob('*/graphify-out/graph.json')):
        folder = graph_file.parents[1].name
        raw = json.loads(graph_file.read_text(encoding='utf-8'))
        types = {}
        for n in raw['nodes']:
            node_id = f'{folder}:{n["id"]}'
            types[n['id']] = kbase_type(n)
            nodes.append({
                'id': node_id,
                'label': n.get('label', n['id']),
                'kbase_type': types[n['id']],
                'kbase_type_source': 'heuristic',
                'folder_id': folder,
                'source_file': n.get('source_file'),
                'section': n.get('source_location'),
            })
        for e in raw['links']:
            s_type, o_type = types.get(e['source'], 'Term'), types.get(e['target'], 'Term')
            links.append({
                'subject': f'{folder}:{e["source"]}',
                'predicate': predicate(e.get('relation', ''), s_type, o_type),
                'graphify_relation': e.get('relation'),
                'object': f'{folder}:{e["target"]}',
                'folder_id': folder,
                'status': status(e, threshold),
                'evidence': [{
                    'source_file': e.get('source_file'),
                    'section': e.get('source_location'),
                    'extracted_by': 'llm',
                    'method': e.get('confidence'),
                    'confidence': e.get('confidence_score'),
                    'reviewed_by': None,
                }],
            })
    return {'schema': 'kbase-graph/0.1', 'approve_threshold': threshold, 'nodes': nodes, 'links': links}


def visible(graph: dict, employee_id: str) -> dict:
    """Same rule as search: only folders the employee holds a grant on."""
    grants = json.loads(ACCESS.read_text(encoding='utf-8'))
    folders = {g['folder_id'] for g in grants if g['employee_id'] == employee_id}
    links = [l for l in graph['links'] if l['folder_id'] in folders and l['status'] == 'approved']
    used = {l['subject'] for l in links} | {l['object'] for l in links}
    nodes = [n for n in graph['nodes'] if n['id'] in used]
    return {'folders': sorted(folders), 'nodes': nodes, 'links': links}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-threshold', type=float, default=0.8)
    parser.add_argument('--as', dest='employee', help='employee id to preview visibility for')
    args = parser.parse_args()
    graph = load(args.approve_threshold)
    if not graph['nodes']:
        raise SystemExit('no graphify output found; run scripts/graph/build_graph.py first')
    TARGET.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'wrote {TARGET.relative_to(ROOT)}: {len(graph["nodes"])} nodes, {len(graph["links"])} links')
    print('node types:', dict(Counter(n['kbase_type'] for n in graph['nodes'])))
    print('predicates:', dict(Counter(l['predicate'] for l in graph['links'])))
    print('status:', dict(Counter(l['status'] for l in graph['links'])))
    if args.employee:
        view = visible(graph, args.employee)
        print(f'{args.employee} can read folders {view["folders"]}: '
              f'{len(view["nodes"])} nodes, {len(view["links"])} approved links')


if __name__ == '__main__':
    main()
