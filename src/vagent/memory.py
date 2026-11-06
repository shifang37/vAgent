"""H2 long-term memory: a retrievable index of *confirmed* project facts.

Memory is a derived index, never a source of truth. Records are projected from the
structured project fields (goal / audience / style / constraints / plan), never from
model free text, so a hallucinated summary cannot pollute the project facts — the
same deterministic-field discipline the rest of the harness already uses.

The index lives in a sidecar file (``<home>/memory.json``), so ``state.json`` keeps its
frozen schema and the 168 contract model hashes do not move. Retrieval is offline: the
default embedder is a deterministic hashing bag-of-ngrams with no network and no model
call. Everything is opt-in (``VAGENT_MEMORY=on``); with memory off the rendered context
and ``contextSignature`` are byte-for-byte what they are today.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

MEMORY_VERSION = 2
# v1 wrote the same ``supersededBy`` field with the same truthiness contract, so its
# sidecars stay readable; every new write uses v2.
READABLE_MEMORY_VERSIONS = (1, MEMORY_VERSION)
MEMORY_FILE = "memory.json"
EMBEDDING_DIM = 256
EMBEDDING_VERSION = "hashing-bigram-v1"
MAX_RECORDS = 4096
MAX_RECORDS_PER_PROJECT = 512
DEFAULT_LIMIT = 4
DEFAULT_BLOCK_BYTES = 1024
# Kinds that hold at most one current value per project, so a replaced record can name
# its successor. Multi-value kinds (constraint / plan) only ever say "removed": their
# siblings are concurrent facts, not earlier versions of one another.
SCALAR_KINDS = frozenset({"goal", "audience", "style"})

# A fact that looks like a credential is never indexed: Keys and secret material must
# not reach a store that a later retrieval step feeds back into the model.
# Boundaries are ASCII lookarounds, not ``\b``: Python counts a CJK character as a word
# character, so ``\b`` would miss ``后台sk-...``.
SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    # A JWT is three base64url segments; its JSON header always encodes as ``eyJ...``.
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?<![A-Za-z0-9])Bearer\s+\S+", re.IGNORECASE),
    # ``api_key: value`` is a credential; the bare phrase "api key" is not.
    re.compile(r"(?<![A-Za-z0-9])api[-_ ]?key\s*[:=：]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:密钥|密码|口令|令牌|秘钥)\s*[:=：]\s*\S+"),
)
# A long hex/base64 blob is only a credential next to a key name, so a commit hash or an
# ordinary 32-digit number is not censored.
CONTEXTUAL_SECRET_PATTERNS = (
    re.compile(
        r"(?:key|token|secret|password|passwd|密码|密钥|口令|令牌)[^\n]{0,16}?[:=：]\s*[A-Fa-f0-9]{32,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:key|token|secret|password|passwd|密码|密钥|口令|令牌)[^\n]{0,16}?[:=：]\s*[A-Za-z0-9+/]{40,}={0,2}",
        re.IGNORECASE,
    ),
)

CJK = re.compile(r"[㐀-鿿]+")
WORD = re.compile(r"[a-z0-9_]+")


def normalize(text: str) -> str:
    """Canonical form for hashing and tokenizing (NFKC folds full-width punctuation)."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def clean(text: str) -> str:
    """Whitespace-collapse only: the stored fact keeps the user's own punctuation."""
    return " ".join(text.split())


def looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS) or any(
        pattern.search(text) for pattern in CONTEXTUAL_SECRET_PATTERNS
    )


def tokens(text: str) -> list[str]:
    """Deterministic tokens: ASCII word runs plus CJK unigrams and bigrams.

    Chinese has no spaces, so character bigrams carry the lexical signal that word
    tokens carry in English; both feed the same hashing embedder.
    """
    lowered = unicodedata.normalize("NFKC", text).casefold()
    result = WORD.findall(lowered)
    for run in CJK.findall(lowered):
        result.extend(run)
        result.extend(run[i : i + 2] for i in range(len(run) - 1))
    return result


class HashingEmbedder:
    """A deterministic, offline embedder: hashing trick over tokens, L2 normalized.

    Same text always yields the same vector on every platform (blake2b, not ``hash``),
    so retrieval is reproducible in tests and across processes without a model call.
    """

    name = EMBEDDING_VERSION
    fingerprint = EMBEDDING_VERSION
    dim = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            return vector
        return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def fingerprint(project_id: str, kind: str, text: str) -> str:
    payload = f"{project_id}\n{kind}\n{normalize(text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_facts(project: dict) -> list[tuple[str, str]]:
    """The confirmed project fields, in a fixed order; nothing model-authored enters."""

    def entries(value) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("title") or item.get("value")
                if isinstance(text, str):
                    out.append(text)
        return out

    facts: list[tuple[str, str]] = []
    for kind in ("goal", "audience", "style"):
        value = project.get(kind)
        if isinstance(value, str) and value.strip():
            facts.append((kind, clean(value)))
    for field, kind in (("constraints", "constraint"), ("plan", "plan")):
        for text in entries(project.get(field)):
            if text.strip():
                facts.append((kind, clean(text)))
    return facts


class MemoryIndex:
    """``memory-v2`` sidecar: the current project facts, with retired history kept.

    Adding the same fact twice is a no-op (same fingerprint). Each ``sync`` is a
    projection of the project's *current* fact set: a record stays active exactly while
    its fingerprint is still a current fact, a fact that disappeared is marked
    ``supersededBy`` rather than deleted (``removed``, or the successor id for a
    single-valued kind), and a fact that comes back is reactivated. Sibling constraints
    and plan items therefore coexist instead of retiring one another. The history stays
    auditable, the same "keep the old version" discipline as artifacts.
    """

    def __init__(self, home, embedder=None, *, clock=None):
        self.path = Path(home) / MEMORY_FILE
        self.embedder = embedder or HashingEmbedder()
        if clock is None:
            from vagent.storage import now as clock

        self.clock = clock
        # ``(st_mtime_ns, st_size) -> records``: one parse serves sync + recall per step.
        self._cache: tuple[tuple[int, int], dict] | None = None

    def _read(self) -> dict:
        """The latest sidecar records, re-parsed only when the file itself changed."""
        try:
            stat = self.path.stat()
        except OSError:
            self._cache = None
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        records = self._load()
        self._cache = (stamp, records)
        return records

    def _load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict) or payload.get("version") not in READABLE_MEMORY_VERSIONS:
            return {}
        records = payload.get("records")
        return records if isinstance(records, dict) else {}

    def _remember(self, records: dict) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache = None
            return
        self._cache = ((stat.st_mtime_ns, stat.st_size), records)

    def _write(self, records: dict) -> dict:
        """Write oldest first, so byte order encodes age and eviction can walk it."""
        ordered = {
            record_id: records[record_id]
            for record_id in sorted(records, key=lambda key: (str(records[key].get("createdAt") or ""), key))
        }
        payload = json.dumps(
            {"version": MEMORY_VERSION, "embedding": self.embedder.fingerprint, "records": ordered},
            ensure_ascii=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from vagent.storage import atomic_write_bytes

        # Atomic replacement (uuid temp name + fsync + replace + FX-02 retry) keeps a
        # concurrent reader from ever seeing a truncated memory index.
        atomic_write_bytes(self.path, payload.encode("utf-8"))
        return ordered

    def _evict(self, records: dict, project_id: str) -> None:
        """Drop superseded records first, then the oldest local ones, then the oldest global."""

        def oldest(keys: list[str]) -> list[str]:
            return sorted(keys, key=lambda key: (str(records[key].get("createdAt") or ""), key))

        def victims(keys: list[str], count: int) -> list[str]:
            ordered = oldest(keys)
            superseded = [key for key in ordered if records[key].get("supersededBy")]
            active = [key for key in ordered if not records[key].get("supersededBy")]
            return (superseded + active)[:count]

        local = [key for key, record in records.items() if record.get("projectId") == project_id]
        if len(local) > MAX_RECORDS_PER_PROJECT:
            for key in victims(local, len(local) - MAX_RECORDS_PER_PROJECT):
                records.pop(key, None)
        overflow = len(records) - MAX_RECORDS
        if overflow > 0:
            for key in victims(list(records), overflow):
                records.pop(key, None)

    def sync(self, project: dict) -> list[str]:
        """Project the project's current facts; returns the ids added in this call.

        Supersede is derived from the current fact *set*, never from "every other
        record of the same kind": several constraints or plan items are concurrent
        facts, so one new sibling must not retire the others. A record is active
        exactly while its fingerprint is one of this project's current facts; a fact
        that disappeared is marked ``removed`` (or, for a single-valued kind, points at
        the id that replaced it); a fact that returns is reactivated. Nothing is
        written unless a record was really added, retired, or revived.
        """
        project_id = project["id"]
        records = self._read()
        current = {
            fingerprint(project_id, kind, text): (kind, text)
            for kind, text in project_facts(project)
            if not looks_secret(text)
        }
        by_kind: dict[str, list[str]] = {}
        for key, (kind, _) in current.items():
            by_kind.setdefault(kind, []).append(key)
        replaced = {
            kind: keys[0] for kind, keys in by_kind.items() if kind in SCALAR_KINDS and len(keys) == 1
        }
        added: list[str] = []
        changed = False
        for record_id, record in records.items():
            if record.get("projectId") != project_id:
                continue
            if record_id in current:
                if record.get("supersededBy"):
                    # The fact is part of the project again; recall it again.
                    record["supersededBy"] = None
                    changed = True
                continue
            if record.get("supersededBy"):
                continue  # already inactive: keep the original reason
            record["supersededBy"] = replaced.get(record.get("kind"), "removed")
            changed = True
        for key, (kind, text) in current.items():
            if key in records:
                continue
            records[key] = {
                "id": key,
                "projectId": project_id,
                "kind": kind,
                "text": text,
                "embedding": self.embedder.fingerprint,
                "vector": self.embedder.embed(text),
                "createdAt": self.clock(),
                "supersededBy": None,
            }
            added.append(key)
            changed = True
        if changed:
            self._evict(records, project_id)
            try:
                self._remember(self._write(records))
            except OSError:
                # The on-disk file did not change; never keep the mutated view cached.
                self._cache = None
                raise
        return [key for key in added if key in records]

    def records(self, project_id: str | None = None, *, current_only: bool = True) -> list[dict]:
        records = self._read().values()
        return [
            record
            for record in records
            if (project_id is None or record.get("projectId") == project_id)
            and (not current_only or not record.get("supersededBy"))
        ]

    def recall(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        cross_project: bool = False,
    ) -> list[dict]:
        """Top-``limit`` current records by cosine similarity; deterministic ties."""
        vector = self.embedder.embed(query)
        scored = []
        for record in self.records(None if cross_project else project_id):
            stored = record.get("vector")
            if not isinstance(stored, list) or len(stored) != len(vector):
                continue
            score = cosine(vector, stored)
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1]["kind"], item[1]["id"]))
        return [record for _, record in scored[:limit]]

    def render(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        budget_bytes: int = DEFAULT_BLOCK_BYTES,
        cross_project: bool = False,
    ) -> str:
        """The recalled block as plain lines, clipped to ``budget_bytes`` (no header)."""
        lines: list[str] = []
        used = 0
        for record in self.recall(project_id, query, limit=limit, cross_project=cross_project):
            line = f"- ({record['kind']}) {record['text']}"
            size = len(line.encode("utf-8")) + 1
            if used + size > budget_bytes:
                break
            lines.append(line)
            used += size
        return "\n".join(lines)
