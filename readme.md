# MT5 Single-Trade Manager

Two terminal-based trading assistants for **MetaTrader 5** — one lightweight,
one feature-rich. Both open a single market position, monitor it with a live
Rich dashboard, and close it on dollar-based TP/SL hits. A Ctrl+C "detach"
mode pushes broker-side TP/SL so the trade survives even if your PC dies.

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

### `pts.py` adds

- **Account validation** — verifies login, trading allowed, and non-zero balance.
- **Partial close** — closes half the position at 50% of TP, locks in profit,
  resets SL to breakeven + small buffer, then runs remainder to its own target.
- **Net P&L tracking** — dashboard shows profit including swap/commission.
- **Safer ticket resolution** — magic-number scan fallback for netting accounts.

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
4. Copy `main.py` (lightweight) or `pts.py` (feature-complete) to a folder.
   `pts.py` is recommended — it adds account validation, partial close, and
   fee-aware P&L tracking on top of everything in `main.py`.

> ⚠️ The `MetaTrader5` Python module only works on **Windows**.
> On Linux/macOS you'll need a Windows VM, Wine, or a remote MT5 host.

---

## ⚙️ Configuration

Edit the top of either file. `main.py` has the core config:

```python
SYMBOL         = "XAUUSD"   # Trading symbol
LOT            = 0.05       # Position size in lots
DEVIATION      = 20         # Max slippage in points
MAGIC          = 234000     # EA/magic number identifier
PROFIT_TP      = 25.00      # $ profit target
PROFIT_SL      = -12.75     # $ loss target (negative)
CHECK_INTERVAL = 0.1        # Polling interval in seconds (100 ms)
```

`pts.py` adds these on top:

```python
# Partial TP (stage 1)
PARTIAL_ENABLED       = True
PARTIAL_TRIGGER_RATIO = 0.50     # Trigger at 50% of full TP
PARTIAL_CLOSE_RATIO   = 0.50     # Close 50% of volume
PARTIAL_NEW_SL_PROFIT = 5.00     # Minimum locked profit after partial

# Final target for the REMAINING position (stage 2)
PARTIAL_TP_REMAINING  = 15.00

MAX_EMPTY_STRIKES     = 5        # Loop safety — breaks if position vanishes
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



---

## ▶️ Usage

```bash
python main.py buy      # lightweight version
python pts.py sell      # full version with partial close
```

Any other argument prints usage and exits.

### What happens at runtime

1. Connects to MT5, validates account (pts.py), validates the symbol,
   resolves the correct filling mode.
2. Fetches the current tick and sends a market order.
3. Enters a live monitoring loop:
   - Displays the dashboard (10 FPS refresh, 100 ms polling).
   - Logs milestone events (every $1 of floating P&L).
   - (pts.py) Triggers a partial close when floating ≥ `PROFIT_TP × PARTIAL_TRIGGER_RATIO`.
   - Closes when profit ≥ `PROFIT_TP` or ≤ `PROFIT_SL`.
4. Prints a final summary panel with gross (main.py) or net (pts.py) totals.

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

3. **Ticket resolution differs between versions.**  
   `main.py` reads `result.order` directly — works on most brokers where  
   order ticket == position ticket. `pts.py` resolves via  
   `find_our_position()` which checks `result.position`, then falls back to  
   magic-number scan. **Use `pts.py` on netting accounts.**

4. **Filling mode with `allowed = 0`** (broker says "client chooses")  
   causes an immediate fatal exit. Should default to IOC instead.

5. **Position is not protected on crash.**  
   Broker-side SL is only set on **Ctrl+C**. If the script is killed
   (crash, power loss, `kill -9`), the trade runs unbounded.

6. **Single position only.** No multi-symbol, multi-ticket, or grid support.

7. **No trailing stop or grid support.**

8. **Final summary reports net P&L** (pts.py includes swap+commission; main.py uses gross).

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
├── main.py      # Lightweight version (TP/SL close + detach)
├── pts.py       # Full version (add partial close, account validation, net P&L)
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
