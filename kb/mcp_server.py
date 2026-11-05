"""Private stdio MCP server. Identity is bound by the host, never by model arguments."""
import asyncio
import os

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from kb.db import Folder, audit, db_session, init_db
from kb.search import get_section, search_documents
from kb.security import authenticate, folder_roles, permitted_folders, pii

mcp = FastMCP('ABC Policy Gateway')


def current(db):
    return authenticate(db, os.environ.get('KB_MCP_SESSION'))


@mcp.tool()
async def searchdocuments(query: str, top_k: int = 5) -> dict:
    """Retrieve cited evidence ONLY from the signed-in employee's current entitled folders."""
    return await asyncio.to_thread(_search_for_employee, query, top_k)


def _search_for_employee(query, top_k):
    if not query.strip() or len(query) > 4000:
        raise ValueError('Query must contain 1–4000 characters.')
    with db_session() as db:
        user = current(db)
        results = search_documents(db, user.id, query, top_k)
        safe = pii.protect(results, user.pii_unmasked)
        audit(db, user.id, 'mcp.searchdocuments', user.id, {'result_ids': [item['id'] for item in results]})
        return {'evidence': safe, 'masking': 'unmasked-entitled' if user.pii_unmasked else pii.mode}


@mcp.tool()
def getdocsection(chunk_id: str) -> dict:
    """Fetch one evidence section after checking the current session and folder access again."""
    with db_session() as db:
        user = current(db)
        section = get_section(db, user.id, chunk_id)
        return {'section': pii.protect(section, user.pii_unmasked)}


@mcp.tool()
def lookupemployeeaccesscontrol() -> dict:
    """Read the signed-in employee's mock access control and PII entitlement. No impersonation."""
    with db_session() as db:
        user = current(db)
        folders = permitted_folders(db, user.id)
        return {'employee_id': user.id, 'business_unit': user.business_unit, 'pii_unmasked': user.pii_unmasked,
                'folders': [{'id': folder_id, 'roles': sorted(folder_roles(db, user.id, folder_id))} for folder_id in folders]}


@mcp.tool()
def listbusinessfolders() -> dict:
    """List business metadata only for folders the employee is entitled to see."""
    with db_session() as db:
        user = current(db)
        allowed = permitted_folders(db, user.id)
        return {'folders': [{'id': item.id, 'path': item.path, 'business_unit': item.business_unit, 'department': item.department}
                            for item in db.scalars(select(Folder).where(Folder.id.in_(allowed)))]}


@mcp.tool()
def checkpiicompliance(text: str) -> dict:
    """Enforce employee PII masking on outgoing text. Does not grant access or certify compliance."""
    if len(text) > 50000:
        raise ValueError('Text is too long.')
    with db_session() as db:
        user = current(db)
        safe = pii.protect(text, user.pii_unmasked)
        return {'text': safe, 'masked': not user.pii_unmasked, 'detector': pii.mode}


@mcp.tool()
def requestaccess(folder_id: str, reason: str) -> dict:
    """Record an explicitly requested access-review request; NEVER grants access or modifies roles."""
    if not 5 <= len(reason) <= 300:
        raise ValueError('Reason must contain 5–300 characters.')
    with db_session() as db:
        user = current(db)
        if not db.get(Folder, folder_id):
            return {'status': 'not_found', 'message': 'Contact the Data Office for folder access.'}
        audit(db, user.id, 'access.review_requested', folder_id, {'requester': user.id, 'reason': pii.mask(reason)})
        return {'status': 'pending_owner_review', 'folder_id': folder_id, 'access_changed': False}


if __name__ == '__main__':
    from kb.telemetry import configure, flush, operation
    from opentelemetry.trace import SpanKind
    configure()
    init_db()
    try:
        with operation('mcp.session', kind=SpanKind.SERVER):
            mcp.run(transport='stdio')
    finally:
        flush()
