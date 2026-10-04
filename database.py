"""
MongoDB Database Module for Railway.com Deployment
with automatic SQLite fallback when MongoDB is unavailable.
"""

import os
import sys
import json
import types
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [DB] %(message)s"
)
logger = logging.getLogger(__name__)

_IS_SQLITE_MODE = False
_IS_MONGODB_MODE = True
_DB_BACKEND = None  # "mongodb" or "sqlite"


# ═══════════════════════════════════════════════════════════════
# psycopg2 MOCKS (unchanged)
# ═══════════════════════════════════════════════════════════════
def _setup_psycopg2_mocks():
    try:
        import psycopg2, psycopg2.pool, psycopg2.extensions, psycopg2.extras
        logger.info("psycopg2 found - using MongoDB/SQLite backend (psycopg2 mocked)")
        return False
    except ImportError:
        logger.info("psycopg2 not found - injecting mocks for compatibility")
        class MockModule(types.ModuleType):
            def __getattr__(self, name):
                if name == "RealDictCursor": return dict
                if name == "STATUS_READY": return 0
                if name == "ThreadedConnectionPool": return object
                return object
        mock_modules = {
            "psycopg2": MockModule("psycopg2"),
            "psycopg2.pool": MockModule("psycopg2.pool"),
            "psycopg2.extensions": MockModule("psycopg2.extensions"),
            "psycopg2.extras": MockModule("psycopg2.extras"),
        }
        mock_modules["psycopg2"].pool = mock_modules["psycopg2.pool"]
        mock_modules["psycopg2"].extensions = mock_modules["psycopg2.extensions"]
        mock_modules["psycopg2"].extras = mock_modules["psycopg2.extras"]
        mock_modules["psycopg2"].connect = lambda *a, **k: _create_mock_connection(*a, **k)
        for module_name, module_obj in mock_modules.items():
            sys.modules[module_name] = module_obj
        return True

_setup_psycopg2_mocks()


# ═══════════════════════════════════════════════════════════════
# MONGODB CONFIG
# ═══════════════════════════════════════════════════════════════
DB_CONFIG = {
    "host": os.environ.get("MONGODB_HOST", "localhost"),
    "database": os.environ.get("MONGODB_DB", "freshbot"),
    "user": os.environ.get("MONGODB_USER", ""),
    "password": os.environ.get("MONGODB_PASSWORD", ""),
    "port": int(os.environ.get("MONGODB_PORT", "27017")),
}


def _get_mongodb_uri() -> str:
    for env_var in ["MONGODB_URI", "MONGO_URL", "MONGODB_URL"]:
        uri = os.environ.get(env_var)
        if uri:
            logger.info(f"Using MongoDB URI from {env_var}")
            return uri
    db_url = os.environ.get("DATABASE_URL")
    if db_url and db_url.startswith("mongodb"):
        logger.info("Using MongoDB URI from DATABASE_URL")
        return db_url
    fallback = "mongodb://localhost:27017/freshbot"
    logger.warning(f"No MongoDB URI found in env, using fallback: {fallback}")
    return fallback


def _get_db_name() -> str:
    uri = _get_mongodb_uri()
    match = re.search(r"/([^/?]+)(\?|$)", uri)
    return match.group(1) if match else "freshbot"


_mongo_client = None
_mongo_db = None


def _get_mongo_client():
    global _mongo_client, _mongo_db
    if _mongo_client is not None:
        return _mongo_client, _mongo_db
    from pymongo import MongoClient
    import certifi
    uri = _get_mongodb_uri()
    needs_tls = "mongodb+srv" in uri or "tls=true" in uri or "ssl=true" in uri
    client = MongoClient(
        uri,
        maxPoolSize=100, minPoolSize=10, maxIdleTimeMS=45000,
        serverSelectionTimeoutMS=5000,
        tlsCAFile=certifi.where() if needs_tls else None
    )
    client.admin.command("ping")
    db_name = _get_db_name()
    db = client[db_name]
    _mongo_client = client
    _mongo_db = db
    logger.info(f"MongoDB connected successfully to database: {db_name}")
    return client, db


# ═══════════════════════════════════════════════════════════════
# SQLITE BACKEND
# ═══════════════════════════════════════════════════════════════
_SQLITE_LOCK = threading.Lock()
_sqlite_conn: Optional[sqlite3.Connection] = None
_sqlite_db_shim: Optional["SQLiteDBShim"] = None


# Column schemas: (col_name, sql_type)
_SQLITE_TABLE_SCHEMAS: Dict[str, List[Tuple[str, str]]] = {
    "users": [
        ("user_id", "INTEGER PRIMARY KEY"),
        ("username", "TEXT"),
        ("first_name", "TEXT"),
        ("credits", "INTEGER DEFAULT 150"),
        ("is_premium", "INTEGER DEFAULT 0"),
        ("premium_expiry", "TEXT"),
        ("cc_checked", "INTEGER DEFAULT 0"),
        ("cc_charged", "INTEGER DEFAULT 0"),
        ("joined_at", "TEXT"),
    ],
    "proxies": [
        ("user_id", "INTEGER"),
        ("proxy", "TEXT"),
        ("added_at", "TEXT"),
        ("PRIMARY KEY (user_id, proxy)", ""),
    ],
    "receipts": [
        ("receipt_id", "TEXT PRIMARY KEY"),
        ("user_id", "INTEGER"),
        ("amount", "REAL"),
        ("currency", "TEXT"),
        ("metadata", "TEXT"),
        ("plan", "TEXT"),
        ("purchased_on", "TEXT"),
        ("expires_on", "TEXT"),
        ("created_at", "TEXT"),
    ],
    "codes": [
        ("code", "TEXT PRIMARY KEY"),
        ("duration_days", "INTEGER"),
        ("max_uses", "INTEGER DEFAULT 1"),
        ("claimed_by", "INTEGER"),
        ("claimed_at", "TEXT"),
        ("created_at", "TEXT"),
    ],
    "plan_keys": [
        ("key", "TEXT PRIMARY KEY"),
        ("duration_days", "INTEGER"),
        ("duration_hours", "INTEGER"),
        ("max_uses", "INTEGER DEFAULT 1"),
        ("claimed_by", "INTEGER"),
        ("claimed_at", "TEXT"),
        ("created_at", "TEXT"),
    ],
    "gate_status": [
        ("gate", "TEXT PRIMARY KEY"),
        ("is_enabled", "INTEGER DEFAULT 1"),
        ("updated_at", "TEXT"),
    ],
    "banned_users": [
        ("user_id", "INTEGER PRIMARY KEY"),
        ("reason", "TEXT"),
        ("banned_at", "TEXT"),
    ],
    "user_sites": [
        ("user_id", "INTEGER"),
        ("url", "TEXT"),
        ("price", "REAL DEFAULT 0"),
        ("added_at", "TEXT"),
        ("PRIMARY KEY (user_id, url)", ""),
    ],
    "pending_feedback": [
        ("_id", "TEXT PRIMARY KEY"),
        ("created_at", "TEXT"),
        ("_data", "TEXT"),  # JSON blob for arbitrary fields
    ],
    "settings": [
        ("key", "TEXT PRIMARY KEY"),
        ("value", "TEXT"),  # JSON-encoded
        ("updated_at", "TEXT"),
    ],
    "global_sites": [
        ("url", "TEXT PRIMARY KEY"),
        ("added_at", "TEXT"),
    ],
    "eren_generators": [
        ("user_id", "INTEGER PRIMARY KEY"),
        ("granted_at", "TEXT"),
    ],
}

_STATS_TABLE_SCHEMA = [
    ("_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id", "INTEGER"),
    ("status", "TEXT"),
    ("timestamp", "TEXT"),
    ("extra", "TEXT"),
]


def _sqlite_create_table(conn: sqlite3.Connection, name: str):
    """Create a table if it doesn't exist yet."""
    if name.endswith("_stats") and name not in _SQLITE_TABLE_SCHEMAS:
        cols = _STATS_TABLE_SCHEMA
    else:
        cols = _SQLITE_TABLE_SCHEMAS.get(name)
    if cols is None:
        # Unknown collection: create a default table with an INTEGER _id
        sql = f'CREATE TABLE IF NOT EXISTS "{name}" (_id INTEGER PRIMARY KEY AUTOINCREMENT)'
        conn.execute(sql)
        return
    pieces = []
    for cname, ctype in cols:
        if cname.startswith("PRIMARY KEY") or cname.startswith("UNIQUE") or cname.startswith("FOREIGN"):
            pieces.append(cname)
        else:
            pieces.append(f'"{cname}" {ctype}'.strip())
    sql = f'CREATE TABLE IF NOT EXISTS "{name}" ({", ".join(pieces)})'
    conn.execute(sql)


def _get_sqlite_conn() -> sqlite3.Connection:
    global _sqlite_conn
    if _sqlite_conn is not None:
        return _sqlite_conn
    db_path = os.environ.get("SQLITE_PATH", "freshbot.db")
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Register Postgres-compat SQL functions so `DEFAULT NOW()` / `NOW()`
    # in DDL and DML work without modification.
    conn.create_function(
        "NOW", 0,
        lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    conn.create_function(
        "CURRENT_TIMESTAMP_PG", 0,
        lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    with _SQLITE_LOCK:
        for name in list(_SQLITE_TABLE_SCHEMAS.keys()):
            _sqlite_create_table(conn, name)
        for gate in ["ST", "STR", "PF", "VBV", "FT", "BL", "PP", "AT", "PW", "PYU"]:
            _sqlite_create_table(conn, f"{gate}_stats")
        conn.commit()
    _sqlite_conn = conn
    logger.info(f"SQLite fallback connected: {db_path}")
    return conn


# ───────────────────────────────────────────────────────────────
# MongoDB-filter → SQL WHERE translator (used by the shim)
# ───────────────────────────────────────────────────────────────
def _adapt_sqlite_value(val):
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    return val


def _deadapt_sqlite_value(val):
    if isinstance(val, str):
        if len(val) >= 10 and val[4] == "-" and val[7] == "-" and "T" in val:
            try:
                return datetime.fromisoformat(val)
            except Exception:
                pass
        if (val.startswith("{") and val.endswith("}")) or (val.startswith("[") and val.endswith("]")):
            try:
                return json.loads(val)
            except Exception:
                pass
    return val


def _filter_to_where(filter_dict: Optional[dict]) -> Tuple[str, list]:
    """Convert a pymongo-style filter dict into a SQL WHERE clause + params."""
    if not filter_dict:
        return "1=1", []
    clauses: List[str] = []
    params: List[Any] = []
    for key, val in filter_dict.items():
        if key == "$or":
            sub = []
            for item in val:
                sc, sp = _filter_to_where(item)
                sub.append(f"({sc})")
                params.extend(sp)
            clauses.append("(" + " OR ".join(sub) + ")")
            continue
        if key == "$and":
            sub = []
            for item in val:
                sc, sp = _filter_to_where(item)
                sub.append(f"({sc})")
                params.extend(sp)
            clauses.append("(" + " AND ".join(sub) + ")")
            continue
        if isinstance(val, dict):
            for op, opval in val.items():
                if op == "$eq":
                    if opval is None:
                        clauses.append(f'"{key}" IS NULL')
                    else:
                        clauses.append(f'"{key}" = ?')
                        params.append(_adapt_sqlite_value(opval))
                elif op == "$ne":
                    if opval is None:
                        clauses.append(f'"{key}" IS NOT NULL')
                    else:
                        clauses.append(f'("{key}" != ? OR "{key}" IS NULL)')
                        params.append(_adapt_sqlite_value(opval))
                elif op == "$gt":
                    clauses.append(f'"{key}" > ?')
                    params.append(_adapt_sqlite_value(opval))
                elif op == "$gte":
                    clauses.append(f'"{key}" >= ?')
                    params.append(_adapt_sqlite_value(opval))
                elif op == "$lt":
                    clauses.append(f'"{key}" < ?')
                    params.append(_adapt_sqlite_value(opval))
                elif op == "$lte":
                    clauses.append(f'"{key}" <= ?')
                    params.append(_adapt_sqlite_value(opval))
                elif op == "$in":
                    if not opval:
                        clauses.append("0=1")
                        continue
                    ph = ",".join(["?"] * len(opval))
                    clauses.append(f'"{key}" IN ({ph})')
                    params.extend([_adapt_sqlite_value(v) for v in opval])
                elif op == "$nin":
                    if not opval:
                        continue
                    ph = ",".join(["?"] * len(opval))
                    clauses.append(f'("{key}" NOT IN ({ph}) OR "{key}" IS NULL)')
                    params.extend([_adapt_sqlite_value(v) for v in opval])
                elif op == "$regex":
                    pattern = str(opval).strip("^$")
                    pattern = pattern.replace(".*", "%").replace(".", "_")
                    clauses.append(f'"{key}" LIKE ?')
                    params.append(pattern)
                elif op == "$type":
                    continue
                else:
                    continue
        elif val is None:
            clauses.append(f'"{key}" IS NULL')
        else:
            clauses.append(f'"{key}" = ?')
            params.append(_adapt_sqlite_value(val))
    if not clauses:
        return "1=1", []
    return " AND ".join(clauses), params


# ───────────────────────────────────────────────────────────────
# SQLite result shims
# ───────────────────────────────────────────────────────────────
class _SQLiteUpdateResult:
    def __init__(self, modified_count=0, upserted_id=None, matched_count=0):
        self.modified_count = modified_count
        self.matched_count = matched_count
        self.upserted_id = upserted_id


class _SQLiteInsertResult:
    def __init__(self, inserted_id=None):
        self.inserted_id = inserted_id


class _SQLiteDeleteResult:
    def __init__(self, deleted_count=0):
        self.deleted_count = deleted_count


class _SQLiteFindCursor:
    """Minimal cursor mimicking pymongo's find() return value."""

    def __init__(self, coll: "SQLiteCollectionShim", filter_dict, projection):
        self._coll = coll
        self._filter = filter_dict or {}
        self._projection = projection
        self._sort = None
        self._limit = None
        self._skip = None

    def sort(self, spec):
        self._sort = spec
        return self

    def limit(self, n):
        self._limit = n
        return self

    def skip(self, n):
        self._skip = n
        return self

    def _build_sql(self):
        where, params = _filter_to_where(self._filter)
        sql = f'SELECT * FROM "{self._coll.name}" WHERE {where}'
        if self._sort:
            order = ", ".join(
                f'"{f}" {"DESC" if d == -1 else "ASC"}' for f, d in self._sort
            )
            sql += f" ORDER BY {order}"
        if self._limit is not None:
            sql += f" LIMIT {int(self._limit)}"
        if self._skip is not None:
            sql += f" OFFSET {int(self._skip)}"
        return sql, params

    def __iter__(self):
        sql, params = self._build_sql()
        with _SQLITE_LOCK:
            cur = self._coll.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        for row in rows:
            doc = self._coll._row_to_doc(cols, row)
            if self._projection:
                doc = self._coll._apply_projection(doc, self._projection)
            yield doc


# ───────────────────────────────────────────────────────────────
# SQLite collection shim
# ───────────────────────────────────────────────────────────────
class SQLiteCollectionShim:
    def __init__(self, conn: sqlite3.Connection, name: str):
        self.conn = conn
        self.name = name

    # ---- helpers -------------------------------------------------
    def _table_exists(self) -> bool:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.name,),
        )
        return cur.fetchone() is not None

    def _existing_columns(self) -> List[str]:
        if not self._table_exists():
            _sqlite_create_table(self.conn, self.name)
            self.conn.commit()
        cur = self.conn.execute(f'PRAGMA table_info("{self.name}")')
        return [row[1] for row in cur.fetchall()]

    def _ensure_columns(self, doc: dict):
        existing = set(self._existing_columns())
        for col in doc.keys():
            if col not in existing and not col.startswith("$"):
                if self.name == "pending_feedback" and col not in ("_id", "created_at", "_data"):
                    continue
                try:
                    self.conn.execute(
                        f'ALTER TABLE "{self.name}" ADD COLUMN "{col}" TEXT'
                    )
                except sqlite3.OperationalError:
                    pass

    def _row_to_doc(self, cols, row) -> Dict[str, Any]:
        doc: Dict[str, Any] = {}
        for c, v in zip(cols, row):
            doc[c] = _deadapt_sqlite_value(v)
        if self.name == "pending_feedback" and "_data" in doc:
            blob = doc.pop("_data")
            if isinstance(blob, dict):
                for k, v in blob.items():
                    doc.setdefault(k, v)
        return doc

    def _apply_projection(self, doc: dict, projection: dict) -> dict:
        include = [k for k, v in projection.items() if v and k != "_id"]
        if include:
            out = {k: doc[k] for k in include if k in doc}
            if projection.get("_id", 1) and "_id" in doc:
                out["_id"] = doc["_id"]
            return out
        out = dict(doc)
        for k, v in projection.items():
            if not v and k in out and k != "_id":
                out.pop(k, None)
        if projection.get("_id", 1) == 0:
            out.pop("_id", None)
        return out

    # ---- CRUD ----------------------------------------------------
    def find_one(self, filter_dict=None, projection=None, sort=None):
        cur = self.find(filter_dict or {}, projection)
        if sort:
            cur = cur.sort(sort)
        cur = cur.limit(1)
        for doc in cur:
            return doc
        return None

    def find(self, filter_dict=None, projection=None):
        return _SQLiteFindCursor(self, filter_dict or {}, projection)

    def count_documents(self, filter_dict=None):
        where, params = _filter_to_where(filter_dict or {})
        cur = self.conn.execute(
            f'SELECT COUNT(*) FROM "{self.name}" WHERE {where}', params
        )
        return cur.fetchone()[0]

    def insert_one(self, doc: dict):
        doc = dict(doc)
        if self.name == "pending_feedback":
            data = {k: v for k, v in doc.items() if k not in ("_id", "created_at", "_data")}
            row = {
                "_id": doc.get("_id"),
                "created_at": _adapt_sqlite_value(doc.get("created_at")),
                "_data": json.dumps(data, default=str),
            }
            cols = list(row.keys())
            ph = ",".join(["?"] * len(cols))
            sql = f'INSERT OR REPLACE INTO "{self.name}" ({",".join(cols)}) VALUES ({ph})'
            with _SQLITE_LOCK:
                cur = self.conn.execute(sql, [row[c] for c in cols])
                self.conn.commit()
            return _SQLiteInsertResult(inserted_id=cur.lastrowid)
        self._ensure_columns(doc)
        cols = [k for k in doc.keys() if not k.startswith("$")]
        vals = [_adapt_sqlite_value(doc[k]) for k in cols]
        ph = ",".join(["?"] * len(cols))
        sql = f'INSERT INTO "{self.name}" ({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({ph})'
        with _SQLITE_LOCK:
            cur = self.conn.execute(sql, vals)
            self.conn.commit()
        return _SQLiteInsertResult(inserted_id=cur.lastrowid)

    def replace_one(self, filter_dict: dict, doc: dict, upsert: bool = False):
        where, params = _filter_to_where(filter_dict)
        with _SQLITE_LOCK:
            self.conn.execute(f'DELETE FROM "{self.name}" WHERE {where}', params)
            self.conn.commit()
        return self.insert_one(doc)

    def _apply_update(self, filter_dict, update_op, upsert: bool) -> _SQLiteUpdateResult:
        set_dict = dict(update_op.get("$set", {}))
        inc_dict = dict(update_op.get("$inc", {}))
        set_on_insert = dict(update_op.get("$setOnInsert", {}))

        where, params = _filter_to_where(filter_dict)

        if set_dict or inc_dict:
            parts = []
            upd_params = []
            for k, v in set_dict.items():
                parts.append(f'"{k}" = ?')
                upd_params.append(_adapt_sqlite_value(v))
            for k, v in inc_dict.items():
                parts.append(f'"{k}" = COALESCE("{k}", 0) + ?')
                upd_params.append(_adapt_sqlite_value(v))
            sql = f'UPDATE "{self.name}" SET {", ".join(parts)} WHERE {where}'
            with _SQLITE_LOCK:
                cur = self.conn.execute(sql, upd_params + params)
                modified = cur.rowcount
                self.conn.commit()
            if modified > 0:
                return _SQLiteUpdateResult(modified_count=modified, matched_count=modified)

        if not upsert:
            return _SQLiteUpdateResult(0)

        insert_doc: Dict[str, Any] = {}
        insert_doc.update(set_on_insert)
        insert_doc.update(set_dict)
        for k, v in inc_dict.items():
            insert_doc.setdefault(k, v)
        for k, v in (filter_dict or {}).items():
            if k.startswith("$"):
                continue
            if isinstance(v, dict):
                continue
            insert_doc.setdefault(k, v)

        self._ensure_columns(insert_doc)
        cols = [c for c in insert_doc.keys() if not c.startswith("$")]
        vals = [_adapt_sqlite_value(insert_doc[c]) for c in cols]
        ph = ",".join(["?"] * len(cols))
        sql = (
            f'INSERT OR IGNORE INTO "{self.name}" '
            f'({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({ph})'
        )
        with _SQLITE_LOCK:
            cur = self.conn.execute(sql, vals)
            self.conn.commit()
        if cur.rowcount > 0:
            return _SQLiteUpdateResult(modified_count=0, upserted_id="new")
        return _SQLiteUpdateResult(0)

    def update_one(self, filter_dict, update_op, upsert: bool = False):
        return self._apply_update(filter_dict, update_op, upsert)

    def update_many(self, filter_dict, update_op):
        return self._apply_update(filter_dict, update_op, upsert=False)

    def find_one_and_update(self, filter_dict, update_op, return_document=True, **kwargs):
        res = self._apply_update(filter_dict, update_op, upsert=False)
        if res.modified_count > 0:
            simple = {k: v for k, v in (filter_dict or {}).items() if not isinstance(v, dict)}
            return self.find_one(simple)
        return None

    def delete_one(self, filter_dict):
        where, params = _filter_to_where(filter_dict)
        with _SQLITE_LOCK:
            cur = self.conn.execute(f'DELETE FROM "{self.name}" WHERE {where}', params)
            self.conn.commit()
        return _SQLiteDeleteResult(cur.rowcount)

    def delete_many(self, filter_dict):
        where, params = _filter_to_where(filter_dict)
        with _SQLITE_LOCK:
            cur = self.conn.execute(f'DELETE FROM "{self.name}" WHERE {where}', params)
            self.conn.commit()
        return _SQLiteDeleteResult(cur.rowcount)

    def create_index(self, *args, **kwargs):
        if not self._table_exists():
            _sqlite_create_table(self.conn, self.name)
            self.conn.commit()
        return None

    def drop(self):
        with _SQLITE_LOCK:
            self.conn.execute(f'DROP TABLE IF EXISTS "{self.name}"')
            self.conn.commit()


class SQLiteDBShim:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._collections: Dict[str, SQLiteCollectionShim] = {}

    def __getitem__(self, name: str) -> SQLiteCollectionShim:
        if name not in self._collections:
            self._collections[name] = SQLiteCollectionShim(self.conn, name)
        return self._collections[name]

    def __getattr__(self, name: str) -> SQLiteCollectionShim:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


# ───────────────────────────────────────────────────────────────
# SQL placeholder + DDL normalization for the raw cursor path
# ───────────────────────────────────────────────────────────────
def _sqlite_convert_placeholders(sql: str) -> str:
    # %(name)s  → :name
    sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
    # %s        → ?
    sql = sql.replace("%s", "?")
    return sql


def _sqlite_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        return any(row[1] == column for row in cur.fetchall())
    except sqlite3.OperationalError:
        return False


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _normalize_pg_type(fragment: str) -> str:
    """Rewrite Postgres type names/keywords to SQLite equivalents."""
    out = fragment
    out = re.sub(r"\bBIGINT\b", "INTEGER", out, flags=re.IGNORECASE)
    out = re.sub(r"\bSMALLINT\b", "INTEGER", out, flags=re.IGNORECASE)
    out = re.sub(r"\bSERIAL\b", "INTEGER", out, flags=re.IGNORECASE)
    out = re.sub(r"\bBIGSERIAL\b", "INTEGER", out, flags=re.IGNORECASE)
    out = re.sub(r"\bVARCHAR\s*\(\s*\d+\s*\)", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bVARCHAR\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bCHARACTER\s+VARYING\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bTIMESTAMP\s+WITH\s+TIME\s+ZONE\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bTIMESTAMP\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bDATE\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bBOOLEAN\b", "INTEGER", out, flags=re.IGNORECASE)
    out = re.sub(r"\bBYTEA\b", "BLOB", out, flags=re.IGNORECASE)
    out = re.sub(r"\bJSONB\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bJSON\b", "TEXT", out, flags=re.IGNORECASE)
    out = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", out, flags=re.IGNORECASE)
    return out


def _normalize_sqlite_sql(conn: sqlite3.Connection, sql: str):
    """
    Translate Postgres-flavoured DDL into SQLite.

    Returns a string to run, or None if the statement should be a no-op,
    or the sentinel "__ADD_COLUMN_SKIP__" if the column already exists
    and the ALTER should be skipped silently.
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        return None
    upper = s.upper()

    # --- ALTER TABLE ... ALTER COLUMN ... TYPE ...  (no-op in SQLite) ---
    if re.match(r"ALTER\s+TABLE\s+\w+\s+ALTER\s+COLUMN\s+", upper):
        return None

    # --- ALTER TABLE ... ADD COLUMN [IF NOT EXISTS] col TYPE [DEFAULT ...] ---
    m = re.match(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s+(.+)$",
        s, re.IGNORECASE | re.DOTALL,
    )
    if m:
        table, col, typedef = m.groups()
        if not _sqlite_table_exists(conn, table):
            return None
        if _sqlite_column_exists(conn, table, col):
            return "__ADD_COLUMN_SKIP__"
        typedef = _normalize_pg_type(typedef)
        return f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typedef}'

    # --- CREATE TABLE / CREATE INDEX / everything else ---
    if upper.startswith("CREATE TABLE") or upper.startswith("CREATE INDEX"):
        s = _normalize_pg_type(s)
        # SQLite doesn't understand "IF NOT EXISTS" on some constraint forms,
        # but the common forms are fine. Return as-is after type rewrite.
        return s

    # --- Fallback: still rewrite NOW() and unsupported types anywhere ---
    s = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", s, flags=re.IGNORECASE)
    s = _normalize_pg_type(s)
    return s


class SQLiteCursorWrapper:
    def __init__(self, conn: sqlite3.Connection, cursor_factory=None):
        self._conn = conn
        self._cursor = conn.cursor()
        self.cursor_factory = cursor_factory
        self._closed = False
        self._description = None
        self._rowcount = -1

    def execute(self, query: str, params=None):
        if self._closed:
            raise Exception("Cursor is closed")

        # 1. Placeholder translation
        sql = _sqlite_convert_placeholders(query)

        # 2. DDL normalization
        normalized = _normalize_sqlite_sql(self._conn, sql)

        # 3. Fast-exit cases
        if normalized is None:
            self._description = None
            self._rowcount = 0
            return self
        if normalized == "__ADD_COLUMN_SKIP__":
            self._description = None
            self._rowcount = 0
            return self
        sql = normalized

        # 4. Bind params
        if params is None:
            sql_params: Any = ()
        elif isinstance(params, dict):
            sql_params = {k: _adapt_sqlite_value(v) for k, v in params.items()}
        elif isinstance(params, (list, tuple)):
            sql_params = tuple(_adapt_sqlite_value(v) for v in params)
        else:
            sql_params = (_adapt_sqlite_value(params),)

        # 5. Run
        with _SQLITE_LOCK:
            try:
                self._cursor.execute(sql, sql_params)
                self._description = self._cursor.description
                self._rowcount = self._cursor.rowcount
                self._conn.commit()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column name" in msg or "already exists" in msg:
                    logger.debug(f"[SQLite] benign DDL skip: {e}")
                    self._description = None
                    self._rowcount = 0
                    return self
                logger.error(f"[SQLite] ERROR: {e} | SQL: {sql[:200]}")
                raise
            except Exception as e:
                logger.error(f"[SQLite] ERROR: {e} | SQL: {sql[:200]}")
                raise
        return self

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self):
        return self._rowcount

    def _row_to_dict(self, row):
        return {k: _deadapt_sqlite_value(row[k]) for k in row.keys()}

    def fetchone(self):
        if self._closed:
            raise Exception("Cursor is closed")
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self.cursor_factory:
            return self._row_to_dict(row)
        return tuple(row)

    def fetchall(self):
        if self._closed:
            raise Exception("Cursor is closed")
        rows = self._cursor.fetchall()
        if self.cursor_factory:
            return [self._row_to_dict(r) for r in rows]
        return [tuple(r) for r in rows]

    def close(self):
        try:
            self._cursor.close()
        finally:
            self._closed = True


class SQLiteConnectionWrapper:
    def __init__(self, conn: sqlite3.Connection, cursor_factory=None):
        self._conn = conn
        self.cursor_factory = cursor_factory
        self._closed = False

    @property
    def closed(self):
        return 1 if self._closed else 0

    @property
    def status(self):
        return 0

    def cursor(self, cursor_factory=None):
        factory = cursor_factory or self.cursor_factory
        return SQLiteCursorWrapper(self._conn, factory)

    def commit(self):
        try:
            self._conn.commit()
        except Exception:
            pass

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        # Do NOT actually close the shared SQLite connection — the pool
        # owns it. Just mark this wrapper as closed.
        self._closed = True


# ───────────────────────────────────────────────────────────────
# Unified DB accessor with MongoDB → SQLite fallback
# ───────────────────────────────────────────────────────────────
def _get_db():
    global _DB_BACKEND, _sqlite_db_shim, _IS_SQLITE_MODE, _IS_MONGODB_MODE
    if _DB_BACKEND == "sqlite":
        if _sqlite_db_shim is None:
            _sqlite_db_shim = SQLiteDBShim(_get_sqlite_conn())
        return _sqlite_db_shim
    if _DB_BACKEND == "mongodb":
        _, db = _get_mongo_client()
        return db
    try:
        _, db = _get_mongo_client()
        _DB_BACKEND = "mongodb"
        _IS_MONGODB_MODE = True
        _IS_SQLITE_MODE = False
        return db
    except Exception as e:
        logger.warning(f"MongoDB unavailable ({e}) - falling back to SQLite")
        _DB_BACKEND = "sqlite"
        _IS_MONGODB_MODE = False
        _IS_SQLITE_MODE = True
        _sqlite_db_shim = SQLiteDBShim(_get_sqlite_conn())
        return _sqlite_db_shim


def get_db_connection():
    if _DB_BACKEND == "sqlite":
        return SQLiteConnectionWrapper(_get_sqlite_conn())
    try:
        db = _get_db()
        if _DB_BACKEND == "sqlite":
            return SQLiteConnectionWrapper(_get_sqlite_conn())
        return MongoConnectionWrapper(db)
    except Exception:
        _get_db()
        return SQLiteConnectionWrapper(_get_sqlite_conn())


def health_check() -> bool:
    try:
        if _DB_BACKEND == "sqlite":
            _get_sqlite_conn().execute("SELECT 1")
            return True
        if _DB_BACKEND == "mongodb":
            _get_mongo_client()[0].admin.command("ping")
            return True
        try:
            _get_mongo_client()[0].admin.command("ping")
            return True
        except Exception:
            _get_db()
            _get_sqlite_conn().execute("SELECT 1")
            return True
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# Utility: convert ISO date strings to datetime objects
# ═══════════════════════════════════════════════════════════════
def _convert_dates_in_doc(doc):
    if isinstance(doc, dict):
        for key, value in doc.items():
            if isinstance(value, str):
                try:
                    if "T" in value and ":" in value and "-" in value:
                        doc[key] = datetime.fromisoformat(value)
                except Exception:
                    pass
            elif isinstance(value, dict):
                _convert_dates_in_doc(value)
            elif isinstance(value, list):
                for item in value:
                    _convert_dates_in_doc(item)
    return doc


# ═══════════════════════════════════════════════════════════════
# SQL parameter substitution (used by MongoDB cursor)
# ═══════════════════════════════════════════════════════════════
def _substitute_params(query: str, params) -> str:
    if params is None:
        return query
    if isinstance(params, dict):
        for key, val in params.items():
            placeholder = f"%({key})s"
            query = query.replace(placeholder, _format_value(val))
        return query
    if not isinstance(params, (list, tuple)):
        params = (params,)
    parts = query.split("%s")
    if len(parts) - 1 == len(params):
        result = parts[0]
        for i, param in enumerate(params):
            result += _format_value(param) + parts[i + 1]
        return result
    parts = query.split("?")
    if len(parts) - 1 == len(params):
        result = parts[0]
        for i, param in enumerate(params):
            result += _format_value(param) + parts[i + 1]
        return result
    logger.warning(f"Parameter count mismatch: {len(parts)-1} placeholders vs {len(params)} params")
    return query


def _format_value(val) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, datetime):
        return f"'{val.isoformat()}'"
    if isinstance(val, str):
        return f"'{val.replace(chr(39), chr(39)+chr(39))}'"
    return f"'{str(val).replace(chr(39), chr(39)+chr(39))}'"


# ═══════════════════════════════════════════════════════════════
# WHERE clause parser (MongoDB side)
# ═══════════════════════════════════════════════════════════════
def _parse_where(where_clause: str) -> Dict[str, Any]:
    if not where_clause:
        return {}
    where_clause = where_clause.strip()
    filt: Dict[str, Any] = {}
    between_pattern = re.compile(
        r"(\w+)\s+BETWEEN\s+(.+?)\s+AND\s+(.+?)(?:\s+AND|\s*$)", re.IGNORECASE
    )
    between_matches = list(between_pattern.finditer(where_clause))
    if between_matches:
        for m in between_matches:
            col = m.group(1).strip()
            low = _parse_sql_value(m.group(2).strip())
            high = _parse_sql_value(m.group(3).strip())
            filt[col] = {"$gte": low, "$lte": high}
        where_clause = between_pattern.sub("", where_clause).strip()
        if where_clause.startswith("AND "):
            where_clause = where_clause[4:]
    if not where_clause:
        return filt
    conditions = re.split(r"\s+AND\s+", where_clause, flags=re.IGNORECASE)
    for cond in conditions:
        cond = cond.strip().strip(";").strip()
        if not cond:
            continue
        or_parts = re.split(r"\s+OR\s+", cond, flags=re.IGNORECASE)
        if len(or_parts) > 1:
            or_filters = []
            for part in or_parts:
                parsed = _parse_single_condition(part.strip())
                if parsed:
                    or_filters.append(parsed)
            if or_filters:
                if "$or" in filt:
                    filt["$or"].extend(or_filters)
                else:
                    filt["$or"] = or_filters
            continue
        parsed = _parse_single_condition(cond)
        if parsed:
            filt.update(parsed)
    return filt


def _parse_single_condition(cond: str) -> Optional[Dict[str, Any]]:
    cond = cond.strip().strip("()").strip()
    if not cond:
        return None
    m = re.match(r"(.+?)\s+IS\s+NULL$", cond, re.IGNORECASE)
    if m:
        return {m.group(1).strip(): {"$eq": None}}
    m = re.match(r"(.+?)\s+IS\s+NOT\s+NULL$", cond, re.IGNORECASE)
    if m:
        return {m.group(1).strip(): {"$ne": None}}
    m = re.match(r"(.+?)\s+NOT\s+IN\s*\((.+?)\)", cond, re.IGNORECASE)
    if m:
        return {m.group(1).strip(): {"$nin": _parse_values_list(m.group(2))}}
    m = re.match(r"(.+?)\s+IN\s*\((.+?)\)", cond, re.IGNORECASE)
    if m:
        return {m.group(1).strip(): {"$in": _parse_values_list(m.group(2))}}
    m = re.match(r"(.+?)\s+LIKE\s+'(.+?)'", cond, re.IGNORECASE)
    if m:
        pattern = m.group(2).replace("%", ".*").replace("_", ".")
        return {m.group(1).strip(): {"$regex": f"^{pattern}$"}}
    m = re.match(r"(.+?)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)", cond, re.IGNORECASE)
    if m:
        return {m.group(1).strip(): {"$gte": _parse_sql_value(m.group(2).strip()),
                                     "$lte": _parse_sql_value(m.group(3).strip())}}
    m = re.match(r"(.+?)\s*>=\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): {"$gte": _parse_sql_value(m.group(2).strip())}}
    m = re.match(r"(.+?)\s*<=\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): {"$lte": _parse_sql_value(m.group(2).strip())}}
    m = re.match(r"(.+?)\s*(!=|<>)\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): {"$ne": _parse_sql_value(m.group(3).strip())}}
    m = re.match(r"(.+?)\s*>\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): {"$gt": _parse_sql_value(m.group(2).strip())}}
    m = re.match(r"(.+?)\s*<\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): {"$lt": _parse_sql_value(m.group(2).strip())}}
    m = re.match(r"(.+?)\s*=\s*(.+)", cond)
    if m:
        return {m.group(1).strip(): _parse_sql_value(m.group(2).strip())}
    logger.warning(f"Could not parse WHERE condition: '{cond}'")
    return None


def _parse_sql_value(val_str: str):
    val_str = val_str.strip()
    if val_str.upper() == "NULL":
        return None
    if val_str.upper() == "TRUE":
        return True
    if val_str.upper() == "FALSE":
        return False
    if (val_str.startswith("'") and val_str.endswith("'")) or \
       (val_str.startswith('"') and val_str.endswith('"')):
        inner = val_str[1:-1].replace("''", "'").replace('""', '"')
        try:
            if "T" in inner and ":" in inner and "-" in inner:
                return datetime.fromisoformat(inner)
        except Exception:
            pass
        try:
            if re.match(r"^-?\d+$", inner):
                return int(inner)
            if re.match(r"^-?\d+\.\d+$", inner):
                return float(inner)
        except Exception:
            pass
        return inner
    if val_str.upper() in ("CURRENT_TIMESTAMP", "NOW()"):
        return datetime.now()
    try:
        return int(val_str)
    except ValueError:
        pass
    try:
        return float(val_str)
    except ValueError:
        pass
    return val_str


def _parse_values_list(vals_str: str) -> List[Any]:
    vals = []
    current = ""
    in_quote = False
    quote_char = None
    for char in vals_str:
        if char in ("'", '"') and not in_quote:
            in_quote = True
            quote_char = char
            current += char
        elif char == quote_char and in_quote:
            current += char
            in_quote = False
            quote_char = None
        elif char == "," and not in_quote:
            vals.append(_parse_sql_value(current.strip()))
            current = ""
        else:
            current += char
    if current.strip():
        vals.append(_parse_sql_value(current.strip()))
    return vals


def _parse_columns(cols_str: str) -> List[str]:
    return [c.strip() for c in cols_str.split(",")]


def _parse_set_clause(set_clause: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    assignments = []
    current = ""
    in_quote = False
    quote_char = None
    for char in set_clause:
        if char in ("'", '"') and not in_quote:
            in_quote = True
            quote_char = char
            current += char
        elif char == quote_char and in_quote:
            current += char
            in_quote = False
            quote_char = None
        elif char == "," and not in_quote:
            assignments.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        assignments.append(current.strip())
    for assignment in assignments:
        m = re.match(r"(.+?)\s*=\s*(.+)", assignment)
        if m:
            col = m.group(1).strip()
            val_expr = m.group(2).strip()
            inc_m = re.match(rf"^{re.escape(col)}\s*\+\s*(-?\d+(?:\.\d+)?)\s*$",
                             val_expr, re.IGNORECASE)
            if inc_m:
                num = inc_m.group(1)
                result[col] = {"__inc__": float(num) if "." in num else int(num)}
                continue
            dec_m = re.match(rf"^{re.escape(col)}\s*-\s*(-?\d+(?:\.\d+)?)\s*$",
                             val_expr, re.IGNORECASE)
            if dec_m:
                num = dec_m.group(1)
                val = float(num) if "." in num else int(num)
                result[col] = {"__inc__": -val}
                continue
            result[col] = _parse_sql_value(val_expr)
    return result


# ═══════════════════════════════════════════════════════════════
# MongoDB cursor wrapper
# ═══════════════════════════════════════════════════════════════
class MongoCursorWrapper:
    def __init__(self, db, cursor_factory=None):
        self.db = db
        self.cursor_factory = cursor_factory
        self._last_result = None
        self._result_type = None
        self._description = None
        self._rowcount = 0
        self._closed = False

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self):
        return self._rowcount

    def execute(self, query: str, params=None):
        if self._closed:
            raise Exception("Cursor is closed")
        try:
            query = _substitute_params(query, params)
            query = query.strip()
            upper = query.upper()
            logger.info(f"[SQL] {query[:180]}")

            if re.match(r"SELECT\s+1\s*;?\s*$", query, re.IGNORECASE):
                self._last_result = [(1,)]
                self._rowcount = 1
                self._result_type = "many"
                return self

            if any(upper.startswith(p) for p in [
                "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                "ALTER TABLE", "DROP TABLE", "PRAGMA", "VACUUM", "ANALYZE"
            ]):
                self._last_result = []
                self._rowcount = 0
                self._result_type = "many"
                return self

            if upper.startswith("SELECT") and (
                " JOIN " in upper or re.search(r"\bJOIN\b", upper) or "DISTINCT ON" in upper
            ):
                handled = self._execute_join_query(query)
                if handled is not None:
                    return handled

            count_match = re.match(
                r"SELECT\s+COUNT\(\*\)(?:\s+AS\s+\w+)?\s+FROM\s+(\w+)"
                r"(?:\s+WHERE\s+(.+?))?;?\s*$",
                query, re.IGNORECASE | re.DOTALL,
            )
            if count_match:
                table, where = count_match.groups()
                filt = _parse_where(where) if where else {}
                cnt = self.db[table.lower()].count_documents(filt)
                self._last_result = [(cnt,)]
                self._rowcount = 1
                self._result_type = "many"
                return self

            select_match = re.match(
                r"SELECT\s+(.+?)\s+FROM\s+(\w+)"
                r"(?:\s+AS\s+\w+)?"
                r"(?:\s+WHERE\s+(.+?))?"
                r"(?:\s+ORDER\s+BY\s+(.+?))?"
                r"(?:\s+LIMIT\s+(\d+))?"
                r"(?:\s+OFFSET\s+(\d+))?"
                r";?\s*$",
                query, re.IGNORECASE | re.DOTALL,
            )
            if select_match:
                cols_str, table, where, order_by, limit_str, offset_str = select_match.groups()
                table = table.lower()
                collection = self.db[table]
                cols = [c.strip() for c in cols_str.split(",")]
                filt = _parse_where(where) if where else {}

                if len(cols) == 1 and cols[0] == "1":
                    doc = collection.find_one(filt)
                    if doc:
                        self._last_result = [(1,)]
                        self._rowcount = 1
                    else:
                        self._last_result = []
                        self._rowcount = 0
                    self._result_type = "many"
                    return self

                projection = None
                if cols_str.strip() != "*":
                    projection = {c: 1 for c in cols}
                    projection["_id"] = 0

                sort = None
                if order_by:
                    sort_parts = order_by.strip().split()
                    sort_col = sort_parts[0]
                    sort_dir = -1 if len(sort_parts) > 1 and sort_parts[1].upper() == "DESC" else 1
                    sort = [(sort_col, sort_dir)]

                limit = int(limit_str) if limit_str else None

                if limit == 1 and not offset_str:
                    doc = collection.find_one(filt, projection, sort=sort)
                    if doc:
                        doc = _convert_dates_in_doc(doc)
                        self._last_result = [doc]
                        self._rowcount = 1
                    else:
                        self._last_result = []
                        self._rowcount = 0
                else:
                    cursor = collection.find(filt, projection)
                    if sort:
                        cursor = cursor.sort(sort)
                    if limit:
                        cursor = cursor.limit(limit)
                    docs = list(cursor)
                    for i in range(len(docs)):
                        docs[i] = _convert_dates_in_doc(docs[i])
                    self._last_result = docs
                    self._rowcount = len(docs)

                self._result_type = "many"
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)"
                r"\s+ON\s+CONFLICT\s*\((.+?)\)\s+DO\s+NOTHING",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, cols_str, vals_str, conflict_cols = m.groups()
                collection = self.db[table.lower()]
                cols = _parse_columns(cols_str)
                vals = _parse_values_list(vals_str)
                doc = dict(zip(cols, vals))
                if table in ("codes", "plan_keys"):
                    doc.setdefault("claimed_by", None)
                    doc.setdefault("claimed_at", None)
                conflict = _parse_columns(conflict_cols)
                filt = {k: doc[k] for k in conflict if k in doc}
                result = collection.update_one(
                    filt, {"$setOnInsert": doc}, upsert=True
                )
                self._rowcount = 1 if result.upserted_id else 0
                self._last_result = []
                self._result_type = "insert"
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)"
                r"\s+ON\s+CONFLICT\s*\((.+?)\)\s+DO\s+UPDATE\s+SET\s+(.+)",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, cols_str, vals_str, conflict_cols, set_clause = m.groups()
                collection = self.db[table.lower()]
                cols = _parse_columns(cols_str)
                vals = _parse_values_list(vals_str)
                doc = dict(zip(cols, vals))
                if table in ("codes", "plan_keys"):
                    doc.setdefault("claimed_by", None)
                    doc.setdefault("claimed_at", None)
                conflict = _parse_columns(conflict_cols)
                filt = {k: doc[k] for k in conflict if k in doc}
                updates = _parse_set_clause(set_clause)
                collection.update_one(
                    filt, {"$set": updates, "$setOnInsert": doc}, upsert=True
                )
                self._rowcount = 1
                self._last_result = []
                self._result_type = "update"
                return self

            m = re.match(
                r"INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, cols_str, vals_str = m.groups()
                collection = self.db[table.lower()]
                doc = dict(zip(_parse_columns(cols_str), _parse_values_list(vals_str)))
                if table in ("codes", "plan_keys"):
                    doc.setdefault("claimed_by", None)
                    doc.setdefault("claimed_at", None)
                try:
                    collection.insert_one(doc)
                    self._rowcount = 1
                except Exception as e:
                    if "duplicate" in str(e).lower() or "E11000" in str(e):
                        self._rowcount = 0
                    else:
                        raise
                self._last_result = []
                self._result_type = "insert"
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, cols_str, vals_str = m.groups()
                collection = self.db[table.lower()]
                doc = dict(zip(_parse_columns(cols_str), _parse_values_list(vals_str)))
                if table in ("codes", "plan_keys"):
                    doc.setdefault("claimed_by", None)
                    doc.setdefault("claimed_at", None)
                collection.insert_one(doc)
                self._rowcount = 1
                self._last_result = []
                self._result_type = "insert"
                return self

            m = re.match(
                r"UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\s+WHERE\s+(.+?))?;?\s*$",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, set_clause, where = m.groups()
                collection = self.db[table.lower()]
                filt = _parse_where(where) if where else {}
                updates = _parse_set_clause(set_clause)
                set_dict = {}
                inc_dict = {}
                for k, v in updates.items():
                    if isinstance(v, dict) and "__inc__" in v:
                        inc_dict[k] = v["__inc__"]
                    else:
                        set_dict[k] = v
                update_op: Dict[str, Any] = {}
                if set_dict:
                    update_op["$set"] = set_dict
                if inc_dict:
                    update_op["$inc"] = inc_dict
                if not update_op:
                    update_op = {"$set": {}}
                result = collection.update_many(filt, update_op)
                self._rowcount = result.modified_count
                self._last_result = []
                self._result_type = "update"
                return self

            m = re.match(
                r"DELETE\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+?))?;?\s*$",
                query, re.IGNORECASE | re.DOTALL,
            )
            if m:
                table, where = m.groups()
                collection = self.db[table.lower()]
                filt = _parse_where(where) if where else {}
                result = collection.delete_many(filt)
                self._rowcount = result.deleted_count
                self._last_result = []
                self._result_type = "delete"
                return self

            logger.error(f"[SQL] UNSUPPORTED QUERY: {query}")
            raise NotImplementedError(f"Unsupported SQL: {query[:200]}")
        except Exception as e:
            logger.error(f"[SQL] ERROR: {e} | Query: {query[:200]}")
            raise

    def _execute_join_query(self, query: str):
        q_flat = re.sub(r"\s+", " ", query.strip())

        m = re.match(
            r"SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
            q_flat, re.IGNORECASE,
        )
        if not m:
            return None
        cols_str, base_table, base_alias = m.groups()
        base_alias = base_alias or base_table

        distinct_on = None
        distinct_m = re.match(
            r"DISTINCT\s+ON\s*\(([^)]+)\)\s+(.+)",
            cols_str.strip(), re.IGNORECASE,
        )
        if distinct_m:
            distinct_on = distinct_m.group(1).strip()
            cols_str = distinct_m.group(2).strip()

        rest = q_flat[m.end():]

        join_parts = []
        for jm in re.finditer(
            r"(?:LEFT\s+|INNER\s+|RIGHT\s+|OUTER\s+|FULL\s+)?JOIN\s+(\w+)"
            r"(?:\s+(?:AS\s+)?(\w+))?\s+ON\s+"
            r"(.+?)(?=\s+(?:LEFT|INNER|RIGHT|OUTER|FULL)?\s*JOIN"
            r"|\s+WHERE|\s+ORDER|\s+LIMIT|\s*$)",
            rest, re.IGNORECASE,
        ):
            join_parts.append({
                "table": jm.group(1),
                "alias": jm.group(2) or jm.group(1),
                "on": jm.group(3).strip(),
            })

        where_m = re.search(r"WHERE\s+(.+?)(?=\s+ORDER\s+BY|\s+LIMIT|\s*$)", rest, re.IGNORECASE)
        where = where_m.group(1).strip() if where_m else None

        order_m = re.search(r"ORDER\s+BY\s+(.+?)(?=\s+LIMIT|\s*$)", rest, re.IGNORECASE)
        order_by = order_m.group(1).strip() if order_m else None

        limit_m = re.search(r"LIMIT\s+(\d+)", rest, re.IGNORECASE)
        limit = int(limit_m.group(1)) if limit_m else None

        aliases = {base_alias, base_table}
        for j in join_parts:
            aliases.add(j["alias"])
            aliases.add(j["table"])

        base_filter = {}
        if where:
            where_stripped = re.sub(
                r"\b(\w+)\.(\w+)",
                lambda mm: mm.group(2) if mm.group(1) in aliases else mm.group(0),
                where,
            )
            base_filter = _parse_where(where_stripped)

        base_collection = self.db[base_table.lower()]
        base_docs = list(base_collection.find(base_filter))
        if not base_docs:
            self._last_result = []
            self._rowcount = 0
            self._result_type = "many"
            return self

        join_indexes = []
        for j in join_parts:
            on_m = re.match(r"(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", j["on"])
            if not on_m:
                join_indexes.append(None)
                continue
            left_alias, left_col, right_alias, right_col = on_m.groups()
            if left_alias in (base_alias, base_table):
                base_col_name = left_col
                joined_col_name = right_col
            else:
                base_col_name = right_col
                joined_col_name = left_col
            joined_docs = list(self.db[j["table"].lower()].find({}))
            index: Dict[Any, List[Dict[str, Any]]] = {}
            for doc in joined_docs:
                key = doc.get(joined_col_name)
                if key is not None:
                    index.setdefault(key, []).append(doc)
            join_indexes.append((j, base_col_name, index))

        merged_rows: List[Dict[str, Any]] = []
        for base_doc in base_docs:
            bucket = [{"__base__": base_doc}]
            for join_info in join_indexes:
                if join_info is None:
                    continue
                j, base_col_name, index = join_info
                base_val = base_doc.get(base_col_name)
                matches = index.get(base_val, [])
                if matches:
                    new_bucket = []
                    for mrow in bucket:
                        for match in matches:
                            nm = dict(mrow)
                            nm[f"__{j['alias']}__"] = match
                            new_bucket.append(nm)
                    bucket = new_bucket
            merged_rows.extend(bucket)

        col_parts = [c.strip() for c in cols_str.split(",")]
        parsed_cols = []
        for c in col_parts:
            if c == "*":
                parsed_cols.append(("*", None))
            elif "." in c:
                a, col = c.split(".", 1)
                parsed_cols.append((col, a))
            else:
                parsed_cols.append((c, None))

        projected: List[Dict[str, Any]] = []
        for mrow in merged_rows:
            base = mrow.get("__base__", {})
            out: Dict[str, Any] = {}
            for col, alias in parsed_cols:
                if col == "*" and alias is None:
                    for k, v in base.items():
                        if k != "_id":
                            out[k] = v
                elif col == "*" and alias:
                    joined_doc = mrow.get(f"__{alias}__")
                    if joined_doc:
                        for k, v in joined_doc.items():
                            if k != "_id":
                                out[k] = v
                elif alias is None:
                    if col in base:
                        out[col] = base[col]
                else:
                    joined_doc = mrow.get(f"__{alias}__")
                    if joined_doc and col in joined_doc:
                        out[col] = joined_doc[col]
            projected.append(out)

        if distinct_on:
            d = distinct_on.split(".", 1)[1] if "." in distinct_on else distinct_on
            seen = set()
            dedup = []
            for p in projected:
                key = p.get(d)
                if key not in seen:
                    seen.add(key)
                    dedup.append(p)
            projected = dedup

        if order_by:
            specs = []
            for part in order_by.split(","):
                part = part.strip()
                bits = part.split()
                col = bits[0].split(".", 1)[1] if "." in bits[0] else bits[0]
                direction = -1 if len(bits) > 1 and bits[1].upper() == "DESC" else 1
                specs.append((col, direction))
            for col, direction in reversed(specs):
                projected.sort(
                    key=lambda x: (x.get(col) is None, x.get(col) if x.get(col) is not None else 0),
                    reverse=(direction == -1),
                )

        if limit:
            projected = projected[:limit]

        self._last_result = projected
        self._rowcount = len(projected)
        self._result_type = "many"
        return self

    def fetchone(self):
        if self._closed:
            raise Exception("Cursor is closed")
        if not self._last_result:
            return None
        row = self._last_result.pop(0)
        if self.cursor_factory and isinstance(row, dict):
            return row
        if isinstance(row, dict):
            return tuple(row.values())
        return row

    def fetchall(self):
        if self._closed:
            raise Exception("Cursor is closed")
        if not self._last_result:
            return []
        rows = self._last_result[:]
        self._last_result = []
        if self.cursor_factory:
            return rows if all(isinstance(r, dict) for r in rows) else [
                dict(zip([f"col_{i}" for i in range(len(r))], r)) if isinstance(r, tuple) else r
                for r in rows
            ]
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append(tuple(row.values()))
            else:
                result.append(row)
        return result

    def close(self):
        self._closed = True


class MongoConnectionWrapper:
    def __init__(self, db, cursor_factory=None):
        self.db = db
        self.cursor_factory = cursor_factory
        self._closed = False
        self._in_transaction = False

    @property
    def closed(self):
        return 1 if self._closed else 0

    @property
    def status(self):
        return 0

    def cursor(self, cursor_factory=None):
        factory = cursor_factory or self.cursor_factory
        return MongoCursorWrapper(self.db, factory)

    def commit(self):
        self._in_transaction = False

    def rollback(self):
        self._in_transaction = False

    def close(self):
        self._closed = True

    def _close_raw(self):
        self._closed = True


# ─── psycopg2 mock helpers ─────────────────────────────────────
class _MockConnection:
    def __init__(self):
        self._closed = False
    def cursor(self, *a, **k):
        return _MockCursor()
    def commit(self): pass
    def rollback(self): pass
    def close(self): self._closed = True
    @property
    def closed(self): return 1 if self._closed else 0


class _MockCursor:
    def execute(self, *a, **k): pass
    def fetchone(self): return None
    def fetchall(self): return []
    def close(self): pass


def _create_mock_connection(*a, **k):
    return _MockConnection()


psycopg2_mod = sys.modules.get("psycopg2")
if psycopg2_mod and not hasattr(psycopg2_mod, "_is_real"):
    psycopg2_mod.connect = _create_mock_connection
    psycopg2_mod.pool.ThreadedConnectionPool = object
    psycopg2_mod.extras.RealDictCursor = dict
    psycopg2_mod.extensions.STATUS_READY = 0

RealDictCursor = dict


# ─── Pooled connection helpers (kept for compat) ───────────────
class SQLiteThreadedPool:
    def __init__(self, minconn, maxconn, *a, **k):
        self.minconn = minconn
        self.maxconn = maxconn
        logger.info("ThreadedPool mocked (MongoDB/SQLite backend)")


class PooledConn:
    def __init__(self):
        self.conn = None
    def __enter__(self):
        self.conn = get_db_connection()
        return self.conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            try:
                if exc_type:
                    self.conn.rollback()
            finally:
                self.conn.close()
        return False


class PooledConn2:
    pass


def _release_connection(conn):
    if conn:
        conn.close()


class NoOpCache(dict):
    def get(self, key, default=None): return None
    def __getitem__(self, key): raise KeyError(key)
    def __setitem__(self, key, value): pass
    def pop(self, key, default=None): return None
    def clear(self): pass


_gate_cache = NoOpCache()
_gate_cache_ttl = 30
_credits_cache = NoOpCache()
_credits_cache_ttl = 5
_premium_cache = NoOpCache()
_premium_cache_ttl = 60


# ═══════════════════════════════════════════════════════════════
# SCHEMA INITIALIZATION
# ═══════════════════════════════════════════════════════════════
def ensure_users_table():
    try:
        db = _get_db()
        db.users.create_index("user_id", unique=True)
        db.users.create_index("username")
        db.users.create_index("is_premium")
        db.users.create_index([("cc_charged", -1)])
        db.users.create_index([("cc_checked", -1)])
        logger.info("Users collection initialized")
    except Exception as e:
        logger.error(f"Error initializing users collection: {e}")
        raise


def ensure_proxy_table():
    try:
        db = _get_db()
        db.proxies.create_index([("user_id", 1), ("proxy", 1)], unique=True)
        db.proxies.create_index("user_id")
        logger.info("Proxies collection initialized")
    except Exception as e:
        logger.error(f"Error initializing proxies collection: {e}")
        raise


def ensure_receipts_table():
    try:
        db = _get_db()
        db.receipts.create_index("receipt_id", unique=True)
        db.receipts.create_index("user_id")
        logger.info("Receipts collection initialized")
    except Exception as e:
        logger.error(f"Error initializing receipts collection: {e}")
        raise


def ensure_codes_table():
    try:
        db = _get_db()
        db.codes.create_index("code", unique=True)
        db.codes.create_index("claimed_by")
        logger.info("Codes collection initialized")
    except Exception as e:
        logger.error(f"Error initializing codes collection: {e}")
        raise


def ensure_plan_keys_table():
    try:
        db = _get_db()
        db.plan_keys.create_index("key", unique=True)
        db.plan_keys.create_index("claimed_by")
        logger.info("Plan keys collection initialized")
    except Exception as e:
        logger.error(f"Error initializing plan_keys collection: {e}")
        raise


def ensure_gate_status_table():
    try:
        db = _get_db()
        db.gate_status.create_index("gate", unique=True)
        logger.info("Gate status collection initialized")
    except Exception as e:
        logger.error(f"Error initializing gate_status collection: {e}")
        raise


def ensure_banned_users_table():
    try:
        db = _get_db()
        db.banned_users.create_index("user_id", unique=True)
        logger.info("Banned users collection initialized")
    except Exception as e:
        logger.error(f"Error initializing banned_users collection: {e}")
        raise


def ensure_user_sites_table():
    try:
        db = _get_db()
        db.user_sites.create_index([("user_id", 1), ("url", 1)], unique=True)
        db.user_sites.create_index("user_id")
        logger.info("User sites collection initialized")
    except Exception as e:
        logger.error(f"Error initializing user_sites collection: {e}")
        raise


def ensure_pending_feedback_table():
    try:
        db = _get_db()
        db.pending_feedback.create_index("created_at")
        logger.info("Pending feedback collection initialized")
    except Exception as e:
        logger.error(f"Error initializing pending_feedback collection: {e}")
        raise


def ensure_settings_table():
    try:
        db = _get_db()
        db.settings.create_index("key", unique=True)
        logger.info("Settings collection initialized")
    except Exception as e:
        logger.error(f"Error initializing settings collection: {e}")
        raise


def ensure_stats_table(gate_name: str):
    try:
        db = _get_db()
        collection = db[f"{gate_name}_stats"]
        collection.create_index("user_id")
        collection.create_index("timestamp")
        collection.create_index("status")
    except Exception as e:
        logger.warning(f"Error initializing stats collection {gate_name}: {e}")


def _migrate_numeric_and_date_fields():
    """Only meaningful for MongoDB; SQLite stores typed values already."""
    if _DB_BACKEND != "mongodb":
        return
    try:
        db = _get_db()
        numeric_fields = ["credits", "is_premium", "cc_checked", "cc_charged"]
        date_fields = ["joined_at", "premium_expiry"]
        for field in numeric_fields:
            for user in db.users.find({field: {"$type": "string"}}):
                val = user[field]
                try:
                    new_val = int(val) if "." not in val else float(val)
                except Exception:
                    new_val = 0
                db.users.update_one({"_id": user["_id"]}, {"$set": {field: new_val}})
        for field in date_fields:
            for user in db.users.find({field: {"$type": "string"}}):
                try:
                    new_val = datetime.fromisoformat(user[field])
                    db.users.update_one({"_id": user["_id"]}, {"$set": {field: new_val}})
                except Exception:
                    pass
        for field in ["purchased_on", "expires_on", "created_at"]:
            for receipt in db.receipts.find({field: {"$type": "string"}}):
                try:
                    new_val = datetime.fromisoformat(receipt[field])
                    db.receipts.update_one({"_id": receipt["_id"]}, {"$set": {field: new_val}})
                except Exception:
                    pass
        for coll_name in ["codes", "plan_keys"]:
            coll = db[coll_name]
            for doc in coll.find({"claimed_at": {"$type": "string"}}):
                try:
                    new_val = datetime.fromisoformat(doc["claimed_at"])
                    coll.update_one({"_id": doc["_id"]}, {"$set": {"claimed_at": new_val}})
                except Exception:
                    pass
        logger.info("Numeric and date field migration completed")
    except Exception as e:
        logger.warning(f"Migration skipped: {e}")


def initialize_schema():
    logger.info(f"Starting schema initialization (backend={_DB_BACKEND or 'auto'})...")
    try:
        ensure_users_table()
        ensure_proxy_table()
        ensure_receipts_table()
        ensure_codes_table()
        ensure_plan_keys_table()
        ensure_gate_status_table()
        ensure_banned_users_table()
        ensure_user_sites_table()
        ensure_pending_feedback_table()
        ensure_settings_table()
        db = _get_db()
        db.global_sites.create_index("url", unique=True)
        for gate in ["ST", "STR", "PF", "VBV", "FT", "BL", "PP", "AT", "PW", "PYU"]:
            ensure_stats_table(gate)
        _migrate_numeric_and_date_fields()
        logger.info(f"Schema initialization completed (backend={_DB_BACKEND})")
    except Exception as e:
        logger.error(f"Schema initialization failed: {e}")
        raise


# ═══════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        user = db.users.find_one({"user_id": user_id})
        if user:
            user = _convert_dates_in_doc(user)
            if "_id" in user:
                user["_id"] = str(user["_id"])
        return user
    except Exception as e:
        logger.error(f"Error fetching user {user_id}: {e}")
        return None


def create_user(user_id: int, username: str, first_name: str = "User", initial_credits: int = 150):
    try:
        db = _get_db()
        user_doc = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "credits": initial_credits,
            "is_premium": 0,
            "premium_expiry": None,
            "cc_checked": 0,
            "cc_charged": 0,
            "joined_at": datetime.now(),
        }
        result = db.users.update_one(
            {"user_id": user_id}, {"$setOnInsert": user_doc}, upsert=True
        )
        if getattr(result, "upserted_id", None):
            logger.info(f"Created user {user_id} ({username})")
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")


def get_user_credits(user_id: int) -> int:
    try:
        db = _get_db()
        user = db.users.find_one({"user_id": user_id}, {"credits": 1})
        return user.get("credits", 0) if user else 0
    except Exception as e:
        logger.error(f"Error fetching credits for user {user_id}: {e}")
        return 0


def update_credits(user_id: int, new_credits: int):
    try:
        db = _get_db()
        db.users.update_one({"user_id": user_id}, {"$set": {"credits": new_credits}})
    except Exception as e:
        logger.error(f"Error updating credits for user {user_id}: {e}")


def deduct_credits_atomic(user_id: int, amount: int) -> int:
    try:
        db = _get_db()
        result = db.users.find_one_and_update(
            {"user_id": user_id, "credits": {"$gte": amount}},
            {"$inc": {"credits": -amount}},
            return_document=True,
        )
        if result:
            return result.get("credits", 0)
        return -1
    except Exception as e:
        logger.error(f"Error deducting credits for user {user_id}: {e}")
        return -1


def get_premium_status(user_id: int) -> Tuple[bool, Optional[datetime]]:
    try:
        db = _get_db()
        user = db.users.find_one(
            {"user_id": user_id}, {"is_premium": 1, "premium_expiry": 1}
        )
        if not user:
            return (False, None)
        is_premium = user.get("is_premium", 0)
        expiry = user.get("premium_expiry")
        if is_premium and expiry:
            if isinstance(expiry, str):
                try:
                    expiry = datetime.fromisoformat(expiry)
                except Exception:
                    expiry = None
            if expiry and datetime.now() < expiry:
                return (True, expiry)
            db.users.update_one(
                {"user_id": user_id},
                {"$set": {"is_premium": 0, "premium_expiry": None, "credits": 150}},
            )
            return (False, None)
        return (False, None)
    except Exception as e:
        logger.error(f"Error fetching premium status for user {user_id}: {e}")
        return (False, None)


def set_premium(user_id: int, days: int) -> bool:
    try:
        expiry = datetime.now() + timedelta(days=days)
        db = _get_db()
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": 1, "premium_expiry": expiry}},
            upsert=True,
        )
        logger.info(f"Set premium for user {user_id} for {days} days")
        return True
    except Exception as e:
        logger.error(f"Error setting premium for user {user_id}: {e}")
        return False


def is_gate_enabled(gate: str) -> bool:
    try:
        db = _get_db()
        db.gate_status.update_one(
            {"gate": gate},
            {"$setOnInsert": {"gate": gate, "is_enabled": 1, "updated_at": datetime.now()}},
            upsert=True,
        )
        status = db.gate_status.find_one({"gate": gate})
        return bool(status.get("is_enabled", 1)) if status else True
    except Exception as e:
        logger.error(f"Error checking gate status for {gate}: {e}")
        return True


def set_gate_status(gate: str, enabled: bool) -> bool:
    try:
        db = _get_db()
        db.gate_status.update_one(
            {"gate": gate},
            {"$set": {"is_enabled": int(enabled), "updated_at": datetime.now()}},
            upsert=True,
        )
        logger.info(f"Set gate {gate} to {'enabled' if enabled else 'disabled'}")
        return True
    except Exception as e:
        logger.error(f"Error setting gate {gate} status: {e}")
        return False


def update_user_stats(user_id: int, is_charged: bool):
    try:
        db = _get_db()
        user = db.users.find_one({"user_id": user_id}, {"cc_checked": 1, "cc_charged": 1})
        if not user:
            return
        updates = {}
        for field in ["cc_checked", "cc_charged"]:
            val = user.get(field)
            if isinstance(val, str):
                try:
                    val = int(val)
                except Exception:
                    val = 0
                updates[field] = val
        if updates:
            db.users.update_one({"user_id": user_id}, {"$set": updates})
        if is_charged:
            db.users.update_one(
                {"user_id": user_id}, {"$inc": {"cc_checked": 1, "cc_charged": 1}}
            )
        else:
            db.users.update_one({"user_id": user_id}, {"$inc": {"cc_checked": 1}})
    except Exception as e:
        logger.error(f"Error updating stats for user {user_id}: {e}")


def get_user_sites_db(user_id: int) -> List[str]:
    try:
        db = _get_db()
        sites = db.user_sites.find(
            {"user_id": int(user_id)}, {"url": 1, "_id": 0}
        ).sort("_id", 1)
        return [s["url"] for s in sites]
    except Exception as e:
        logger.error(f"Error fetching user sites for {user_id}: {e}")
        return []


def add_user_sites_db(user_id: int, sites: List[str]) -> int:
    added = 0
    try:
        db = _get_db()
        for s in sites:
            if not s:
                continue
            url = str(s).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            url = url.lower().rstrip("/")
            try:
                result = db.user_sites.update_one(
                    {"user_id": int(user_id), "url": url},
                    {"$setOnInsert": {
                        "user_id": int(user_id),
                        "url": url,
                        "price": 0.0,
                        "added_at": datetime.now(),
                    }},
                    upsert=True,
                )
                if getattr(result, "upserted_id", None) is not None:
                    added += 1
            except Exception as e:
                logger.error(f"Error inserting site {url} for user {user_id}: {e}")
        logger.info(f"Added {added} sites for user {user_id} (input: {len(sites)})")
    except Exception as e:
        logger.error(f"Error adding sites for user {user_id}: {e}")
    return added


def clear_user_sites_db(user_id: int) -> int:
    try:
        db = _get_db()
        result = db.user_sites.delete_many({"user_id": int(user_id)})
        return result.deleted_count
    except Exception as e:
        logger.error(f"Error clearing sites for {user_id}: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# CODES / PLAN KEYS
# ═══════════════════════════════════════════════════════════════
def add_code(code: str, duration_days: int, max_uses: int = 1) -> bool:
    try:
        db = _get_db()
        db.codes.insert_one({
            "code": code,
            "duration_days": duration_days,
            "max_uses": max_uses,
            "claimed_by": None,
            "claimed_at": None,
            "created_at": datetime.now(),
        })
        return True
    except Exception as e:
        logger.error(f"Error adding code {code}: {e}")
        return False


def get_code(code: str) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        code_doc = db.codes.find_one({"code": code})
        if code_doc:
            code_doc = _convert_dates_in_doc(code_doc)
            if "_id" in code_doc:
                code_doc["_id"] = str(code_doc["_id"])
        return code_doc
    except Exception as e:
        logger.error(f"Error fetching code {code}: {e}")
        return None


def claim_code(user_id: int, code: str) -> Tuple[bool, str]:
    try:
        db = _get_db()
        code_doc = db.codes.find_one({"code": code})
        if not code_doc:
            return False, "Invalid code."
        if code_doc.get("claimed_by") is not None:
            return False, "Code already claimed."
        db.codes.update_one(
            {"code": code, "claimed_by": None},
            {"$set": {"claimed_by": user_id, "claimed_at": datetime.now()}},
        )
        duration = code_doc.get("duration_days", 0)
        expiry = datetime.now() + timedelta(days=duration)
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": 1, "premium_expiry": expiry}},
            upsert=True,
        )
        return True, f"Code redeemed! Premium extended by {duration} days."
    except Exception as e:
        logger.error(f"Error claiming code {code} for user {user_id}: {e}")
        return False, "Database error during code redemption."


def add_plan_key(key: str, duration_days: int, max_uses: int = 1) -> bool:
    try:
        db = _get_db()
        db.plan_keys.insert_one({
            "key": key,
            "duration_days": duration_days,
            "max_uses": max_uses,
            "claimed_by": None,
            "claimed_at": None,
            "created_at": datetime.now(),
        })
        return True
    except Exception as e:
        logger.error(f"Error adding plan key {key}: {e}")
        return False


def get_plan_key(key: str) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        key_doc = db.plan_keys.find_one({"key": key})
        if key_doc:
            key_doc = _convert_dates_in_doc(key_doc)
            if "_id" in key_doc:
                key_doc["_id"] = str(key_doc["_id"])
        return key_doc
    except Exception as e:
        logger.error(f"Error fetching plan key {key}: {e}")
        return None


def claim_plan_key(user_id: int, key: str) -> Tuple[bool, str]:
    try:
        db = _get_db()
        key_doc = db.plan_keys.find_one({"key": key})
        if not key_doc:
            return False, "Invalid key."
        if key_doc.get("claimed_by") is not None:
            return False, "Key already claimed."
        db.plan_keys.update_one(
            {"key": key, "claimed_by": None},
            {"$set": {"claimed_by": user_id, "claimed_at": datetime.now()}},
        )
        duration = key_doc.get("duration_days", key_doc.get("days", 0))
        expiry = datetime.now() + timedelta(days=duration)
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": 1, "premium_expiry": expiry}},
            upsert=True,
        )
        return True, f"Key redeemed! Premium extended by {duration} days."
    except Exception as e:
        logger.error(f"Error claiming plan key {key} for user {user_id}: {e}")
        return False, "Database error during key redemption."


def get_latest_receipt_plan(user_id: int) -> Optional[str]:
    try:
        db = _get_db()
        receipt = db.receipts.find_one(
            {"user_id": user_id},
            sort=[("purchased_on", -1)],
            projection={"plan": 1, "_id": 0},
        )
        return receipt.get("plan") if receipt else None
    except Exception as e:
        logger.error(f"Error fetching latest receipt plan for user {user_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# RECEIPTS
# ═══════════════════════════════════════════════════════════════
def save_receipt(user_id: int, receipt_id: str, amount: float,
                 currency: str = "USD", metadata: dict = None) -> bool:
    try:
        db = _get_db()
        db.receipts.insert_one({
            "receipt_id": receipt_id,
            "user_id": user_id,
            "amount": amount,
            "currency": currency,
            "metadata": metadata or {},
            "created_at": datetime.now(),
        })
        return True
    except Exception as e:
        logger.error(f"Error saving receipt {receipt_id}: {e}")
        return False


def get_receipt(receipt_id: str) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        receipt = db.receipts.find_one({"receipt_id": receipt_id})
        if receipt:
            receipt = _convert_dates_in_doc(receipt)
            if "_id" in receipt:
                receipt["_id"] = str(receipt["_id"])
        return receipt
    except Exception as e:
        logger.error(f"Error fetching receipt {receipt_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# PROXIES
# ═══════════════════════════════════════════════════════════════
def add_proxy(user_id: int, proxy: str) -> bool:
    try:
        db = _get_db()
        result = db.proxies.update_one(
            {"user_id": user_id, "proxy": proxy},
            {"$setOnInsert": {"user_id": user_id, "proxy": proxy, "added_at": datetime.now()}},
            upsert=True,
        )
        return getattr(result, "upserted_id", None) is not None
    except Exception as e:
        logger.error(f"Error adding proxy for user {user_id}: {e}")
        return False


def get_proxies(user_id: int) -> List[str]:
    try:
        db = _get_db()
        proxies = db.proxies.find({"user_id": user_id}, {"proxy": 1, "_id": 0}).sort("_id", 1)
        return [p["proxy"] for p in proxies]
    except Exception as e:
        logger.error(f"Error fetching proxies for user {user_id}: {e}")
        return []


def remove_proxy(user_id: int, proxy: str) -> bool:
    try:
        db = _get_db()
        result = db.proxies.delete_one({"user_id": user_id, "proxy": proxy})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error removing proxy for user {user_id}: {e}")
        return False


def clear_proxies(user_id: int) -> int:
    try:
        db = _get_db()
        result = db.proxies.delete_many({"user_id": user_id})
        return result.deleted_count
    except Exception as e:
        logger.error(f"Error clearing proxies for user {user_id}: {e}")
        return 0


def count_proxies(user_id: int) -> int:
    try:
        db = _get_db()
        return db.proxies.count_documents({"user_id": user_id})
    except Exception as e:
        logger.error(f"Error counting proxies for user {user_id}: {e}")
        return 0


def get_all_proxies_with_users() -> List[Dict[str, Any]]:
    try:
        db = _get_db()
        cursor = db.proxies.find({}, {"user_id": 1, "proxy": 1, "_id": 0}).sort("user_id", 1)
        return [{"user_id": p.get("user_id"), "proxy": p.get("proxy")} for p in cursor]
    except Exception as e:
        logger.error(f"Error fetching all proxies with users: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# BANNED USERS
# ═══════════════════════════════════════════════════════════════
def ban_user(user_id: int, reason: str = "") -> bool:
    try:
        db = _get_db()
        db.banned_users.update_one(
            {"user_id": user_id},
            {"$set": {"reason": reason, "banned_at": datetime.now()}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")
        return False


def unban_user(user_id: int) -> bool:
    try:
        db = _get_db()
        result = db.banned_users.delete_one({"user_id": user_id})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error unbanning user {user_id}: {e}")
        return False


def is_banned(user_id: int) -> bool:
    try:
        db = _get_db()
        return db.banned_users.find_one({"user_id": user_id}) is not None
    except Exception as e:
        logger.error(f"Error checking ban status for user {user_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════
def log_stat(gate: str, user_id: int, status: str, extra: dict = None) -> bool:
    try:
        db = _get_db()
        db[f"{gate}_stats"].insert_one({
            "user_id": user_id,
            "status": status,
            "timestamp": datetime.now(),
            "extra": extra or {},
        })
        return True
    except Exception as e:
        logger.error(f"Error logging stat for gate {gate}, user {user_id}: {e}")
        return False


def get_stats(gate: str, user_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        db = _get_db()
        filt = {}
        if user_id is not None:
            filt["user_id"] = user_id
        stats = db[f"{gate}_stats"].find(filt).sort("timestamp", -1).limit(limit)
        result = []
        for s in stats:
            s = _convert_dates_in_doc(s)
            if "_id" in s:
                s["_id"] = str(s["_id"])
            result.append(s)
        return result
    except Exception as e:
        logger.error(f"Error fetching stats for gate {gate}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# CHARGE LEADERBOARD
# ═══════════════════════════════════════════════════════════════
def get_charge_leaderboard(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        db = _get_db()
        users = db.users.find(
            {"cc_charged": {"$gt": 0}},
            {"user_id": 1, "username": 1, "first_name": 1, "cc_checked": 1, "cc_charged": 1},
        ).sort("cc_charged", -1).limit(limit)
        result = []
        for u in users:
            try:
                charged = int(u.get("cc_charged", 0))
            except (ValueError, TypeError):
                charged = 0
            try:
                hits = int(u.get("cc_checked", 0))
            except (ValueError, TypeError):
                hits = 0
            result.append({
                "user_id": u.get("user_id"),
                "username": u.get("username"),
                "first_name": u.get("first_name"),
                "cc_charged": charged,
                "cc_checked": hits,
            })
        return result
    except Exception as e:
        logger.error(f"Error fetching charge leaderboard: {e}")
        return []


def get_user_charge_rank(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        user = db.users.find_one(
            {"user_id": user_id},
            {"cc_checked": 1, "cc_charged": 1, "username": 1, "first_name": 1},
        )
        if not user:
            return None
        try:
            charged = int(user.get("cc_charged", 0))
        except (ValueError, TypeError):
            charged = 0
        try:
            hits = int(user.get("cc_checked", 0))
        except (ValueError, TypeError):
            hits = 0
        if hits == 0:
            return None
        rate = (charged / hits * 100) if hits > 0 else 0.0
        total = db.users.count_documents({"cc_charged": {"$gt": 0}})
        if charged == 0:
            return {
                "rank": None, "total": total, "charged": 0, "hits": hits,
                "rate": rate, "username": user.get("username"),
                "first_name": user.get("first_name"),
            }
        higher = db.users.count_documents({"cc_charged": {"$gt": charged}})
        return {
            "rank": higher + 1, "total": total, "charged": charged, "hits": hits,
            "rate": rate, "username": user.get("username"),
            "first_name": user.get("first_name"),
        }
    except Exception as e:
        logger.error(f"Error fetching user rank for {user_id}: {e}")
        return None


def clear_all_charge_stats() -> int:
    try:
        db = _get_db()
        result = db.users.update_many(
            {"$or": [{"cc_checked": {"$ne": 0}}, {"cc_charged": {"$ne": 0}}]},
            {"$set": {"cc_checked": 0, "cc_charged": 0}},
        )
        return result.modified_count
    except Exception as e:
        logger.error(f"Error clearing charge stats: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# USER LISTING
# ═══════════════════════════════════════════════════════════════
def get_all_user_ids() -> List[int]:
    try:
        db = _get_db()
        users = db.users.find({}, {"user_id": 1, "_id": 0})
        return [u["user_id"] for u in users if "user_id" in u]
    except Exception as e:
        logger.error(f"Error fetching all user IDs: {e}")
        return []


def get_all_users() -> List[Dict[str, Any]]:
    try:
        db = _get_db()
        users = db.users.find({})
        result = []
        for u in users:
            u = _convert_dates_in_doc(u)
            if "_id" in u:
                u["_id"] = str(u["_id"])
            result.append(u)
        return result
    except Exception as e:
        logger.error(f"Error fetching all users: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# GLOBAL SITES
# ═══════════════════════════════════════════════════════════════
def get_global_sites() -> List[str]:
    try:
        db = _get_db()
        return [s["url"] for s in db.global_sites.find({}, {"url": 1, "_id": 0})]
    except Exception as e:
        logger.error(f"Error fetching global sites: {e}")
        return []


def set_global_sites(sites: List[str]) -> int:
    try:
        db = _get_db()
        db.global_sites.drop()
        docs = []
        if sites:
            docs = [{"url": s.strip(), "added_at": datetime.now()} for s in sites if s and s.strip()]
            for d in docs:
                db.global_sites.insert_one(d)
        return len(docs)
    except Exception as e:
        logger.error(f"Error setting global sites: {e}")
        return 0


def add_global_sites(sites: List[str]) -> int:
    added = 0
    try:
        db = _get_db()
        for s in sites:
            s = str(s).strip()
            if not s:
                continue
            if not s.startswith(("http://", "https://")):
                s = "https://" + s
            s = s.rstrip("/").lower()
            result = db.global_sites.update_one(
                {"url": s},
                {"$setOnInsert": {"url": s, "added_at": datetime.now()}},
                upsert=True,
            )
            if getattr(result, "upserted_id", None):
                added += 1
        return added
    except Exception as e:
        logger.error(f"Error adding global sites: {e}")
        return 0


def clear_global_sites() -> int:
    try:
        db = _get_db()
        return db.global_sites.delete_many({}).deleted_count
    except Exception as e:
        logger.error(f"Error clearing global sites: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════
def get_setting(key: str, default=None):
    try:
        db = _get_db()
        doc = db.settings.find_one({"key": key})
        if doc is None:
            return default
        return doc.get("value", default)
    except Exception as e:
        logger.error(f"Error fetching setting '{key}': {e}")
        return default


def set_setting(key: str, value) -> bool:
    try:
        db = _get_db()
        db.settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value, "updated_at": datetime.now()}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"Error setting '{key}': {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# PENDING FEEDBACK
# ═══════════════════════════════════════════════════════════════
def save_pending_feedback(pid: str, data: dict) -> bool:
    try:
        db = _get_db()
        payload = dict(data)
        payload["_id"] = pid
        payload["created_at"] = datetime.now()
        db.pending_feedback.replace_one({"_id": pid}, payload, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Error saving pending feedback {pid}: {e}")
        return False


def get_pending_feedback(pid: str) -> Optional[dict]:
    try:
        db = _get_db()
        return db.pending_feedback.find_one({"_id": pid})
    except Exception as e:
        logger.error(f"Error fetching pending feedback {pid}: {e}")
        return None


def delete_pending_feedback(pid: str) -> bool:
    try:
        db = _get_db()
        result = db.pending_feedback.delete_one({"_id": pid})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting pending feedback {pid}: {e}")
        return False


def cleanup_old_pending_feedback(hours: int = 48) -> int:
    try:
        db = _get_db()
        cutoff = datetime.now() - timedelta(hours=hours)
        result = db.pending_feedback.delete_many({"created_at": {"$lt": cutoff}})
        return result.deleted_count
    except Exception as e:
        logger.error(f"Error cleaning up old pending feedback: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# INITIALIZATION
# ═══════════════════════════════════════════════════════════════
try:
    # Force backend detection now.
    _get_db()
    initialize_schema()
    if _DB_BACKEND == "sqlite":
        logger.warning("Running with SQLite fallback (MongoDB unavailable)")
    else:
        logger.info("MongoDB database module initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize database: {e}")
    raise
