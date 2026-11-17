"""monitoring — pipeline health checks and alerting.

Built after forward_test/2026Q4b silently recorded ZERO sessions from 2026-09-21 to
2026-09-30: snapshot.py wrote 0-row price_snapshots.csv files and exited, and nothing
noticed for nine days. Every check here exists because a specific failure happened or
could plainly happen the same way.

Read-only with respect to the trading system: nothing in this package places an order,
edits a book, or writes to the production DB. It writes only under monitoring/state/.
"""
