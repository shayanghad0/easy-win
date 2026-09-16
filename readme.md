# MT5 Single-Trade Manager

A terminal-based trading assistant for **MetaTrader 5** that opens a single
market position, monitors it with a live Rich dashboard, and closes it when
your dollar-based Take-Profit or Stop-Loss target is hit.

Designed for **discretionary traders** who want to decide *when* to enter but
let the script enforce disciplined, fixed-dollar exits — with a Ctrl+C
"detach" mode that hands the trade over to the broker.

---

## ✨ Features

- **One-shot market execution** — `buy` or `sell` from the command line.
- **Dollar-based TP/SL** (e.g. `+$25 / −$12.75`) instead of price/pip targets.
- **Broker-agnostic price conversion** using `mt5.order_calc_profit()` as a probe
  — reliable on XAUUSD where `trade_tick_value` is often wrong.
- **Live Rich dashboard**: entry/current price, swap, commission, P&L bar,
  rolling event log, and elapsed timer.
- **Detach on Ctrl+C**: pushes broker-side TP/SL and exits cleanly, so the
  trade keeps running on MT5's servers even if your PC shuts down.
- **Automatic filling-mode detection** (FOK / IOC) per symbol.
- **Auto-close** when the target or stop threshold is reached.

---

## 📋 Requirements

| Requirement | Version |
|---|---|
| Python | 3.8+ |
| MetaTrader 5 terminal | Windows (MT5 Python API is Windows-only) |
| Broker account | Any MT5 broker with the target symbol enabled |

### Python packages

```bash
pip install MetaTrader5 rich
```

---

## 🚀 Installation

1. **Install MetaTrader 5** and log in to your broker account.
2. Make sure **Algo Trading** is enabled in the MT5 toolbar.
3. Install the Python dependencies:
   ```bash
   pip install MetaTrader5 rich
   ```
4. Clone / copy `order.py` to a folder of your choice.

> ⚠️ The `MetaTrader5` Python module only works on **Windows**.
> On Linux/macOS you'll need a Windows VM, Wine, or a remote MT5 host.

---

## ⚙️ Configuration

Edit the top of `order.py`:

```python
SYMBOL         = "XAUUSD"   # Trading symbol
LOT            = 0.05       # Position size in lots
DEVIATION      = 20         # Max slippage in points
MAGIC          = 234000     # EA/magic number identifier
PROFIT_TP      = 25.00      # $ profit target
PROFIT_SL      = -12.75     # $ loss target (negative)
CHECK_INTERVAL = 1          # Polling interval in seconds
```

| Setting | Description |
|---|---|
| `SYMBOL` | Any symbol your broker offers (`XAUUSD`, `EURUSD`, `US30`, …). |
| `LOT` | Position volume. Use your broker's minimum (often 0.01). |
| `DEVIATION` | Max price slippage the broker may apply, in **points**. |
| `MAGIC` | Unique integer to tag orders from this script. |
| `PROFIT_TP` | Gross price-based profit that triggers a close. |
| `PROFIT_SL` | Gross price-based loss that triggers a close (must be negative). |
| `CHECK_INTERVAL` | How often the loop polls MT5 (seconds). |

> **Note on fees:** `PROFIT_TP` / `PROFIT_SL` are compared against
> `pos.profit` (price P&L only). See **Known Limitations** below.

---

## ▶️ Usage

```bash
python order.py buy     # open a BUY position
python order.py sell    # open a SELL position
```

Any other argument prints usage and exits.

### What happens at runtime

1. Connects to MT5, validates the symbol, resolves the correct filling mode.
2. Fetches the current tick and sends a market order.
3. Enters a live monitoring loop:
   - Displays the dashboard (4 FPS refresh).
   - Logs milestone events (every $1 of profit).
   - Closes when profit ≥ `PROFIT_TP` or ≤ `PROFIT_SL`.
4. Prints a final summary panel.

### Detach mode (Ctrl+C)

Pressing **Ctrl+C** while the dashboard is live does *not* close the trade.
Instead it:

1. Computes TP/SL **prices** corresponding to your dollar targets.
2. Sends a `TRADE_ACTION_SLTP` request to the broker.
3. Exits the script.

From that point on, MT5 itself manages the exit — you can turn off your PC.

---

## 📊 Dashboard Explained

```
┌───────────────────────────────────────────┐
│  ● XAUUSD  ▲ BUY      ⏱  00:04:21  ● LIVE │
├───────────────────────────────────────────┤
│  Ticket   123456789    Volume   0.05      │
│  Entry    2654.32      Current  2657.10   │
│  TP       +$25.00      SL       -$12.75   │
│  Swap     -0.42        Commission  -0.35  │
├───────────────────────────────────────────┤
│                  $+8.14 USD                │
│   SL │━━━━━━━━━●─────────────────────│ TP  │
├───────────────────────────────────────────┤
│  Events                                    │
│   14:02:11  Position opened @ 2654.32000   │
│   14:03:02  Profit reached +$5.00          │
├───────────────────────────────────────────┤
│        Press Ctrl+C to set broker-side    │
│        TP/SL and detach                    │
└───────────────────────────────────────────┘
```

- **P&L bar** fills from SL (red) to TP (green) as `pos.profit` moves.
- **Event log** keeps the last 6 entries.
- **Border color** of the P&L panel matches profit sign.

---

## 🧠 How the Dollar → Price Conversion Works

Because `trade_tick_value` is unreliable for metals, the script probes
MT5 with a hypothetical 1.00 price-unit move and asks MT5 itself what that
would be worth:

```python
probe_price  = entry + 1.0  # for BUY
probe_profit = mt5.order_calc_profit(ORDER_TYPE_BUY, SYMBOL, volume, entry, probe_price)
profit_per_unit = abs(probe_profit)   # $ per 1.00 of price
delta_tp = abs(PROFIT_TP) / profit_per_unit
tp_price = entry + delta_tp
```

The resulting TP/SL prices are then clamped to respect
`trade_stops_level` (broker's minimum distance in points).

---

## ⚠️ Known Limitations

These are worth understanding **before you trade real money**.

1. **Fees are not included in triggers.**  
   TP/SL are compared against `pos.profit` — price P&L only. Commission
   and swap are **not** subtracted. A `+$25` exit may net `$23` or less
   after fees, especially on gold held overnight.

2. **Swap accumulates silently.**  
   On XAUUSD, swap can be $5–$15 per 0.05 lot per week. Long holds can
   eat most of your target.

3. **`result.order` is used as the position ticket.**  
   On most brokers order ticket == position ticket for market orders,
   but this is not guaranteed. On some accounts (especially netting)
   the very first loop iteration may think the position vanished.

4. **Filling mode with `allowed = 0`** (broker says "client chooses")  
   causes an immediate fatal exit. Should default to IOC instead.

5. **Position is not protected on crash.**  
   Broker-side SL is only set on **Ctrl+C**. If the script is killed
   (crash, power loss, `kill -9`), the trade runs unbounded.

6. **Single position only.** No multi-symbol, multi-ticket, or grid support.

7. **No trailing stop, no breakeven, no partial closes.**

8. **Final summary reports gross P&L**, not net of swap/commission.

9. **Windows only** (MetaTrader5 Python module constraint).

---

## 🛠 Troubleshooting

| Symptom | Likely Cause / Fix |
|---|---|
| `initialize() failed` | MT5 terminal not running, or not logged in. |
| `Order check failed` | Symbol not visible, market closed, or insufficient margin. |
| `Order send failed` retcode `10030` | Filling mode not supported. Try a different `filling_type`. |
| Position instantly "closed externally" | `result.order` mismatch — patch to use `result.deal` or match by `MAGIC`. |
| Ctrl+C does nothing | Focus must be on the terminal running the script. |
| TP/SL not accepted on detach | Broker's min stop distance higher than your target — widen `PROFIT_TP/SL`. |

---

## 🔒 Safety Checklist

Before running on a live account:

- [ ] Test on a **demo account** first.
- [ ] Set `LOT` to your broker's **minimum** (usually 0.01).
- [ ] Confirm `SYMBOL` is visible in MT5's Market Watch.
- [ ] Verify `PROFIT_TP` / `PROFIT_SL` in **your account's currency** (USD assumed).
- [ ] Account for **commission + swap** when sizing targets.
- [ ] Enable **Algo Trading** in MT5.
- [ ] Keep the MT5 terminal **open** while the script runs.

---

## 📁 Project Structure

```
.
├── order.py     # The entire bot
└── README.md    # This file
```

---

## ⚖️ Disclaimer

This software is provided **as-is**, for educational and personal use.
Trading leveraged instruments carries substantial risk of loss.
The author is **not responsible** for any financial losses incurred
through the use of this script. Always test on a demo account first,
and never risk money you cannot afford to lose.

---

## 📜 License

MIT — free to use, modify, and distribute.
