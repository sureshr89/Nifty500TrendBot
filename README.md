# NIFTY 500 Trend Bot

Live Dhan monitoring with NIFTY 500 market breadth and S1 paper-trading strategies.

## Data and market universe

- The official NIFTY 500 membership defines the 500-stock universe.
- NSE may be used only to identify or verify the official universe.
- Dhan Security IDs are used to map tradable instruments.
- Live stock LTP, index LTP, previous close and trading calculations are sourced from Dhan.
- The dashboard shows Dhan live coverage so missing quotes can be detected.

## Mandatory market basis for new trades

New trades are allowed only when all five broad-market indices and NIFTY 500 breadth agree.

### BUY environment

All of the following must be positive:

- Nifty 50 % change
- Nifty Next 50 % change
- Nifty Midcap 150 % change
- Nifty Smallcap 250 % change
- Nifty 500 % change
- NIFTY 500 A/D ratio above 1 (more advances than declines)

Result: BUY direction is eligible for the strategy filters.

### SELL environment

All of the following must be negative:

- Nifty 50 % change
- Nifty Next 50 % change
- Nifty Midcap 150 % change
- Nifty Smallcap 250 % change
- Nifty 500 % change
- NIFTY 500 A/D ratio below 1 (more declines than advances)

Result: SELL direction is eligible for the strategy filters.

### Any mixed, zero or missing basis

Result: **NO NEW TRADES**.

The market basis is an entry filter only. Existing open positions are not force-exited when an index reverses; normal stop-loss, target and existing exit rules remain active.

## Strategies

The bot maintains S1 paper-trading strategies and applies the configured stock, sector and market filters before allowing a new trade.

## Position and risk controls

- Maximum simultaneous open positions: **4**
- Maximum capital per trade: **₹1.5 lakh**
- Risk and quantity are calculated from the configured stop-loss and position-sizing rules.
- A new trade must pass the configured risk limits before it is opened.

The four-position limit is a simultaneous-open-position limit, not a daily maximum-trades limit.

## Trade exits and history

Paper trades use the configured:

- Stop-loss
- Target
- Existing time/strategy exit logic

Trade history is persisted separately from dashboard display. Dashboard updates must not delete live or closed trade history.

## Dashboard

The Live Market section displays:

- All five broad-market indices
- Dhan live LTP
- Previous close
- Percentage change
- BUY / SELL / NOT ALIGNED direction
- NIFTY 500 advancing and declining stocks
- NIFTY 500 A/D direction
- Final mandatory market basis
- Dhan live coverage

## Secrets

Set these environment variables for Streamlit or local execution:

`DHAN_CLIENT_ID`

`DHAN_ACCESS_TOKEN`
