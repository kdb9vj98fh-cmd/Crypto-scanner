def scan_market(symbol):

    data = candles(
        symbol,
        "15m",
        120,
    )

    setup = find_setup(data)

    if not setup:
        return symbol, None, None

    trade = create_trade(
        symbol,
        setup,
        data,
    )

    return symbol, trade, None