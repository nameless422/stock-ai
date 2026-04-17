from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

import pymysql


IntegrityError = pymysql.err.IntegrityError


class Row(Mapping):
    def __init__(self, columns_or_cursor, values):
        if hasattr(columns_or_cursor, "description"):
            columns = [item[0] for item in columns_or_cursor.description]
        else:
            columns = columns_or_cursor
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self):
        return len(self._mapping)


class MysqlCursorWrapper:
    def __init__(self, cursor, row_factory):
        self._cursor = cursor
        self._row_factory = row_factory

    def execute(self, sql, params=None):
        sql = _rewrite_mysql_sql(sql)
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(_rewrite_mysql_sql(sql), seq_of_params)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return _wrap_mysql_row(self._cursor, row, self._row_factory)

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [_wrap_mysql_row(self._cursor, row, self._row_factory) for row in rows]

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class MysqlConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn
        self.row_factory = None

    def cursor(self):
        return MysqlCursorWrapper(self._conn.cursor(), self.row_factory)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def connect(database_url: str):
    parsed = urlparse(_normalize_mysql_url(database_url))
    if parsed.scheme != "mysql":
        raise ValueError("Only MySQL is supported. Please set STOCK_AI_DB_URL=mysql://user:pass@host:3306/dbname?charset=utf8mb4")
    query = parse_qs(parsed.query)
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        database=(parsed.path or "/").lstrip("/"),
        charset=query.get("charset", ["utf8mb4"])[0],
        autocommit=False,
    )
    return MysqlConnectionWrapper(conn)


def _normalize_mysql_url(database_url: str) -> str:
    if str(database_url).startswith("mysql+pymysql://"):
        return str(database_url).replace("mysql+pymysql://", "mysql://", 1)
    return str(database_url)


def _rewrite_mysql_sql(sql: str) -> str:
    sql = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
    return sql.replace("?", "%s")


def _wrap_mysql_row(cursor, row, row_factory):
    if row is None:
        return None
    if row_factory is Row:
        columns = [item[0] for item in cursor.description]
        return Row(columns, row)
    return row
