"""Stable page / top / left ordering shared by PDF evidence and retrieval chunks."""
from collections import defaultdict
from dataclasses import asdict, replace


def order_pdf_blocks(parsed):
    groups = defaultdict(list)
    for index, block in enumerate(parsed.blocks):
        # A source and its generated interpretation stay together, even on exact ties.
        key = (block.page, block.metadata.get('object_id') or f'block:{index}')
        groups[key].append((index, block))

    def position(group):
        index, block = next(((i, b) for i, b in group if not b.metadata.get('generated')), group[0])
        box = block.metadata.get('bbox') or {}
        return (block.page if block.page is not None else float('inf'),
                box.get('top', float('inf')) / (box.get('page_height') or 1),
                box.get('left', float('inf')) / (box.get('page_width') or 1), index)

    ordered = []
    for group in sorted(groups.values(), key=position):
        for _, block in sorted(group, key=lambda pair: (bool(pair[1].metadata.get('generated')), pair[0])):
            ordered.append(replace(block, metadata={**block.metadata, 'sequence': len(ordered),
                'ordering_method': 'page-y-x' if block.metadata.get('bbox') else 'page-only-fallback'}))
    return replace(parsed, blocks=ordered)


def evidence_snapshot(parsed, stored_assets):
    """Keep the same ordered blocks while replacing temporary paths with object URIs."""
    asset_uris = {original['path']: stored['path'] for original, stored in zip(parsed.assets, stored_assets)}
    snapshot = asdict(parsed)
    snapshot['assets'] = stored_assets
    for block in snapshot['blocks']:
        if block.get('asset_path') in asset_uris:
            block['asset_path'] = asset_uris[block['asset_path']]
    return snapshot
