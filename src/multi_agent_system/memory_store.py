"""Independent, versioned SQLite repository for long-term memory (L1.2).

No model, Redis, dotenv or graph is accessed. Callers supply a host identity;
matching coordinates are not authentication or proof of source ownership.
Each operation owns and closes its connection. Immutable versions and their
sources are committed atomically with a current-version pointer. This is NOT
a distributed transaction with Redis or an automatic memory-writing workflow.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol

from pydantic import TypeAdapter, ValidationError

from .memory_records import (
    Identifier, MemoryIdentity, MemoryKind, MemoryNamespace, MemoryRecord, MemorySourceRef,
    MemoryStatus, PositiveVersion, validate_memory_successor,
)

if TYPE_CHECKING:
    from .config import Settings


APPLICATION_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
APPLICATION_ID = 0x41474D31  # AGM1: refuse accidentally opening another database.
MAX_QUERY_LIMIT = 1000
_ID = TypeAdapter(Identifier)
_VERSION = TypeAdapter(PositiveVersion)


class MemoryStoreError(RuntimeError):
    """Explicit storage failure; never represented as an empty retrieval."""


class MemoryStoreUnavailable(MemoryStoreError):
    pass


class MemoryStoreBusy(MemoryStoreUnavailable):
    pass


class MemoryStoreCorrupt(MemoryStoreError):
    pass


class MemorySchemaError(MemoryStoreError):
    pass


class MemoryConflict(MemoryStoreError):
    """Stale predecessor, duplicate identity or conflicting idempotency key."""


def _raise_sqlite_error(error: sqlite3.Error) -> None:
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    # Python 3.10 may lack exported error-code constants/attributes. Unknown
    # codes still produce an explicit unavailable error, not AttributeError.
    if code in {getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)}:
        raise MemoryStoreBusy("memory database lock wait exceeded") from None
    if code in {getattr(sqlite3, "SQLITE_CORRUPT", 11), getattr(sqlite3, "SQLITE_NOTADB", 26)}:
        raise MemoryStoreCorrupt("memory database is damaged or not SQLite") from None
    raise MemoryStoreUnavailable("SQLite memory operation failed") from None


class MemoryRepository(Protocol):
    def save(self, record: MemoryRecord, *, identity: MemoryIdentity) -> MemoryRecord: ...

    def save_batch(self, records: tuple[MemoryRecord, ...], *, identity: MemoryIdentity) -> list[MemoryRecord]: ...

    def conversation_records(self, *, identity: MemoryIdentity, thread_ids: tuple[str, ...],
                             include_promoted: bool = False) -> list[MemoryRecord]: ...

    def get(
        self, memory_id: str, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        version: int | None = None,
    ) -> MemoryRecord | None: ...

    def history(
        self, memory_id: str, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]: ...

    def query_active(
        self, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        at: datetime, kinds: tuple[MemoryKind, ...] | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]: ...

    def list_records(
        self, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        statuses: tuple[MemoryStatus, ...] | None = None,
        kinds: tuple[MemoryKind, ...] | None = None, limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]: ...

    def backup(self, destination: str | Path) -> Path: ...

    def close(self) -> None: ...


def resolve_memory_db_path(path: str | Path) -> Path:
    """Resolve relative configuration against the application root, not cwd."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("memory database path must be nonempty")
    if str(path) == ":memory:" or str(path).startswith("file:"):
        raise ValueError("memory repository requires a persistent filesystem path")
    target = Path(path)
    if not target.is_absolute():
        target = APPLICATION_ROOT / target
    target = target.resolve()
    # Check names only; never open a protected configuration file.
    if any(part.casefold() == ".env" or part.casefold().startswith(".env.") for part in target.parts):
        raise ValueError("protected configuration paths cannot be used for memory storage")
    return target


def _namespace_key(namespace: MemoryNamespace) -> str:
    return json.dumps(namespace.key(), ensure_ascii=False, separators=(",", ":"))


def _time_key(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory query time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be an integer in 1..{MAX_QUERY_LIMIT}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")


def _filter_values(values: tuple | None, enum_type: type) -> tuple[str, ...] | None:
    if values is None:
        return None
    if type(values) is not tuple or not values:
        raise ValueError("memory filters must be nonempty tuples of canonical values")
    result = []
    for value in values:
        if not isinstance(value, enum_type) and type(value) is not str:
            raise ValueError("memory filter has an invalid type")
        result.append(enum_type(value).value)
    if len(set(result)) != len(result):
        raise ValueError("duplicate memory filters are not allowed")
    return tuple(result)


_DDL = (
    """CREATE TABLE memory_versions (
        memory_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version >= 1),
        namespace_key TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, verification_status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        observed_at TEXT, expires_at TEXT, content_hash TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(memory_id, version), UNIQUE(namespace_key, idempotency_key)
    )""",
    """CREATE TABLE memory_heads (
        memory_id TEXT PRIMARY KEY, namespace_key TEXT NOT NULL,
        current_version INTEGER NOT NULL,
        FOREIGN KEY(memory_id, current_version) REFERENCES memory_versions(memory_id, version)
    )""",
    """CREATE TABLE memory_sources (
        memory_id TEXT NOT NULL, version INTEGER NOT NULL,
        source_index INTEGER NOT NULL, source_kind TEXT NOT NULL, source_json TEXT NOT NULL,
        PRIMARY KEY(memory_id, version, source_index),
        FOREIGN KEY(memory_id, version) REFERENCES memory_versions(memory_id, version)
    )""",
    "CREATE INDEX memory_filter_idx ON memory_versions(namespace_key, status, kind, expires_at)",
    "CREATE INDEX memory_head_namespace_idx ON memory_heads(namespace_key)",
    "CREATE INDEX memory_source_kind_idx ON memory_sources(source_kind, memory_id, version)",
)
_COLUMNS = {
    "memory_versions": {
        "memory_id", "version", "namespace_key", "scope", "kind", "status",
        "verification_status", "created_at", "updated_at", "observed_at", "expires_at",
        "content_hash", "idempotency_key", "record_json",
    },
    "memory_heads": {"memory_id", "namespace_key", "current_version"},
    "memory_sources": {"memory_id", "version", "source_index", "source_kind", "source_json"},
}


class SQLiteMemoryRepository:
    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5000):
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60000:
            raise ValueError("busy_timeout_ms must be an integer in 1..60000")
        self.db_path = resolve_memory_db_path(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._initializing = True
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise MemoryStoreUnavailable("memory database directory is unavailable") from None
        with self._connection() as connection:
            self._migrate(connection)
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise MemoryStoreUnavailable("SQLite WAL mode is unavailable")
            self._check_integrity(connection)
        self._initializing = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise MemoryStoreUnavailable("memory repository is closed")
        connection = None
        try:
            connection = sqlite3.connect(
                self.db_path.as_uri() + ("?mode=rwc" if self._initializing else "?mode=rw"),
                uri=True, timeout=self.busy_timeout_ms / 1000, isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        except sqlite3.Error as error:
            _raise_sqlite_error(error)
        except OSError:
            raise MemoryStoreUnavailable("memory database filesystem is unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        # No executescript: it commits implicitly, defeating migration atomicity.
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and app_id == 0 and not objects:
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or app_id != APPLICATION_ID:
                raise MemorySchemaError("unsupported or foreign memory database schema")
            SQLiteMemoryRepository._check_schema(connection)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        if (connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
            raise MemorySchemaError("unsupported or foreign memory database schema")
        for table, expected in _COLUMNS.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if columns != expected:
                raise MemorySchemaError("memory schema columns are incomplete or incompatible")
        # Verify uniqueness/FKs, not just the column names of a damaged schema.
        actual = {
            row[0]: " ".join(row[1].split()).lower()
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN "
                "('memory_versions','memory_heads','memory_sources')"
            )
        }
        for ddl in _DDL[:3]:
            table = ddl.split()[2]
            if actual.get(table) != " ".join(ddl.split()).lower():
                raise MemorySchemaError("memory schema constraints are incompatible")
        indexes = {row[0]: " ".join(row[1].split()).lower() for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )}
        for ddl in _DDL[3:]:
            if indexes.get(ddl.split()[2]) != " ".join(ddl.split()).lower():
                raise MemorySchemaError("memory schema indexes are incomplete or incompatible")

    @staticmethod
    def _check_integrity(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MemoryStoreCorrupt("memory database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MemoryStoreCorrupt("memory database references are inconsistent")
        invalid_head = connection.execute(
            "SELECT 1 FROM memory_heads h JOIN memory_versions v "
            "ON v.memory_id=h.memory_id AND v.version=h.current_version "
            "WHERE h.namespace_key<>v.namespace_key OR h.current_version <> "
            "(SELECT max(version) FROM memory_versions WHERE memory_id=h.memory_id) LIMIT 1"
        ).fetchone()
        orphan = connection.execute(
            "SELECT 1 FROM memory_versions v LEFT JOIN memory_heads h ON h.memory_id=v.memory_id "
            "WHERE h.memory_id IS NULL OR h.namespace_key<>v.namespace_key LIMIT 1"
        ).fetchone()
        if invalid_head is not None or orphan is not None:
            raise MemoryStoreCorrupt("memory current-version pointers are inconsistent")

    @staticmethod
    def _owned(namespace: MemoryNamespace, identity: MemoryIdentity) -> MemoryNamespace:
        return MemoryIdentity.model_validate(identity).validate_namespace(namespace)

    @staticmethod
    def _decode(row: sqlite3.Row, connection: sqlite3.Connection) -> MemoryRecord:
        try:
            record = MemoryRecord.model_validate_json(row["record_json"])
        except ValidationError:
            raise MemoryStoreCorrupt("stored memory record failed contract validation") from None
        expected = SQLiteMemoryRepository._indexed(record)
        if any(row[name] != value for name, value in expected.items()):
            raise MemoryStoreCorrupt("stored memory record and indexes disagree")
        sources = connection.execute(
            "SELECT source_index, source_kind, source_json FROM memory_sources "
            "WHERE memory_id=? AND version=? ORDER BY source_index",
            (record.memory_id, record.version),
        ).fetchall()
        expected_sources = [
            (i, source.kind.value, source.model_dump_json())
            for i, source in enumerate(record.source_refs)
        ]
        if [tuple(source) for source in sources] != expected_sources:
            raise MemoryStoreCorrupt("stored memory source associations disagree")
        return record

    @classmethod
    def _check_contents(cls, connection: sqlite3.Connection) -> None:
        """Validate a backup snapshot's contracts/chain without loading the whole DB."""
        connection.row_factory = sqlite3.Row
        previous = None
        for row in connection.execute("SELECT * FROM memory_versions ORDER BY memory_id, version"):
            record = cls._decode(row, connection)
            try:
                if previous is not None and record.memory_id == previous.memory_id:
                    validate_memory_successor(previous, record)
                elif record.version != 1:
                    raise ValueError("missing initial version")
            except ValueError:
                raise MemoryStoreCorrupt("stored memory version chain is inconsistent") from None
            previous = record

    @staticmethod
    def _indexed(record: MemoryRecord) -> dict:
        return {
            "memory_id": record.memory_id, "version": record.version,
            "namespace_key": _namespace_key(record.namespace), "scope": record.scope.value,
            "kind": record.kind.value, "status": record.status.value,
            "verification_status": record.verification_status.value,
            "created_at": _time_key(record.created_at), "updated_at": _time_key(record.updated_at),
            "observed_at": _time_key(record.observed_at) if record.observed_at else None,
            "expires_at": _time_key(record.expires_at) if record.expires_at else None,
            "content_hash": record.content_hash, "idempotency_key": record.idempotency_key,
        }

    def save(self, record: MemoryRecord, *, identity: MemoryIdentity) -> MemoryRecord:
        return self.save_batch((record,), identity=identity)[0]

    def save_batch(self, records: tuple[MemoryRecord, ...], *, identity: MemoryIdentity) -> list[MemoryRecord]:
        """Atomic SQLite batch; not a distributed transaction with Redis."""
        if type(records) is not tuple or len(records) > 1000:
            raise ValueError("memory batch must be a tuple of at most 1000 records")
        records = tuple(MemoryRecord.model_validate(record) for record in records)
        if len({record.memory_id for record in records}) != len(records):
            raise ValueError("duplicate memory identities in batch")
        for record in records:
            self._owned(record.namespace, identity)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                saved = [self._save_in_transaction(connection, record, identity) for record in records]
                connection.execute("COMMIT")
                return saved
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                raise MemoryConflict("memory uniqueness or reference constraint conflict") from None
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _save_in_transaction(self, connection, record, identity):
        record = MemoryRecord.model_validate(record)  # Revalidate unsafe model_copy.
        namespace = self._owned(record.namespace, identity)
        namespace_key = _namespace_key(namespace)
        payload = record.model_dump_json()
        replay = connection.execute(
            "SELECT * FROM memory_versions WHERE namespace_key=? AND idempotency_key=?",
            (namespace_key, record.idempotency_key),
        ).fetchone()
        if replay is not None:
            stored = self._decode(replay, connection)
            if stored.model_dump_json() != payload:
                raise MemoryConflict("idempotency key already has a different payload")
            return stored
        head = connection.execute("SELECT * FROM memory_heads WHERE memory_id=?", (record.memory_id,)).fetchone()
        if head is None:
            if record.version != 1:
                raise MemoryConflict("memory predecessor does not exist")
        else:
            if head["namespace_key"] != namespace_key:
                raise MemoryConflict("memory identity already belongs to another namespace")
            previous_row = connection.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND version=?",
                (record.memory_id, head["current_version"]),
            ).fetchone()
            if previous_row is None:
                raise MemoryStoreCorrupt("current memory version is missing")
            try:
                validate_memory_successor(self._decode(previous_row, connection), record)
            except ValueError:
                raise MemoryConflict("memory predecessor, identity or lifecycle conflict") from None
        fields = {**self._indexed(record), "record_json": payload}
        names = tuple(fields)
        connection.execute(
            f"INSERT INTO memory_versions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            tuple(fields.values()),
        )
        connection.executemany(
            "INSERT INTO memory_sources VALUES (?,?,?,?,?)",
            [(record.memory_id, record.version, i, source.kind.value, source.model_dump_json())
             for i, source in enumerate(record.source_refs)],
        )
        if head is None:
            connection.execute("INSERT INTO memory_heads VALUES (?,?,?)", (record.memory_id, namespace_key, record.version))
        elif connection.execute(
                "UPDATE memory_heads SET current_version=? WHERE memory_id=? AND current_version=?",
                (record.version, record.memory_id, head["current_version"])).rowcount != 1:
            raise MemoryConflict("current memory version changed concurrently")
        return record

    def conversation_records(self, *, identity: MemoryIdentity, thread_ids: tuple[str, ...],
                             include_promoted: bool = False) -> list[MemoryRecord]:
        """Management-only association scan; includes historical provenance.

        Bounded fail-closed scan, not a relevance query. Never used by Workers.
        """
        identity = MemoryIdentity.model_validate(identity)
        if not identity.conversation_id or type(thread_ids) is not tuple or type(include_promoted) is not bool:
            raise ValueError("conversation binding and canonical options required")
        threads = {_ID.validate_python(item) for item in thread_ids}
        result = []
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT v.* FROM memory_heads h JOIN memory_versions v "
                "ON v.memory_id=h.memory_id AND v.version=h.current_version ORDER BY v.memory_id LIMIT 10001")
            for index, row in enumerate(rows):
                if index == 10000:
                    raise MemoryStoreUnavailable("conversation cleanup scan limit exceeded")
                record = self._decode(row, connection)
                try:
                    identity.validate_namespace(record.namespace)
                except ValueError:
                    continue
                private = record.namespace.conversation_id == identity.conversation_id
                promoted = include_promoted and record.scope.value in {"user", "project"}
                if not private and not promoted:
                    continue
                if not private:
                    # A corrected head may no longer mention the original run.
                    sources = connection.execute("SELECT source_json FROM memory_sources WHERE memory_id=?",
                                                 (record.memory_id,))
                    associated = False
                    for source in sources:
                        try:
                            ref = MemorySourceRef.model_validate_json(source[0])
                        except ValidationError:
                            raise MemoryStoreCorrupt("stored memory source is invalid") from None
                        if ref.thread_id in threads or (ref.artifact_ref is not None and ref.artifact_ref.thread_id in threads):
                            associated = True
                            break
                    if not associated:
                        continue
                if record.status not in {MemoryStatus.DELETED, MemoryStatus.SUPERSEDED}:
                    result.append(record)
                if len(result) > 1000:
                    raise MemoryStoreUnavailable("conversation cleanup batch limit exceeded")
        return result

    def get(
        self, memory_id: str, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        version: int | None = None,
    ) -> MemoryRecord | None:
        memory_id = _ID.validate_python(memory_id)
        namespace = self._owned(namespace, identity)
        namespace_key = _namespace_key(namespace)
        if version is not None:
            version = _VERSION.validate_python(version)
        with self._connection() as connection:
            connection.execute("BEGIN")  # Consistent record/source snapshot.
            if version is None:
                row = connection.execute(
                    "SELECT v.* FROM memory_heads h JOIN memory_versions v "
                    "ON v.memory_id=h.memory_id AND v.version=h.current_version "
                    "WHERE h.memory_id=? AND h.namespace_key=?", (memory_id, namespace_key),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM memory_versions WHERE memory_id=? AND namespace_key=? AND version=?",
                    (memory_id, namespace_key, version),
                ).fetchone()
            record = self._decode(row, connection) if row is not None else None
            if record is not None and record.namespace != namespace:
                raise MemoryStoreCorrupt("memory head namespace and version disagree")
            return record

    def history(
        self, memory_id: str, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]:
        memory_id = _ID.validate_python(memory_id)
        namespace_key = _namespace_key(self._owned(namespace, identity))
        _page(limit, offset)
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND namespace_key=? "
                "ORDER BY version LIMIT ? OFFSET ?", (memory_id, namespace_key, limit, offset),
            ).fetchall()
            return [self._decode(row, connection) for row in rows]

    def query_active(
        self, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        at: datetime, kinds: tuple[MemoryKind, ...] | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]:
        """Only current active, verified, time-valid versions; NOT relevance ranking."""
        _time_key(at)  # None/naive values cannot bypass the effectiveness filter.
        return self._query(namespace, identity, (MemoryStatus.ACTIVE,), kinds, at, limit, offset)

    def list_records(
        self, *, namespace: MemoryNamespace, identity: MemoryIdentity,
        statuses: tuple[MemoryStatus, ...] | None = None,
        kinds: tuple[MemoryKind, ...] | None = None, limit: int = 100, offset: int = 0,
    ) -> list[MemoryRecord]:
        """Explicit management view, including candidates/expired/deleted if requested."""
        return self._query(namespace, identity, statuses, kinds, None, limit, offset)

    def _query(self, namespace, identity, statuses, kinds, at, limit, offset):
        namespace = self._owned(namespace, identity)
        namespace_key = _namespace_key(namespace)
        _page(limit, offset)
        status_values = _filter_values(statuses, MemoryStatus)
        kind_values = _filter_values(kinds, MemoryKind)
        clauses = ["h.namespace_key=?"]
        parameters: list = [namespace_key]
        for field, values in (("status", status_values), ("kind", kind_values)):
            if values is not None:
                clauses.append(f"v.{field} IN ({','.join('?' for _ in values)})")
                parameters.extend(values)
        if at is not None:
            now = _time_key(at)
            clauses.extend([
                "v.verification_status <> 'unverified'", "v.created_at<=?",
                "(v.observed_at IS NULL OR v.observed_at<=?)",
                "(v.expires_at IS NULL OR v.expires_at>?)",
            ])
            parameters.extend([now, now, now])
        parameters.extend([limit, offset])
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT v.* FROM memory_heads h JOIN memory_versions v "
                "ON v.memory_id=h.memory_id AND v.version=h.current_version WHERE "
                + " AND ".join(clauses) + " ORDER BY v.updated_at DESC, v.memory_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            records = [self._decode(row, connection) for row in rows]
            if any(record.namespace != namespace for record in records):
                raise MemoryStoreCorrupt("memory head namespace and version disagree")
            if at is not None and any(not record.is_usable_at(at) for record in records):
                raise MemoryStoreCorrupt("memory eligibility indexes are inconsistent")
            return records

    def backup(self, destination: str | Path) -> Path:
        """SQLite online backup includes committed WAL data; destination must be new."""
        target = resolve_memory_db_path(destination)
        if target == self.db_path:
            raise ValueError("backup destination must differ from the source")
        deadline = time.monotonic() + self.busy_timeout_ms / 1000

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise MemoryStoreBusy("memory backup time limit exceeded")

        with self._connection() as source:
            self._check_integrity(source)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb"):
                    pass  # Reserve without overwriting any existing user database.
            except FileExistsError:
                raise ValueError("backup destination already exists") from None
            except OSError:
                raise MemoryStoreUnavailable("memory backup destination is unavailable") from None
            try:
                with closing(sqlite3.connect(str(target), isolation_level=None)) as output:
                    source.backup(output, pages=128, progress=progress, sleep=0.01)
                    self._check_schema(output)
                    self._check_integrity(output)
                    self._check_contents(output)
            except Exception:
                # Remove only the just-reserved incomplete backup, never the source.
                target.unlink(missing_ok=True)
                raise
        return target

    @classmethod
    def restore(
        cls, backup_path: str | Path, destination: str | Path, *, busy_timeout_ms: int = 5000,
    ) -> SQLiteMemoryRepository:
        """Validate/read an existing backup and restore into a NEW database only."""
        source = resolve_memory_db_path(backup_path)
        if not source.is_file():
            raise MemoryStoreUnavailable("memory backup does not exist")
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60000:
            raise ValueError("busy_timeout_ms must be an integer in 1..60000")
        target = resolve_memory_db_path(destination)
        if target == source:
            raise ValueError("restore destination must differ from the backup")
        reserved = False
        completed = False
        deadline = time.monotonic() + busy_timeout_ms / 1000

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise MemoryStoreBusy("memory restore time limit exceeded")

        try:
            # Read-only inspection/copy: no migration or writes to the backup.
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
                if (connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                        or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
                    raise MemorySchemaError("unsupported memory backup schema")
                cls._check_schema(connection)
                cls._check_integrity(connection)
                cls._check_contents(connection)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open("xb"):
                        pass
                except FileExistsError:
                    raise ValueError("restore destination already exists") from None
                reserved = True
                with closing(sqlite3.connect(str(target), isolation_level=None)) as output:
                    connection.backup(output, pages=128, progress=progress, sleep=0.01)
                    cls._check_schema(output)
                    cls._check_integrity(output)
                    cls._check_contents(output)
                completed = True
        except sqlite3.Error as error:
            _raise_sqlite_error(error)
        except OSError:
            raise MemoryStoreUnavailable("memory restore filesystem is unavailable") from None
        finally:
            # Success is distinguished by the validated copy, not file existence.
            if reserved and not completed:
                target.unlink(missing_ok=True)
        return cls(destination, busy_timeout_ms=busy_timeout_ms)

    def close(self) -> None:
        # No pooled/open connections survive an operation.
        self._closed = True

    def __enter__(self) -> SQLiteMemoryRepository:
        if self._closed:
            raise MemoryStoreUnavailable("memory repository is closed")
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def create_memory_repository(settings: Settings) -> SQLiteMemoryRepository | None:
    """Explicit factory. Importing/configuring alone performs no database IO."""
    if not settings.long_term_memory_enabled:
        return None
    return SQLiteMemoryRepository(
        settings.memory_db_path, busy_timeout_ms=settings.memory_db_busy_timeout_ms,
    )
