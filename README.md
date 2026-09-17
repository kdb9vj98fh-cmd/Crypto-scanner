# Crypto Scanner
Binance USD-M Futures scanner for 15m/30m/1h. Runs with GitHub Actions about every 15 minutes, uses closed candles, records signals and later TP/stop outcomes in signals.csv. If stop and target occur in the same candle, status is UNCLEAR. No trading/API keys.

Note: this build uses the 100 most liquid eligible Binance USD-M perpetuals as its universe. A strict current market-cap Top 100 requires an additional market-cap data source.
