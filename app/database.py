"""MySQL 连接配置与连接生命周期管理。

应用代码通过环境变量读取数据库配置，避免把账号和密码写入源码。查询模块使用
字典游标，使 SQL 返回值能够按字段名读取，减少表字段顺序变化带来的风险。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class DatabaseConfigurationError(RuntimeError):
    """数据库环境变量缺失或格式错误。"""


class DatabaseConnectionError(RuntimeError):
    """数据库连接无法建立。"""


def database_config() -> dict[str, Any]:
    """从环境变量生成 PyMySQL 连接参数。

    服务端代码不能像导入脚本一样交互式询问密码，否则 FastAPI 启动后可能一直
    等待终端输入。因此查询服务要求明确设置 ``MYSQL_PASSWORD``；空密码也可以
    通过将该变量设置为空字符串来表达。
    """

    password = os.getenv("MYSQL_PASSWORD")
    if password is None:
        raise DatabaseConfigurationError(
            "缺少 MYSQL_PASSWORD 环境变量，请在启动查询服务前设置数据库密码"
        )

    port_text = os.getenv("MYSQL_PORT", "3306")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise DatabaseConfigurationError(
            f"MYSQL_PORT 必须是整数，当前值为 {port_text!r}"
        ) from exc

    if not 1 <= port <= 65535:
        raise DatabaseConfigurationError("MYSQL_PORT 必须在 1 到 65535 之间")

    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": port,
        "user": os.getenv("MYSQL_USER", "root"),
        "password": password,
        "database": os.getenv("MYSQL_DATABASE", "my_project"),
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": 10,
        "read_timeout": 30,
    }


@contextmanager
def open_database_connection() -> Iterator[Any]:
    """创建并可靠关闭一个供查询使用的 MySQL 连接。

    当前模块只执行 SELECT，因此启用 autocommit，不创建需要调用方管理的长事务。
    连接失败会转换为项目自己的异常，后续 FastAPI 可以统一映射为服务错误，同时
    不把数据库密码等底层连接参数返回给前端。
    """

    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise DatabaseConfigurationError(
            "缺少 PyMySQL，请先执行: uv add pymysql"
        ) from exc

    try:
        connection = pymysql.connect(
            **database_config(),
            cursorclass=DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseConnectionError("无法连接 MySQL，请检查数据库配置和服务状态") from exc

    try:
        yield connection
    finally:
        connection.close()

