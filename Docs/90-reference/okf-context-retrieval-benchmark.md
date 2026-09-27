# OKF context retrieval benchmark

**Retrieval only -- not answer quality.** This page reports how often the stored OKF knowledge
bundle puts the right object in front of a model, for a fixed set of questions with known expected
objects. It says nothing about whether an answer written from those documents is correct. That
needs paid model calls nobody has authorised, and the harness has no way to make one: no
`--live`, no provider import, no embedding.

Tracker rows: R11-OKF02 (its "before/after answer evaluation" remainder stays open, and this is the
free half of it) and R11-FP13 (which measured retrieval over the live catalog and answers over the
footprint, and had not measured retrieval over the stored bundle). Status lives in
[the tracker](../60-delivery/03-tracker.md); this page is evidence.

Harness: [`scripts/okf_context_benchmark.py`](../../scripts/okf_context_benchmark.py). Corpus:
[`okf_context_corpus.json`](../../tests/fixtures/quality_benchmark_corpus/okf_context_corpus.json).
Pinned by [`tests/test_okf_context_benchmark.py`](../../tests/test_okf_context_benchmark.py).
The ranking measured is `aida.okf_context`: lexical, BM25-style idf over names, approved
descriptions, column names and meanings, concept aliases and tool inputs. No number below was used
to change it, and this task changed no ranking code.

## Reproduce

```
# Default: builds the fixture estate, renders the real bundle, selects with aida.okf_context.
# No network, no database, no settings, deterministic. Nothing is written anywhere.
python scripts/okf_context_benchmark.py --max-chars 16000 --max-chars 4000 --max-chars 2000 --max-chars 1000
python scripts/okf_context_benchmark.py --format markdown   # the tables on this page
python scripts/okf_context_benchmark.py --check-corpus      # the corpus resolves against the estate

# Against a running development stack. Reads only; every call leaves the platform's ordinary read
# audit record. --org takes an id (looking one up by name needs PlatformAdmin).
AIDA_BASE_URL=http://localhost:8000 python scripts/okf_context_benchmark.py \
    --api --org <organization id> --datasource "Customer Master"
AIDA_BASE_URL=http://localhost:8000 python scripts/okf_context_benchmark.py \
    --api --org <organization id> --product-version <context product version id>
```

The script builds no `Settings`, so it needs no `AIDA_ENVIRONMENT`. It exits 0 whatever it
measures; no threshold is set for any number.

## What is measured

For each question with known expected object(s):

- **hit@1, hit@3, delivered.** Is an expected object among the first k *distinct subjects* handed
  to the reader (a wide table's column sets count as their parent), or anywhere in the documents
  returned. Any-of: one expected object is enough. `direct` means the ranker chose it; `via_link`
  means a one-hop link from a chosen document brought it (a concept to its mapped table, a routine
  to the table it writes). Linked documents are appended after every directly ranked one, so a
  `via_link` object cannot be hit@1 by construction. MRR follows from the same order.
- **Status split.** MATCHED, AMBIGUOUS (the route's `MATCHED` with a non-empty `ambiguous` list) and
  NO_MATCH, each against what the corpus says it should be. A NO_MATCH answer to a question the
  bundle cannot answer is correct; a MATCHED one is a false match.
- **False ambiguity.** Among questions the corpus says have exactly one right object, how often the
  ranker reported a tie. **Correct ambiguity** is the converse, for questions two objects answer
  equally.
- **Gap kept.** An object whose only path is lineage nobody approved must not be delivered.
- **Characters returned against the budget**, and how many cases had a section cut.
- **Text delivered.** Where a case names a phrase the answer stands on, whether it is in the
  delivered text (one case: a wide table's column meaning arrives only if the right column set does).

**Ablations** (in-process only) re-rank the same questions from a modified copy of the frozen
snapshot with a signal removed: `no_descriptions` (every approved description, column meaning and
concept definition), `no_aliases`, and `names_only` (also columns, parameters and tool inputs). The
documents handed out are always the full bundle's, so a difference is a difference in what the ranker
could see. Removing a signal also changes the idf every other term is weighted by, so a delta can carry
second-order effects.

## The corpus, and how it grew

28 cases, 24 that expect an object and 4 that expect NO_MATCH. 11 are **reused** by id from
`footprint_enrichment_corpus.json` (6) and `answer_evaluation_corpus.json` (5), so their question and
expected object are read from those files at load time and cannot drift from them; the answer corpus's
three refusal-verdict cases have no expected object and the loader refuses them. 17 are this task's.
Categories: alias 4, name 4, description 4, lineage 3, gap 2, routine 1, column 1, ambiguity 1, tool 1,
no_match 4, paraphrase 3.

**Authorship, plainly.** The own cases and the estate overlay (approved descriptions and column meanings
for 7 of the 10 fixture tables; three have none) were written by the session that read the ranking, and
then run once. Three cases were **added after seeing the first run**, and are labelled in the corpus:

| Added | Why |
|---|---|
| `alias-word-alone` | The first run (25 cases) showed **no difference** when aliases were removed, because every alias question ("closing position") also contains "position", a word of the concept's own name. This question uses only the alias word. |
| `no-match-adjacent-satisfaction` | The three original no_match questions share no word with the estate, so they could only pass. This one shares a word ("customer") with a real table while asking for something no object holds. |
| `tracker-balances-by-date` | Not authored here: the question the tracker's own live check asked (2026-09-19), which the walkthrough recorded as ambiguous. |

First run, 25 cases, for the record: full 17/22 hit@1, 20/22 hit@3; no_descriptions 14/22; no_aliases
17/22 (identical to full); names_only 12/22; NO_MATCH 3/3.

Cases whose ground truth exists only in the fixture (`fixture_only`: text the overlay authored, or the
fixture's tool) are skipped by `--api`, so a running stack is never scored on the absence of text it was
never given. The first live runs did exactly that wrong, on three cases, until the flag existed.

## Results: fixture estate (deterministic)

Estate: the harness estate of `scripts/quality_benchmark.py` (10 tables, 2 routines with reviewed and
undecided lineage, one concept with an alias, one tool) plus the overlay: 21 documents, 7 of 10 tables
with an approved description, one 150-column table split into column sets. Budget 16,000 characters.

| variant | hit@1 | hit@3 | delivered | MRR | direct/link/missed |
|---|---|---|---|---|---|
| full | 19/24 (0.792) | 22/24 (0.917) | 22/24 (0.917) | 0.854 | 19/3/2 |
| no_descriptions | 16/24 (0.667) | 19/24 (0.792) | 19/24 (0.792) | 0.729 | 16/3/5 |
| no_aliases | 18/24 (0.750) | 21/24 (0.875) | 21/24 (0.875) | 0.812 | 18/3/3 |
| names_only | 13/24 (0.542) | 15/24 (0.625) | 16/24 (0.667) | 0.594 | 13/3/8 |

| variant | NO_MATCH correct | false ambiguity | correct ambiguity | gap kept | text delivered | mean chars |
|---|---|---|---|---|---|---|
| full | 3/4 (0.750) | 0/18 (0.000) | 1/1 (1.000) | 2/2 (1.000) | 1/1 (1.000) | 1416 |
| no_descriptions | 3/4 (0.750) | 0/18 (0.000) | 1/1 (1.000) | 2/2 (1.000) | 1/1 (1.000) | 1194 |
| no_aliases | 3/4 (0.750) | 0/18 (0.000) | 1/1 (1.000) | 2/2 (1.000) | 1/1 (1.000) | 1389 |
| names_only | 3/4 (0.750) | 0/18 (0.000) | 0/1 (0.000) | 2/2 (1.000) | 0/1 (0.000) | 1284 |

Budget sweep, full ranking:

| budget | hit@1 | delivered | text delivered | mean chars | cases with sections cut |
|---|---|---|---|---|---|
| 16000 | 19/24 (0.792) | 22/24 (0.917) | 1/1 (1.000) | 1416 | 0 |
| 4000 | 19/24 (0.792) | 22/24 (0.917) | 1/1 (1.000) | 1416 | 0 |
| 2000 | 19/24 (0.792) | 22/24 (0.917) | 1/1 (1.000) | 1118 | 8 |
| 1000 | 19/24 (0.792) | 22/24 (0.917) | 1/1 (1.000) | 736 | 15 |

The misses, by name (full ranking): `paraphrase-payroll` and `paraphrase-suspicious` are answered NO_MATCH
although the estate holds the object (nothing in the question shares a word with it), and
`no-match-adjacent-satisfaction` is a **false match** -- "customer satisfaction" is answered with the
customer table, its tool and the orders table. `paraphrase-money-at-day-end` reaches the right table
only by accident of wording: "end" and "day" are words of the concept "End of day position", and the
concept links to the table.

## Results: a running stack (2026-09-19, one run each)

Development stack, the Northwind sample estate, read as `Analyst`.

**Source bundle** -- Customer Master (Postgres, sample), publication 2, profile `atlas-okf-export/4`,
23 documents, 17 objects and routines of which **one** has an approved description. 15 cases measured,
13 skipped (5 need a product bundle, 6 are fixture-only, 2 name objects this estate does not hold; the
skipped list is printed with each run).

| variant | hit@1 | hit@3 | delivered | MRR | direct/link/missed |
|---|---|---|---|---|---|
| full | 8/11 (0.727) | 9/11 (0.818) | 9/11 (0.818) | 0.773 | 8/1/2 |

| variant | NO_MATCH correct | false ambiguity | correct ambiguity | gap kept | text delivered | mean chars |
|---|---|---|---|---|---|---|
| full | 3/4 (0.750) | 1/8 (0.125) | n/a | 2/2 (1.000) | n/a | 1981 |

The one false ambiguity is `tracker-balances-by-date`: "Which table holds account balances by date?"
ties the table `fact_account_balances` with the **view** `vw_account_balance_movement` -- identical
score (6.743069), identical matched terms -- and the route reports the two as ambiguous, which is what
the tracker's walkthrough recorded. Whether that is *false* depends on the label: the question says
"table" and one of the two is a view, but the ranker does not weigh an object's kind. The gap cases
hold on the real freeze: the routine with proposed lineage is delivered alone, and the table it is
proposed to write is not.

**Product bundle** -- `banking_context` version 1 (PUBLISHED), publication 3, 10 documents, 3 objects
and routines (2 tables, 1 routine, plus the banking concept), one approved description. 15 cases
measured, 13 skipped.

| variant | hit@1 | hit@3 | delivered | MRR | direct/link/missed |
|---|---|---|---|---|---|
| full | 8/11 (0.727) | 11/11 (1.000) | 11/11 (1.000) | 0.864 | 8/3/0 |

| variant | NO_MATCH correct | false ambiguity | correct ambiguity | gap kept | text delivered | mean chars |
|---|---|---|---|---|---|---|
| full | 4/4 (1.000) | 0/8 (0.000) | n/a | n/a | n/a | 2024 |

## What the numbers say

- **What a lexical ranker gets right is what it was given words for.** A name, a routine's name, an
  approved description's words and an alias's words all reach their object at rank 1, direct, on the
  fixture and on the live stack. The concept-to-table and routine-to-table cases arrive by link
  (3 of 24 on the fixture), always after the source document.
- **Words it was not given, it does not find.** Two of three paraphrase questions are refused (NO_MATCH)
  where the object exists. A refusal is the honest failure; the third failure mode is worse -- an object
  chosen because a *different* word overlapped ("customers" on a question about balances).
- **False matches on adjacent out-of-scope questions are real.** A question that shares any word with a
  table gets that table. On the live source bundle "customer satisfaction" returned the `customer` table;
  on the 3-object product it was correctly refused only because no such table is in it.
- **Approved descriptions help exactly where they exist.** On the fixture (7 of 10 tables described) removing
  them costs 3 hit@1 (19 to 16); on the live source bundle one object in 17 has one, so the signal was
  nearly absent and the live numbers say little about it.
- **Aliases did not show on the original corpus** (see above); with an alias-only question they cost 1
  case (19 to 18).
- **A tight budget cut sections before it lost objects.** Down to 1,000 characters no object left the
  delivered set, but 15 of 28 cases had a section left out. Mean use is about 9% of the default budget on the
  fixture.
- **Ties come from near-duplicates the fixture does not have.** Fixture false ambiguity is 0 of 18; the
  live estate, which holds a table and a view of the same subject, ties on the tracker's own question.

## What these numbers do not show

- **Answer quality.** Nothing here asks a model anything. A document delivered is not an answer given,
  and the before/after answer evaluation R11-OKF02 names stays open and unrun.
- **How often production questions are answered.** 28 questions authored by the session that read the
  ranker, over an estate it also authored (partly): a regression instrument and an ablation illustration.
  n is small: one case moves a rate by about 4 points, and the correct-ambiguity, gap and text figures
  rest on one or two cases each.
- **That evidence, not just the object, arrived.** "Delivered" means some section of the object's document
  was handed over. Only one case checks text; at a small budget the object is delivered with sections
  left out and still counts.
- **Freeze fidelity.** The fixture is built from facts, not frozen from a catalog, so it shows what the
  ranker does with a bundle and not what the freeze puts in one. Its documents are smaller than production
  ones (no captured view definitions), so character figures are a lower bound. The live runs are the
  check on the freeze, and cover it only for the objects these cases touch.
- **Live figures beyond one run on one estate.** One dated run per bundle, one small organization. 13 of
  28 cases did not apply to either live bundle, and a source bundle holds no concepts or tools.
- **Ambiguity generally.** One correct-ambiguity case and one live false ambiguity.
- **A comparison between rankers or a pass mark.** No threshold is set, and the numbers move whenever the
  ranking or the fixture does; `test_the_measured_numbers_are_the_ones_the_results_document_reports` pins the
  fixture numbers so that moving them is a deliberate, visible change to this page as well.
