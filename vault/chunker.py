import re
from dataclasses import dataclass


@dataclass
class VaultChunk:
    chunk_index: int
    heading_path: str   # e.g. "Architecture > Sync > Triggers"
    text: str


def chunk_vault_file(body: str, max_words: int = 400) -> list[VaultChunk]:
    """Split a vault .md body into chunks by heading hierarchy.

    Each top-level section becomes one chunk. Oversized sections are split
    by blank-line paragraphs into max_words buckets. Returns [] for empty body.
    """
    if not body.strip():
        return []

    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    sections: list[tuple[str, str]] = []   # (heading_path, content)
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    last_end = 0

    for m in heading_re.finditer(body):
        content = body[last_end : m.start()].strip()
        if content:
            sections.append((_build_path(heading_stack), content))

        level = len(m.group(1))
        title = m.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        last_end = m.end() + 1

    remaining = body[last_end:].strip()
    if remaining:
        sections.append((_build_path(heading_stack), remaining))

    chunks: list[VaultChunk] = []
    chunk_idx = 0

    for heading_path, content in sections:
        words = content.split()
        if len(words) <= max_words:
            chunks.append(VaultChunk(chunk_index=chunk_idx, heading_path=heading_path, text=content))
            chunk_idx += 1
        else:
            paragraphs = re.split(r"\n\s*\n", content)
            bucket: list[str] = []
            bucket_words = 0
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                pw = len(para.split())
                if bucket_words + pw > max_words and bucket:
                    chunks.append(VaultChunk(chunk_index=chunk_idx, heading_path=heading_path,
                                             text="\n\n".join(bucket)))
                    chunk_idx += 1
                    bucket = [para]
                    bucket_words = pw
                else:
                    bucket.append(para)
                    bucket_words += pw
            if bucket:
                chunks.append(VaultChunk(chunk_index=chunk_idx, heading_path=heading_path,
                                         text="\n\n".join(bucket)))
                chunk_idx += 1

    return chunks


def _build_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)
