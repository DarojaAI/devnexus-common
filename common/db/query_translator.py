"""Translate asyncpg-style ``$N`` parameters to psycopg 3 ``%s`` placeholders.

Foundation for the psycopg 3 migration (issues #11 and #27). PostgreSQL's
``asyncpg`` driver uses numbered placeholders like ``$1``, ``$2``; psycopg 3
(DB-API) uses ``%s`` for every positional parameter. Rather than rewrite
~300 queries across downstream repos, we translate at the boundary inside
``DatabaseManager`` before handing SQL to psycopg 3.

The walker is character-by-character so it stays correct in the presence
of every place PostgreSQL lets you embed a literal ``$``:

* single-quoted string literals (``'…$1…'``) — including the ``''`` escape
* double-quoted identifiers (``"…$1…"``)
* dollar-quoted blocks (``$$…$$`` and ``$tag$…$tag$``) used by PL/pgSQL
* line (``-- …``) and block (``/* … */``) comments
* ``$``-prefixed numbers (e.g. ``-1.50``) which are NOT parameters

Example::

    >>> translate_asyncpg_to_psycopg("SELECT * FROM t WHERE id = $1")
    'SELECT * FROM t WHERE id = %s'
    >>> translate_asyncpg_to_psycopg("SELECT '$1 literal' FROM t")
    "SELECT '$1 literal' FROM t"
"""

from __future__ import annotations

__all__ = ["translate_asyncpg_to_psycopg"]


def _read_dollar_tag(sql: str, i: int) -> tuple[str | None, int]:
    """Try to read a ``$tag$`` opener starting at position ``i``.

    Returns ``(tag, index_after_closing_dollar)`` on success, or
    ``(None, i)`` if the characters at ``i`` do not form a valid opener.
    A valid tag is either empty (``$$``) or starts with a letter/underscore
    and continues with letters, digits, and underscores — the same rule
    PostgreSQL uses to disambiguate dollar-quoted strings from ``$N``
    parameters.

    The caller is expected to be in the ``normal`` state; this helper does
    not advance ``i`` past characters that are not part of a valid tag.
    """
    if i >= len(sql) or sql[i] != "$":
        return None, i
    j = i + 1
    # Empty tag: ``$$``.
    if j < len(sql) and sql[j] == "$":
        return sql[i : j + 1], j + 1
    # Tagged form: ``$identifier$``.
    if j >= len(sql):
        return None, i
    first = sql[j]
    if not (("a" <= first <= "z") or ("A" <= first <= "Z") or first == "_"):
        return None, i
    j += 1
    while j < len(sql):
        c = sql[j]
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c == "_":
            j += 1
            continue
        break
    if j >= len(sql) or sql[j] != "$":
        return None, i
    return sql[i : j + 1], j + 1


def translate_asyncpg_to_psycopg(sql: str) -> str:
    """Rewrite ``$1, $2, $3`` placeholders to ``%s, %s, %s`` for psycopg 3.

    Correctly handles: 'string literals', "identifiers", $$dollar quoted$$,
    $tag$...$tag$, -- line comments, /* block comments */, and $-prefixed
    numbers (e.g. -1.50, which is NOT a parameter).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    # State names: 'normal', 'single', 'double', 'line_comment',
    # 'block_comment', 'dollar_quoted'.
    state = "normal"
    # Closing tag for the current dollar-quoted block, e.g. ``$func$``.
    dollar_tag = ""

    while i < n:
        ch = sql[i]

        if state == "line_comment":
            out.append(ch)
            if ch == "\n":
                state = "normal"
            i += 1
            continue

        if state == "block_comment":
            if ch == "*" and i + 1 < n and sql[i + 1] == "/":
                out.append("*/")
                state = "normal"
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        if state == "single":
            if ch == "'":
                # SQL-standard doubled-quote escape: '' inside a string literal.
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                out.append("'")
                state = "normal"
                i += 1
                continue
            out.append(ch)
            i += 1
            continue

        if state == "double":
            if ch == '"':
                out.append('"')
                state = "normal"
                i += 1
                continue
            out.append(ch)
            i += 1
            continue

        if state == "dollar_quoted":
            if ch == "$" and sql.startswith(dollar_tag, i):
                out.append(dollar_tag)
                state = "normal"
                i += len(dollar_tag)
                dollar_tag = ""
                continue
            out.append(ch)
            i += 1
            continue

        # state == "normal"
        if ch == "'":
            out.append("'")
            state = "single"
            i += 1
            continue
        if ch == '"':
            out.append('"')
            state = "double"
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            out.append("--")
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            out.append("/*")
            state = "block_comment"
            i += 2
            continue
        if ch == "$":
            # 1) Dollar-quoted opener: $$ or $tag$.
            tag, next_i = _read_dollar_tag(sql, i)
            if tag is not None:
                out.append(tag)
                dollar_tag = tag
                state = "dollar_quoted"
                i = next_i
                continue
            # 2) $N parameter: a digit must follow the $ immediately.
            if i + 1 < n and "0" <= sql[i + 1] <= "9":
                out.append("%s")
                i += 1  # skip the leading $
                while i < n and "0" <= sql[i] <= "9":
                    i += 1
                continue
            # 3) Stray $ (e.g. $-1.50). Pass through verbatim.
            out.append("$")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)
