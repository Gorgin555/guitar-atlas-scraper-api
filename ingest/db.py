"""
GUITAR ATLAS - Supabase / Postgres 接続ヘルパ
============================================

Supabase の Python クライアントは便利だが、薄い PostgREST 層を経由するため
1万件級の bulk insert にはやや弱い。よってここでは `psycopg` で直接 Postgres に
接続するルートも用意し、用途で使い分ける。

  - read系・少量書き込み: supabase-py（PostgREST）
  - 大量bulk insert / on conflict: psycopg 直接接続

Supabase 接続文字列の取得方法:
  Supabase 管理画面 → Project Settings → Database → Connection string → "URI"
  → 形式: postgresql://postgres:<PASSWORD>@db.<ref>.supabase.co:5432/postgres
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


def get_pg_dsn() -> str:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError(
            "SUPABASE_DB_URL is not set. "
            "Set it in .env (Project Settings → Database → Connection string → URI)."
        )
    return dsn


@contextmanager
def pg_conn() -> Iterator[psycopg.Connection]:
    """psycopg コネクションのコンテキストマネージャ。autocommit=False。"""
    with psycopg.connect(get_pg_dsn(), row_factory=dict_row) as conn:
        yield conn


def get_supabase_client():
    """supabase-py クライアント。読み取り・少量書き込み用。"""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
    return create_client(url, key)
