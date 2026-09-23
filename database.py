"""
MongoDB Database Module for Railway.com Deployment
Drop-in replacement for database1.py with MongoDB backend.
"""

import os
import sys
import types
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [DB] %(message)s"
)
logger = logging.getLogger(__name__)

_IS_SQLITE_MODE = False
_IS_MONGODB_MODE = True

def _setup_psycopg2_mocks():
    try:
        import psycopg2, psycopg2.pool, psycopg2.extensions, psycopg2.extras
        logger.info("psycopg2 found - using MongoDB backend (psycopg2 mocked)")
        return False
    except ImportError:
        logger.info("psycopg2 not found - injecting mocks for MongoDB compatibility")
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
    try:
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
    except ImportError:
        logger.error("pymongo not installed. Add 'pymongo>=4.0' and 'certifi' to requirements.txt")
        raise
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise

def _get_db():
    _, db = _get_mongo_client()
    return db

def health_check() -> bool:
    try:
        client, _ = _get_mongo_client()
        client.admin.command("ping")
        return True
    except Exception as e:
        logger.error(f"MongoDB health check failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════════
# UTILITY: Convert ISO date strings to datetime objects in documents
# ═══════════════════════════════════════════════════════════════

def _convert_dates_in_doc(doc):
    if isinstance(doc, dict):
        for key, value in doc.items():
            if isinstance(value, str):
                try:
                    if 'T' in value and ':' in value and '-' in value:
                        doc[key] = datetime.fromisoformat(value)
                except:
                    pass
            elif isinstance(value, dict):
                _convert_dates_in_doc(value)
            elif isinstance(value, list):
                for item in value:
                    _convert_dates_in_doc(item)
    return doc

# ═══════════════════════════════════════════════════════════════
# SQL PARAMETER SUBSTITUTION
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
# WHERE CLAUSE PARSER
# ═══════════════════════════════════════════════════════════════

def _parse_where(where_clause: str) -> Dict[str, Any]:
    if not where_clause:
        return {}
    where_clause = where_clause.strip()
    filt = {}
    between_pattern = re.compile(r"(\w+)\s+BETWEEN\s+(.+?)\s+AND\s+(.+?)(?:\s+AND|\s*$)", re.IGNORECASE)
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
        return {m.group(1).strip(): {"$gte": _parse_sql_value(m.group(2).strip()), "$lte": _parse_sql_value(m.group(3).strip())}}
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
    if (val_str.startswith("'") and val_str.endswith("'")) or (val_str.startswith('"') and val_str.endswith('"')):
        inner = val_str[1:-1].replace("''", "'").replace('""', '"')
        try:
            if 'T' in inner and ':' in inner and '-' in inner:
                return datetime.fromisoformat(inner)
        except:
            pass
        try:
            if re.match(r'^-?\d+$', inner):
                return int(inner)
            if re.match(r'^-?\d+\.\d+$', inner):
                return float(inner)
        except:
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
    result = {}
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
            result[m.group(1).strip()] = _parse_sql_value(m.group(2).strip())
    return result

# ═══════════════════════════════════════════════════════════════
# MONGODB CURSOR WRAPPER - Generic SQL Translator
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

            select_match = re.match(
                r"SELECT\s+(.+?)\s+FROM\s+(\w+)"
                r"(?:\s+AS\s+\w+)?"
                r"(?:\s+WHERE\s+(.+?))?"
                r"(?:\s+ORDER\s+BY\s+(.+?))?"
                r"(?:\s+LIMIT\s+(\d+))?"
                r"(?:\s+OFFSET\s+(\d+))?"
                r";?\s*$",
                query, re.IGNORECASE | re.DOTALL
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
                    logger.info(f"[Mongo] SELECT 1 FROM {table} WHERE {filt} -> exists={self._rowcount > 0}")
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
                logger.info(f"[Mongo] SELECT FROM {table} WHERE {filt} ORDER={sort} LIMIT={limit} -> {self._rowcount} docs")
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)"
                r"\s+ON\s+CONFLICT\s*\((.+?)\)\s+DO\s+NOTHING",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, cols_str, vals_str, conflict_cols = m.groups()
                collection = self.db[table.lower()]
                cols = _parse_columns(cols_str)
                vals = _parse_values_list(vals_str)
                doc = dict(zip(cols, vals))
                if table in ('codes', 'plan_keys'):
                    if 'claimed_by' not in doc:
                        doc['claimed_by'] = None
                    if 'claimed_at' not in doc:
                        doc['claimed_at'] = None
                conflict = _parse_columns(conflict_cols)
                filt = {k: doc[k] for k in conflict if k in doc}
                result = collection.update_one(
                    filt,
                    {"$setOnInsert": doc},
                    upsert=True
                )
                self._rowcount = 1 if result.upserted_id else 0
                self._last_result = []
                self._result_type = "insert"
                logger.info(f"[Mongo] INSERT ... ON CONFLICT DO NOTHING INTO {table} -> {'inserted' if self._rowcount else 'skipped'}")
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)"
                r"\s+ON\s+CONFLICT\s*\((.+?)\)\s+DO\s+UPDATE\s+SET\s+(.+)",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, cols_str, vals_str, conflict_cols, set_clause = m.groups()
                collection = self.db[table.lower()]
                cols = _parse_columns(cols_str)
                vals = _parse_values_list(vals_str)
                doc = dict(zip(cols, vals))
                if table in ('codes', 'plan_keys'):
                    if 'claimed_by' not in doc:
                        doc['claimed_by'] = None
                    if 'claimed_at' not in doc:
                        doc['claimed_at'] = None
                conflict = _parse_columns(conflict_cols)
                filt = {k: doc[k] for k in conflict if k in doc}
                updates = _parse_set_clause(set_clause)
                collection.update_one(
                    filt,
                    {"$set": updates, "$setOnInsert": doc},
                    upsert=True
                )
                self._rowcount = 1
                self._last_result = []
                self._result_type = "update"
                logger.info(f"[Mongo] INSERT ... ON CONFLICT DO UPDATE INTO {table} WHERE {filt}")
                return self

            m = re.match(
                r"INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, cols_str, vals_str = m.groups()
                collection = self.db[table.lower()]
                doc = dict(zip(_parse_columns(cols_str), _parse_values_list(vals_str)))
                if table in ('codes', 'plan_keys'):
                    if 'claimed_by' not in doc:
                        doc['claimed_by'] = None
                    if 'claimed_at' not in doc:
                        doc['claimed_at'] = None
                try:
                    collection.insert_one(doc)
                    self._rowcount = 1
                    logger.info(f"[Mongo] INSERT OR IGNORE INTO {table} -> inserted")
                except Exception as e:
                    if "duplicate" in str(e).lower() or "E11000" in str(e):
                        self._rowcount = 0
                        logger.info(f"[Mongo] INSERT OR IGNORE INTO {table} -> duplicate ignored")
                    else:
                        raise
                self._last_result = []
                self._result_type = "insert"
                return self

            m = re.match(
                r"INSERT\s+INTO\s+(\w+)\s*\((.+?)\)\s*VALUES\s*\((.+?)\)",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, cols_str, vals_str = m.groups()
                collection = self.db[table.lower()]
                doc = dict(zip(_parse_columns(cols_str), _parse_values_list(vals_str)))
                if table in ('codes', 'plan_keys'):
                    if 'claimed_by' not in doc:
                        doc['claimed_by'] = None
                    if 'claimed_at' not in doc:
                        doc['claimed_at'] = None
                result = collection.insert_one(doc)
                self._rowcount = 1
                self._last_result = []
                self._result_type = "insert"
                logger.info(f"[Mongo] INSERT INTO {table} -> _id={result.inserted_id}")
                return self

            m = re.match(
                r"UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\s+WHERE\s+(.+?))?;?\s*$",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, set_clause, where = m.groups()
                collection = self.db[table.lower()]
                filt = _parse_where(where) if where else {}
                updates = _parse_set_clause(set_clause)
                result = collection.update_many(filt, {"$set": updates})
                self._rowcount = result.modified_count
                self._last_result = []
                self._result_type = "update"
                logger.info(f"[Mongo] UPDATE {table} WHERE {filt} -> modified {result.modified_count}")
                return self

            m = re.match(
                r"DELETE\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+?))?;?\s*$",
                query, re.IGNORECASE | re.DOTALL
            )
            if m:
                table, where = m.groups()
                collection = self.db[table.lower()]
                filt = _parse_where(where) if where else {}
                result = collection.delete_many(filt)
                self._rowcount = result.deleted_count
                self._last_result = []
                self._result_type = "delete"
                logger.info(f"[Mongo] DELETE FROM {table} WHERE {filt} -> deleted {result.deleted_count}")
                return self

            logger.error(f"[SQL] UNSUPPORTED QUERY: {query}")
            raise NotImplementedError(f"Unsupported SQL: {query[:200]}")

        except Exception as e:
            logger.error(f"[SQL] ERROR: {e} | Query: {query[:200]}")
            raise

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

# ═══════════════════════════════════════════════════════════════
# MONGODB CONNECTION WRAPPER
# ═══════════════════════════════════════════════════════════════

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
        logger.debug("commit (no-op for MongoDB)")

    def rollback(self):
        self._in_transaction = False
        logger.debug("rollback (no-op for MongoDB)")

    def close(self):
        self._closed = True

    def _close_raw(self):
        self._closed = True


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

class SQLiteThreadedPool:
    def __init__(self, minconn, maxconn, *a, **k):
        self.minconn = minconn
        self.maxconn = maxconn
        logger.info("SQLiteThreadedPool mocked for MongoDB")

class PooledConn:
    def __init__(self):
        self.conn = None
    def __enter__(self):
        db = _get_db()
        self.conn = MongoConnectionWrapper(db)
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

def get_db_connection():
    db = _get_db()
    return MongoConnectionWrapper(db)

def _release_connection(conn):
    if conn:
        conn.close()

class NoOpCache(dict):
    def get(self, key, default=None):
        return None
    def __getitem__(self, key):
        raise KeyError(key)
    def __setitem__(self, key, value):
        pass
    def pop(self, key, default=None):
        return None
    def clear(self):
        pass

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
    """Key/value settings store used by sitechk (max price, etc.)."""
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
        logger.debug(f"Stats collection {gate_name}_stats initialized")
    except Exception as e:
        logger.warning(f"Error initializing stats collection {gate_name}: {e}")

def _migrate_numeric_and_date_fields():
    """Convert string numeric and date fields to proper types."""
    try:
        db = _get_db()
        numeric_fields = ['credits', 'is_premium', 'cc_checked', 'cc_charged']
        date_fields = ['joined_at', 'premium_expiry']
        for field in numeric_fields:
            users = db.users.find({field: {"$type": "string"}})
            for user in users:
                val = user[field]
                try:
                    if re.match(r'^-?\d+(\.\d+)?$', val):
                        new_val = int(val) if '.' not in val else float(val)
                    else:
                        new_val = 0
                    db.users.update_one({"_id": user["_id"]}, {"$set": {field: new_val}})
                except Exception as e:
                    logger.warning(f"Could not migrate {field} for user {user.get('user_id')}: {e}")
        for field in date_fields:
            users = db.users.find({field: {"$type": "string"}})
            for user in users:
                val = user[field]
                try:
                    new_val = datetime.fromisoformat(val)
                    db.users.update_one({"_id": user["_id"]}, {"$set": {field: new_val}})
                except Exception as e:
                    logger.warning(f"Could not migrate {field} for user {user.get('user_id')}: {e}")

        for field in ['purchased_on', 'expires_on', 'created_at']:
            receipts = db.receipts.find({field: {"$type": "string"}})
            for receipt in receipts:
                val = receipt[field]
                try:
                    new_val = datetime.fromisoformat(val)
                    db.receipts.update_one({"_id": receipt["_id"]}, {"$set": {field: new_val}})
                except Exception as e:
                    logger.warning(f"Could not migrate {field} for receipt {receipt.get('receipt_id')}: {e}")

        for coll_name in ['codes', 'plan_keys']:
            coll = db[coll_name]
            for doc in coll.find({'claimed_at': {"$type": "string"}}):
                val = doc['claimed_at']
                try:
                    new_val = datetime.fromisoformat(val)
                    coll.update_one({"_id": doc["_id"]}, {"$set": {'claimed_at': new_val}})
                except Exception as e:
                    logger.warning(f"Could not migrate claimed_at for {coll_name} {doc.get('_id')}: {e}")

        logger.info("Numeric and date field migration completed")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

def initialize_schema():
    logger.info("Starting MongoDB schema initialization...")
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
        logger.info("MongoDB schema initialization completed successfully")
    except Exception as e:
        logger.error(f"Schema initialization failed: {e}")
        raise

# ═══════════════════════════════════════════════════════════════
# USER MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        db = _get_db()
        user = db.users.find_one({"user_id": user_id})
        if user:
            user = _convert_dates_in_doc(user)
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
            "joined_at": datetime.now()
        }
        result = db.users.update_one(
            {"user_id": user_id},
            {"$setOnInsert": user_doc},
            upsert=True
        )
        if result.upserted_id:
            logger.info(f"Created user {user_id} ({username})")
        else:
            logger.debug(f"User {user_id} already exists")
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
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"credits": new_credits}}
        )
        logger.debug(f"Updated credits for user {user_id} to {new_credits}")
    except Exception as e:
        logger.error(f"Error updating credits for user {user_id}: {e}")

def deduct_credits_atomic(user_id: int, amount: int) -> int:
    try:
        db = _get_db()
        result = db.users.find_one_and_update(
            {"user_id": user_id, "credits": {"$gte": amount}},
            {"$inc": {"credits": -amount}},
            return_document=True
        )
        if result:
            return result.get("credits", 0)
        else:
            return -1
    except Exception as e:
        logger.error(f"Error deducting credits for user {user_id}: {e}")
        return -1

def get_premium_status(user_id: int) -> Tuple[bool, Optional[datetime]]:
    try:
        db = _get_db()
        user = db.users.find_one(
            {"user_id": user_id},
            {"is_premium": 1, "premium_expiry": 1}
        )
        if not user:
            return (False, None)
        is_premium = user.get("is_premium", 0)
        expiry = user.get("premium_expiry")
        if is_premium and expiry:
            if isinstance(expiry, str):
                try:
                    expiry = datetime.fromisoformat(expiry)
                except:
                    expiry = None
            if expiry and datetime.now() < expiry:
                return (True, expiry)
            else:
                db.users.update_one(
                    {"user_id": user_id},
                    {"$set": {"is_premium": 0, "premium_expiry": None, "credits": 150}}
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
            upsert=True
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
            upsert=True
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
            upsert=True
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
        for field in ['cc_checked', 'cc_charged']:
            val = user.get(field)
            if isinstance(val, str):
                try:
                    val = int(val)
                except:
                    val = 0
                updates[field] = val
        if updates:
            db.users.update_one({"user_id": user_id}, {"$set": updates})
        if is_charged:
            db.users.update_one(
                {"user_id": user_id},
                {"$inc": {"cc_checked": 1, "cc_charged": 1}}
            )
        else:
            db.users.update_one(
                {"user_id": user_id},
                {"$inc": {"cc_checked": 1}}
            )
        logger.debug(f"Updated stats for user {user_id} (charged={is_charged})")
    except Exception as e:
        logger.error(f"Error updating stats for user {user_id}: {e}")

def get_user_sites_db(user_id: int) -> List[str]:
    try:
        db = _get_db()
        sites = db.user_sites.find(
            {"user_id": int(user_id)},
            {"url": 1, "_id": 0}
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
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            url = url.lower().rstrip('/')
            try:
                result = db.user_sites.update_one(
                    {"user_id": int(user_id), "url": url},
                    {"$setOnInsert": {
                        "user_id": int(user_id),
                        "url": url,
                        "price": 0.0,
                        "added_at": datetime.now()
                    }},
                    upsert=True
                )
                if result.upserted_id is not None:
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
# CODES MANAGEMENT
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
            "created_at": datetime.now()
        })
        logger.info(f"Added code {code} for {duration_days} days")
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
            {"$set": {"claimed_by": user_id, "claimed_at": datetime.now()}}
        )
        duration = code_doc.get("duration_days", 0)
        expiry = datetime.now() + timedelta(days=duration)
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": 1, "premium_expiry": expiry}},
            upsert=True
        )
        logger.info(f"User {user_id} claimed code {code}")
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
            "created_at": datetime.now()
        })
        logger.info(f"Added plan key {key}")
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
            {"$set": {"claimed_by": user_id, "claimed_at": datetime.now()}}
        )
        duration = key_doc.get("duration_days", key_doc.get("days", 0))
        expiry = datetime.now() + timedelta(days=duration)
        db.users.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": 1, "premium_expiry": expiry}},
            upsert=True
        )
        logger.info(f"User {user_id} claimed plan key {key}")
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
            projection={"plan": 1, "_id": 0}
        )
        return receipt.get("plan") if receipt else None
    except Exception as e:
        logger.error(f"Error fetching latest receipt plan for user {user_id}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# RECEIPTS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def save_receipt(user_id: int, receipt_id: str, amount: float, currency: str = "USD", metadata: dict = None) -> bool:
    try:
        db = _get_db()
        db.receipts.insert_one({
            "receipt_id": receipt_id,
            "user_id": user_id,
            "amount": amount,
            "currency": currency,
            "metadata": metadata or {},
            "created_at": datetime.now()
        })
        logger.info(f"Saved receipt {receipt_id} for user {user_id}")
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
            receipt["_id"] = str(receipt["_id"])
        return receipt
    except Exception as e:
        logger.error(f"Error fetching receipt {receipt_id}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# PROXIES MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def add_proxy(user_id: int, proxy: str) -> bool:
    try:
        db = _get_db()
        result = db.proxies.update_one(
            {"user_id": user_id, "proxy": proxy},
            {"$setOnInsert": {
                "user_id": user_id,
                "proxy": proxy,
                "added_at": datetime.now()
            }},
            upsert=True
        )
        return result.upserted_id is not None
    except Exception as e:
        logger.error(f"Error adding proxy for user {user_id}: {e}")
        return False

def get_proxies(user_id: int) -> List[str]:
    try:
        db = _get_db()
        proxies = db.proxies.find(
            {"user_id": user_id},
            {"proxy": 1, "_id": 0}
        ).sort("_id", 1)
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
    """Return every proxy in the database along with the user_id that owns it.
    Sorted by user_id for a clean output."""
    try:
        db = _get_db()
        cursor = db.proxies.find(
            {},
            {"user_id": 1, "proxy": 1, "_id": 0}
        ).sort("user_id", 1)
        result = []
        for p in cursor:
            result.append({
                "user_id": p.get("user_id"),
                "proxy": p.get("proxy"),
            })
        return result
    except Exception as e:
        logger.error(f"Error fetching all proxies with users: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
# BANNED USERS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def ban_user(user_id: int, reason: str = "") -> bool:
    try:
        db = _get_db()
        db.banned_users.update_one(
            {"user_id": user_id},
            {"$set": {"reason": reason, "banned_at": datetime.now()}},
            upsert=True
        )
        logger.info(f"Banned user {user_id}")
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
# STATS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def log_stat(gate: str, user_id: int, status: str, extra: dict = None) -> bool:
    try:
        db = _get_db()
        collection = db[f"{gate}_stats"]
        collection.insert_one({
            "user_id": user_id,
            "status": status,
            "timestamp": datetime.now(),
            "extra": extra or {}
        })
        logger.debug(f"Logged stat for gate {gate}, user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error logging stat for gate {gate}, user {user_id}: {e}")
        return False

def get_stats(gate: str, user_id: int = None, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        db = _get_db()
        collection = db[f"{gate}_stats"]
        filt = {}
        if user_id is not None:
            filt["user_id"] = user_id
        stats = collection.find(filt).sort("timestamp", -1).limit(limit)
        result = []
        for s in stats:
            s = _convert_dates_in_doc(s)
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
    """Return the top N users sorted by cc_charged (descending).
    Only users with at least 1 charged hit are included."""
    try:
        db = _get_db()
        users = db.users.find(
            {"cc_charged": {"$gt": 0}},
            {"user_id": 1, "username": 1, "first_name": 1, "cc_checked": 1, "cc_charged": 1}
        ).sort("cc_charged", -1).limit(limit)

        result = []
        for u in users:
            charged = u.get("cc_charged", 0)
            hits = u.get("cc_checked", 0)
            try:
                charged = int(charged)
            except (ValueError, TypeError):
                charged = 0
            try:
                hits = int(hits)
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
    """Return the user's rank, charged count, hits, and rate.
    Returns None only if the user has zero hits (no activity at all).
    If the user has hits but 0 charged, `rank` is None so they can be
    shown without a rank."""
    try:
        db = _get_db()
        user = db.users.find_one(
            {"user_id": user_id},
            {"cc_checked": 1, "cc_charged": 1, "username": 1, "first_name": 1}
        )
        if not user:
            return None

        charged = user.get("cc_charged", 0)
        hits = user.get("cc_checked", 0)
        try:
            charged = int(charged)
        except (ValueError, TypeError):
            charged = 0
        try:
            hits = int(hits)
        except (ValueError, TypeError):
            hits = 0

        if hits == 0:
            return None   # no activity at all

        rate = (charged / hits * 100) if hits > 0 else 0.0

        if charged == 0:
            return {
                "rank": None,
                "total": db.users.count_documents({"cc_charged": {"$gt": 0}}),
                "charged": 0,
                "hits": hits,
                "rate": rate,
                "username": user.get("username"),
                "first_name": user.get("first_name"),
            }

        higher = db.users.count_documents({"cc_charged": {"$gt": charged}})
        total = db.users.count_documents({"cc_charged": {"$gt": 0}})

        return {
            "rank": higher + 1,
            "total": total,
            "charged": charged,
            "hits": hits,
            "rate": rate,
            "username": user.get("username"),
            "first_name": user.get("first_name"),
        }
    except Exception as e:
        logger.error(f"Error fetching user rank for {user_id}: {e}")
        return None

def clear_all_charge_stats() -> int:
    """Reset cc_checked and cc_charged to 0 for ALL users.
    Returns the number of users affected."""
    try:
        db = _get_db()
        result = db.users.update_many(
            {"$or": [
                {"cc_checked": {"$ne": 0}},
                {"cc_charged": {"$ne": 0}},
            ]},
            {"$set": {"cc_checked": 0, "cc_charged": 0}}
        )
        logger.info(f"Cleared charge stats for {result.modified_count} users")
        return result.modified_count
    except Exception as e:
        logger.error(f"Error clearing charge stats: {e}")
        return 0

# ═══════════════════════════════════════════════════════════════
# GET ALL USER IDs (for broadcast)
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
            u["_id"] = str(u["_id"])
            result.append(u)
        return result
    except Exception as e:
        logger.error(f"Error fetching all users: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
# GLOBAL SITES MANAGEMENT (replaces sites.txt)
# ═══════════════════════════════════════════════════════════════

def get_global_sites() -> List[str]:
    try:
        db = _get_db()
        sites = db.global_sites.find({}, {"url": 1, "_id": 0})
        return [s["url"] for s in sites]
    except Exception as e:
        logger.error(f"Error fetching global sites: {e}")
        return []

def set_global_sites(sites: List[str]) -> int:
    try:
        db = _get_db()
        db.global_sites.drop()
        if sites:
            docs = [{"url": s.strip(), "added_at": datetime.now()} for s in sites if s and s.strip()]
            if docs:
                db.global_sites.insert_many(docs)
        return len(docs) if sites else 0
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
            if not s.startswith(('http://', 'https://')):
                s = 'https://' + s
            s = s.rstrip('/').lower()
            result = db.global_sites.update_one(
                {"url": s},
                {"$setOnInsert": {"url": s, "added_at": datetime.now()}},
                upsert=True
            )
            if result.upserted_id:
                added += 1
        logger.info(f"Added {added} global sites")
        return added
    except Exception as e:
        logger.error(f"Error adding global sites: {e}")
        return 0

def clear_global_sites() -> int:
    try:
        db = _get_db()
        result = db.global_sites.delete_many({})
        return result.deleted_count
    except Exception as e:
        logger.error(f"Error clearing global sites: {e}")
        return 0

# ═══════════════════════════════════════════════════════════════
# SETTINGS MANAGEMENT (key/value runtime config, used by /setprice)
# ═══════════════════════════════════════════════════════════════

def get_setting(key: str, default=None):
    """Return the value of a setting, or `default` if not set."""
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
    """Upsert a setting value. Returns True on success."""
    try:
        db = _get_db()
        db.settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": value, "updated_at": datetime.now()}},
            upsert=True,
        )
        logger.info(f"Setting '{key}' updated to {value!r}")
        return True
    except Exception as e:
        logger.error(f"Error setting '{key}': {e}")
        return False

# ═══════════════════════════════════════════════════════════════
# PENDING FEEDBACK MANAGEMENT (persists across restarts)
# ═══════════════════════════════════════════════════════════════

def save_pending_feedback(pid: str, data: dict) -> bool:
    """Store a pending feedback entry."""
    try:
        db = _get_db()
        data["_id"] = pid
        data["created_at"] = datetime.now()
        db.pending_feedback.replace_one({"_id": pid}, data, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Error saving pending feedback {pid}: {e}")
        return False

def get_pending_feedback(pid: str) -> Optional[dict]:
    """Retrieve a pending feedback entry."""
    try:
        db = _get_db()
        return db.pending_feedback.find_one({"_id": pid})
    except Exception as e:
        logger.error(f"Error fetching pending feedback {pid}: {e}")
        return None

def delete_pending_feedback(pid: str) -> bool:
    """Delete a pending feedback entry (after approve/reject)."""
    try:
        db = _get_db()
        result = db.pending_feedback.delete_one({"_id": pid})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error deleting pending feedback {pid}: {e}")
        return False

def cleanup_old_pending_feedback(hours: int = 48) -> int:
    """Remove pending feedback older than N hours. Returns count deleted."""
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
    initialize_schema()
    logger.info("MongoDB database module initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize MongoDB database: {e}")
    raise
