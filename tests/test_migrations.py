"""The schema is applied incrementally, so upgrades must work on an old file."""

from app import config, db


def test_existing_database_gains_the_priority_column(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn

    # Build a database at an earlier schema version, as an existing install has.
    monkeypatch.setattr(db, "SCHEMA", db.SCHEMA[:2])
    db.migrate()
    conn = db.connect()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(follow)")}
    assert "priority" not in columns
    conn.execute(
        "INSERT INTO show (id, name) VALUES (1, 'Kept')",
    )
    conn.execute("INSERT INTO follow (show_id, followed_at) VALUES (1, '2024-01-01T00:00:00+00:00')")
    conn.commit()

    # Now upgrade to the current schema.
    monkeypatch.undo()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    db.migrate()

    columns = {row["name"] for row in db.connect().execute("PRAGMA table_info(follow)")}
    assert "priority" in columns
    row = db.connect().execute("SELECT show_id, priority FROM follow").fetchone()
    assert row["show_id"] == 1 and row["priority"] == 0

    db._local.conn.close()
    del db._local.conn


def test_migrate_is_idempotent(database):
    db.migrate()
    db.migrate()
    version = db.connect().execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == len(db.SCHEMA)


def test_a_v4_database_gains_the_cast_column_without_losing_shows(tmp_path, monkeypatch):
    """The live install is at v4 with hundreds of shows; this is that upgrade."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "v4.db")
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn

    monkeypatch.setattr(db, "SCHEMA", db.SCHEMA[:4])
    db.migrate()
    conn = db.connect()
    assert "cast_list" not in {r["name"] for r in conn.execute("PRAGMA table_info(show)")}
    conn.execute("INSERT INTO show (id, name, summary) VALUES (1, 'Kept', 'A show.')")
    conn.execute("INSERT INTO follow (show_id, followed_at) VALUES (1, '2024-01-01T00:00:00+00:00')")
    conn.commit()

    monkeypatch.undo()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "v4.db")
    db.migrate()

    conn = db.connect()
    assert "cast_list" in {r["name"] for r in conn.execute("PRAGMA table_info(show)")}
    row = conn.execute("SELECT name, summary, cast_list FROM show WHERE id = 1").fetchone()
    assert row["name"] == "Kept"
    assert row["summary"] == "A show."
    # Not fetched yet, which is what tells the app to go and look.
    assert row["cast_list"] is None

    db._local.conn.close()
    del db._local.conn
