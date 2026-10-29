import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from kb.config import settings
from kb.db import Employee, Folder, Grant, LoginSession

ROLES = {'owner', 'delegate', 'maker', 'checker', 'reader'}
PII_TECHNICAL_FIELDS = {'id', 'document_id', 'folder_id', 'employee_id', 'source_uri', 'raw_parsed_uri',
    'raw_index_reference', 'source_sha256', 'parser', 'curation_method', 'tool', 'mode', 'escalation',
    'chunk_id', 'status', 'masking', 'detector', 'roles', 'created_at', 'latest_date'}


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_matches(password: str, stored: str) -> bool:
    salt, expected = stored.split(':', 1)
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, actual)


def issue_session(db, employee_id: str) -> str:
    token = secrets.token_urlsafe(48)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), employee_id=employee_id,
                        expires_at=(datetime.now(timezone.utc) + timedelta(hours=settings().session_ttl_hours)).isoformat()))
    return token


def authenticate(db, token: str | None) -> Employee:
    if not token or len(token) > 200:
        raise HTTPException(401, 'Please sign in.')
    session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
    if not session or session.expires_at <= datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, 'Session expired. Please sign in.')
    employee = db.get(Employee, session.employee_id)
    if not employee or not employee.active:
        raise HTTPException(401, 'Account unavailable.')
    return employee


def folder_roles(db, employee_id: str, folder_id: str) -> set[str]:
    # Owner/delegate privileges inherit; ordinary grants are exact-folder only.
    folder = db.get(Folder, folder_id)
    if not folder:
        return set()
    roles = set(db.scalars(select(Grant.role).where(Grant.employee_id == employee_id, Grant.folder_id == folder_id)))
    visited = {folder_id}
    while folder.parent_id and folder.parent_id not in visited:
        folder = db.get(Folder, folder.parent_id)
        if not folder:
            break
        visited.add(folder.id)
        roles |= set(db.scalars(select(Grant.role).where(Grant.employee_id == employee_id, Grant.folder_id == folder.id, Grant.role.in_(['owner', 'delegate']))))
    return roles


def permitted_folders(db, employee_id: str) -> list[str]:
    return [folder.id for folder in db.scalars(select(Folder)) if folder_roles(db, employee_id, folder.id)]


def require_role(db, employee_id: str, folder_id: str, roles: set[str]):
    if not folder_roles(db, employee_id, folder_id).intersection(roles):
        raise HTTPException(403, 'Folder permission denied.')


class PIIGateway:
    """Layered masking. Local rule mode is a demo, never a compliance guarantee."""
    def __init__(self):
        self._nlp = None
        try:
            import spacy
            self._nlp = spacy.load('en_core_web_sm')
        except (ImportError, OSError):
            pass

    @property
    def mode(self):
        return 'rules+local-ner' if self._nlp else 'rules-only-demo'

    def mask(self, text: str, known_names=()) -> str:
        if settings().app_mode == 'infrastructure' and not self._nlp and not settings().guardrails_enabled:
            raise HTTPException(503, 'PII protection requires provisioned local NER or inference guardrails.')
        references = {}
        def reserve(match):
            key = 'CITATIONTOKEN' + chr(65 + len(references)) + 'END'
            references[key] = match.group(0)
            return key
        text = re.sub(r'\[[0-9a-f]{32}:(?:[0-9a-f]{32}:)?\d{6}\]', reserve, text)
        for name in sorted(known_names, key=len, reverse=True):
            if name:
                text = re.sub(re.escape(name), '[NAME]', text, flags=re.I)
        patterns = [
            ('EMAIL', r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),
            ('IP', r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
            ('IP', r'(?i)(?<!\w)(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?!\w)'),
            ('CARD', r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)'),
            ('PHONE', r'(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?){2,4}\d{3,4}(?!\w)'),
            ('ID', r'(?i)\b[STFGM]\d{7}[A-Z]\b'),
            ('ID', r'(?i)\b(?:passport|national\s+id|identity\s+(?:number|no)|id\s+number)\s*[:#-]?\s*[A-Z0-9-]{5,20}\b'),
            ('ADDRESS', r'(?i)\b(?:home|residential|postal|mailing)\s+address\s*[:=-]\s*[^\n;]{4,200}'),
            ('ADDRESS', r'(?i)\b\d{1,5}\s+(?:[A-Z][\w-]*\s+){1,5}(?:street|road|avenue|drive|lane|boulevard|crescent)\b(?:[^\n;]{0,60})'),
            ('NAME', r'(?i)\b(?:customer\s+name|full\s+name|employee\s+name|name)\s*[:=]\s*[^\n;,]{2,80}'),
        ]
        for label, pattern in patterns:
            text = re.sub(pattern, f'[{label}]', text)
        if self._nlp:
            doc = self._nlp(text)
            for entity in reversed(doc.ents):
                if entity.label_ == 'PERSON':
                    text = text[:entity.start_char] + '[NAME]' + text[entity.end_char:]
        else:
            # Offline demo heuristic, deliberately limited to likely personal given names;
            # generic Title Case would corrupt policy terms such as "Risk Owner".
            given = 'Alice|Bob|Carol|David|Emma|Frank|Grace|Henry|Isabel|Jack|John|Jane|James|Jennifer|Michael|Mary|Patricia|Robert|Sarah|Thomas|William|Alex|Blair|Casey|Drew|Ellis|Finley|Gray|Harper|Indigo|Jordan'
            text = re.sub(r'\b(?:' + given + r')(?:[ -][A-Z][a-z0-9]{1,25}){1,3}\b', '[NAME]', text)
            text = re.sub(r'\b(?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b', '[NAME]', text)
        for marker, reference in references.items():
            text = text.replace(marker, reference)
        return text

    def protect(self, value, unmasked: bool = False):
        if unmasked:
            return value
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.protect(item) for item in value]
        if isinstance(value, dict):
            return {key: item if key in PII_TECHNICAL_FIELDS else self.protect(item) for key, item in value.items()}
        return value


pii = PIIGateway()
