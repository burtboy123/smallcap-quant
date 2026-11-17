"""
data/storage.py

Database read/write operations using SQLAlchemy ORM.

Design principles:
- All ORM models live here (single source of truth for schema).
- Upsert semantics everywhere — re-running the pipeline never creates duplicates.
- Point-in-time discipline is enforced at the query level, not the application level.
- SQLite in development, PostgreSQL in production — the ORM abstracts the difference.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config.settings import DATABASE_URL, DATA_LAGS


# ---------------------------------------------------------------------------
# Engine / Session Factory
# ---------------------------------------------------------------------------

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
        )
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stocks"

    ticker = Column(String(10), primary_key=True)
    company_name = Column(String(255))
    sector = Column(String(100))
    industry = Column(String(100))
    market_cap_category = Column(String(20))
    is_active = Column(Boolean, default=True)
    first_seen_date = Column(Date)
    last_seen_date = Column(Date)
    created_at = Column(DateTime, default=datetime.utcnow)


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("ticker", "date"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False)
    date = Column(Date, nullable=False)
    open = Column(Numeric(12, 4))
    high = Column(Numeric(12, 4))
    low = Column(Numeric(12, 4))
    close = Column(Numeric(12, 4))
    adj_close = Column(Numeric(12, 4))
    volume = Column(BigInteger)
    created_at = Column(DateTime, default=datetime.utcnow)


class Fundamental(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (UniqueConstraint("ticker", "fiscal_year", "fiscal_quarter"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    fiscal_quarter = Column(Integer, nullable=False)
    period_end_date = Column(Date, nullable=False)
    filing_date = Column(Date)
    available_date = Column(Date, nullable=False)  # ONLY date used in backtests

    # Income Statement
    revenue = Column(Numeric(20, 2))
    gross_profit = Column(Numeric(20, 2))
    operating_income = Column(Numeric(20, 2))
    net_income = Column(Numeric(20, 2))
    ebitda = Column(Numeric(20, 2))
    eps_basic = Column(Numeric(10, 4))
    eps_diluted = Column(Numeric(10, 4))
    shares_outstanding = Column(BigInteger)

    # Balance Sheet
    total_assets = Column(Numeric(20, 2))
    total_equity = Column(Numeric(20, 2))
    total_debt = Column(Numeric(20, 2))
    cash_and_equivalents = Column(Numeric(20, 2))
    net_debt = Column(Numeric(20, 2))
    book_value_per_share = Column(Numeric(10, 4))

    # Cash Flow
    operating_cash_flow = Column(Numeric(20, 2))
    capex = Column(Numeric(20, 2))
    free_cash_flow = Column(Numeric(20, 2))

    # Derived Ratios (stored for query performance)
    gross_margin = Column(Numeric(8, 4))
    operating_margin = Column(Numeric(8, 4))
    net_margin = Column(Numeric(8, 4))
    roa = Column(Numeric(8, 4))
    roe = Column(Numeric(8, 4))

    created_at = Column(DateTime, default=datetime.utcnow)


class FactorScore(Base):
    __tablename__ = "factor_scores"
    __table_args__ = (UniqueConstraint("ticker", "calculation_date"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False)
    calculation_date = Column(Date, nullable=False)

    ev_ebitda = Column(Numeric(10, 4))
    pe_ratio = Column(Numeric(10, 4))
    pb_ratio = Column(Numeric(10, 4))
    fcf_yield = Column(Numeric(8, 4))
    earnings_yield = Column(Numeric(8, 4))

    roic = Column(Numeric(8, 4))
    gross_margin = Column(Numeric(8, 4))
    f_score = Column(Integer)
    accruals_ratio = Column(Numeric(8, 4))
    debt_to_ebitda = Column(Numeric(8, 4))

    momentum_12_1 = Column(Numeric(8, 4))
    momentum_1m = Column(Numeric(8, 4))
    eps_revision_3m = Column(Numeric(8, 4))

    value_score = Column(Numeric(6, 2))
    quality_score = Column(Numeric(6, 2))
    momentum_score = Column(Numeric(6, 2))
    composite_score = Column(Numeric(6, 2))

    universe_rank = Column(Integer)
    universe_size = Column(Integer)
    percentile = Column(Numeric(6, 2))

    # Phase 6: auxiliary signals
    short_interest_ratio    = Column(Numeric(8, 4))
    short_interest_change   = Column(Numeric(8, 4))
    short_interest_score    = Column(Numeric(6, 2))
    insider_buy_count       = Column(Integer)
    insider_sell_count      = Column(Integer)
    insider_score           = Column(Numeric(6, 2))
    beats_last_4q           = Column(Integer)
    avg_surprise_pct        = Column(Numeric(8, 4))
    earnings_surprise_score = Column(Numeric(6, 2))
    proximity_52w           = Column(Numeric(6, 2))
    revenue_accel_score     = Column(Numeric(6, 2))

    created_at = Column(DateTime, default=datetime.utcnow)


class PortfolioHolding(Base):
    __tablename__ = "portfolio_holdings"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    rebalance_date = Column(Date, nullable=False)
    ticker = Column(String(10), nullable=False)
    composite_score = Column(Numeric(6, 2))
    rank_in_portfolio = Column(Integer)
    weight = Column(Numeric(6, 4))
    entry_price = Column(Numeric(12, 4))
    rationale = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_date = Column(DateTime, default=datetime.utcnow)
    strategy_name = Column(String(100))
    start_date = Column(Date)
    end_date = Column(Date)
    is_in_sample = Column(Boolean)
    total_return = Column(Numeric(10, 4))
    annualized_return = Column(Numeric(8, 4))
    benchmark_return = Column(Numeric(8, 4))
    alpha = Column(Numeric(8, 4))
    sharpe_ratio = Column(Numeric(8, 4))
    sortino_ratio = Column(Numeric(8, 4))
    max_drawdown = Column(Numeric(8, 4))
    total_transaction_costs = Column(Numeric(12, 2))
    number_of_trades = Column(Integer)
    parameters = Column(Text)  # JSON string
    notes = Column(Text)


# ---------------------------------------------------------------------------
# Schema Initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    Base.metadata.create_all(get_engine())
    logger.info(f"Database initialised: {DATABASE_URL}")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_stock(session: Session, ticker: str, **kwargs) -> None:
    """Insert or update a stock record."""
    existing = session.get(Stock, ticker)
    if existing:
        for k, v in kwargs.items():
            setattr(existing, k, v)
    else:
        session.add(Stock(ticker=ticker, **kwargs))


def upsert_prices(session: Session, prices_df: pd.DataFrame) -> int:
    """
    Bulk upsert price rows from a DataFrame.
    Expected columns: ticker, date, open, high, low, close, adj_close, volume.
    Returns number of rows written.
    """
    if prices_df.empty:
        return 0

    required = {"ticker", "date", "open", "high", "low", "close", "adj_close", "volume"}
    missing = required - set(prices_df.columns)
    if missing:
        raise ValueError(f"prices_df missing columns: {missing}")

    records = prices_df[list(required)].to_dict("records")
    engine = get_engine()

    with engine.begin() as conn:
        if "sqlite" in DATABASE_URL:
            # SQLite: INSERT OR REPLACE
            stmt = sqlite_insert(Price.__table__)
            conn.execute(stmt.prefix_with("OR REPLACE"), records)
        else:
            # PostgreSQL: ON CONFLICT DO UPDATE
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(Price.__table__).values(records)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ticker", "date"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "adj_close": stmt.excluded.adj_close,
                    "volume": stmt.excluded.volume,
                },
            )
            conn.execute(stmt)

    logger.debug(f"Upserted {len(records)} price rows")
    return len(records)


def upsert_fundamentals(session: Session, fundamentals_df: pd.DataFrame) -> int:
    """
    Bulk upsert fundamental rows.
    Automatically computes available_date if not already in the DataFrame.

    RULE 1 ENFORCEMENT: available_date = MAX(filing_date, period_end_date + 90 days)
    This is the ONLY date used when querying fundamentals during backtesting.
    """
    if fundamentals_df.empty:
        return 0

    df = fundamentals_df.copy()

    # Enforce the 90-day reporting lag (Rule 1)
    lag = timedelta(days=DATA_LAGS["fundamental_lag_days"])

    if "available_date" not in df.columns:
        df["period_end_date"] = pd.to_datetime(df["period_end_date"])
        df["available_date"] = df["period_end_date"] + lag
    else:
        # Verify that supplied available_date respects the minimum lag
        df["period_end_date"] = pd.to_datetime(df["period_end_date"])
        df["available_date"] = pd.to_datetime(df["available_date"])
        min_allowed = df["period_end_date"] + lag
        violations = df["available_date"] < min_allowed
        if violations.any():
            # Correct violations silently — always apply the conservative lag
            df.loc[violations, "available_date"] = min_allowed[violations]
            logger.warning(
                f"Corrected {violations.sum()} available_date values that violated the "
                f"{DATA_LAGS['fundamental_lag_days']}-day reporting lag (Rule 1)."
            )

    # Compute derived fields if missing
    rev = df.get("revenue", pd.Series(float("nan"), index=df.index)).replace(0, float("nan"))
    if "net_debt" not in df.columns:
        df["net_debt"] = df.get("total_debt", 0) - df.get("cash_and_equivalents", 0)
    if "free_cash_flow" not in df.columns:
        df["free_cash_flow"] = df.get("operating_cash_flow", 0) - df.get("capex", 0)
    if "gross_margin" not in df.columns:
        df["gross_margin"] = df.get("gross_profit", float("nan")) / rev
    if "operating_margin" not in df.columns:
        df["operating_margin"] = df.get("operating_income", float("nan")) / rev
    if "net_margin" not in df.columns:
        df["net_margin"] = df.get("net_income", float("nan")) / rev
    if "roa" not in df.columns and "total_assets" in df.columns:
        df["roa"] = df.get("net_income", float("nan")) / df["total_assets"].replace(0, float("nan"))
    if "roe" not in df.columns and "total_equity" in df.columns:
        df["roe"] = df.get("net_income", float("nan")) / df["total_equity"].replace(0, float("nan"))
    # Derive fields that SimFin free plan omits
    # EBITDA proxy: use operating_income (conservative — true EBITDA is always >= EBIT)
    if "ebitda" not in df.columns or df.get("ebitda", pd.Series()).isna().all():
        df["ebitda"] = df.get("operating_income", float("nan"))
    # Book value per share: total_equity / shares_outstanding
    if "book_value_per_share" not in df.columns or df.get("book_value_per_share", pd.Series()).isna().all():
        shares = df.get("shares_outstanding", pd.Series(float("nan"), index=df.index)).replace(0, float("nan"))
        df["book_value_per_share"] = df.get("total_equity", float("nan")) / shares
    # EPS diluted: net_income / shares_outstanding
    if "eps_diluted" not in df.columns or df.get("eps_diluted", pd.Series()).isna().all():
        shares = df.get("shares_outstanding", pd.Series(float("nan"), index=df.index)).replace(0, float("nan"))
        df["eps_diluted"] = df.get("net_income", float("nan")) / shares
    if "eps_basic" not in df.columns or df.get("eps_basic", pd.Series()).isna().all():
        df["eps_basic"] = df.get("eps_diluted", float("nan"))

    count = 0
    for _, row in df.iterrows():
        existing = (
            session.query(Fundamental)
            .filter_by(
                ticker=row["ticker"],
                fiscal_year=row["fiscal_year"],
                fiscal_quarter=row["fiscal_quarter"],
            )
            .first()
        )
        row_dict = row.where(pd.notna(row), None).to_dict()
        if existing:
            for k, v in row_dict.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
        else:
            # Only pass columns that exist on the model
            valid = {k: v for k, v in row_dict.items() if hasattr(Fundamental, k)}
            session.add(Fundamental(**valid))
        count += 1

    session.commit()
    logger.debug(f"Upserted {count} fundamental rows")
    return count


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def load_prices(
    tickers: list[str],
    start: date,
    end: date,
    session: Optional[Session] = None,
) -> pd.DataFrame:
    """
    Load price data for a list of tickers between start and end (inclusive).
    Returns a DataFrame with columns: ticker, date, open, high, low, close, adj_close, volume.
    """
    s = session or get_session()
    try:
        rows = (
            s.query(Price)
            .filter(
                Price.ticker.in_(tickers),
                Price.date >= start,
                Price.date <= end,
            )
            .order_by(Price.ticker, Price.date)
            .all()
        )
        if not rows:
            return pd.DataFrame(
                columns=["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"]
            )
        return pd.DataFrame(
            [
                {
                    "ticker": r.ticker,
                    "date": r.date,
                    "open": float(r.open) if r.open else None,
                    "high": float(r.high) if r.high else None,
                    "low": float(r.low) if r.low else None,
                    "close": float(r.close) if r.close else None,
                    "adj_close": float(r.adj_close) if r.adj_close else None,
                    "volume": int(r.volume) if r.volume else None,
                }
                for r in rows
            ]
        )
    finally:
        if session is None:
            s.close()


def load_fundamentals_as_of(
    tickers: list[str],
    as_of: date,
    session: Optional[Session] = None,
) -> pd.DataFrame:
    """
    Load the most recent fundamental row for each ticker with available_date <= as_of.
    This is the canonical point-in-time query for fundamental data.

    RULE 1: Only returns data that would have been available on `as_of`.
    """
    s = session or get_session()
    try:
        # Subquery: for each ticker, find the max available_date <= as_of
        subq = (
            s.query(
                Fundamental.ticker,
                func.max(Fundamental.available_date).label("max_avail"),
            )
            .filter(
                Fundamental.ticker.in_(tickers),
                Fundamental.available_date <= as_of,
            )
            .group_by(Fundamental.ticker)
            .subquery()
        )

        rows = (
            s.query(Fundamental)
            .join(
                subq,
                (Fundamental.ticker == subq.c.ticker)
                & (Fundamental.available_date == subq.c.max_avail),
            )
            .all()
        )

        if not rows:
            return pd.DataFrame()

        columns = [c.key for c in Fundamental.__table__.columns]
        # Coerce Decimal to float — SQLAlchemy returns Decimal for Numeric columns
        def _coerce(v):
            from decimal import Decimal
            return float(v) if isinstance(v, Decimal) else v

        return pd.DataFrame(
            [{col: _coerce(getattr(r, col)) for col in columns} for r in rows]
        )
    finally:
        if session is None:
            s.close()


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

save_prices = upsert_prices
save_fundamentals = upsert_fundamentals
get_db_connection = get_session


def save_portfolio_holdings(holdings_df: pd.DataFrame, rebalance_date) -> None:
    """
    Save the current portfolio picks to portfolio_holdings table.
    holdings_df must have columns: ticker, composite_score,
    rank_in_portfolio, weight, entry_price
    """
    s = get_session()
    try:
        s.execute(
            text("DELETE FROM portfolio_holdings WHERE rebalance_date = :date"),
            {"date": str(rebalance_date)},
        )
        for _, row in holdings_df.iterrows():
            s.execute(
                text("""
                    INSERT INTO portfolio_holdings
                        (rebalance_date, ticker, composite_score,
                         rank_in_portfolio, weight, entry_price, rationale)
                    VALUES
                        (:date, :ticker, :score, :rank, :weight, :price, :rationale)
                """),
                {
                    "date": str(rebalance_date),
                    "ticker": row["ticker"],
                    "score": float(row["composite_score"]),
                    "rank": int(row["rank_in_portfolio"]),
                    "weight": float(row["weight"]),
                    "price": float(row.get("entry_price", 0)),
                    "rationale": str(row.get("conviction_tier", "")),
                },
            )
        s.commit()
        logger.info(f"Saved {len(holdings_df)} holdings for {rebalance_date}")
    except Exception as e:
        s.rollback()
        logger.error(f"Failed to save holdings: {e}")
        raise
    finally:
        s.close()


def save_backtest_result(result: dict) -> None:
    """Persist a backtest result dict to the database."""
    s = get_session()
    try:
        params = result.pop("parameters", {})
        record = BacktestResult(
            **{k: v for k, v in result.items() if hasattr(BacktestResult, k)},
            parameters=json.dumps(params),
        )
        s.add(record)
        s.commit()
        logger.info(f"Saved backtest result: {result.get('strategy_name', 'unnamed')}")
    finally:
        s.close()
