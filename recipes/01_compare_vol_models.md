# Compare volatility models on an American equity option

---
asof: "2026-09-10"
symbol: SPY
---

Load the SPY option chain as of 2026-09-10.

Define target_option as the 30-day ATM American equity put on SPY.

Compare Flat Vol, Term Structure, Local Vol, and Heston volatility models by pricing target_option and ranking their fit to the chain (shown below). Call the winner working_vol_model.
