import sys
import time
from collections import deque
from datetime import datetime, timedelta
import MetaTrader5 as mt5

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich import box

console = Console()

# ----------------------------------------------------------------------
# 1. CONFIGURATION
# ----------------------------------------------------------------------
SYMBOL         = "XAUUSD"   # <-- change to EURUSD / XAUUSD as needed
LOT            = 0.05
DEVIATION      = 20
MAGIC          = 234000
PROFIT_TP      = 25.00       # $ profit target
PROFIT_SL      = -12.75      # $ loss target (negative)
CHECK_INTERVAL = 1

# ----------------------------------------------------------------------
# 2. CONNECT TO METATRADER 5
# ----------------------------------------------------------------------
if not mt5.initialize():
    console.print("[bold red]✗ initialize() failed[/] error code =", mt5.last_error())
    sys.exit(1)

# ----------------------------------------------------------------------
# 3. VALIDATE COMMAND-LINE ARGUMENT
# ----------------------------------------------------------------------
if len(sys.argv) != 2 or sys.argv[1].lower() not in ("buy", "sell"):
    console.print("[yellow]Usage:[/] python main.py <buy|sell>")
    mt5.shutdown()
    sys.exit(1)

direction  = sys.argv[1].lower()
order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

# ----------------------------------------------------------------------
# 4. SYMBOL INFO + FILLING MODE
# ----------------------------------------------------------------------
symbol_info = mt5.symbol_info(SYMBOL)
if symbol_info is None:
    console.print(f"[red]✗ Could not get symbol info for {SYMBOL}[/]")
    mt5.shutdown()
    sys.exit(1)

if not symbol_info.visible:
    if not mt5.symbol_select(SYMBOL, True):
        console.print(f"[red]✗ Failed to select {SYMBOL}[/]")
        mt5.shutdown()
        sys.exit(1)
    symbol_info = mt5.symbol_info(SYMBOL)

allowed = symbol_info.filling_mode
if allowed & 1:
    filling_type = mt5.ORDER_FILLING_FOK
elif allowed & 2:
    filling_type = mt5.ORDER_FILLING_IOC
else:
    console.print(f"[red]✗ Neither FOK nor IOC allowed for {SYMBOL}[/]")
    mt5.shutdown()
    sys.exit(1)

# ----------------------------------------------------------------------
# 5. GET PRICE + BUILD ORDER REQUEST
# ----------------------------------------------------------------------
tick = mt5.symbol_info_tick(SYMBOL)
if tick is None:
    console.print("[red]✗ Could not get tick[/]")
    mt5.shutdown()
    sys.exit(1)

price = tick.ask if direction == "buy" else tick.bid

request = {
    "action":       mt5.TRADE_ACTION_DEAL,
    "symbol":       SYMBOL,
    "volume":       LOT,
    "type":         order_type,
    "price":        price,
    "deviation":    DEVIATION,
    "magic":        MAGIC,
    "comment":      "python script open",
    "type_time":    mt5.ORDER_TIME_GTC,
    "type_filling": filling_type,
}

check = mt5.order_check(request)
if check is None or check.retcode != 0:
    console.print(f"[red]✗ Order check failed:[/] {check}")
    mt5.shutdown()
    sys.exit(1)

with console.status("[cyan]Sending order to broker...", spinner="dots"):
    result = mt5.order_send(request)

if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
    console.print(f"[red]✗ Order send failed:[/] {result}")
    mt5.shutdown()
    sys.exit(1)

position_ticket = result.order
entry_price     = result.price
start_time      = time.time()

# ----------------------------------------------------------------------
# 6. HELPERS  (accurate — uses MT5's own profit calculator)
# ----------------------------------------------------------------------
def compute_tp_sl_prices(pos, direction, sym_info):
    """
    Compute TP/SL prices corresponding to PROFIT_TP / PROFIT_SL using
    MT5's own order_calc_profit(). This is immune to broker-specific
    quirks in trade_tick_value (which is a known issue for XAUUSD and
    other metals).

    Returns: (tp_price, sl_price, tp_profit_actual, sl_profit_actual)
    """
    entry   = pos.price_open
    volume  = pos.volume
    digits  = sym_info.digits
    point   = sym_info.point
    stops   = sym_info.trade_stops_level  # in points

    otype = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

    # --- 1. probe: how much $ does a 1.00 price-unit move yield? ---
    probe_price = entry + 1.0 if direction == "buy" else entry - 1.0
    probe_profit = mt5.order_calc_profit(otype, SYMBOL, volume, entry, probe_price)
    if probe_profit is None or probe_profit == 0:
        return None, None, None, None

    profit_per_unit = abs(probe_profit)   # $ per 1.00 of price

    # --- 2. convert money targets into price deltas ---
    delta_tp = abs(PROFIT_TP) / profit_per_unit
    delta_sl = abs(PROFIT_SL) / profit_per_unit

    if direction == "buy":
        tp_price = entry + delta_tp
        sl_price = entry - delta_sl
    else:
        tp_price = entry - delta_tp
        sl_price = entry + delta_sl

    tp_price = round(tp_price, digits)
    sl_price = round(sl_price, digits)

    # --- 3. enforce broker minimum stop distance ---
    t = mt5.symbol_info_tick(SYMBOL)
    if t and stops > 0:
        min_dist = stops * point
        if direction == "buy":
            if t.bid - sl_price < min_dist:
                sl_price = round(t.bid - min_dist, digits)
            if tp_price - t.bid < min_dist:
                tp_price = round(t.bid + min_dist, digits)
        else:
            if sl_price - t.ask < min_dist:
                sl_price = round(t.ask + min_dist, digits)
            if t.ask - tp_price < min_dist:
                tp_price = round(t.ask - min_dist, digits)

    # --- 4. report the TRUE profit at these prices ---
    tp_profit = mt5.order_calc_profit(otype, SYMBOL, volume, entry, tp_price)
    sl_profit = mt5.order_calc_profit(otype, SYMBOL, volume, entry, sl_price)

    return tp_price, sl_price, tp_profit, sl_profit


# ----------------------------------------------------------------------
# 7. EVENTS LOG
# ----------------------------------------------------------------------
events = deque(maxlen=6)

def log_event(msg, style="white"):
    ts = datetime.now().strftime("%H:%M:%S")
    events.append((ts, msg, style))

log_event(f"Position opened @ {entry_price:.5f}", "bold green")

# ----------------------------------------------------------------------
# 8. DASHBOARD BUILDER
# ----------------------------------------------------------------------
def build_dashboard(pos, tick, direction, elapsed, events):
    dir_style = "bold green" if direction == "buy" else "bold red"
    arrow     = "▲" if direction == "buy" else "▼"

    header = Text(justify="center")
    header.append("● ", style="bold green")
    header.append(f"{SYMBOL}", style="bold white")
    header.append(f"  {arrow} {direction.upper()}", style=dir_style)
    header.append("      ")
    header.append(f"⏱  {str(timedelta(seconds=int(elapsed)))}", style="bold cyan")
    header.append("      ")
    header.append("● LIVE", style="bold red")

    current_price = tick.bid if direction == "buy" else tick.ask
    profit        = pos.profit

    color = "green" if profit > 0 else ("red" if profit < 0 else "white")

    swap       = getattr(pos, "swap", 0.0) or 0.0
    commission = getattr(pos, "commission", 0.0) or 0.0

    digits = symbol_info.digits

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(style="bold white")
    table.add_column(style="dim", justify="right")
    table.add_column(style="bold white")

    table.add_row("Ticket", str(pos.ticket), "Volume", f"{pos.volume:.2f}")
    table.add_row("Entry",  f"{pos.price_open:.{digits}f}",
                  "Current", f"{current_price:.{digits}f}")
    table.add_row(
        "TP", f"[bold green]+${PROFIT_TP:.2f}[/]",
        "SL", f"[bold red]-${abs(PROFIT_SL):.2f}[/]",
    )
    table.add_row(
        "Swap", f"{swap:+.2f}",
        "Commission", f"{commission:+.2f}",
    )

    pct = (profit - PROFIT_SL) / (PROFIT_TP - PROFIT_SL)
    pct = max(0.0, min(1.0, pct))

    bar_width = 40
    filled    = int(round(pct * bar_width))

    bar = Text(justify="center")
    bar.append("SL ", style="bold red")
    bar.append("│", style="dim")
    bar.append("━" * filled, style=color)
    bar.append("●", style=f"bold {color}")
    bar.append("━" * (bar_width - filled), style="dim")
    bar.append("│ ", style="dim")
    bar.append("TP", style="bold green")

    pnl_text = Text(justify="center")
    pnl_text.append(f"${profit:+.2f}", style=f"bold {color}")
    pnl_text.append("  USD", style="dim")

    events_text = Text()
    for ts, msg, style in events:
        events_text.append(f"  {ts}  ", style="dim")
        events_text.append(f"{msg}\n", style=style)

    hint = Text(justify="center")
    hint.append("Press ", style="dim")
    hint.append("Ctrl+C", style="bold yellow")
    hint.append(" to set broker-side TP/SL and detach", style="dim")

    return Group(
        Panel(Align.center(header), box=box.ROUNDED, border_style="cyan", padding=(0, 1)),
        Panel(table, title="[bold]Position[/]", title_align="left",
              box=box.ROUNDED, border_style="blue", padding=(0, 1)),
        Panel(
            Group(Align.center(pnl_text), Text(""), Align.center(bar)),
            title="[bold]P&L[/]", title_align="left",
            box=box.ROUNDED, border_style=color, padding=(0, 1),
        ),
        Panel(events_text, title="[bold]Events[/]", title_align="left",
              box=box.ROUNDED, border_style="dim", padding=(0, 1)),
        hint,
    )

# ----------------------------------------------------------------------
# 9. LIVE MONITORING LOOP
# ----------------------------------------------------------------------
pos             = None
close_reason    = None
last_milestone  = 0
milestone_step  = 1.0

try:
    with Live(console=console, refresh_per_second=4,
              screen=True, transient=False) as live:
        while True:
            positions = mt5.positions_get(ticket=position_ticket)

            if positions is None or len(positions) == 0:
                log_event("Position closed externally", "yellow")
                close_reason = "external"
                break

            pos  = positions[0]
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                time.sleep(CHECK_INTERVAL)
                continue

            elapsed = time.time() - start_time
            profit  = pos.profit

            current_ms = int(profit / milestone_step)
            if profit > 0 and current_ms > last_milestone:
                log_event(f"Profit reached +${current_ms:.2f}", "green")
                last_milestone = current_ms

            live.update(build_dashboard(pos, tick, direction, elapsed, events))

            if profit >= PROFIT_TP:
                log_event(f"TP reached  ${profit:+.2f}", "bold green")
                close_reason = "tp"
                break

            if profit <= PROFIT_SL:
                log_event(f"SL reached  ${profit:+.2f}", "bold red")
                close_reason = "sl"
                break

            time.sleep(CHECK_INTERVAL)

except KeyboardInterrupt:
    close_reason = "manual_detach"

# ----------------------------------------------------------------------
# 10. POST-LOOP: Ctrl+C → set broker-side TP/SL and detach
# ----------------------------------------------------------------------
if close_reason == "manual_detach":
    console.print()
    console.print(Panel(
        Align.center(Text.from_markup(
            "[bold yellow]⏸  Ctrl+C detected[/]\n"
            "[dim]Setting broker-side TP/SL and detaching...[/]"
        )),
        box=box.ROUNDED, border_style="yellow", padding=(1, 2),
    ))

    positions = mt5.positions_get(ticket=position_ticket)
    if positions is None or len(positions) == 0:
        console.print("[yellow]Position is already closed — nothing to do.[/]")
        mt5.shutdown()
        sys.exit(0)

    pos      = positions[0]
    sym_info = mt5.symbol_info(SYMBOL)
    tp_price, sl_price, tp_profit, sl_profit = compute_tp_sl_prices(pos, direction, sym_info)

    if tp_price is None or sl_price is None:
        console.print("[red]✗ Could not compute TP/SL prices (bad symbol metadata).[/]")
        mt5.shutdown()
        sys.exit(1)

    digits = sym_info.digits

    sltp_request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   SYMBOL,
        "position": position_ticket,
        "tp":       tp_price,
        "sl":       sl_price,
        "magic":    MAGIC,
        "comment":  "python script detach",
    }

    res = mt5.order_send(sltp_request)

    console.print()
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        console.print(Panel(
            Align.center(Text.from_markup(
                f"[bold red]✗ Failed to set TP/SL[/]\n"
                f"[dim]Ticket: {position_ticket}[/]\n"
                f"[dim]Retcode: {getattr(res, 'retcode', 'None')}[/]\n"
                f"[dim]Comment: {getattr(res, 'comment', '')}[/]"
            )),
            title="[bold]Detach Result[/]", box=box.ROUNDED,
            border_style="red", padding=(1, 3),
        ))
    else:
        # show the TRUE profit at the actual TP/SL prices
        tp_str = f"{tp_profit:+.2f}" if tp_profit is not None else "n/a"
        sl_str = f"{sl_profit:+.2f}" if sl_profit is not None else "n/a"

        console.print(Panel(
            Align.center(Text.from_markup(
                f"[bold green]✓ TP/SL set — position left open[/]\n\n"
                f"  Ticket        [bold]{position_ticket}[/]\n"
                f"  Take Profit   [bold green]{tp_price:.{digits}f}[/]   "
                f"[dim](≈ ${tp_str})[/]\n"
                f"  Stop Loss     [bold red]{sl_price:.{digits}f}[/]   "
                f"[dim](≈ ${sl_str})[/]\n\n"
                f"[dim]The broker will manage the exit from now on.[/]"
            )),
            title="[bold]Detach Result[/]", box=box.ROUNDED,
            border_style="green", padding=(1, 3),
        ))

    mt5.shutdown()
    sys.exit(0)

# ----------------------------------------------------------------------
# 11. NORMAL CLOSE (TP / SL hit inside the loop)
# ----------------------------------------------------------------------
final_profit = pos.profit if pos is not None else 0.0

positions = mt5.positions_get(ticket=position_ticket)
if positions is None or len(positions) == 0:
    console.print()
    console.print(Panel(
        Align.center(Text.from_markup(
            f"[bold yellow]Position was already closed[/]\n"
            f"[dim]Ticket: {position_ticket}[/]"
        )),
        title="[bold]Result[/]", box=box.ROUNDED, border_style="yellow",
    ))
    mt5.shutdown()
    sys.exit(0)

close_tick = mt5.symbol_info_tick(SYMBOL)
if close_tick is None:
    console.print("[red]✗ Could not get tick for closing order.[/]")
    mt5.shutdown()
    sys.exit(1)

close_price = close_tick.bid if direction == "buy" else close_tick.ask

close_request = {
    "action":       mt5.TRADE_ACTION_DEAL,
    "symbol":       SYMBOL,
    "volume":       positions[0].volume,
    "type":         mt5.ORDER_TYPE_SELL if direction == "buy" else mt5.ORDER_TYPE_BUY,
    "position":     position_ticket,
    "price":        close_price,
    "deviation":    DEVIATION,
    "magic":        MAGIC,
    "comment":      "python script close",
    "type_time":    mt5.ORDER_TIME_GTC,
    "type_filling": filling_type,
}

close_result = mt5.order_send(close_request)

# ----------------------------------------------------------------------
# 12. FINAL SUMMARY
# ----------------------------------------------------------------------
console.print()

if close_result is None or close_result.retcode != mt5.TRADE_RETCODE_DONE:
    summary = Text.from_markup(
        f"[bold red]✗ Close failed[/]\n"
        f"[dim]Ticket: {position_ticket}[/]\n"
        f"[dim]Retcode: {getattr(close_result, 'retcode', 'None')}[/]"
    )
    border = "red"
else:
    pnl_style  = "green" if final_profit > 0 else ("red" if final_profit < 0 else "white")
    reason_map = {"tp": "Take-Profit", "sl": "Stop-Loss", "external": "External"}
    reason_txt = reason_map.get(close_reason, close_reason or "—")

    summary = Text.from_markup(
        f"[bold green]✓ Position closed[/]\n\n"
        f"  Ticket       [bold]{position_ticket}[/]\n"
        f"  Reason       [bold]{reason_txt}[/]\n"
        f"  Final P&L    [bold {pnl_style}]${final_profit:+.2f}[/]\n"
        f"  Close Price  [bold]{close_result.price:.{symbol_info.digits}f}[/]"
    )
    border = pnl_style

console.print(Panel(
    Align.center(summary),
    title="[bold]Result[/]", box=box.ROUNDED, border_style=border, padding=(1, 3),
))

mt5.shutdown()