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

# ======================================================================
# 1. CONFIGURATION
# ======================================================================
SYMBOL         = "XAUUSD"
LOT            = 0.02          # <-- changed
DEVIATION      = 20
MAGIC          = 234000
PROFIT_TP      = 30.00
PROFIT_SL      = -12.75
CHECK_INTERVAL = 1

# --- Partial TP (stage 1) ---
PARTIAL_ENABLED       = True
PARTIAL_TRIGGER_RATIO = 0.50
PARTIAL_CLOSE_RATIO   = 0.50
PARTIAL_NEW_SL_PROFIT = 5.00

# --- Final target for the REMAINING position (stage 2) ---
PARTIAL_TP_REMAINING  = 15.00

# --- Loop safety ---
MAX_EMPTY_STRIKES     = 5

# ======================================================================
# 2. CONNECT
# ======================================================================
if not mt5.initialize():
    console.print("[bold red]✗ initialize() failed[/] error code =", mt5.last_error())
    sys.exit(1)

acct = mt5.account_info()
if acct is None:
    console.print("[bold red]✗ Not logged in to any MT5 account.[/]")
    console.print("[yellow]Open the MT5 terminal, log in, then re-run.[/]")
    console.print(f"[dim]Last error: {mt5.last_error()}[/]")
    mt5.shutdown()
    sys.exit(1)

if not acct.trade_allowed:
    console.print("[bold red]✗ Trading is disabled on this account.[/]")
    console.print(f"[dim]Account: {acct.login} | Server: {acct.server} | "
                  f"Mode: {'DEMO' if acct.trade_mode == 0 else 'REAL'}[/]")
    mt5.shutdown()
    sys.exit(1)

if acct.balance <= 0 and acct.equity <= 0:
    console.print("[bold red]✗ Account has zero balance/equity.[/]")
    console.print(f"[dim]Account {acct.login} on {acct.server}[/]")
    mt5.shutdown()
    sys.exit(1)

console.print(
    f"[green]✓ Connected:[/] {acct.login} @ {acct.server} | "
    f"Balance ${acct.balance:.2f} | Equity ${acct.equity:.2f} | "
    f"{'DEMO' if acct.trade_mode == 0 else 'REAL'}"
)

# ======================================================================
# 3. CLI ARG
# ======================================================================
if len(sys.argv) != 2 or sys.argv[1].lower() not in ("buy", "sell"):
    console.print("[yellow]Usage:[/] python order.py <buy|sell>")
    mt5.shutdown()
    sys.exit(1)

direction  = sys.argv[1].lower()
order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

# ======================================================================
# 4. SYMBOL INFO + FILLING MODE
# ======================================================================
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
    filling_type = mt5.ORDER_FILLING_IOC

# ======================================================================
# 5. OPEN INITIAL POSITION
# ======================================================================
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

# ======================================================================
# 5b. RESOLVE THE REAL POSITION TICKET
# ======================================================================
def find_our_position(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pos_ticket = getattr(result, "position", 0) or 0
        if pos_ticket:
            plist = mt5.positions_get(ticket=pos_ticket)
            if plist and len(plist) > 0:
                return plist[0]

        plist = mt5.positions_get(symbol=SYMBOL)
        if plist:
            for p in plist:
                if p.magic == MAGIC:
                    return p

        time.sleep(0.2)
    return None

_pos = find_our_position(timeout=5.0)
if _pos is None:
    console.print("[bold red]✗ Order sent, but no matching position found.[/]")
    console.print(f"[dim]Order ticket: {getattr(result, 'order', '?')} | "
                  f"Deal: {getattr(result, 'deal', '?')} | "
                  f"Position field: {getattr(result, 'position', '?')}[/]")
    mt5.shutdown()
    sys.exit(1)

position_ticket = _pos.ticket
entry_price     = _pos.price_open
start_time      = time.time()

# ======================================================================
# 6. HELPERS
# ======================================================================
def net_pnl(pos):
    """Floating P&L including swap and commission (when exposed)."""
    profit = pos.profit or 0.0
    swap   = getattr(pos, "swap", 0.0) or 0.0
    comm   = getattr(pos, "commission", 0.0) or 0.0
    return profit + swap + comm

def normalize_volume(vol, info):
    step = info.volume_step or 0.01
    v = round(vol / step) * step
    v = max(info.volume_min, min(info.volume_max, v))
    return round(v, 8)

def profit_per_unit(pos, sym_info, otype):
    probe = pos.price_open + 1.0 if otype == mt5.ORDER_TYPE_BUY else pos.price_open - 1.0
    p = mt5.order_calc_profit(otype, SYMBOL, pos.volume, pos.price_open, probe)
    if p is None or p == 0:
        return None
    return abs(p)

def price_for_pnl(pos, sym_info, otype, target_pnl):
    ppu = profit_per_unit(pos, sym_info, otype)
    if ppu is None:
        return None
    delta  = target_pnl / ppu
    digits = sym_info.digits
    if otype == mt5.ORDER_TYPE_BUY:
        return round(pos.price_open + delta, digits)
    else:
        return round(pos.price_open - delta, digits)

def compute_tp_sl_prices(pos, direction, sym_info):
    digits = sym_info.digits
    point  = sym_info.point
    stops  = sym_info.trade_stops_level
    otype  = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

    ppu = profit_per_unit(pos, sym_info, otype)
    if ppu is None:
        return None, None, None, None

    delta_tp = abs(PROFIT_TP) / ppu
    delta_sl = abs(PROFIT_SL) / ppu

    if direction == "buy":
        tp_price = pos.price_open + delta_tp
        sl_price = pos.price_open - delta_sl
    else:
        tp_price = pos.price_open - delta_tp
        sl_price = pos.price_open + delta_sl

    tp_price = round(tp_price, digits)
    sl_price = round(sl_price, digits)

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

    tp_profit = mt5.order_calc_profit(otype, SYMBOL, pos.volume, pos.price_open, tp_price)
    sl_profit = mt5.order_calc_profit(otype, SYMBOL, pos.volume, pos.price_open, sl_price)
    return tp_price, sl_price, tp_profit, sl_profit

def send_partial_close(pos, direction, volume):
    t = mt5.symbol_info_tick(SYMBOL)
    if t is None:
        return None
    if direction == "buy":
        close_price = t.bid
        close_type  = mt5.ORDER_TYPE_SELL
    else:
        close_price = t.ask
        close_type  = mt5.ORDER_TYPE_BUY

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       volume,
        "type":         close_type,
        "position":     pos.ticket,
        "price":        close_price,
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      "python partial close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }
    return mt5.order_send(req)

def send_sltp(position_ticket, sl_price, tp_price):
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   SYMBOL,
        "position": position_ticket,
        "sl":       sl_price if sl_price else 0.0,
        "tp":       tp_price if tp_price else 0.0,
        "magic":    MAGIC,
    }
    return mt5.order_send(req)

# ======================================================================
# 7. EVENTS LOG
# ======================================================================
events = deque(maxlen=8)

def log_event(msg, style="white"):
    ts = datetime.now().strftime("%H:%M:%S")
    events.append((ts, msg, style))

log_event(f"Position #{position_ticket} opened @ {entry_price:.{symbol_info.digits}f}",
          "bold green")

# ======================================================================
# 8. DASHBOARD
# ======================================================================
def build_dashboard(pos, tick, direction, elapsed, events,
                    partial_done, locked_profit):
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
    floating      = net_pnl(pos)
    color = "green" if floating > 0 else ("red" if floating < 0 else "white")

    swap       = getattr(pos, "swap", 0.0) or 0.0
    commission = getattr(pos, "commission", 0.0) or 0.0
    digits     = symbol_info.digits

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(style="bold white")
    table.add_column(style="dim", justify="right")
    table.add_column(style="bold white")

    stage = "[bold yellow]PARTIAL DONE[/]" if partial_done else "[dim]FULL[/]"

    table.add_row("Ticket", str(pos.ticket), "Volume", f"{pos.volume:.2f}")
    table.add_row("Entry",  f"{pos.price_open:.{digits}f}",
                  "Current", f"{current_price:.{digits}f}")
    table.add_row("Stage", stage,
                  "Locked", f"[bold green]+${locked_profit:.2f}[/]")
    table.add_row(
        "TP(full)", f"[bold green]+${PROFIT_TP:.2f}[/]",
        "SL(full)", f"[bold red]-${abs(PROFIT_SL):.2f}[/]",
    )
    table.add_row(
        "Rem.TP",
        f"[bold green]+${PARTIAL_TP_REMAINING:.2f}[/]" if partial_done else "[dim]—[/]",
        "Swap/Comm",
        f"{swap:+.2f} / {commission:+.2f}",
    )

    if not partial_done:
        lo, hi = PROFIT_SL, PROFIT_TP
        val    = floating
    else:
        lo = PARTIAL_NEW_SL_PROFIT - locked_profit
        hi = PARTIAL_TP_REMAINING
        val = floating

    span = (hi - lo) if (hi - lo) != 0 else 1
    pct  = max(0.0, min(1.0, (val - lo) / span))

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
    pnl_text.append(f"${floating:+.2f}", style=f"bold {color}")
    pnl_text.append("  floating  ", style="dim")
    if partial_done:
        pnl_text.append(f"(+ ${locked_profit:.2f} locked)", style="bold yellow")

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

# ======================================================================
# 9. LIVE LOOP
# ======================================================================
pos             = None
close_reason    = None
partial_done    = False
locked_profit   = 0.0
last_milestone  = 0
milestone_step  = 1.0
empty_strikes   = 0

try:
    with Live(console=console, refresh_per_second=4,
              screen=True, transient=False) as live:
        while True:
            positions = mt5.positions_get(ticket=position_ticket)

            if positions is None or len(positions) == 0:
                empty_strikes += 1
                if empty_strikes >= MAX_EMPTY_STRIKES:
                    log_event("Position closed externally", "yellow")
                    close_reason = "external"
                    break
                time.sleep(CHECK_INTERVAL)
                continue
            else:
                empty_strikes = 0

            pos  = positions[0]
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                time.sleep(CHECK_INTERVAL)
                continue

            elapsed  = time.time() - start_time
            floating = net_pnl(pos)

            current_ms = int(floating / milestone_step)
            if floating > 0 and current_ms > last_milestone:
                log_event(f"Floating reached +${current_ms:.2f}", "green")
                last_milestone = current_ms

            # ---------- STAGE 1: PARTIAL ----------
            if PARTIAL_ENABLED and not partial_done:
                trigger = PROFIT_TP * PARTIAL_TRIGGER_RATIO
                if floating >= trigger:
                    vol_to_close = normalize_volume(
                        pos.volume * PARTIAL_CLOSE_RATIO, symbol_info
                    )
                    if vol_to_close >= pos.volume:
                        vol_to_close = normalize_volume(
                            pos.volume - symbol_info.volume_min, symbol_info
                        )

                    log_event(
                        f"Partial trigger @ ${floating:+.2f} → closing {vol_to_close:.2f}",
                        "bold yellow",
                    )

                    pr = send_partial_close(pos, direction, vol_to_close)
                    if pr is None or pr.retcode != mt5.TRADE_RETCODE_DONE:
                        log_event(
                            f"Partial close FAILED rc={getattr(pr, 'retcode', '?')}",
                            "bold red",
                        )
                    else:
                        locked_profit = round(
                            floating * (vol_to_close / pos.volume), 2
                        )
                        partial_done = True
                        log_event(f"Locked ≈ +${locked_profit:.2f}", "bold green")

                        desired_remaining_pnl = PARTIAL_NEW_SL_PROFIT - locked_profit
                        otype = (mt5.ORDER_TYPE_BUY if direction == "buy"
                                 else mt5.ORDER_TYPE_SELL)

                        plist = mt5.positions_get(ticket=position_ticket)
                        if plist and len(plist):
                            pos_after = plist[0]
                            sl_price  = price_for_pnl(
                                pos_after, symbol_info, otype, desired_remaining_pnl
                            )
                            if sl_price is not None:
                                res_sl = send_sltp(position_ticket, sl_price, 0.0)
                                if res_sl and res_sl.retcode == mt5.TRADE_RETCODE_DONE:
                                    log_event(
                                        f"SL moved → {sl_price:.{symbol_info.digits}f} "
                                        f"(min +${PARTIAL_NEW_SL_PROFIT:.2f})",
                                        "bold green",
                                    )
                                else:
                                    log_event(
                                        f"SL move FAILED rc={getattr(res_sl, 'retcode', '?')}",
                                        "bold red",
                                    )

                        plist = mt5.positions_get(ticket=position_ticket)
                        if plist and len(plist):
                            pos = plist[0]

            # ---------- STAGE 2: REMAINING / FULL ----------
            floating = net_pnl(pos)

            if partial_done:
                if floating >= PARTIAL_TP_REMAINING:
                    log_event(f"Remainder TP hit  ${floating:+.2f}", "bold green")
                    close_reason = "tp_partial_remainder"
                    break
            else:
                if floating >= PROFIT_TP:
                    log_event(f"TP reached  ${floating:+.2f}", "bold green")
                    close_reason = "tp"
                    break
                if floating <= PROFIT_SL:
                    log_event(f"SL reached  ${floating:+.2f}", "bold red")
                    close_reason = "sl"
                    break

            live.update(build_dashboard(
                pos, tick, direction, elapsed, events,
                partial_done, locked_profit,
            ))

            time.sleep(CHECK_INTERVAL)

except KeyboardInterrupt:
    close_reason = "manual_detach"

# ======================================================================
# 10. Ctrl+C → detach
# ======================================================================
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

    if partial_done:
        otype    = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        tp_price = price_for_pnl(pos, sym_info, otype, PARTIAL_TP_REMAINING)
        desired_remaining_pnl = PARTIAL_NEW_SL_PROFIT - locked_profit
        sl_price = price_for_pnl(pos, sym_info, otype, desired_remaining_pnl)
    else:
        tp_price, sl_price, _, _ = compute_tp_sl_prices(pos, direction, sym_info)

    if tp_price is None and sl_price is None:
        console.print("[red]✗ Could not compute TP/SL prices.[/]")
        mt5.shutdown()
        sys.exit(1)

    res = send_sltp(position_ticket, sl_price, tp_price)

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
        digits = sym_info.digits
        tp_str = f"{tp_price:.{digits}f}" if tp_price else "—"
        sl_str = f"{sl_price:.{digits}f}" if sl_price else "—"
        console.print(Panel(
            Align.center(Text.from_markup(
                f"[bold green]✓ TP/SL set — position left open[/]\n\n"
                f"  Ticket        [bold]{position_ticket}[/]\n"
                f"  Stage         {'PARTIAL DONE' if partial_done else 'FULL'}\n"
                f"  Locked        [bold green]+${locked_profit:.2f}[/]\n"
                f"  Take Profit   [bold green]{tp_str}[/]\n"
                f"  Stop Loss     [bold red]{sl_str}[/]\n\n"
                f"[dim]The broker will manage the exit from now on.[/]"
            )),
            title="[bold]Detach Result[/]", box=box.ROUNDED,
            border_style="green", padding=(1, 3),
        ))

    mt5.shutdown()
    sys.exit(0)

# ======================================================================
# 11. NORMAL CLOSE
# ======================================================================
positions = mt5.positions_get(ticket=position_ticket)
if positions is None or len(positions) == 0:
    console.print()
    console.print(Panel(
        Align.center(Text.from_markup(
            f"[bold yellow]Position already closed[/]\n"
            f"[dim]Ticket: {position_ticket}[/]\n"
            f"[dim]Locked before close: ${locked_profit:+.2f}[/]"
        )),
        title="[bold]Result[/]", box=box.ROUNDED, border_style="yellow",
    ))
    mt5.shutdown()
    sys.exit(0)

pos_now        = positions[0]
final_floating = net_pnl(pos_now)

close_tick = mt5.symbol_info_tick(SYMBOL)
if close_tick is None:
    console.print("[red]✗ Could not get tick for closing order.[/]")
    mt5.shutdown()
    sys.exit(1)

close_price = close_tick.bid if direction == "buy" else close_tick.ask

close_request = {
    "action":       mt5.TRADE_ACTION_DEAL,
    "symbol":       SYMBOL,
    "volume":       pos_now.volume,
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

# ======================================================================
# 12. FINAL SUMMARY
# ======================================================================
console.print()

total_realized = locked_profit + (
    final_floating
    if (close_result and close_result.retcode == mt5.TRADE_RETCODE_DONE)
    else 0.0
)

if close_result is None or close_result.retcode != mt5.TRADE_RETCODE_DONE:
    summary = Text.from_markup(
        f"[bold red]✗ Close failed[/]\n"
        f"[dim]Ticket: {position_ticket}[/]\n"
        f"[dim]Retcode: {getattr(close_result, 'retcode', 'None')}[/]\n"
        f"[dim]Locked so far: ${locked_profit:+.2f}[/]"
    )
    border = "red"
else:
    pnl_style  = "green" if total_realized > 0 else ("red" if total_realized < 0 else "white")
    reason_map = {
        "tp":                   "Take-Profit (full)",
        "sl":                   "Stop-Loss (full)",
        "tp_partial_remainder": "Take-Profit (remainder after partial)",
        "external":             "External",
    }
    reason_txt = reason_map.get(close_reason, close_reason or "—")

    rows = [
        f"  Ticket       [bold]{position_ticket}[/]",
        f"  Reason       [bold]{reason_txt}[/]",
        f"  Partial      {'yes' if partial_done else 'no'}",
        f"  Locked       [bold green]${locked_profit:+.2f}[/]",
        f"  Final leg    [bold {pnl_style}]${final_floating:+.2f}[/]",
        f"  ──────────────────────",
        f"  NET TOTAL    [bold {pnl_style}]${total_realized:+.2f}[/]",
    ]
    summary = Text.from_markup("\n".join(rows))
    border  = pnl_style

console.print(Panel(
    Align.center(summary),
    title="[bold]Result[/]", box=box.ROUNDED, border_style=border, padding=(1, 3),
))

mt5.shutdown()