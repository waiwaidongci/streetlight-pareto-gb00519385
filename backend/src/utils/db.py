"""数据库方言相关的小工具。"""


def row_locked(db):
    """PostgreSQL 支持 SELECT ... FOR UPDATE；SQLite 等本地方言不支持，返回普通查询。"""
    return db.bind.dialect.name == 'postgresql'
