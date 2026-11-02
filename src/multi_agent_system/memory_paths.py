"""Host-controlled deny roots for ordinary Python IO, not an OS sandbox."""
from pathlib import Path

APPLICATION_MEMORY_ROOT = Path(__file__).resolve().parents[2] / ".agentg" / "memory"


def protected_memory_roots(db_path: str | Path | None = None) -> list[str]:
    roots = [APPLICATION_MEMORY_ROOT.resolve()]
    if db_path is not None:
        parent = Path(db_path).resolve().parent
        # The configured database parent is dedicated application data storage;
        # deny the whole directory, including WAL/SHM, backups and renamed files.
        roots.append(parent)
    return list(dict.fromkeys(str(path) for path in roots))


def is_memory_path(path: str | Path, roots: list[str]) -> bool:
    target = Path(path).resolve()
    return any(target == Path(root) or Path(root) in target.parents for root in roots)


def configured_memory_roots(settings) -> list[str]:
    """Disabling retrieval does not expose an already configured private store."""
    from .memory_store import resolve_memory_db_path
    try:
        path = resolve_memory_db_path(settings.memory_db_path)
    except ValueError:
        if settings.long_term_memory_enabled:
            raise
        path = None  # Unused malformed configuration must not enable file IO.
    return protected_memory_roots(path)
