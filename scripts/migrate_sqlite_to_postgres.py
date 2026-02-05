"""
Copy data from a SQLite database into Postgres using the existing ORM model.

Usage:
    python scripts/migrate_sqlite_to_postgres.py \
        --source sqlite:///eniq.db \
        --target postgresql+psycopg2://eniq:eniq@localhost:5434/eniq
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from eniq_agg.models import Analysis, Base


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def migrate(source_url: str, target_url: str) -> None:
    src_engine = create_engine(source_url, future=True)
    dst_engine = create_engine(target_url, future=True)

    Base.metadata.create_all(dst_engine)
    logger.info("Created schema in target DB")

    with Session(src_engine) as src_session, Session(dst_engine) as dst_session:
        rows = src_session.scalars(select(Analysis)).all()
        logger.info("Found %s rows to migrate", len(rows))
        for row in rows:
            payload: Dict[str, Any] = {
                column.name: getattr(row, column.name)
                for column in Analysis.__table__.columns
            }
            dst_session.merge(Analysis(**payload))
        dst_session.commit()
    logger.info("Migration completed successfully")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite data to Postgres")
    parser.add_argument(
        "--source",
        required=True,
        help="Source DB URL (e.g., sqlite:///eniq.db)",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Target DB URL (e.g., postgresql+psycopg2://user:pass@host:5432/db)",
    )
    args = parser.parse_args()
    migrate(args.source, args.target)


if __name__ == "__main__":
    main()
