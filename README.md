# NIFTY 500 Trend Bot

Phase 1: Dhan-only multi-timeframe stock classification.

The app classifies the NIFTY 500 universe into:

- BUY SET: positive return over 1 year, 6 months, 1 month, and 1 week.
- SELL SET: negative return over 1 year, 6 months, 1 month, and 1 week.

It also determines the current NIFTY 500 market mode from live index LTP versus PDC:

- LTP > PDC → BUY mode
- LTP < PDC → SELL mode
- LTP = PDC → NEUTRAL

All market and historical data used for the scan come from Dhan endpoints. No entry, exit, order, or paper-trading logic is included in this phase.

## Secrets

Set these environment variables for Streamlit or local execution:

`DHAN_CLIENT_ID`

`DHAN_ACCESS_TOKEN`
