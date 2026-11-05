"""Auditable crypto-only universe exclusions shared by config and data clients."""

from __future__ import annotations

# Mandatory exclusions cannot be removed by dashboard/setup configuration.
# They cover fiat/stable assets, tokenized commodities, and identified securities
# that do not belong in the directional crypto strategy.
MANDATORY_EXCLUDED_BASE_ASSETS = frozenset(
    {
        "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI", "USDE",
        "USDS", "USD1", "USDJ", "GUSD", "PYUSD", "DUSD", "RLUSD", "XUSD",
        "U", "EUR", "EURI", "AEUR", "BRL", "TRY", "GBP", "AUD", "BIDR",
        "IDRT", "UAH", "NGN", "RUB", "XAUT", "PAXG", "BFUSD", "SPCX",
    }
)

# Exact Binance bStocks snapshot from the official public bStocks landing page,
# reviewed 2026-11-05. Exact identifiers avoid unsafe generic suffix filtering.
KNOWN_TOKENIZED_SECURITY_BASE_ASSETS = frozenset(
    {
        "AAPLB", "AAOIB", "ADBEB", "AGPUB", "ALABB", "AMATB", "AMCB",
        "AMDB", "AMZNB", "APPB", "ARMB", "ASMLB", "ASTSB", "AVGOB",
        "AXTIB", "BABAB", "BBB", "BEB", "BMNRB", "BNCB", "BOTB", "BRKBB",
        "BSPB", "CBRSB", "COHRB", "COINB", "CRCLB", "CRDOB", "CRMB",
        "CRWDB", "CRWVB", "CYPHB", "DELLB", "DJTB", "DRAMB", "EWYB",
        "FLNCB", "FWDIB", "GEVB", "GLWB", "GMEB", "GOOGLB", "GPROB",
        "GSB", "HIMSB", "HOODB", "HPEB", "IBMB", "INTCB", "INTWB",
        "IRENB", "JPMB", "KORUB", "KSTRB", "LITEB", "LLYB", "MDBB",
        "METAB", "MRNAB", "MRVLB", "MSFTB", "MSTRB", "MUB", "MUUB",
        "MVLLB", "NBISB", "NFLXB", "NOKB", "NVDAB", "ORCLB", "PDDB",
        "PENGB", "PLTRB", "PYPLB", "QCOMB", "QNTB", "QQQB", "RDDTB",
        "RKLBB", "SECZB", "SHAZB", "SKDDB", "SKHYB", "SKUUB", "SMCIB",
        "SMHB", "SNDKB", "SNXXB", "SOXLB", "SOXSB", "SPCXB", "SPYB",
        "SQQQB", "STXB", "TMFB", "TQQQB", "TSLAB", "TSMB", "USARB",
        "USDEB", "UVXYB", "VSTB", "WDCB", "WENB", "ZMB",
    }
)

# Binance leveraged-token names are explicit. Raw suffix matching is unsafe because
# legitimate assets such as JUP end in "UP".
KNOWN_LEVERAGED_BASE_ASSETS = frozenset(
    f"{asset}{direction}"
    for asset in (
        "1INCH", "AAVE", "ADA", "BCH", "BNB", "BTC", "DOT", "EOS", "ETH",
        "FIL", "LINK", "LTC", "SXP", "TRX", "UNI", "XLM", "XRP", "YFI",
    )
    for direction in ("UP", "DOWN", "BULL", "BEAR")
)

EFFECTIVE_MANDATORY_EXCLUDED_BASE_ASSETS = (
    MANDATORY_EXCLUDED_BASE_ASSETS
    | KNOWN_TOKENIZED_SECURITY_BASE_ASSETS
    | KNOWN_LEVERAGED_BASE_ASSETS
)
