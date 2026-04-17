import sqlite3 as _sqlite3
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

import pymysql


IntegrityError = (_sqlite3.IntegrityError, pymysql.err.IntegrityError)


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
        sql = _rewrite_mysql_sql(sql)
        self._cursor.executemany(sql, seq_of_params)
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


def connect(database_target):
    if is_mysql_target(database_target):
        parsed = urlparse(_normalize_mysql_url(database_target))
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
    return _sqlite3.connect(database_target)


def is_mysql_target(database_target):
    return str(database_target).startswith(("mysql://", "mysql+pymysql://"))


def _normalize_mysql_url(database_target):
    if str(database_target).startswith("mysql+pymysql://"):
        return str(database_target).replace("mysql+pymysql://", "mysql://", 1)
    return str(database_target)


def _rewrite_mysql_sql(sql):
    sql = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
    return sql.replace("?", "%s")


def _wrap_mysql_row(cursor, row, row_factory):
    if row is None:
        return None
    if row_factory is Row:
        columns = [item[0] for item in cursor.description]
        return Row(columns, row)
    return row
