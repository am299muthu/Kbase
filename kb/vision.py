"""Opt-in visual enrichment; never upload whole PDFs or whole-page renders."""
import hashlib
from pathlib import Path

from kb.config import settings
from kb.progress import report_progress
from kb.parsers import ParsedBlock, ParsedDocument, ParserLimitError, _limit
from kb.providers import ModelUnavailable, describe_visual, validate_visual_region


from kb.telemetry import traced

@traced('ingestion.vision')
def enrich_pdf_visuals(parsed: ParsedDocument, asset_dir: Path) -> ParsedDocument:
    cfg = settings()
    if not cfg.pdf_vision_enabled:
        if any(block.metadata.get('requires_vl') for block in parsed.blocks):
            raise ModelUnavailable('Scanned PDF reading requires its configured VL model; no unreadable placeholder was indexed.')
        return parsed
    if parsed.parser != 'docling+pypdf+pdfplumber':
        raise ModelUnavailable('PDF visual enrichment requires PDF_PARSER=docling_hybrid.')
    if not cfg.external_models_enabled or not cfg.allow_external_vision_images:
        raise ModelUnavailable('External vision and image-transfer approval must be explicitly enabled.')
    assets = {(asset['path'], asset.get('object_id'), asset.get('page')): asset for asset in parsed.assets}
    blocks = []
    cache = {}
    count = 0
    total = sum(bool(block.asset_path and block.metadata.get('object_id') and not block.metadata.get('generated')) for block in parsed.blocks)
    total_chars = sum(len(block.text) for block in parsed.blocks)
    for block in parsed.blocks:
        blocks.append(block)
        if not block.asset_path or not block.metadata.get('object_id') or block.metadata.get('generated'):
            continue
        asset = assets.get((block.asset_path, block.metadata['object_id'], block.page))
        if not asset:
            raise ValueError('Visual source asset is missing')
        validate_visual_region(asset)
        path = Path(asset['path']).resolve()
        if not path.is_relative_to(asset_dir.resolve()) or path.stat().st_size > cfg.vision_max_image_bytes:
            raise ValueError('Visual source asset violates path or byte limits')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('Visual source asset failed integrity validation')
        count += 1
        if count > _limit('PARSER_MAX_PDF_OBJECTS', 100):
            raise ParserLimitError('Visual enrichment exceeds the object limit')
        key = (asset['sha256'], asset['object_kind'])
        report_progress('visual_enrichment', completed=count - 1, total=total)
        if key not in cache:
            cache[key] = describe_visual(data, asset['object_kind'])
        description = cache[key]
        report_progress('visual_enrichment', completed=count, total=total)
        text = '[Generated visual interpretation; verify against the source object]\n' + description['description']
        if description['visible_text']:
            text += '\nModel-read visible text (not verified transcription):\n' + '\n'.join(description['visible_text'])
        if description['uncertainties']:
            text += '\nUncertainties:\n' + '\n'.join(description['uncertainties'])
        total_chars += len(text)
        if total_chars > _limit('PARSER_MAX_TEXT_CHARS', 20000000) or len(blocks) + 1 > _limit('PARSER_MAX_BLOCKS', 200000):
            raise ParserLimitError('Visual enrichment exceeds text/block limits')
        metadata = {**block.metadata, 'generated': True, 'model': description['model'],
                    'source_asset_sha256': asset['sha256']}
        blocks.append(ParsedBlock(text, block.section, block.page, 'vision_description', block.asset_path, metadata))
    if len(blocks) > _limit('PARSER_MAX_BLOCKS', 200000):
        raise ParserLimitError('Visual enrichment exceeds block limits')
    return ParsedDocument(blocks, parsed.assets, parsed.parser)
