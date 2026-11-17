"""Re-embed the DPDP knowledge base with the embedding model that is configured now.

WHY: the knowledge base's vectors (`knowledge_chunks.embedding`) were made by one embedding model, and a retrieval question is embedded by the model in `NVIDIA_EMBED_MODEL`. When the provider
retires a model (HTTP 410, 16 Nov 2026: nvidia/llama-nemotron-embed-vl-1b-v2) and the setting is changed, the stored vectors and the new questions come from different models and retrieval
returns the wrong provisions even when it no longer errors. Changing `.env` alone is therefore not enough: every chunk has to be embedded again.

WHAT IT DOES (and nothing else):
  * reads every row of `knowledge_chunks` (id, content), leaves text, metadata and ids untouched;
  * embeds all the text first (input_type="passage", NVIDIA_EMBED_DIMENSIONS wide) and checks the count and the width of every vector;
  * only then writes all of them in ONE transaction, so a failure part-way leaves the table exactly as it was;
  * before writing, saves the old vectors to a JSON file (`--backup-file`), which `--restore` puts back.

USAGE (run in the backend container, from /app):
    python -m tools.reembed_knowledge_base                       # dry run: counts, settings, no API call, no write
    python -m tools.reembed_knowledge_base --apply --backup-file /tmp/knowledge_embeddings_before.json
    python -m tools.reembed_knowledge_base --restore --backup-file /tmp/knowledge_embeddings_before.json
Copy the backup out of the container before anything else touches it: docker cp <container>:/tmp/knowledge_embeddings_before.json .
Take a database dump first as well (pg_dump -t knowledge_documents -t knowledge_chunks).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.session import async_session_factory
from app.llm.client import NvidiaLLMClient

_BATCH = 32


async def _load(db) -> list[tuple[str, str]]:
    rows = (await db.execute(text("select id::text, content from knowledge_chunks order by id"))).all()
    return [(r[0], r[1]) for r in rows]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def dry_run() -> int:
    s = get_settings()
    async with async_session_factory() as db:
        rows = await _load(db)
        docs = (await db.execute(text("select count(*) from knowledge_documents"))).scalar_one()
    print(f"embedding model now configured : {s.nvidia_embed_model}")
    print(f"vector width                   : {s.nvidia_embed_dimensions}")
    print(f"knowledge documents / chunks   : {docs} / {len(rows)}")
    print("dry run: no API call made, nothing written. Re-run with --apply --backup-file <path> to re-embed.")
    return 0


async def apply(backup_file: Path) -> int:
    s = get_settings()
    width = s.nvidia_embed_dimensions
    async with async_session_factory() as db:
        rows = await _load(db)
        if not rows:
            print("knowledge_chunks is empty: nothing to re-embed (the knowledge base has to be ingested, not re-embedded).")
            return 1
        old = (await db.execute(text("select id::text, embedding::text from knowledge_chunks order by id"))).all()
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_text(json.dumps({"model_at_backup_time": s.nvidia_embed_model, "rows": [{"id": r[0], "embedding": r[1]} for r in old]}), encoding="utf-8")
    print(f"backed up {len(old)} old vectors to {backup_file}")

    client = NvidiaLLMClient(s)
    vectors: list[list[float]] = []
    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        vectors.extend(await client.embed([content for _, content in batch], input_type="passage"))
        print(f"embedded {len(vectors)}/{len(rows)}", end="\r", flush=True)
    print()
    if len(vectors) != len(rows):
        print(f"ABORT: asked for {len(rows)} vectors, got {len(vectors)}. Nothing was written.")
        return 2
    bad = [i for i, v in enumerate(vectors) if len(v) != width]
    if bad:
        print(f"ABORT: {len(bad)} vectors are not {width} wide (first: row {bad[0]}, width {len(vectors[bad[0]])}). Nothing was written.")
        return 2

    async with async_session_factory() as db:
        async with db.begin():
            for (chunk_id, _), vec in zip(rows, vectors, strict=True):
                await db.execute(text("update knowledge_chunks set embedding = cast(:v as vector) where id = cast(:i as uuid)"), {"v": _vector_literal(vec), "i": chunk_id})
        n = (await db.execute(text("select count(*) from knowledge_chunks"))).scalar_one()
    print(f"re-embedded {len(rows)} chunks with {s.nvidia_embed_model} ({width} wide); table now has {n} rows.")
    return 0


async def restore(backup_file: Path) -> int:
    data = json.loads(backup_file.read_text(encoding="utf-8"))
    async with async_session_factory() as db:
        async with db.begin():
            for r in data["rows"]:
                await db.execute(text("update knowledge_chunks set embedding = cast(:v as vector) where id = cast(:i as uuid)"), {"v": r["embedding"], "i": r["id"]})
    print(f"restored {len(data['rows'])} vectors from {backup_file} (made with {data.get('model_at_backup_time')}).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Re-embed the DPDP knowledge base with the configured embedding model")
    p.add_argument("--apply", action="store_true", help="re-embed and write (needs --backup-file)")
    p.add_argument("--restore", action="store_true", help="put the vectors in --backup-file back")
    p.add_argument("--backup-file", type=Path)
    a = p.parse_args()
    if a.apply and a.restore:
        p.error("choose --apply or --restore, not both")
    if (a.apply or a.restore) and a.backup_file is None:
        p.error("--backup-file is required with --apply and --restore")
    if a.restore:
        return asyncio.run(restore(a.backup_file))
    if a.apply:
        return asyncio.run(apply(a.backup_file))
    return asyncio.run(dry_run())


if __name__ == "__main__":
    sys.exit(main())
