---
name: private-reading-guide
description: Create or substantially rewrite a source-grounded private reading guide for books, papers, long articles, reports, policy/research materials, or chapters. Optimize for durable private notes, Obsidian, Kindle-friendly Markdown, and V2.1 Human Preview integration. Use when the user wants a thoughtful reading guide rather than a mechanical summary.
---

# Private Reading Guide

Revision: 2026-09-30 · V2.1 integration baseline

Use this skill to produce a private reading guide that helps the reader enter a source with a small number of durable questions, understand the source on its own terms, and later return to the note as a long-term knowledge entry.

Explicit user instructions always override defaults below.

## Scope

This skill applies to:

- books;
- academic papers;
- long articles;
- reports;
- policy or research materials;
- chapters or sections;
- source-grounded rereading notes.

Do not force a book-length structure onto short materials. A short paper or article may need one strong question line, two short sections, or simply a compact continuous essay. The number of themes follows the source.

## Default reader profile

Unless the task says otherwise:

- The guide is private, not written for publication.
- It may be read before, during, or after the first reading.
- It may later become a long-term Obsidian note.
- The reader values restrained, lucid, research-note prose over teacherly study-guide language.
- Main text should read as continuous prose, not a dashboard.
- Kindle output favors linear reading on a small screen.

## V2.1 pipeline contract

When used inside the V2.1 knowledge-ingestion workflow, treat inputs in this order:

1. **SourceRecord** — identity, version, source role, completeness, provenance, evidence scope.
2. **Authorized ExtractedSource** — primary writing basis for what the source actually says.
3. **Reading coverage** — what text, figures, and tables were actually read.
4. **CurationData** — auxiliary organization and evidence links; never a replacement for the source.
5. **Reader context** — only what the user has explicitly supplied.
6. **Style rules** — the current finished-prose constraints.

Expected outputs:

- `guide_markdown`
- `evidence_links`
- `unresolved_issues`
- `coverage_statement`

See `references/v2.1-integration.md` for the detailed contract.

## Required inputs

Identify as many of these as are available:

1. **Primary source**
   - title, author(s), edition/translation/version if relevant;
   - full text, extracted Markdown, excerpts, chapter notes, or another authorized source representation;
   - if incomplete, preserve the actual coverage rather than implying full-source review.

2. **Purpose**
   - pre-reading orientation;
   - rereading guide;
   - thematic study;
   - course companion;
   - knowledge-base entry;
   - another explicit goal.

3. **Reader and destination**
   - self or another reader;
   - Kindle, Obsidian, Markdown, EPUB, print, Human Preview, etc.

4. **Reader interests**
   - only when explicitly provided;
   - interests guide selection, not factual interpretation.

5. **Cross-reading materials**
   - other books, courses, lecture notes, models, or concepts the user wants connected.
   - never force a one-to-one mapping.

6. **Must-keep content**
   - concepts, chapters, passages, routes, source notes, or appendices.

7. **Compression level**
   - preserve most content;
   - moderate compression;
   - major rewrite keeping only the core.

8. **Verification scope**
   - source-only by default;
   - source + supplied comparison material;
   - external bibliographic verification only when the surrounding workflow or user explicitly requires it;
   - content fact-check only when explicitly requested.

If a missing item materially changes the result, ask for it. Otherwise proceed conservatively and do not invent the missing fact.

## Source discipline

The guide must stay grounded in the supplied source.

Keep these categories distinct:

1. what the source explicitly says;
2. the guide's interpretation;
3. cross-reading with another source or model;
4. claims that would require external verification.

Rules:

- Do not silently correct, reconcile, modernize, or replace the source's claims.
- Do not attribute a claim to the source when the authorized source does not support it.
- Preserve the source's terminology, organization, framing, and level of detail when these matter.
- If the source itself declares its identity or version, record and use that declaration; do not automatically turn ingestion into fact-checking.
- Formal bibliographic verification is a separate operation.
- Content fact-checking is a separate operation and is not triggered by ordinary guide writing.
- For autobiographical, political, policy, or participant accounts, distinguish recollection or normative judgment from independently established fact.
- Do not turn historical evidence into a current claim without explicit support.

## Visual-source discipline

Figures and images require their own reading status.

- A preserved image is not automatically a read image.
- If an image has not actually been visually read, use only text that the source itself supplies around it, such as a caption.
- Do not infer nodes, arrows, values, legend categories, colors, or spatial relations from a filename or caption alone.
- AI visual interpretation belongs in a separate visual observation / image-notes layer, not in the source text.
- If the actual read representation was a preview or placeholder, do not imply that full-resolution detail was inspected.

## Core editorial principle

**Main prose helps the reader understand what to look for; appendices help the reader find it again.**

Do not mix essay prose, process logs, checklists, metadata dumps, study instructions, and source caveats at the same textual level.

## Structure

Adapt to the source. Do not force fixed headings.

### For books and long reports

Typical components may include:

- a short entry point;
- several recurring questions or thematic lines that genuinely recur;
- one or two priority chapters or clusters;
- selective cross-reading;
- a light reading route;
- a concise source/version note;
- an appendix when useful.

Three to five themes can work for a long book, but they are never mandatory.

### For papers and shorter articles

Prefer a tighter shape:

- identify the actual problem or distinction;
- follow the argument as it develops;
- explain the strongest conceptual contribution or tension;
- state why the material is worth keeping when supported;
- end with one concentrated coverage / limitation paragraph if needed.

Do not inflate a short source into artificial sections.

### For Human Preview

The ReadingGuide supplies the human-facing guide portion. It does not own operational statuses such as staging, cloud archive, hashes, task IDs, or queue state.

A typical preview reading flow is:

- compact title / author / source;
- one short version-and-reading-coverage paragraph;
- ReadingGuide prose;
- one concentrated evidence-range paragraph;
- human decision.

Only show separate “转换状态 / 资料状态” lines when their distinction materially helps the user decide.

## Writing style

Target: **low-rhetoric, high-texture modern Chinese**.

Prefer:

- ordinary, precise words;
- verbs over abstract noun chains;
- conversational syntax carrying serious concepts;
- long sentences for conditions and distinctions, short sentences for landing points;
- restrained transitions;
- repeated correct keywords when conceptual continuity matters;
- paragraphs that complete one movement of understanding;
- judgments that arrive after the material that supports them;
- concrete actors, actions, objects, decisions, payments, places, or events after abstract stretches;
- restrained authorial presence.

Avoid:

- ornamental vocabulary used only to sound elegant;
- stacked abstract nouns when an action can be named;
- formulaic transitions such as “综上所述”“由此可见”“值得注意的是” repeated across sections;
- teacherly commands such as “你应该”“建议重点”“读完自问” unless explicitly requested;
- forced aphorisms;
- mechanical “观点—论据—总结” paragraphs;
- replacing a key term with vague synonyms merely to avoid repetition;
- disclaimer-heavy openings;
- dashboard-like cards and label walls in finished prose.

## Hard finished-prose constraints

These rules apply to Human Preview and `导读.md` style outputs.

### 1. Do not create empty contrasts

Unless there is a real comparison object, do not build force by inventing a denied alternative.

Avoid unnecessary templates such as:

- “不是……而是……”
- “要……而不是……”
- “并非……而在于……”
- “重点不是……而是……”
- “真正的问题不是……而是……”

If no comparison is required, state the positive point directly.

### 2. Finished output contains finished content

Do not include:

- prompts;
- editing instructions;
- failed attempts;
- discarded plans;
- error logs;
- “已删除……”;
- “这里不讨论……”;
- process narration;
- long self-check reports.

Process belongs in review / runtime / ledger layers.

### 3. Concentrate limitations

Do not append a caveat to every paragraph. Put source coverage, unread figures, incomplete versions, or citation limits into one concise paragraph when possible.

### 4. Report numbers neutrally

A weak or partial number does not by itself justify an overall negative judgment. If it reflects a tradeoff, state the tradeoff. Otherwise report the state plainly.

### 5. Keep prose continuous

Use natural paragraphs and sparse headings. Avoid excessive line breaks, cards, tables, labels, and one-field-per-line presentation in the finished guide.

### 6. Do not re-explain a sentence that is already clear

After a sentence lands, move forward.

See `references/style-guide.md` for the fuller editing guide.

## Paragraph design

Treat a paragraph as one movement of understanding.

A useful pattern is:

1. begin from a fact, scene, distinction, or assumption;
2. add a complication;
3. let the meaning shift;
4. land on a restrained judgment or a new question.

Not every paragraph needs all four steps.

Paragraph endings may:

- land on a concrete distinction;
- turn toward the next problem;
- suspend judgment;
- rename what the previous paragraph has shown.

Do not end every paragraph with a summary.

## Sentence design

Use sentence length deliberately.

- Longer sentences: background, conditions, contrasts, qualifications.
- Shorter sentences: distinctions, reversals, landing points.

A short sentence should be earned by the preceding material.

Examples of useful shape when the distinction is real:

- “达成共识、作出承诺、执行承诺，是三件不同的事。”
- “有资产，不等于今天有钱。”

Do not imitate these mechanically.

## Cross-reading rules

When connecting the source to a course, theory, or another book:

1. Name the concept only when it genuinely illuminates the source.
2. Explain the mechanism in plain language before or alongside the label.
3. State the limit of the analogy when needed.
4. Never claim the source proves a model merely because it resembles the model.
5. Prefer a few recurring concept families over chapter-by-chapter matching.

## Reading questions

Questions can appear naturally in prose.

Prefer:

“到了这里，一个问题就出现了：为什么……？”

Avoid turning every section into a worksheet.

Use a separate question list only when the user explicitly wants exercises, review prompts, or internalization tasks.

## Drafting workflow

1. **Resolve source identity and coverage**
   - Read SourceRecord when available.
   - Know what version you actually have.
   - Do not infer “full text” from a successful conversion.

2. **Read the authorized source**
   - Use ExtractedSource as the primary textual basis.
   - Do not draft source claims from an old summary when source text is available.

3. **Build a private source map**
   - event / section / argument;
   - actors;
   - what happens or what is claimed;
   - mechanism or tension;
   - relevance to explicit reader context;
   - cross-reading candidate;
   - unresolved issue.

4. **Find the natural backbone**
   - For long material, identify recurring questions.
   - For short material, identify the minimum structure needed.
   - Do not force a fixed theme count.

5. **Draft the main prose**
   - Let evidence precede major judgments.
   - Minimize instructions to the reader.

6. **Add selective cross-reading**
   - Only where it sharpens a mechanism or distinction.

7. **Add route / appendix only if useful**
   - Kindle route stays light.
   - Obsidian appendix may be richer.

8. **Build evidence links**
   - Every source-attributed guide paragraph should be traceable to source evidence at the precision actually available.
   - Do not invent page or bbox locators.

9. **Run a source pass**
   - Check every specific attribution against the source.
   - Distinguish interpretation and unresolved items.

10. **Run a style pass**
    - Remove empty contrasts.
    - Remove process residue.
    - Consolidate repeated caveats.
    - Replace noun chains with actions.
    - Remove repeated teacherly wording.
    - Delete sentences that merely restate the previous sentence.

11. **Run a usefulness pass**
    - The guide should leave the reader with a small number of durable handles.
    - It should remain useful after the first reading.

## Kindle mode

When output is for Kindle:

- Use clean Markdown unless another format is requested.
- Prefer H1/H2/H3 headings and short sections.
- Avoid wide tables.
- Keep the main body linear.
- For books, keep only about 6–8 priority chapters in a compact appendix unless the user asks for all chapters.
- For papers/articles, do not manufacture a chapter appendix.
- Keep source/version notes brief.
- Avoid Obsidian-only syntax unless requested.

## Obsidian mode

When output is for Obsidian:

- Preserve a readable main essay.
- Appendices may include a chapter index, concept map, source notes, or later-reading questions.
- Use stable headings that can later be linked.
- Metadata and reference scaffolding must not overwhelm the main note.

## Final checks

Before finalizing, verify:

- The guide follows the source's actual scope.
- Long sources are organized by genuine recurring questions, not a disguised chapter list.
- Short sources are not inflated into artificial thematic structures.
- Source statements and interpretation are distinguishable.
- Cross-reading is selective.
- No external model or course concept is falsely attributed to the source.
- Images are described only to the extent actually read.
- Source limitations are concentrated rather than repeated.
- No unnecessary “不是……而是……” style contrast remains.
- No process notes remain in finished prose.
- Important judgments are supported before they are stated.
- Abstract passages are periodically grounded in concrete content.
- Keywords remain consistent.
- The ending does not over-summarize.
- Kindle output has no wide tables or oversized appendix.
- Obsidian output remains readable even with reference scaffolding removed.
- `evidence_links`, `unresolved_issues`, and `coverage_statement` match the actual reading coverage when used in V2.1.
