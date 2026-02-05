import logging
from typing import Optional

import typer
from sqlalchemy import select

from eniq_agg.config import settings
from eniq_agg.db import get_session
from eniq_agg.ingest import ingest_all
from eniq_agg.models import Analysis
from eniq_agg.bot import start_bot

app = typer.Typer(add_completion=False)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@app.command()
def ingest() -> None:
    """Fetch analyses from eniq.ai and store in DB."""
    ingest_all()


@app.command()
def last(count: int = 5, department: Optional[str] = None) -> None:
    """Print last N analyses (by call_date)."""
    with get_session() as session:
        stmt = select(Analysis).order_by(Analysis.call_date.desc())
        if department:
            stmt = stmt.where(Analysis.department == department)
        stmt = stmt.limit(count)
        rows = session.scalars(stmt).all()
        for row in rows:
            typer.echo(
                f"[{row.call_date}] {row.department or '-'} | {row.representative or '-'} "
                f"| {row.title or row.summary or row.id}"
            )


@app.command()
def bot() -> None:
    """Run Telegram bot for quick stats."""
    if not settings.telegram_bot_token:
        raise typer.BadParameter("TELEGRAM_BOT_TOKEN is not set in env")
    typer.echo("Starting Telegram bot...")
    start_bot()


if __name__ == "__main__":
    app()
