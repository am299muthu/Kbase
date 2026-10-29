#!/usr/bin/env python3
"""Back up and reset only pdf-pilot; originals, users, grants and audit survive.

Run --plan first. Stop app/workers before --apply. No bucket/index is dropped.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select
from opensearchpy.helpers import bulk
from kb.config import settings
from kb.db import (Chunk, ControlAttestation, Document, IndexedGeneration,
                   ProcessingRequest, ProcessingProgress, audit, db_session)
from kb.search import client
from kb.storage import ObjectStore, file_digest, validate_key

FOLDER = 'pdf-pilot'
INDEXES = ('abc-kb-curated-v1', 'abc-kb-curated-multimodal-v2',
           'abc-kb-curated-openrouter-v1')


def owned_key(key, ids):
    parts = validate_key(key).split('/')
    return ((len(parts) >= 4 and parts[0] in {'staging', 'raw'}
             and parts[1] == FOLDER and parts[2] in ids)
            or (len(parts) >= 3 and parts[0] in {'assets', 'curated', 'raw-parsed'}
                and parts[1] in ids))


def inventory():
    store = ObjectStore()
    if store.backend != 's3' or store.cfg.s3_endpoint_url.rstrip('/') != 'http://127.0.0.1:3900':
        raise RuntimeError('This reset is limited to the existing local Garage service')
    with db_session() as db:
        docs = list(db.scalars(select(Document).where(Document.folder_id == FOLDER)))
        if db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.status == 'processing')):
            raise RuntimeError('Stop/recover active processing before reset')
        ids = sorted(d.id for d in docs)
        sources = []
        for doc in docs:
            path = ROOT / 'KM_Data' / 'Pdf' / doc.title
            if not path.is_file() or file_digest(path) != doc.sha256:
                raise RuntimeError('Original PDF missing or changed; reset refused')
            sources.append({'id': doc.id, 'title': doc.title, 'sha256': doc.sha256})
    objects = []
    for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.cfg.s3_bucket):
        for item in page.get('Contents', []):
            if owned_key(item['Key'], set(ids)):
                objects.append({'key': item['Key'], 'size': item['Size'], 'etag': item['ETag']})
    query = {'bool': {'filter': [{'term': {'folder_id': FOLDER}}, {'terms': {'document_id': ids}}]}}
    search = client()
    indexes = {}
    for index in INDEXES:
        if search.indices.exists(index=index):
            folder_count = search.count(index=index, body={'query': {'term': {'folder_id': FOLDER}}})['count']
            count = search.count(index=index, body={'query': query})['count']
            if count != folder_count:
                raise RuntimeError('Untracked pilot chunks require investigation before reset')
            indexes[index] = count
    return {'folder': FOLDER, 'documents': sorted(sources, key=lambda x: x['id']),
            'objects': sorted(objects, key=lambda x: x['key']), 'indexes': indexes,
            'bucket': store.cfg.s3_bucket}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = directory / 'reset-plan.json'
    plan = inventory()
    if not args.apply:
        if plan_path.exists():
            raise RuntimeError('Plan already exists; use a new directory')
        plan_path.write_text(json.dumps(plan, indent=2))
        print(json.dumps({'documents': len(plan['documents']), 'objects': len(plan['objects']),
                          'indexes': plan['indexes'], 'plan': str(plan_path)}))
        return
    if plan != json.loads(plan_path.read_text()):
        raise RuntimeError('State changed since plan; prepare a fresh plan')
    with socket.socket() as sock:
        if sock.connect_ex((settings().host, settings().port)) == 0:
            raise RuntimeError('Stop the app before applying the reset')
    if (directory / 'database.sqlite').exists():
        raise RuntimeError('Backup already exists; inspect prior run before resuming')
    cfg = settings()
    if not cfg.database_url.startswith('sqlite:///'):
        raise RuntimeError('This local reset requires SQLite')
    with sqlite3.connect(cfg.database_url.removeprefix('sqlite:///')) as source:
        with sqlite3.connect(directory / 'database.sqlite') as target:
            source.backup(target)
    store, search = ObjectStore(), client()
    backups = []
    for obj in plan['objects']:
        target = directory / 'objects' / obj['key']
        target.parent.mkdir(parents=True, exist_ok=True)
        store.fetch(obj['key'], target)
        if target.stat().st_size != obj['size']:
            raise RuntimeError('Backup size mismatch')
        backups.append({**obj, 'sha256': file_digest(target)})
    ids = [d['id'] for d in plan['documents']]
    query = {'bool': {'filter': [{'term': {'folder_id': FOLDER}}, {'terms': {'document_id': ids}}]}}
    for index, expected in plan['indexes'].items():
        if expected > 10000:
            raise RuntimeError('Reset exceeds bounded backup size')
        rows = search.search(index=index, body={'query': query, 'size': max(expected, 1)})['hits']['hits']
        with (directory / (index + '.jsonl')).open('w') as output:
            count = 0
            for row in rows:
                output.write(json.dumps(row) + '\n')
                count += 1
        if count != expected:
            raise RuntimeError('Index changed while backing up')
    (directory / 'object-backup-manifest.json').write_text(json.dumps(backups, indent=2))
    if inventory() != plan:
        raise RuntimeError('State changed during backup; reset refused')
    # No mutations above this point beyond backup files.
    for index, count in plan['indexes'].items():
        if count:
            rows = [json.loads(line) for line in (directory / (index + '.jsonl')).read_text().splitlines()]
            deleted, errors = bulk(search, [{'_op_type': 'delete', '_index': index, '_id': row['_id']}
                                           for row in rows], refresh=True)
            if errors or deleted != count:
                raise RuntimeError('Incomplete index cleanup; inspect backup before continuing')
    for obj in plan['objects']:
        store.client.delete_object(Bucket=plan['bucket'], Key=obj['key'])
    remaining = inventory()
    if remaining['objects'] or any(remaining['indexes'].values()):
        raise RuntimeError('Cleanup verification failed; SQL records retained')
    with db_session() as db:
        request_ids = select(ProcessingRequest.id).where(ProcessingRequest.document_id.in_(ids))
        db.execute(delete(ControlAttestation).where(ControlAttestation.request_id.in_(request_ids)))
        db.execute(delete(ProcessingProgress).where(ProcessingProgress.request_id.in_(request_ids)))
        db.execute(delete(IndexedGeneration).where(IndexedGeneration.document_id.in_(ids)))
        db.execute(delete(Chunk).where(Chunk.document_id.in_(ids)))
        db.execute(delete(ProcessingRequest).where(ProcessingRequest.document_id.in_(ids)))
        db.execute(delete(Document).where(Document.id.in_(ids), Document.folder_id == FOLDER))
        audit(db, 'operator', 'folder.document_reset', FOLDER,
              {'document_ids': ids, 'backup': str(directory),
               'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest()})
    receipt = {'reset': FOLDER, 'documents_removed': len(ids),
               'objects_removed': len(plan['objects']), 'indexes': plan['indexes']}
    (directory / 'reset-receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
