# Spec: hourly / 4h up-down book recorder (not installed)

Purpose: give the T4 favourite-underreaction lead a test on the series that now trades (Chainlink range-TWAP settlement).

- Markets: `btc-updown-1h-<epoch>`, `eth-updown-1h-<epoch>`, `btc-updown-4h-<epoch>`, `eth-updown-4h-<epoch>`. Discover each new window from Gamma at T-10 min and subscribe to both tokens.
- Feed: Polymarket market websocket (book, price_change, last_trade_price), with the same binary rows as `shadow_rec.py` (`pm` top of book in exchange time, plus a top-5 depth row every 1 s; `tr` prints with side, size and tx hash). Re-snapshot over REST `/book` every 60 s to check for drift.
- Reference: Chainlink `btc-usd-twap-60s` / `eth-usd-twap-60s` stream values if a public read exists; otherwise Binance spot 1 s (already recorded by `updown-rec`) plus a reconstructed 60 s TWAP. Record the official start price from the market's resolution data after settlement.
- Settlement: Gamma `outcomePrices` after close. Keep the resolution text.
- Load: about 4 markets x 2 tokens of extra subscriptions on the existing recorder box; under 50 MB/day. Run as its own user unit with MemoryMax=300M and nice 10, separate from the live services.
- Analysis gate: 3 weeks of dev, then a frozen TWAP-aware fair value (expected range-average vs start, given the part of the window already elapsed), then 2 weeks of holdout. Bar: edge_net >= +1c/share after half-spread and 0.01 p(1-p), with a 90% interval above 0.
