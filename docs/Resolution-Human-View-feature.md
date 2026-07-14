Add Resolution Layers, Human View, Semantic Annotations, and Flow Verification to Atlas



Goal



Extend Atlas with a resolution-based comprehension layer that allows:



1\. The complete codebase model to remain available to AI agents.

2\. Human users to view the same system at adjustable abstraction levels.

3\. The existing slider controller to control the resolution of the Human View.

4\. Users and AI agents to attach structured annotations to any graph item.

5\. AI agents to inspect the underlying implementation of a selected feature and explain its exact execution flow.

6\. Users to compare intended behaviour against observed or statically inferred behaviour.

7\. Generated human explanations to be cached and invalidated only when their underlying source changes.

8\. All new functionality to integrate with the existing Atlas architecture rather than creating a separate disconnected subsystem.



The result should be a shared semantic model with two presentation modes:



\* AI View: complete, high-resolution representation.

\* Human View: adaptive representation controlled by the existing resolution slider.



The AI View and Human View must reference the same canonical nodes and relationships.



⸻



Operating Instructions



Before modifying code:



1\. Analyse the complete Atlas repository.

2\. Identify the current:

&#x20;   \* graph model

&#x20;   \* node and edge schemas

&#x20;   \* indexing pipeline

&#x20;   \* repository analysis pipeline

&#x20;   \* summarisation pipeline

&#x20;   \* frontend graph components

&#x20;   \* existing slider controller

&#x20;   \* state-management architecture

&#x20;   \* persistence layer

&#x20;   \* model-provider abstraction

&#x20;   \* prompt construction system

&#x20;   \* background-job system

&#x20;   \* caching system

&#x20;   \* file-change detection or Git integration

&#x20;   \* note or annotation functionality

3\. Use Atlas itself, where available, to inspect:

&#x20;   \* architectural relationships

&#x20;   \* feature boundaries

&#x20;   \* component ownership

&#x20;   \* existing data flows

4\. Do not assume names, frameworks, database technologies, or component locations.

5\. Reuse existing abstractions where appropriate.

6\. Do not create duplicate graph stores, duplicate slider state, duplicate repository indexes, or competing note systems.

7\. Preserve existing behaviour unless a migration is explicitly required.

8\. Prefer incremental additions behind feature flags where the change affects core graph behaviour.

9\. Create a written implementation plan before editing code.

10\. Execute the implementation only after mapping each proposed change to the existing architecture.



⸻



Product Strategy



Canonical representation



Atlas must maintain one canonical semantic representation of the codebase.



That representation may contain:



\* source files

\* modules

\* classes

\* functions

\* features

\* components

\* systems

\* dependencies

\* execution relationships

\* data-flow relationships

\* prompts

\* tools

\* model calls

\* user-facing behaviours

\* decisions

\* annotations

\* generated explanations

\* verification evidence



The AI View and Human View must not be separate disconnected graphs.



They are separate projections of the same canonical graph.



AI View



The AI View exposes the maximum available detail.



It should include all indexed nodes and relationships that the active filters permit.



It is intended for:



\* model reasoning

\* expert inspection

\* debugging

\* tracing

\* architecture analysis

\* dependency analysis

\* verification



The AI View must not be simplified merely because the Human View is using a low resolution.



Human View



The Human View presents a simplified projection of the canonical graph.



Its content is controlled by:



\* the existing slider controller

\* the currently selected scope

\* the currently selected feature or node

\* the user’s chosen presentation mode

\* relevance to the current conversation or task



The Human View should reduce cognitive load without changing the underlying truth.



It must preserve traceability back to canonical nodes.



⸻



Resolution Model



Implement a configurable semantic resolution hierarchy.



The exact number of slider positions should fit the existing slider controller, but the conceptual hierarchy must support the following levels.



Resolution 0: System



Show:



\* primary systems

\* major architectural responsibilities

\* external boundaries

\* core data flows

\* major decision points



Hide:



\* individual files

\* low-level implementation details

\* utility functions

\* incidental dependencies



Resolution 1: Component



Show:



\* major components

\* services

\* subsystems

\* packages

\* major relationships between them



Resolution 2: Feature



Show:



\* recognised product or system features

\* feature relationships

\* files and components primarily responsible for each feature

\* feature inputs, outputs, state transitions, and external effects



Resolution 3: Module or File



Show:



\* important files and modules

\* purpose of each file

\* major exported behaviour

\* key dependencies

\* feature membership



Resolution 4: Function



Show:



\* functions

\* methods

\* signatures

\* inputs

\* outputs

\* side effects

\* callers

\* callees

\* feature membership

\* important branches



Resolution 5: Raw Implementation



Show:



\* source-level nodes

\* precise call relationships

\* exact definitions

\* raw code navigation

\* detailed dependency and flow information



If the current slider uses a different numeric range, map its values to these semantic levels rather than replacing the slider.



⸻



Existing Slider Integration



The existing slider controller must remain the single source of truth for Human View resolution.



Implement the following behaviour:



1\. When Human View is disabled:

&#x20;   \* the slider may retain its last value

&#x20;   \* changing the slider must not destructively alter the canonical graph

&#x20;   \* the AI View remains full fidelity

2\. When Human View is enabled:

&#x20;   \* the slider determines which semantic levels are rendered

&#x20;   \* transitions should update the graph immediately when cached data exists

&#x20;   \* missing human explanations should be generated lazily

&#x20;   \* the UI should display a loading state only for unresolved items

&#x20;   \* already cached items should remain interactive

3\. Slider changes must:

&#x20;   \* preserve the current selection where possible

&#x20;   \* preserve graph position or camera focus where possible

&#x20;   \* map selected nodes between abstraction levels

&#x20;   \* avoid resetting the whole workspace

&#x20;   \* avoid rerunning full repository analysis

4\. The slider should affect:

&#x20;   \* visible node types

&#x20;   \* visible edge types

&#x20;   \* node grouping

&#x20;   \* explanation depth

&#x20;   \* labels

&#x20;   \* summaries

&#x20;   \* amount of supporting evidence shown

&#x20;   \* default expansion depth

5\. The slider must not affect:

&#x20;   \* canonical indexing completeness

&#x20;   \* AI reasoning access

&#x20;   \* stored relationships

&#x20;   \* raw source availability

&#x20;   \* underlying analysis quality



⸻



Human View Toggle



Add a clear Human View toggle using the existing application design system.



The toggle must switch between:



AI View



\* complete graph

\* full technical labels

\* all applicable relationships

\* detailed metadata

\* model-oriented representation



Human View



\* resolution-controlled projection

\* simplified labels

\* feature-oriented explanations

\* progressive disclosure

\* reduced edge density

\* human-readable flow descriptions



View mapping



When switching views:



1\. Keep the same semantic selection.

2\. Highlight the equivalent canonical item.

3\. Preserve active annotations.

4\. Preserve conversation context.

5\. Preserve viewport context where technically reasonable.

6\. Display a mapping trail showing:

&#x20;   \* selected human node

&#x20;   \* canonical graph node or nodes

&#x20;   \* underlying files and functions



A selected Human View feature must be traceable into the AI View.



A selected AI View implementation node must be traceable upward into its Human View feature, component, and system.



⸻



Semantic Pyramid



Create or extend a semantic pyramid over the codebase.



Each higher layer must reference the lower-level evidence from which it was derived.



Example:



System

→ Component

→ Feature

→ Module

→ Function

→ Source span



Each node must have stable identity where possible.



Required fields should include equivalents of:



\* canonical ID

\* node type

\* name

\* repository identity

\* source revision

\* source references

\* parent semantic node

\* child semantic nodes

\* related feature IDs

\* related component IDs

\* dependency edges

\* flow edges

\* generated explanation IDs

\* annotation IDs

\* generation provenance

\* content hash

\* stale status

\* last analysed timestamp

\* confidence or evidence quality

\* schema version



Use the existing schema style and persistence technology.



Do not introduce these exact field names if equivalent fields already exist.



⸻



Initial Analysis and Incremental Updates



Initial analysis



During initial repository analysis:



1\. Build or update the complete canonical graph.

2\. Identify existing semantic levels.

3\. Generate required AI-facing summaries.

4\. Construct upward relationships:

&#x20;   \* functions into modules

&#x20;   \* modules into features

&#x20;   \* features into components

&#x20;   \* components into systems

5\. Store evidence references for every generated abstraction.

6\. Avoid generating all possible Human View descriptions unless current architecture makes that cheaper than lazy generation.



Incremental update



When source changes:



1\. Detect changed files or source spans.

2\. Invalidate affected low-level nodes.

3\. Reanalyse only affected implementation nodes.

4\. Identify impacted:

&#x20;   \* functions

&#x20;   \* modules

&#x20;   \* features

&#x20;   \* components

&#x20;   \* systems

5\. Cascade stale status upward.

6\. Preserve unaffected cached explanations.

7\. Regenerate AI-facing summaries required for canonical accuracy.

8\. Regenerate Human View explanations only when:

&#x20;   \* the user opens them

&#x20;   \* the system prefetch policy determines they are likely to be needed

&#x20;   \* an active selected feature depends on them

9\. Retain historical versions for audit where the existing persistence model supports versioning.



⸻



Human Explanation Cache



Human explanations must be generated on demand and cached.



The cache identity must account for:



\* canonical node ID

\* source content hash

\* repository revision

\* resolution level

\* explanation mode

\* preferred language

\* relevant user settings

\* prompt or template version

\* model generation version where required



An explanation remains valid until:



\* its source evidence changes

\* its prompt schema materially changes

\* the user explicitly requests regeneration

\* a linked approved intent changes

\* its semantic parent-child mapping changes



Do not invalidate the entire Human View cache because one file changes.



Use dependency-aware invalidation.



⸻



Presentation Modes



Resolution and presentation style are separate concepts.



Resolution controls how much structural detail is shown.



Presentation mode controls how the selected information is explained.



Support an extensible presentation-mode model.



Initial modes should include equivalents of:



\* Standard

\* Beginner

\* Concise

\* ADHD or Focus

\* Expert



Do not hard-code insulting, diagnostic, or medically definitive assumptions.



The ADHD or Focus mode should be an optional cognitive-load presentation mode, not a diagnosis.



Focus mode behaviour



Use:



\* short explanation blocks

\* one concept at a time

\* prominent current-focus indicator

\* reduced simultaneous edge count

\* clear next action

\* collapsible evidence

\* minimal repeated context

\* persistent breadcrumb trail

\* explicit “why this matters”

\* visible progress through a flow



The existing slider remains responsible for structural resolution.



The presentation mode modifies wording and layout at the selected resolution.



⸻



Feature Model



Features must become first-class semantic objects.



Each feature should support equivalents of:



\* feature name

\* plain-English description

\* current observed behaviour

\* intended behaviour

\* approved behaviour

\* inputs

\* outputs

\* side effects

\* user-visible effects

\* entry points

\* exit points

\* failure modes

\* related functions

\* related files

\* related modules

\* related components

\* related external systems

\* data touched

\* permissions required

\* model prompts involved

\* tools involved

\* tests

\* traces

\* annotations

\* open questions

\* approval state

\* intent-fidelity evidence

\* revision history



Reuse or extend existing feature-detection functionality.



⸻



Feature Discovery



Allow a user to describe a feature in natural language.



Example:



“Users upload a document, the application extracts its content, stores embeddings, and makes the document searchable in later conversations.”



Atlas must then:



1\. Search the canonical code graph.

2\. Search semantic summaries.

3\. Search relevant identifiers and source content.

4\. Trace probable entry points.

5\. Trace probable downstream calls.

6\. identify related UI, API, service, storage, job, model, prompt, and tool nodes.

7\. Propose one or more candidate feature mappings.

8\. Show evidence for each candidate.

9\. Ask the user to:

&#x20;   \* confirm

&#x20;   \* reject

&#x20;   \* merge

&#x20;   \* split

&#x20;   \* rename

&#x20;   \* refine

10\. Store the approved feature mapping.



Do not treat keyword overlap as sufficient evidence.



Feature discovery must combine:



\* semantic similarity

\* dependency relationships

\* call-flow evidence

\* data-flow evidence

\* naming evidence

\* runtime evidence when available

\* existing annotations

\* tests

\* documentation



⸻



Annotation System



Add structured annotations to graph items at every resolution level.



Annotations may be created by:



\* users

\* AI models

\* automated analysers

\* verification processes



Annotations must attach to canonical semantic objects, not merely screen coordinates.



Supported targets should include:



\* system

\* component

\* feature

\* file

\* module

\* class

\* function

\* flow edge

\* dependency edge

\* source range

\* decision

\* trace

\* test

\* explanation



Annotation types



Support extensible types, initially including:



\* user note

\* model observation

\* intended change

\* implementation instruction

\* bug suspicion

\* contradiction

\* security concern

\* performance concern

\* architecture concern

\* question

\* decision

\* approval

\* rejection

\* historical context

\* verification result



Annotation fields



Use equivalents of:



\* annotation ID

\* target canonical ID

\* target resolution

\* author type

\* author identity

\* model or agent identity

\* annotation type

\* body

\* structured payload

\* status

\* priority

\* confidence

\* evidence references

\* related feature IDs

\* related task IDs

\* created timestamp

\* updated timestamp

\* resolved timestamp

\* archived timestamp

\* source revision

\* supersedes annotation ID

\* parent annotation ID

\* tags



Annotation lifecycle



Support:



\* open

\* under review

\* accepted

\* rejected

\* resolved

\* archived

\* superseded



Archived annotations remain auditable but should not be injected into every model context.



⸻



Decision and Approval Flow



Implement a clear progression:



1\. Suggestion

&#x20;   \* Human or model proposes a change.

2\. Analysis

&#x20;   \* Atlas identifies affected semantic objects and code.

3\. Discussion

&#x20;   \* Human and model add annotations.

4\. Proposed intent

&#x20;   \* Atlas generates a structured intended-behaviour statement.

5\. Human approval

&#x20;   \* User approves, edits, or rejects it.

6\. Decision lock

&#x20;   \* Approved intent becomes an immutable versioned reference.

&#x20;   \* It may later be superseded, but not silently rewritten.

7\. Implementation

&#x20;   \* Coding agent receives the approved intent and linked evidence.

8\. Verification

&#x20;   \* Atlas compares resulting implementation with approved intent.

9\. Closure

&#x20;   \* Decision is marked implemented, partially implemented, failed, or superseded.

10\. Archival

&#x20;   \* Resolved discussion can be collapsed from normal context while remaining available for audit.



Do not make “immutable” mean impossible to correct.



Use versioned supersession.



⸻



Exact Flow Review



Add an action for any selected feature:



Review Exact Flow



This must instruct the model to inspect the extended implementation beneath the selected feature.



The review must examine, where applicable:



\* UI event

\* route

\* controller

\* API request

\* validation

\* authentication

\* authorisation

\* service call

\* domain logic

\* state change

\* database access

\* file access

\* queue or job dispatch

\* external API call

\* model invocation

\* prompt construction

\* retrieved context

\* tool call

\* response transformation

\* frontend state update

\* error path

\* retry path

\* logging and telemetry

\* cleanup

\* final output



The generated flow explanation must:



1\. Use the complete canonical graph.

2\. Read relevant code when summaries are insufficient.

3\. Identify each stage in execution order.

4\. Name the responsible files, classes, functions, or source ranges.

5\. Explain input and output at each stage.

6\. Explain transformations and decisions.

7\. Identify async boundaries.

8\. Identify external dependencies.

9\. Identify failure and fallback paths.

10\. Identify uncertain or inferred steps.

11\. Distinguish:

&#x20;   \* statically proven relationships

&#x20;   \* runtime-observed relationships

&#x20;   \* model-inferred relationships

&#x20;   \* human-approved intent

12\. Link every important statement back to evidence.

13\. Produce a Human View explanation at the selected resolution.

14\. Allow expansion into lower-resolution evidence.



The review must not state that the feature is “working perfectly” without evidence.



Instead, it must produce a verification status.



Possible statuses:



\* Verified against available evidence

\* Partially verified

\* Behaviour differs from approved intent

\* Insufficient runtime evidence

\* Static analysis only

\* Verification failed



⸻



Flow Mechanism Representation



Create a structured flow representation, not only prose.



A flow step should support equivalents of:



\* step ID

\* sequence position

\* parallel group

\* source node

\* destination node

\* operation

\* input schema

\* output schema

\* transformation

\* condition

\* branch

\* side effect

\* async boundary

\* retry behaviour

\* error behaviour

\* evidence references

\* runtime trace references

\* linked annotations

\* intended behaviour reference

\* observed behaviour

\* verification state



The UI should be able to render this representation as:



\* ordered flow

\* collapsible step list

\* graph path

\* timeline

\* future visual execution mode



Do not make the first implementation dependent on a complex animated simulation.



Start with a reliable structured representation.



⸻



Intent Fidelity



Implement an evidence-based intent-fidelity assessment for features.



It must compare:



\* approved intended behaviour

\* current static implementation

\* test evidence

\* runtime trace evidence

\* unresolved annotations

\* known contradictions



Do not generate a percentage from model confidence alone.



If a score is displayed, it must be decomposable.



Example dimensions:



\* required behaviour implemented

\* prohibited behaviour absent

\* expected paths represented

\* failure paths represented

\* tests present

\* tests passing

\* runtime observations matching

\* unresolved contradictions

\* stale evidence



The UI must allow a user to inspect why a score was assigned.



Where evidence is inadequate, show “insufficient evidence” rather than an invented score.



⸻



Model Context Retrieval



Models must request or receive only the resolution necessary for the current task.



Implement a retrieval contract supporting requests such as:



\* architecture-level context for the full repository

\* feature-level context for authentication

\* function-level context for token validation

\* raw code for selected implementation nodes

\* annotations relevant to the current task

\* active approved decisions

\* unresolved contradictions

\* archived context only when requested



The retrieval result should include:



\* selected semantic nodes

\* relevant parent context

\* necessary child evidence

\* active annotations

\* approved intent

\* staleness markers

\* provenance

\* token estimate where available



Avoid supplying every note and every source file to every request.



⸻



Agent Collaboration



Every model-authored annotation, decision, or explanation must identify its origin.



Track equivalents of:



\* provider

\* model

\* agent role

\* task

\* run

\* timestamp

\* prompt schema version

\* repository revision



Multiple agents must be able to contribute without silently overwriting one another.



Conflicting model suggestions should be stored as separate proposals until resolved.



⸻



UI Requirements



Global controls



Add or extend:



\* Human View toggle

\* existing resolution slider

\* presentation-mode selector

\* current semantic scope

\* stale-data indicator

\* analysis-status indicator



Node interaction



Selecting an item should display:



\* human explanation

\* canonical identity

\* resolution level

\* parent and child semantic nodes

\* source evidence

\* feature membership

\* annotations

\* current intent

\* verification state

\* change history

\* available actions



Required actions



Include:



\* Add note

\* Add intended change

\* Ask model to review

\* Review exact flow

\* Compare with approved intent

\* Show implementation evidence

\* Open in AI View

\* Open in Human View

\* Expand one resolution level

\* Collapse one resolution level

\* Mark resolved

\* Archive

\* Regenerate explanation



Visual density



At lower Human View resolutions:



\* cluster implementation nodes

\* reduce visible edges

\* prioritise feature and flow relationships

\* collapse utilities and incidental dependencies

\* expose hidden detail on selection



At higher resolutions:



\* progressively reveal implementation relationships

\* retain semantic breadcrumbs

\* avoid losing the selected feature context



⸻



Data Consistency



All projections must preserve canonical traceability.



A generated Human View item must never become an orphaned textual summary.



It must reference:



\* canonical node or nodes

\* repository revision

\* evidence nodes

\* source hash

\* resolution level

\* generation metadata



When canonical mappings change:



\* mark old Human View explanations stale

\* preserve them for history where useful

\* generate updated mappings on demand



⸻



Migration Strategy



1\. Inspect existing graph schema.

2\. Identify fields that can be extended.

3\. Add schema versions.

4\. Create non-destructive migrations.

5\. Backfill semantic levels from existing repository data.

6\. Preserve existing nodes and relationships.

7\. Introduce feature flags for:

&#x20;   \* Human View

&#x20;   \* semantic resolution

&#x20;   \* annotations

&#x20;   \* exact-flow review

8\. Add a migration rollback strategy.

9\. Do not require complete repository reindexing unless technically unavoidable.

10\. Document any required one-time regeneration.



⸻



Implementation Phases



Phase 1: Architecture mapping



Deliver:



\* repository architecture map

\* current graph schema

\* current slider data flow

\* reusable components

\* required schema changes

\* migration plan

\* risk register



No speculative coding before this map exists.



Phase 2: Canonical semantic hierarchy



Implement:



\* semantic node levels

\* parent-child mappings

\* stable canonical references

\* incremental invalidation

\* schema migrations



Phase 3: Human View and slider integration



Implement:



\* Human View toggle

\* slider-to-resolution mapping

\* graph projection

\* selection mapping between views

\* basic cached human explanations



Phase 4: Annotation system



Implement:



\* structured annotation schema

\* note creation

\* model observations

\* annotation statuses

\* filtering

\* audit history

\* graph-item attachment



Phase 5: Feature objects and intended behaviour



Implement:



\* feature schema

\* feature discovery

\* feature approval

\* current versus intended behaviour

\* versioned decisions



Phase 6: Exact-flow review



Implement:



\* model-driven flow analysis

\* structured flow schema

\* evidence links

\* human-readable explanation

\* resolution-aware display



Phase 7: Verification and intent fidelity



Implement:



\* evidence categories

\* static comparison

\* test integration

\* runtime evidence integration where available

\* decomposable status or score

\* contradiction reporting



Phase 8: Optimisation



Implement:



\* lazy Human View generation

\* dependency-aware cache invalidation

\* targeted model context retrieval

\* prefetching for likely selected nodes

\* telemetry for token usage and latency



⸻



Testing Strategy



Add automated tests covering:



Resolution



\* slider maps correctly to semantic levels

\* lower resolution hides implementation detail

\* higher resolution reveals it

\* canonical mappings remain intact

\* selection survives view switching



Caching



\* cached explanations are reused

\* source changes invalidate affected explanations

\* unrelated changes do not invalidate everything

\* different modes produce separate cache entries



Annotations



\* notes attach to canonical objects

\* notes remain visible across resolutions

\* archived notes leave default model context

\* superseded decisions retain audit history



Feature discovery



\* feature descriptions map to relevant code

\* evidence is attached

\* false keyword matches are not automatically accepted

\* users can merge and split candidate mappings



Exact-flow review



\* ordered flow is generated

\* evidence links resolve

\* unknown steps are marked uncertain

\* async and failure paths are represented

\* raw code expansion works



Intent fidelity



\* scores or statuses are evidence-backed

\* insufficient evidence does not produce false certainty

\* approved-intent changes invalidate old assessments



Migration



\* existing repositories continue to load

\* old graph data remains accessible

\* rollback is possible

\* schema versions are respected



⸻



Non-Functional Requirements



Performance



\* slider movement should feel immediate for cached data

\* view switching should not trigger full reanalysis

\* graph projection should be incremental

\* large repositories must not render every node at low resolution



Token efficiency



\* use existing summaries where sufficient

\* request raw code only when needed

\* cache Human View output

\* scope annotations by relevance and status

\* avoid repeated whole-repository prompts

\* record token usage by operation



Reliability



\* never silently lose annotations

\* never silently rewrite approved intent

\* never present stale explanations as current

\* never claim runtime verification from static analysis alone



Security and privacy



\* respect repository access controls

\* avoid sending restricted code to unauthorised providers

\* use existing provider and privacy policies

\* redact secrets from generated explanations and traces

\* do not log sensitive prompt or payload data without explicit policy



Accessibility



\* keyboard navigation

\* non-colour-only status indicators

\* readable low-density Human View

\* screen-reader labels

\* reduced-motion support



⸻



Required Deliverables



Before implementation, produce:



1\. Existing architecture assessment.

2\. Exact files and components to modify.

3\. Proposed data-model changes.

4\. Migration plan.

5\. API contract changes.

6\. UI component plan.

7\. Model-prompt and retrieval plan.

8\. Cache-invalidation design.

9\. Test plan.

10\. Identified risks and mitigations.



After implementation, produce:



1\. Code changes.

2\. Database migrations.

3\. Automated tests.

4\. Feature flags.

5\. Developer documentation.

6\. User-facing usage documentation.

7\. Example repository walkthrough.

8\. Exact-flow review example.

9\. Token and latency measurements.

10\. Known limitations.

11\. Follow-up recommendations separated into:

&#x20;   \* required

&#x20;   \* valuable

&#x20;   \* experimental



⸻



Acceptance Criteria



The feature is complete only when all of the following are true:



1\. Atlas can display the same repository in AI View and Human View.

2\. The existing slider controls Human View resolution.

3\. Switching views preserves semantic selection.

4\. Human View nodes map back to canonical graph nodes.

5\. Users can annotate items at multiple resolution levels.

6\. Model-created observations are distinguishable from user notes.

7\. Notes remain attached when moving between resolutions.

8\. A user can describe or select a feature and ask Atlas to review its exact flow.

9\. Atlas reads the necessary underlying code rather than relying solely on summaries.

10\. The flow report distinguishes proven, observed, inferred, and intended behaviour.

11\. Every significant flow statement can be traced to evidence.

12\. Human explanations are cached.

13\. Source changes invalidate only affected cached explanations.

14\. Approved intent is versioned and cannot be silently replaced.

15\. Verification does not claim certainty without adequate evidence.

16\. Existing Atlas functionality continues to work.

17\. The implementation does not create a second disconnected graph architecture.

18\. The implementation includes tests, migrations, documentation, and rollback guidance.



⸻



Final Execution Instruction



Start by analysing the repository and producing the requested architecture assessment and implementation map.



Then implement the feature incrementally in the listed phases.



At every phase:



1\. Reuse existing Atlas systems.

2\. Keep the canonical graph authoritative.

3\. Preserve traceability.

4\. Run relevant tests.

5\. Record assumptions.

6\. Report deviations from this plan.

7\. Do not simplify requirements silently.

8\. Do not add visually impressive behaviour unless it improves comprehension.

9\. Prefer reliable structured flows over speculative visual gimmicks.

10\. Optimise for the user answering:



\* What does this system do?

\* How does this feature work?

\* What code implements it?

\* Does it match what I intended?

\* What changed?

\* What requires my attention?

\* What evidence supports that conclusion?

