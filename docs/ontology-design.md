# KBase ontology and graph layer: draft design

Status: draft 0.1 for discussion, not built. The schema lives in
[`ontology/kbase-ontology.yaml`](../ontology/kbase-ontology.yaml).

## Goal: knowledge as a shared service

KBase is the bank's store for governed unstructured knowledge: policies,
procedures, product documents and guidance. The end goal is a shared service
that any approved assistant, agent or app can call, not a feature inside one
app. Structured data products stay on the data platform; KBase is the
unstructured counterpart, and the ontology gives both the same names and keys
so an answer can join a policy to the data it governs.

What that means in practice:

| Piece | Shared service version |
|---|---|
| Ingestion and approval | One pipeline and one maker-checker flow for every department |
| Knowledge products | Each department publishes products into one catalog |
| Interfaces | One search API, MCP agent tools and graph tools, the same for every consumer |
| Consumers | Each assistant or app registers its use case and the products it reads before getting access |
| Access | Checked on every call against the end user's folders, never the calling app's |
| Models | Behind one internal gateway, so models can change without changing consumers |
| Measurement | Usage and answer quality tracked per product and per consumer |

This is the pattern DBS describes publicly: one governed data platform, a
central GenAI service with pre-approved patterns, a searchable reuse
repository, and every AI use case reviewed before it goes live.

## Why

Search finds passages. It cannot answer questions that need links between
documents, such as "which policy version governs this product today" or
"what changed between version 3 and 4". A small graph next to search adds
those links, and each link points back to the approved section it came from.

This follows what large organisations do (see "What we borrowed" below):
standard bank terms for names, the model kept as reviewed code, the data in
ordinary tables, and permissions checked on every object.

## The model in one picture

```
KnowledgeProduct --contains--> Document --has_section--> Section
                                   |                        |
                               supersedes            defines / states
                                   v                        v
                               Document                Term    Rule --applies_to--> Product
                                                                                      |
                            Policy --evidenced_by--> Document         Product --governed_by--> Policy
Every node --in_domain--> Domain
```

Eight node types: KnowledgeProduct, Document, Section, Product, Policy, Rule,
Term, Domain. Ten link types, listed in the `Predicate` enum.

**Document is not copied.** The existing `documents` table stays the system of
record. Graph Document nodes reference it by `document_id` and `version`.

## Rules every link follows

1. **Evidence is required.** Each link has at least one evidence row: document
   id, version, section, page, how it was found, confidence, and who approved it.
2. **Permissions come from evidence.** A user sees a link only if they can read
   the folder of at least one approved evidence row. This reuses
   `permitted_folders`, the same check search uses. A node with no visible
   links is hidden.
3. **Only approved links feed answers.** Status goes proposed, then approved or
   rejected, and later retired.
4. **Links retire with their source.** When a document is superseded, links
   whose only evidence is the old version become retired. Links also evidenced
   by the new version stay.

## How links get made

Extraction runs only after a document is approved by a checker, in this order:

| Pass | Method | Example | Review |
|---|---|---|---|
| 1 | `metadata` | owner, domain, version, supersedes, effective dates | none, it is already approved metadata |
| 2 | `rule` | document to section, section defines term ("X means ...") | none for structure, spot checks |
| 3 | `llm` | section states rule, rule applies to product | local model only, limited by JSON Schema generated from the ontology, then a judge pass marks keep or discard with confidence |
| 4 | `human` | links the maker adds by hand | checker approves |

LLM links below a confidence threshold go to the checker queue as `proposed`.
We extract only what the competency questions need, not every possible fact,
which keeps cost close to plain search indexing.

## Building the graph with graphify (in the app, demo)

Built into KBase on 2026-11-11: the **Knowledge graph** page, `kb/graph_build.py`,
`kb/ontology.py`, tables `graph_builds`, `graph_nodes`, `graph_links`,
`graph_evidence`, and the `/api/graph/...` endpoints. No business-domain rules
yet: the ontology (0.2.0) has generic types (Organisation, Person, Product,
Policy, Rule, Process, Metric, Event, Location, Term) and 17 predicates.

How a build runs (one folder, chosen indexed documents):
1. The documents are staged from their indexed chunks, so the graph sees what
   search sees. One folder per build, so every node and link has that folder's access.
2. graphify extracts entities and links. Demo backend: the local Claude CLI
   (`KBASE_GRAPH_BACKEND=claude-cli`, model `sonnet`), so document text goes to
   Claude. Production: `KBASE_GRAPH_BACKEND=openai` with the internal model server.
3. One constrained call aligns entities to ontology types and links to
   predicates (a JSON schema allows only ontology values; each link is shown with
   its evidence sentence). Entities and links seen before keep their earlier type
   and predicate, so only new items go to the model.
4. Each link gets evidence: the indexed chunk, page and sentence where both entities appear.
5. Links start as **proposed**. Folder owners and checkers approve or reject them
   on the Review queue (one by one, or all stated links scoring 0.9 or more) and
   can correct an entity's type. Every decision is in the activity log.
6. Only **approved** links reach answers: the `graphfacts` MCP tool adds them
   under "Approved links in the knowledge graph", each cited by its passage, and
   the answer's access re-check covers those passages.

Rebuilds: graphify's per-file cache is kept per folder, so an unchanged document
is not read again (measured: 124 s first build, 18 s rebuild, same 24 entities and
29 links). A decision on the same source, predicate and target is kept; if a
rebuild changes an approved link's predicate it goes back to review with a note,
and a rejection of the pair stands. Time limits are on the Processing settings page.

Measured on four synthetic documents: 24 entities, 29 links; after giving the
alignment the evidence sentence, `related_to` fell from 15 of 25 links to 6 of 24.

Still open: graphify output varies between runs when the cache is cleared; the
annual report (2,469 chunks) is unticked by default because a build would take
30 minutes or more and many model calls; the ontology needs department input
before predicates mean business rules.

### Earlier offline scripts (superseded by the app)

```
python scripts/graph/build_graph.py     # one graphify run per access folder
python scripts/graph/import_graph.py    # map to KBase types, add folder and evidence
python scripts/graph/import_graph.py --as E005   # preview what one employee can see
```

- graphify is installed in its own environment, `.venv-graphify`
  (`pip install "graphifyy[pdf,office]"`), so it does not touch KBase's dependencies.
- **Per folder runs** give permissions: each folder's approved documents get
  their own graph, and every node and link carries that `folder_id`.
- **Backend** comes from `KBASE_GRAPH_BACKEND`. Demo default is `claude-cli`
  (synthetic documents only). Real product: `openai` with `OPENAI_BASE_URL`
  and `OPENAI_MODEL` pointing at the internal model server.
- **Our types are mapped after extraction.** graphify cannot be given custom
  node or link types, so `import_graph.py` maps them with simple rules and marks
  them `heuristic`. Loose links become `related_to`.
- **Status:** links graphify marks EXTRACTED with confidence at or above 0.8
  start as approved; the rest are proposed for a checker. The threshold is a
  flag and still a business decision.
- Output is `output/graph/kbase-graph.json`. Loading it into database tables
  comes later.

Known limits from the first demo run:
- Each run sent about 113k input tokens for a 50 word document, mostly
  graphify's own instructions. An internal model needs a large context window,
  or a lower `--token-budget`.
- The type rules are rough: 22 of 39 links are `related_to`, and some nodes are
  mistyped (for example a retention period typed as Term). A second
  classification pass or checker review is needed before relying on types.

## Storage

Three tables in the existing database (SQLite locally, Postgres when deployed):

- `kg_nodes` (id, type, label, domain, status, attributes JSON)
- `kg_links` (id, subject_id, predicate, object_id, status)
- `kg_evidence` (link_id, document_id, version, section_label, page,
  extracted_by, confidence, status, reviewed_by)

Two or three hop questions run as plain SQL. A graph database is only worth it
if questions routinely need longer paths.

## How agents use it

Two tools added to the existing MCP agent, both filtered by the caller's folders:

- `related(entity, predicate?)`: neighbours of a node, with evidence citations.
- `governing_policy(product, on_date?)`: the policy version in force on a date.

The agent combines these with the current OpenSearch results and cites the
evidence sections exactly as it cites search passages today.

## Competency questions (the acceptance tests)

| ID | Question | Graph path | Priority |
|---|---|---|---|
| CQ-01 | Which policy version governs product P today? | Product governed_by Policy, evidenced_by Document where effective_from <= today and not superseded | high |
| CQ-02 | Which eligibility rules apply to product P? | Product <- applies_to Rule where rule_kind = eligibility, with the Section that states it | high |
| CQ-03 | What changed between version 3 and version 4 of policy Q? | Document v4 supersedes v3; compare Rules stated by sections of each | medium |
| CQ-04 | Which documents define term T, and do the definitions agree? | Term <- defines Section <- has_section Document | medium |

Each question becomes a saved query with an expected answer. They rerun after
every content or ontology change, and a knowledge product cannot publish while
a high priority question fails.

## Tools

- **LinkML** for the schema file, so one YAML generates the tables, Pydantic
  models, the extraction JSON Schema and SHACL checks.
- **SKOS** terms for the glossary (synonyms, broader term).
- **FIBO** names where they fit (Product, Policy). We borrow names, we do not
  import FIBO.
- **pySHACL** to check extracted data against the rules.
- **Local models hosted internally** (for example Qwen) for extraction,
  judging, routing help and answer synthesis. No outside model services in
  production; the outside API is only for the demo on synthetic documents.

Note: no large bank we found publicly uses LinkML. Banks that publish use FIBO
with OWL, or Goldman's Legend. LinkML is chosen because it suits a small team
and outputs OWL if we need it later.

## What we borrowed

| From | Idea |
|---|---|
| Banks using FIBO | Borrow standard names, store in relational tables |
| Goldman Legend, FINOS | Model as code, reviewed before release |
| Palantir | Permissions on every object; agents act through tools, not the database |
| LinkedIn support graph | Rules plus LLM extraction, keep document structure |
| Microsoft GraphRAG | Full LLM graph building is costly; extract selectively |
| Bloomberg | Entity resolution is its own job |
| DBS | One platform and a central GenAI service that many assistants call |
| DBS | Platform team builds, separate governance owners decide meaning |
| DBS | Searchable repository of past work so teams reuse instead of rebuild |
| DBS | Vector search and a knowledge graph side by side behind the CSO Assistant |
| DBS | Every AI use case reviewed against principles before going live |
| DBS | Value measured from the start |

## Layered view and five additions

A reference design we reviewed organises the same platform in layers. It
matches our design closely, so we use its layer names and adopt five ideas
from it.

| Layer | What sits there in KBase |
|---|---|
| Sources | Documents; structured data from the data platform |
| Process: from file to evidence | Intake (maker-checker), parse, index, graph extraction |
| Govern: what it knows | Ontology and graph (the "domain pack"), suggested links waiting for approval, evaluation with competency questions |
| Retrieve: answers | Router, document search, live data access, answer gate |
| Access: who asks | Agents over MCP, analysts in chat, apps through the API, reviewer workbench |
| Foundation | Local models hosted internally (for example Qwen), knowledge store, file store, query engine |
| Across all layers | Identity, one policy engine, secrets, trace and audit, pipelines that run on upload |

Its guiding rule matches ours: **rules decide first; a model is used only when
the rules cannot.**

### The five additions

1. **Router ("judge"), driven by access control.** A step before retrieval
   that decides where a request may look: document search, the graph, live
   structured data, or a mix. The deciding input is what the caller is allowed
   to reach, checked by the policy engine. The caller can be a **user** or an
   **agent**:
   - A user is routed only to the sources and folders their grants cover.
   - An agent has its own identity and registered scope (its use case and the
     knowledge products it may read).
   - When an agent acts for a user, it gets the overlap of both: only what the
     agent is registered for **and** the user may see.
   Within the allowed sources, rules pick the best one first; the local model
   helps only when rules cannot decide. Today the agent plans questions but
   always searches documents.
2. **Live data access.** Answer questions that need current figures by querying
   the data platform's structured data at the source, through a governed query
   layer, instead of only linking to it by shared keys. Read only, with the
   same permission check as documents.
3. **Answer gate with number checks.** Before an answer is returned, check that
   every claim has a citation (KBase already does this) and also that every
   number in the answer matches the cited passage or data result exactly.
   Answers that fail are blocked or returned with the failing claim removed.
4. **One policy engine, for users and agents.** Move permission decisions out
   of scattered code into one engine that every layer calls: router, search,
   graph, data access, API and workbench. Both users and agents are principals
   in it. Folder grants become its first set of policies, and each registered
   agent gets a policy for its scope. One place to change and audit who, or
   which agent, can see what.
5. **Reviewer workbench.** A separate entry point for checkers: approve
   documents, approve suggested graph links, review evaluation failures and
   answer gate blocks. Today review is spread across the main app.

### Where they fit in the build order

- Answer gate number checks and the policy engine come early, alongside the
  graph tools, because they make every later answer safer.
- The router comes with the graph tools, since it decides when to use them.
- Live data access needs agreement with the data platform owners and comes
  after the pilot department is live.
- The reviewer workbench comes with the LLM extraction pass, when the volume of
  suggestions to approve grows.

## Working with departments

The business logic comes from the departments that own it, not from us. We
build the structure; they supply and approve the meaning.

| Departments own | We own |
|---|---|
| Competency questions and the decisions they support | The schema mechanics and generated tables |
| Rule and term definitions for their domain | Extraction passes and the judge step |
| Which documents are authoritative | Storage, permissions and evidence tracking |
| Approving ontology changes in their domain | Running the competency questions as tests |

**Intake template.** Each department fills one row per question:

| Field | Example (synthetic) |
|---|---|
| Question | Which policy version governs product P today? |
| Decision it supports | Which terms an RM quotes to a customer |
| Source documents | Product policy, latest approved version |
| Expected answer for one real case | Policy Q, version 4, effective 1 March |
| Owner | Department product owner |
| In scope now or later | Now |

**Review.** Department experts act as checkers for links in their own domain,
using the existing maker-checker flow. A change to their part of the ontology
needs their owner's approval.

**When departments disagree.** If two departments define the same term
differently, keep both as separate Terms scoped to each domain and link them,
rather than forcing one definition. Raise it with both owners.

**Start with one department.** Run the intake with a single pilot department,
build its competency questions to passing, then add the next.

## Build order

1. Tables plus metadata and rule passes, no LLM. Answers CQ-01 and CQ-04.
2. Two agent tools and permission filtering, with tests.
3. LLM pass with judge and checker queue. Answers CQ-02 and CQ-03.
4. Knowledge product pages in the UI, with the competency question results.
5. Consumer registration and a versioned API, so other assistants and apps
   can call KBase as a shared service with their use case on record.

## Open questions for the business

- Which department pilots first? Bancassurance matches the vendor decks.
- Who in each department owns the knowledge product and signs off ontology
  changes, and who acts as checker?
- What confidence threshold sends an LLM link to the checker?
- Should a node be hidden when its only evidence is a restricted document, or
  shown with the label masked?
- Which structured data may the router query live, and who owns that access?
- How are agents registered and who approves an agent's scope?
