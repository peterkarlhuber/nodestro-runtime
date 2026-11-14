"""Preselection of skills for one step (NAL-B4): which assigned ``on_demand`` skills the catalog offers.

An agent may have many skills (groups make 30 or more practical), but a catalog of all of them costs context and
makes the model's choice worse. Above :data:`~.schemas.SKILL_SELECTION_THRESHOLD` ``on_demand`` skills, a step offers
the best :data:`~.schemas.SKILL_SELECTION_SIZE` of them for its prompt, ranked with BM25 over each skill's name and
description, plus every ``always`` skill and every skill the orchestrator recommended. The ranking is local and
deterministic (same prompt and skills, same choice; ties go by name) and recorded (:class:`~.schemas.SkillSelection`).

Only assigned skills are ever candidates: a selection narrows the set, it never adds to it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import math
import re
import unicodedata

from .schemas import SKILL_SELECTION_SIZE, SKILL_SELECTION_THRESHOLD, SkillCandidate, SkillMode, SkillSelection

__all__ = ["Candidate", "rank", "select", "tokens"]

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: Words longer than this are cut to it, so "analysis", "analyse" and "analysieren" meet (a crude, language-neutral
#: stem; the same cut applies to the prompt and the skills).
_STEM = 6
_K1 = 1.5
_B = 0.75
#: The most candidates a record keeps.
_RECORDED = 20


@dataclass(frozen=True)
class Candidate:
    """What the selection knows of one assigned skill."""

    name: str
    description: str
    mode: SkillMode = "on_demand"


def _fold(text: str) -> str:
    """Lower case without accents (``Prüfung`` and ``prufung`` meet)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def tokens(text: str) -> list[str]:
    """The search words of ``text``: folded, split at everything but letters and digits, cut to a stem, without
    single characters. Names like ``source-check`` give ``source`` and ``check``."""
    return [word[:_STEM] for word in _WORD.findall(_fold(text)) if len(word) > 1]


def rank(query: str, candidates: Sequence[Candidate]) -> list[tuple[Candidate, float]]:
    """Every candidate with its BM25 score for ``query``, best first; equal scores in name order."""
    documents = [tokens(f"{item.name} {item.name} {item.description}") for item in candidates]
    count = len(documents)
    if not count:
        return []
    average = sum(len(document) for document in documents) / count or 1.0
    frequency: Counter[str] = Counter()
    for document in documents:
        frequency.update(set(document))
    wanted = Counter(tokens(query))
    scored = []
    for item, document in zip(candidates, documents, strict=True):
        terms = Counter(document)
        score = 0.0
        for term, repeats in wanted.items():
            found = terms.get(term, 0)
            if not found:
                continue
            idf = math.log(1 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5))
            norm = found + _K1 * (1 - _B + _B * len(document) / average)
            score += repeats * idf * found * (_K1 + 1) / norm
        scored.append((item, round(score, 6)))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0].name))


def select(
    query: str,
    candidates: Sequence[Candidate],
    *,
    recommended: Sequence[str] = (),
    threshold: int = SKILL_SELECTION_THRESHOLD,
    size: int = SKILL_SELECTION_SIZE,
) -> SkillSelection:
    """Which of ``candidates`` a step offers for ``query``.

    ``always`` skills and the ``recommended`` names an agent has are always offered; recommended names it does not
    have are listed as ignored. With at most ``threshold`` ``on_demand`` skills, all are offered (``all``);
    otherwise the best ``size`` by BM25 (``bm25``), with the best-scored candidates recorded.
    """
    names = {item.name for item in candidates}
    hints = list(dict.fromkeys(name.strip() for name in recommended if name.strip()))
    chosen_hints = [name for name in hints if name in names]
    ignored = [name for name in hints if name not in names]
    on_demand = [item for item in candidates if item.mode == "on_demand"]
    if len(on_demand) <= threshold:
        return SkillSelection(
            method="all",
            assigned=len(candidates),
            offered=[item.name for item in candidates],
            recommended=chosen_hints,
            ignored=ignored,
        )
    ranked = rank(query, on_demand)
    best = {item.name for item, _score in ranked[:size]}
    keep = best | set(chosen_hints) | {item.name for item in candidates if item.mode == "always"}
    return SkillSelection(
        method="bm25",
        assigned=len(candidates),
        offered=[item.name for item in candidates if item.name in keep],
        recommended=chosen_hints,
        ignored=ignored,
        candidates=[
            SkillCandidate(name=item.name, score=score, chosen=item.name in keep) for item, score in ranked[:_RECORDED]
        ],
    )
