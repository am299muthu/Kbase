# Demonstration guide (planned, not recorded)

Plan a 7–10 minute screen-share with four tasks. This guide does not claim a deployment or recording exists. Use synthetic fixtures only; never show source credentials, `.env`, session cookies, real employee information or unreviewed corpus files. Do not display government IDs in a publicly shared recording; agree on a private verification channel with the assessor if required.

## Before recording

- Follow README setup and run `init --demo-documents`, then `serve`. Check `/health`, document statuses, main retail DOCX and separate escalation Markdown.
- Record whether this is local extractive/lexical mode or explicitly configured OpenSearch/OpenRouter mode. Do not call the default mode an LLM or semantic retrieval demonstration.
- The app is not deployed merely because localhost works. If a deployed demonstration is required, finish and verify deployment first and update `deployed.md` truthfully.
- Begin as E003 (retail maker), using your local `DEMO_PASSWORD`. Do not reveal passwords on screen. Keep the trace panel available and explain tool **names, arguments, returned results, citations and final outcome**, not hidden reasoning.

## Task 1 — Entitled policy answer (Q001, about 90 seconds)

As E003 ask: **What must happen before account activation?**

Expected operational sequence:

1. `lookupemployeeaccesscontrol {}` returns E003's retail-root grant and PII flag.
2. `listbusinessfolders {}` returns retail business metadata only.
3. `searchdocuments {"query":"What must happen before account activation?","top_k":5}` returns retail evidence.
4. One or more `getdocsection {"chunk_id":"<returned ID>"}` calls recheck actual source sections.
5. `checkpiicompliance {"text":"<draft answer>"}` filters outgoing text.

Show the identity-verification-before-activation evidence and matching citation/snippet. If local mode shows longer source excerpts, say so explicitly; do not imply fluent LLM synthesis occurred. Inspect answer/snippets/trace for masking of the synthetic contact. Discuss that masking is approximate, not certified.

## Task 2 — Distinct-document, cross-format answer (Q025, about 2 minutes)

As E003 ask: **Using the retail policy DOCX and escalation supplement Markdown, explain high-risk escalation timing, evidence packet contents and record retention.**

The same MCP policy workflow should resolve multiple source sections. Require evidence for: Compliance escalation within **1 business day**, verification outcome, high-risk classification reason, retained-record reference, and **7-year** retention. Show an actual DOCX main-policy citation and a Markdown supplement citation. The main-policy Markdown/DOCX pair repeats the same text; that pair alone does not prove distinct-document synthesis.

Retrieval does not enforce format/source diversity. If either source is missing, disclose the failed case; do not stage a successful outcome or claim the model used unseen evidence. A k=3 versus k=5 comparison can be evaluated separately with conditions recorded, not silently changed mid-task.

## Task 3 — Structured access workflow (Q026, about 1 minute)

Ask: **What are my permissions and which folders can I access?**

Show `lookupemployeeaccesscontrol → listbusinessfolders → checkpiicompliance`. Explain that current structured grant records, not just policy text, determine the answer. E003 initially sees only `retail-banking`; PII visibility is a separate actual seeded flag. The agent does not accept an employee ID argument to impersonate someone else.

## Task 4 — Audited access-review request (Q031, about 1 minute)

Ask: **Request access to technology**.

Show `lookupemployeeaccesscontrol {}` then `requestaccess {"folder_id":"technology","reason":"Employee explicitly requested access review via chat."}`. Result should be `pending_owner_review` with `access_changed: false`. Explain the final response: a review request was recorded; **no access was granted**. Reopen own-permissions view or query to confirm no technology grant. This is an audit event, not a complete ticket/owner-inbox implementation.

Optionally ask Q048 about technology incident requirements as E003 and inspect all trace/citation folders. The invariant is no technology evidence, not a promise of a universal refusal; irrelevant retail excerpts should be acknowledged as a relevance failure.

## Brief maker-checker walkthrough and wrap-up

With time remaining, upload a synthetic file as E003, show pending status, then use E002 to approve. A maker cannot self-approve, including an owner. Show indexed status after worker processing and the audit event; do not imply a staged pre-approved document just completed the workflow live. E001/E004 can demonstrate owner/delegate access management separately; do not mutate the evaluation baseline without resetting it.

Close with the architecture's local/opt-in/production boundaries, evaluation set and manual rubric, actual measured results **only if collected**, and honest outstanding gaps (no full corpus load, incomplete clarification/out-of-scope controls, no HA/security certification). CI/CD and external deployment are requirements to demonstrate if completed later, not facts to invent in the presentation.
