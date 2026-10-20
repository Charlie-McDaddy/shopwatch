"""The marketplace identity migration preserves listing rows and their dependents."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app import db


def _prepare_pre_0020_database(tmp_path: Path, monkeypatch) -> Path:
    migration_dir = tmp_path / "pre-0020-migrations"
    migration_dir.mkdir()
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "0020_marketplace_listing_identity.sql":
            shutil.copy(path, migration_dir / path.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migration_dir)
    database = tmp_path / "legacy.db"
    db.migrate(database)

    with db.connect(database) as conn:
        conn.execute("INSERT INTO products (name, model) VALUES ('Soundbar', 'MODEL-1')")
        conn.execute("INSERT INTO retailers (name, slug) VALUES ('Store', 'store')")
        conn.execute(
            "INSERT INTO listings (product_id, retailer_id, url, advertised_price, freight) "
            "VALUES (1, 1, 'https://store.example/item', 120.0, 10.0)"
        )
        conn.execute(
            "INSERT INTO field_provenance "
            "(listing_id, field, state, source, manual_locked, note) "
            "VALUES (1, 'advertised_price', 'MANUAL', 'operator', 1, 'confirmed')"
        )
        conn.execute(
            "INSERT INTO price_history "
            "(product_id, listing_id, retailer_id, advertised_price, source) "
            "VALUES (1, 1, 1, 120.0, 'manual')"
        )
        conn.execute(
            "INSERT INTO purchases "
            "(product_id, listing_id, retailer_id, retailer_name, purchased_at, price_paid) "
            "VALUES (1, 1, 1, 'Store', '2026-01-02', 130.0)"
        )
        conn.commit()
    return database


def _apply_0020(database: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "MIGRATIONS_DIR", Path(__file__).parents[1] / "app" / "migrations")
    assert "0020_marketplace_listing_identity.sql" in db.migrate(database)


def test_marketplace_identity_upgrade_preserves_listing_children(tmp_path, monkeypatch):
    database = _prepare_pre_0020_database(tmp_path, monkeypatch)

    _apply_0020(database, monkeypatch)

    with db.connect(database) as conn:
        listing = conn.execute("SELECT * FROM listings WHERE id = 1").fetchone()
        assert (listing["advertised_price"], listing["freight"], listing["offer_key"]) == (
            120.0, 10.0, "",
        )
        provenance = conn.execute(
            "SELECT state, source, manual_locked, note FROM field_provenance WHERE listing_id = 1"
        ).fetchone()
        assert tuple(provenance) == ("MANUAL", "operator", 1, "confirmed")
        history = conn.execute(
            "SELECT listing_id, advertised_price FROM price_history WHERE id = 1"
        ).fetchone()
        assert tuple(history) == (1, 120.0)
        purchase = conn.execute(
            "SELECT listing_id, retailer_id, price_paid FROM purchases WHERE id = 1"
        ).fetchone()
        assert tuple(purchase) == (1, 1, 130.0)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_failed_marketplace_identity_upgrade_rolls_back_rebuild(tmp_path, monkeypatch):
    database = _prepare_pre_0020_database(tmp_path, monkeypatch)
    migration_dir = tmp_path / "failing-migrations"
    migration_dir.mkdir()
    for path in (Path(__file__).parents[1] / "app" / "migrations").glob("*.sql"):
        shutil.copy(path, migration_dir / path.name)
    migration = migration_dir / "0020_marketplace_listing_identity.sql"
    sql = migration.read_text().replace(
        "COMMIT;", "SELECT * FROM injected_migration_failure;\nCOMMIT;"
    )
    migration.write_text(sql)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migration_dir)

    with pytest.raises(sqlite3.OperationalError, match="injected_migration_failure"):
        db.migrate(database)

    with db.connect(database) as conn:
        assert conn.execute("SELECT advertised_price FROM listings WHERE id = 1").fetchone()[0] == 120.0
        assert conn.execute("SELECT COUNT(*) FROM field_provenance WHERE listing_id = 1").fetchone()[0] == 1
        assert conn.execute("SELECT listing_id FROM price_history WHERE id = 1").fetchone()[0] == 1
        assert conn.execute("SELECT listing_id FROM purchases WHERE id = 1").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='listings_rebuilt'").fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
        assert "0020_marketplace_listing_identity.sql" not in applied
