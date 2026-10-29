#!/usr/bin/env python3
"""Show recent OpenSearch queries captured by the index search slow log.

Read-only. Needs the slow log enabled (index.search.slowlog.threshold.query.trace=0ms).
Embedding arrays are shortened so the query structure and filters are readable.

Usage: python3 scripts/show_search_queries.py [-n 5] [--index abc-kb-curated-multimodal-v2]
"""
import argparse
import json
import re
from pathlib import Path

LOG = Path('/opt/homebrew/var/log/opensearch/opensearch_homebrew_index_search_slowlog.log')
LINE = re.compile(r'^\[(?P<ts>[^\]]+)\]\[\w+\s*\]\[i\.s\.s\.query\s*\].*?\[(?P<index>[^\]]+)\]\[\d+\] took\[(?P<took>[^\]]+)\].*?total_hits\[(?P<hits>[^\]]+)\].*?source\[(?P<source>.*)\], id\[')


def shorten(value):
    if isinstance(value, list):
        if len(value) > 8 and all(isinstance(item, (int, float)) for item in value):
            return f'[{len(value)} numbers: {value[0]}, {value[1]}, ...]'
        return [shorten(item) for item in value]
    if isinstance(value, dict):
        return {key: shorten(item) for key, item in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-n', type=int, default=5, help='number of recent queries')
    parser.add_argument('--index', default=None)
    args = parser.parse_args()
    if not LOG.exists():
        raise SystemExit(f'No slow log yet at {LOG}. Enable it and run a search first.')
    entries = []
    for line in LOG.read_text(errors='replace').splitlines():
        match = LINE.match(line)
        if match and (not args.index or match['index'] == args.index):
            entries.append(match)
    for match in entries[-args.n:]:
        print(f"--- {match['ts']}  index={match['index']}  took={match['took']}  hits={match['hits']}")
        try:
            print(json.dumps(shorten(json.loads(match['source'])), indent=2))
        except ValueError:
            print(match['source'][:2000])


if __name__ == '__main__':
    main()
