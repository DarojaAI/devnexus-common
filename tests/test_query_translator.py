"""Unit tests for ``common.db.query_translator``.

These tests pin down the behavior of :func:`translate_asyncpg_to_psycopg`,
the safety-net shim that converts asyncpg's ``$N`` placeholders to psycopg
3's ``%s`` placeholders. The translator is on the hot path for every
downstream query, so each edge case is covered by an explicit test rather
than parametrized (clearer failure messages during the migration).

Edge cases covered:

* basic ``$N`` substitution (single, many, repeated, two-digit)
* single-quoted string literals (with the ``''`` escape)
* double-quoted identifiers
* dollar-quoted strings (both ``$$…$$`` and ``$tag$…$tag$``)
* line and block comments (PG block comments don't nest)
* ``$``-prefixed numbers like ``-1.50`` which are NOT parameters
* type casts (``$1::int``), ``ANY($1)``, function calls
* empty/whitespace-only SQL, no-param SQL, leading/trailing params
"""

from __future__ import annotations


from common.db.query_translator import translate_asyncpg_to_psycopg


# ---------------------------------------------------------------------------
# Basic $N substitution
# ---------------------------------------------------------------------------


class TestBasicSubstitution:
    def test_single_param(self):
        assert (
            translate_asyncpg_to_psycopg("SELECT * FROM t WHERE id = $1")
            == "SELECT * FROM t WHERE id = %s"
        )

    def test_two_params(self):
        sql = "SELECT * FROM t WHERE a = $1 AND b = $2"
        assert translate_asyncpg_to_psycopg(sql) == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_many_params(self):
        sql = "INSERT INTO t (a, b, c) VALUES ($1, $2, $3)"
        assert translate_asyncpg_to_psycopg(sql) == (
            "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)"
        )

    def test_repeated_params(self):
        sql = "SELECT * FROM t WHERE x = $1 OR y = $1"
        assert translate_asyncpg_to_psycopg(sql) == (
            "SELECT * FROM t WHERE x = %s OR y = %s"
        )

    def test_param_number_ten_is_one_param(self):
        # $10 is parameter #10, NOT $1 followed by a literal "0".
        # psycopg sees one placeholder, asyncpg also sees one.
        assert translate_asyncpg_to_psycopg("SELECT $10") == "SELECT %s"


# ---------------------------------------------------------------------------
# String literals
# ---------------------------------------------------------------------------


class TestStringLiterals:
    def test_dollar_inside_single_quote(self):
        sql = "SELECT '$1 literal' FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_inside_double_quote(self):
        sql = 'SELECT "weird$1col" FROM t'
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_escaped_single_quote_preserves_dollar(self):
        sql = "SELECT 'it''s $1' FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_at_end_of_string_literal(self):
        sql = "SELECT 'ends with $' FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_in_middle_of_string(self):
        sql = "SELECT 'a $1 b' FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_in_middle_of_identifier(self):
        sql = 'SELECT "col$1name" FROM t'
        assert translate_asyncpg_to_psycopg(sql) == sql


# ---------------------------------------------------------------------------
# Dollar-quoted strings
# ---------------------------------------------------------------------------


class TestDollarQuotedStrings:
    def test_simple_dollar_dollar(self):
        sql = "SELECT $$PL/pgSQL $1 stays$$"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_tagged_dollar_quoted(self):
        sql = "SELECT $func$body$func$ FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_inside_dollar_quoted(self):
        sql = "SELECT $func$body $1 here$func$ FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_dollar_quoted_with_param_after(self):
        sql = "SELECT $$body$$ || $1"
        assert translate_asyncpg_to_psycopg(sql) == "SELECT $$body$$ || %s"

    def test_tagged_dollar_with_underscore_and_digits(self):
        sql = "SELECT $my_tag_1$x = $1$xmy_tag_1$ FROM t"
        # Inside the dollar-quoted block the $1 is literal; the outer
        # $my_tag_1$ is the opener and xmy_tag_1$ is the closer (it must
        # match the opener byte-for-byte).
        assert translate_asyncpg_to_psycopg(sql) == sql


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class TestComments:
    def test_line_comment(self):
        sql = "SELECT 1 -- comment $1\n FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_line_comment_at_end(self):
        # No trailing newline: the $1 is still inside the line comment.
        sql = "SELECT 1 -- comment $1"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_block_comment(self):
        sql = "SELECT 1 /* skip $1 */ FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_nested_block_comment_no(self):
        # PostgreSQL does NOT nest block comments: the first ``*/`` closes
        # the comment. The $1 inside is still protected, but the $3 after
        # the close is in normal state and gets replaced.
        sql = "/* outer $1 /* inner $2 */ outer $3 */"
        expected = "/* outer $1 /* inner $2 */ outer %s */"
        assert translate_asyncpg_to_psycopg(sql) == expected

    def test_block_comment_followed_by_param(self):
        sql = "SELECT 1 /* $1 */ $2"
        assert translate_asyncpg_to_psycopg(sql) == "SELECT 1 /* $1 */ %s"


# ---------------------------------------------------------------------------
# Negative numbers
# ---------------------------------------------------------------------------


class TestNegativeNumbers:
    def test_negative_integer(self):
        sql = "SELECT * FROM t WHERE x > $-1"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_negative_decimal(self):
        sql = "SELECT * FROM t WHERE x = $-1.50"
        assert translate_asyncpg_to_psycopg(sql) == sql


# ---------------------------------------------------------------------------
# Type casts and array constructors
# ---------------------------------------------------------------------------


class TestTypeCasts:
    def test_param_with_cast_int(self):
        sql = "SELECT $1::int"
        assert translate_asyncpg_to_psycopg(sql) == "SELECT %s::int"

    def test_param_with_cast_text(self):
        sql = "SELECT $2::text"
        assert translate_asyncpg_to_psycopg(sql) == "SELECT %s::text"

    def test_param_inside_any_array(self):
        sql = "SELECT * FROM t WHERE col = ANY($1)"
        assert translate_asyncpg_to_psycopg(sql) == (
            "SELECT * FROM t WHERE col = ANY(%s)"
        )

    def test_param_inside_function_call(self):
        sql = "SELECT lower($1)"
        assert translate_asyncpg_to_psycopg(sql) == "SELECT lower(%s)"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string(self):
        assert translate_asyncpg_to_psycopg("") == ""

    def test_only_whitespace(self):
        assert translate_asyncpg_to_psycopg("   \n  ") == "   \n  "

    def test_no_params(self):
        sql = "SELECT 1 FROM t"
        assert translate_asyncpg_to_psycopg(sql) == sql

    def test_param_at_start(self):
        assert translate_asyncpg_to_psycopg("$1 = 1") == "%s = 1"

    def test_param_at_end(self):
        assert (
            translate_asyncpg_to_psycopg("SELECT 1 WHERE x = $1")
            == "SELECT 1 WHERE x = %s"
        )

    def test_consecutive_params(self):
        assert translate_asyncpg_to_psycopg("$1$2") == "%s%s"

    def test_string_concat_with_param(self):
        assert translate_asyncpg_to_psycopg("SELECT 'a' || $1") == "SELECT 'a' || %s"

    def test_multiline_sql(self):
        sql = "SELECT *\nFROM t\nWHERE id = $1"
        assert translate_asyncpg_to_psycopg(sql) == ("SELECT *\nFROM t\nWHERE id = %s")

    def test_two_params_separated_by_semicolon(self):
        assert translate_asyncpg_to_psycopg("SELECT $1; $2") == "SELECT %s; %s"

    def test_param_followed_by_arithmetic_operator(self):
        assert translate_asyncpg_to_psycopg("SELECT $1 + 1") == "SELECT %s + 1"

    def test_string_with_only_dollar(self):
        assert translate_asyncpg_to_psycopg("SELECT '$' FROM t") == "SELECT '$' FROM t"
