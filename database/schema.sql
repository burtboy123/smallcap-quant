-- =============================================================================
-- QUANT SYSTEM — DATABASE SCHEMA
-- =============================================================================
-- Supports both PostgreSQL (production) and SQLite (development).
-- SQLite does not support BIGSERIAL or JSONB; the Python ORM (SQLAlchemy)
-- handles dialect-specific types at the ORM layer. This file documents
-- the intended PostgreSQL production schema.
-- =============================================================================

-- Core universe table
CREATE TABLE IF NOT EXISTS stocks (
    ticker VARCHAR(10) PRIMARY KEY,
    company_name VARCHAR(255),
    sector VARCHAR(100),
    industry VARCHAR(100),
    market_cap_category VARCHAR(20),  -- 'small_cap', 'mid_cap'
    is_active BOOLEAN DEFAULT TRUE,
    first_seen_date DATE,
    last_seen_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Daily price data (point-in-time, never modified after insert)
CREATE TABLE IF NOT EXISTS prices (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(10) REFERENCES stocks(ticker),
    date DATE NOT NULL,
    open DECIMAL(12,4),
    high DECIMAL(12,4),
    low DECIMAL(12,4),
    close DECIMAL(12,4),
    adj_close DECIMAL(12,4),
    volume BIGINT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, date)
);

-- Fundamental data with mandatory 90-day reporting lag.
-- CRITICAL: available_date = MAX(filing_date, period_end_date + 90 days)
-- THIS IS THE ONLY DATE USED IN BACKTESTS — never use period_end_date directly.
CREATE TABLE IF NOT EXISTS fundamentals (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(10) REFERENCES stocks(ticker),
    fiscal_year INT NOT NULL,
    fiscal_quarter INT NOT NULL,        -- 1, 2, 3, 4
    period_end_date DATE NOT NULL,      -- When the quarter actually ended
    filing_date DATE,                   -- When it was filed with SEC (may be NULL if unknown)
    available_date DATE NOT NULL,       -- = MAX(filing_date, period_end_date + 90 days)
                                        -- THIS IS THE ONLY DATE USED IN BACKTESTS

    -- Income Statement
    revenue DECIMAL(20,2),
    gross_profit DECIMAL(20,2),
    operating_income DECIMAL(20,2),
    net_income DECIMAL(20,2),
    ebitda DECIMAL(20,2),
    eps_basic DECIMAL(10,4),
    eps_diluted DECIMAL(10,4),
    shares_outstanding BIGINT,

    -- Balance Sheet
    total_assets DECIMAL(20,2),
    total_equity DECIMAL(20,2),
    total_debt DECIMAL(20,2),
    cash_and_equivalents DECIMAL(20,2),
    net_debt DECIMAL(20,2),             -- total_debt - cash_and_equivalents (computed at insert)
    book_value_per_share DECIMAL(10,4),

    -- Cash Flow
    operating_cash_flow DECIMAL(20,2),
    capex DECIMAL(20,2),
    free_cash_flow DECIMAL(20,2),       -- operating_cash_flow - capex (computed at insert)

    -- Derived Ratios (computed at insert time, stored for query performance)
    gross_margin DECIMAL(8,4),          -- gross_profit / revenue
    operating_margin DECIMAL(8,4),      -- operating_income / revenue
    net_margin DECIMAL(8,4),            -- net_income / revenue
    roa DECIMAL(8,4),                   -- net_income / total_assets
    roe DECIMAL(8,4),                   -- net_income / total_equity

    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, fiscal_year, fiscal_quarter)
);

-- Computed factor scores — calculated monthly, stored for speed.
-- All scores are percentile ranks (0-100) within the universe on that date.
CREATE TABLE IF NOT EXISTS factor_scores (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(10) REFERENCES stocks(ticker),
    calculation_date DATE NOT NULL,     -- First trading day of the month

    -- Value Factors (raw values)
    ev_ebitda DECIMAL(10,4),
    pe_ratio DECIMAL(10,4),
    pb_ratio DECIMAL(10,4),
    fcf_yield DECIMAL(8,4),            -- FCF / EV (higher = better)
    earnings_yield DECIMAL(8,4),       -- 1 / PE (higher = better)

    -- Quality Factors (raw values)
    roic DECIMAL(8,4),
    gross_margin DECIMAL(8,4),
    f_score INT,                        -- Piotroski F-Score 0-9
    accruals_ratio DECIMAL(8,4),       -- Lower = better earnings quality
    debt_to_ebitda DECIMAL(8,4),

    -- Momentum Factors (raw values)
    momentum_12_1 DECIMAL(8,4),        -- 12-month return excluding most recent month
    momentum_1m DECIMAL(8,4),          -- 1-month return (reversal signal — not in composite)
    eps_revision_3m DECIMAL(8,4),      -- EPS estimate change over 3 months

    -- Composite Scores (percentile rank 0-100 within universe on this date)
    value_score DECIMAL(6,2),
    quality_score DECIMAL(6,2),
    momentum_score DECIMAL(6,2),
    composite_score DECIMAL(6,2),      -- Weighted: 35% value, 40% quality, 25% momentum

    -- Ranking
    universe_rank INT,                  -- 1 = best in universe
    universe_size INT,                  -- Total stocks ranked this month
    percentile DECIMAL(6,2),           -- Top 1% = 99th percentile

    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, calculation_date)
);

-- Monthly portfolio holdings
CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id BIGSERIAL PRIMARY KEY,
    rebalance_date DATE NOT NULL,
    ticker VARCHAR(10) REFERENCES stocks(ticker),
    composite_score DECIMAL(6,2),
    rank_in_portfolio INT,              -- 1 = highest conviction
    weight DECIMAL(6,4),               -- Portfolio weight (e.g. 0.04 = 4%)
    entry_price DECIMAL(12,4),
    rationale TEXT,                     -- LLM-generated explanation (Phase 5)
    created_at TIMESTAMP DEFAULT NOW()
);

-- Earnings-transcript sentiment from agents/sentiment_agent.py (Claude), cached 90 days.
-- Added to this file 2026-10-04 from the live database's own definition: until then the
-- table existed only in quant_system.db, so a fresh setup never created it, save failed,
-- and the composite's 0.11 sentiment weight silently ran on neutral fill.
CREATE TABLE IF NOT EXISTS sentiment_scores (
    id BIGSERIAL PRIMARY KEY,
    ticker VARCHAR(10) NOT NULL,
    transcript_date DATE NOT NULL,
    available_date DATE NOT NULL,       -- when the score became usable (no lookahead)
    overall_sentiment REAL,             -- -1.0 .. +1.0
    guidance_direction VARCHAR(20),     -- raised | maintained | lowered | mixed | none
    management_confidence REAL,         -- 0.0 .. 1.0
    red_flags TEXT,                     -- JSON list
    key_positives TEXT,                 -- JSON list
    summary TEXT,
    raw_response TEXT,
    model_used VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, transcript_date)
);

-- Backtest results — one row per backtest run
CREATE TABLE IF NOT EXISTS backtest_results (
    id BIGSERIAL PRIMARY KEY,
    run_date TIMESTAMP DEFAULT NOW(),
    strategy_name VARCHAR(100),
    start_date DATE,
    end_date DATE,
    is_in_sample BOOLEAN,
    -- Performance
    total_return DECIMAL(10,4),
    annualized_return DECIMAL(8,4),
    benchmark_return DECIMAL(8,4),
    alpha DECIMAL(8,4),
    sharpe_ratio DECIMAL(8,4),
    sortino_ratio DECIMAL(8,4),
    max_drawdown DECIMAL(8,4),
    -- Costs
    total_transaction_costs DECIMAL(12,2),
    number_of_trades INT,
    -- Parameters
    parameters TEXT,                    -- JSON blob (TEXT in SQLite, JSONB in PostgreSQL)
    notes TEXT
);

-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices(ticker, date);
CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_available ON fundamentals(ticker, available_date);
CREATE INDEX IF NOT EXISTS idx_factor_scores_date ON factor_scores(calculation_date);
CREATE INDEX IF NOT EXISTS idx_factor_scores_composite ON factor_scores(calculation_date, composite_score);
CREATE INDEX IF NOT EXISTS idx_portfolio_holdings_date ON portfolio_holdings(rebalance_date);
CREATE INDEX IF NOT EXISTS idx_sentiment_ticker_available ON sentiment_scores(ticker, available_date);
