# OCBC Enterprise Knowledge Base Build

## **AI Engineering Techniques and Architectures Project**

### **Project Overview**

For this project, use /Users/MutharasanAnbarasan/PROJECTS/Kbase/Initial_Project_Requirements.md file to build UI and Knowledge base (Apache OpenSearch) and build, and evaluate an agentic AI system that helps users (employee/staff) complete answering their questions from knowledge base indexed data. Knowledge data exist in Doc, Docx, Pdf folders under ~/PROJECTS/Knowledge_Base/KM_Data/. Rlevant parsers must be used to parse the content and use embedding model to ingest into LocalStack folder (S3).  The system to include a Retrieval-Augmented Generation (RAG) capability over a corpus of knowledge base documents indexed for best retreival methodoglogy. It must also include an agentic layer that can plan, select tools, call one or more Model Context Protocol (MCP) servers, use mock structured data such as employee records, employee existing access control data and produce grounded, cited responses only from data which he already have access to. Mock data may be generated as one-time considering current folders.

 To remain free-tier compatible, developer may run the web application, agent orchestrator, RAG index, and MCP server processes within a single deployed service, or they may deploy MCP servers as separate services if their hosting plan permits. The agentic system must also run locally for development.

Finally, for poc demonstratation, video showing the deployed application carrying out two agentic tasks. During the demo, the presenter must explain how the agent is correctly calling MCP-exposed tools, including the relevant tool names, tool-call arguments, returned results, retrieved citations, and final response/ behavior.



### **Learning Outcomes (so implementation should cover)**

- Demonstrate excellent AI engineering skills through the design, implementation, and evaluation of an agentic AI system.  
- Demonstrate the ability to select appropriate AI system design patterns and architectures, including RAG, tool use, agent orchestration, establishment of Apache OpenSearch indexing of raw data and curated data, and MCP-based integration.  
- Implement a working LLM-based agentic system that combines policy RAG with agentic workflows over structured mock data.  
- Build and call MCP servers that expose tools used by the AI agent to complete realistic staff tasks.  Employee may ask any question, and Agentic AI to process the retrieved information from Apache Open Search through RAG with prioper answer. Establish guradrail inplace, so unwanted / harsh/ scarstic questions are handled properly with citations and independent grounding checks.
- Evaluate both answer quality and agent behavior, including groundedness, citation accuracy, tool selection, workflow completion, and latency.  
- Utilize AI tooling as appropriate and document the impact of that tooling on the development process.

### **Project Description**

Parsing data and indexing the data deatisl are given in [requirements.md](http://requirements.md) file.

In addition, create employee dataset and access control data, so code base build will use this data.

For embedding models, local embedding models can be used .

#### **1 Environment and Reproducibility**

- Create a virtual environment, such as venv, conda, or an equivalent setup.  
- List dependencies in requirements.txt, pyproject.toml, package.json, or environment.yml as appropriate.  
- Provide a README.md with setup, local run, and evaluation instructions.  
- Set fixed seeds where applicable, such as deterministic chunking or evaluation sampling.  
- Ensure secrets such as API keys are read from environment variables and are not committed to the repository.

#### **2 Policy Corpus Ingestion and Indexing**

- Parse and clean  documents, handling PDF (ELECTRONIC BASED), DOC, DOCX, 
- Chunk documents using a justified strategy, such as heading-aware chunking or token windows with overlap.  
- Embed chunks using a free embedding model, local model, or free-tier API.  
- Store embedded chunks in a local Apache OpenSearch.  **OpenSearch for curated search, PostgreSQL for employee and control records and a policy-controlled retrieval and model gateway**. 
- Persist enough document metadata to support citations, including document title or ID, section, and source snippet.

#### **3 Retrieval Augmented Generation (RAG)**

- Implement top-k retrieval with optional filtering, query rewriting, or reranking.  
- Build a prompting strategy that injects retrieved chunks and source metadata into the LLM context.  
- Generate answers that cite source document IDs, titles, or sections and include supporting snippets where appropriate.  
- Add guardrails that refuse or redirect out-of-corpus docs questions, limit unsupported claims, and distinguish policy facts from recommendations.  
- Include at least one complex question requiring retrieval from multiple  documents from different file formats.

#### **4 Agentic System Design**

- Build an agent orchestrator that can interpret user intent, decide whether RAG alone is sufficient, select tools, call MCP-exposed tools, and synthesize final responses.  
- Support at least two multi-step  workflows
- Implement a visible or logged trace of agent reasoning steps at the architectural level: selected tools, tool arguments, tool outputs, retrieved docs sources, final answer basis, and any escalation decision. Do not expose hidden chain-of-thought; provide concise operational traces instead.  
- Handle failures gracefully, such as unavailable MCP tools, missing employee IDs, no access for any of the knwoledge base data or ambiguous requests that require clarification.  

#### **5 MCP Server and Tool Integration**

- Implement one or more MCP servers that expose tools for the agent. The MCP server may run as a local process using stdio, a localhost HTTP service, a Streamable HTTP service, or another MCP-compatible approach supported by your system's architecture.  
- Expose at least five MCP tools. At least one tool must use the RAG index or retrieve policy evidence, and at least one tool must use mock structured data or perform a mock operation.  
- Tools should include tools such as the following: searchdocuments, getdocsection, lookupemployeeaccesscontrol and listbusinessfolders.  
- The agent must actually call MCP-exposed tools during execution; hard-coded direct function calls are not sufficient unless they are wrapped and invoked through the MCP layer.  
- Document the MCP architecture, transport choice, tool schemas, and how the agent client discovers and calls tools.

#### **6 Web Application**

- Developer can use Flask, FastAPI, React based JS, CSS/HTML or an alternative framework for the web application.  Apply common theme and follow theme of www.ocbc.com
- The web app should include a chat interface where users can ask any questions / workflow questions.  
- Provide a /chat endpoint, or equivalent, that receives user requests and returns the final answer, citations, snippets, and a concise tool-call trace.  
- Provide a /health endpoint for apache OpenSearch/ application endpoint, or equivalent, that returns a simple JSON status including app status and, where feasible, MCP connectivity status.  

- To remain free-tier compatible, a single-service deployment is acceptable: the web app, agent orchestrator, local vector store, mock JSON data, and MCP server process may all run in one deployed service.  
- Students with sufficient free-tier resources are encouraged to deploy the MCP server as a separate service and call it via HTTP. If deployed separately, configure the MCP server URL with environment variables.  
- The application should not require a paid database. Acceptable storage includes committed employee data files, Postgressql, a small local vector store built during deployment, or a hosted free-tier option.  
- If the free-tier service spins down after inactivity, the README and demo should explain the expected cold-start behavior.


#### **9 Evaluation of the Agentic RAG Application**

- Provide an evaluation set of 50 questions or tasks covering policy QA and ag*entic workflows. Include straightforward policy* questions, multi-document questions, tool-requiring tasks, ambiguous requests, and out-of-scope requests  with correct or gold answers.  
- Report answer quality metrics: groundedness, citation accuracy, and optionally exact or partial match against short gold answers.  
- Report agent behavior metrics: tool selection accuracy, workflow completion rate, escalation or clarification accuracy, and action-safety pass rate.  
- Report system metrics: latency p50/p95 for 10-20 representative queries or tasks. If free-tier cold starts affect latency, report cold-start and warm-start behavior separately where possible.  
- Include at least one ablation or comparison, such as different retrieval k values, chunk sizes, prompt variants, or agent tool availability.

#### **10 Design Documentation**

- Briefly justify design choices, including agent framework or manual orchestration approach, MCP server design, transport choice, tool schemas, embedding model, chunking strategy, retrieval k, vector store, deployment architecture, and safety guardrails.  
- Include an architecture diagram or text-based architecture showing the web app, agent orchestrator, MCP client, MCP server or servers, RAG index, mock structured data, and LLM provider.  
- Describe the two required agentic demo tasks and the expected sequence of MCP tool calls for each task.

### **Example Agentic Tasks**

deployed demo must include four completed agentic tasks. Examples include:

- **Four Questions can be picked from evaluation dataset generated  - source involving direct answer, another where answer coming from mutiple documents using mutiple mutiple tool calls**

### **Recommended Free-Tier Architecture**

The following architecture is recommended for students who want to remain within a typical free-tier deployment model:

Single Render/Railway Web Service  
 ├── Web chat UI and /chat API  
 ├── Agent orchestrator and MCP client  
 ├── Local MCP server process over stdio or localhost HTTP  
 ├── Policy RAG index using Apache OpenSearch  
 ├── Mock employee, PTO, benefits, and ticket data as JSON or SQLite  
 └── LLM and embedding providers configured through environment variables

Developer are encouraged to use a more production-perfect architecture with separate MCP services, but this is not required. The project should be possible to complete without paid hosting, paid databases, or private organizational systems.

### **Submission Guidelines**

Your submission must include both:

1. a link to your recorded demo presentation, and
2. a link to your GitHub repository (either individual's repository or for a group, the group's shared repository)
3. **A link to a recorded screen-share demonstration video of the working, deployed application, involving screen capture with voiceover.**
   - The demo should be between 7 and 10 minutes long.  
   - All group members must speak and be present on camera.  
   - All group members must show their government ID.  
   - The demo must show the system carrying out two agentic tasks end-to-end.  
   - For each agentic task, the presenter must explain how the agent is correctly calling MCP tools, including the tool names, arguments, outputs, retrieved citations, and final answer or action.  
   - The demo should include a quick walkthrough of design, deployment, CI/CD, and evaluation results.
4. **You must share your repository with the GitHub account, quantic-grader. Your GitHub repository must contain the following:**
   - All developed code  
   - README.md with introductory description, setup, local run, deployment instructions.  
   - design-and-evaluation.md explaining architecture, RAG design, MCP server design, agent orchestration, tool schemas, safety guardrails, deployment choices, and evaluation questions, expected answers and evaluation results.  
   - ai-tooling.md describing which AI code tools you used and how, including what worked well and what did not.  
   - deployed.md containing the deployed URL, health endpoint URL if available, and any notes about free-tier cold starts.  
   - evaluation/ or equivalent containing the evaluation questions, expected answers or rubrics, scripts, and reported results.  
   - mockdata/ or equivalent containing  employee access control details, etc
   - mcp/ or equivalent containing the MCP server code and tool definitions.

|                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Addresses **ALL** of the project requirements at an outstanding level, including: • Outstanding deployed agentic AI system for OCBC Knowledge base (using OpenSearch effectively to highest standards) that combines policy RAG with correct, cited, grounded responses. • MCP integration is fully functional: the agent discovers and calls MCP-exposed tools correctly, with clear tool-call traces and graceful error handling. • At least two end-to-end agentic tasks are completed in the deployed demo, each requiring multi-step reasoning, RAG retrieval, and structured/mock-data tool use. • Excellent RAG ingestion, indexing, retrieval, citations, and guardrails. • Excellent architecture, including clear separation of web app, agent orchestration, MCP client/server, RAG index, mock data, and LLM provider. • Free-tier-compatible deployment on Render, Railway, or equivalent is fully functional, with environment variables and cold-start behavior documented. • CI/CD runs on push/PR and includes meaningful build/start checks plus at least one MCP tool discovery or call test. • Excellent evaluation results covering groundedness, citation accuracy, tool selection accuracy, workflow completion, safety, and latency. • Excellent design documentation and excellent demo presentation of features, architecture, , MCP calls, and evaluation, meeting all demo requirements. |

