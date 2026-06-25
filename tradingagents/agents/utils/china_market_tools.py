"""China Market tools — A-share capital flow and margin trading data."""
from langchain_core.tools import tool
from typing import Annotated

# stock_data_hub provides the actual MX-backed data
try:
    from tradingagents.dataflows.stock_data_hub import get_capital_flow, get_margin_data

    @tool
    def get_ashare_capital_flow(
        ticker: Annotated[str, "A-share ticker symbol, e.g. 600522.SS or 000100.SZ"],
    ) -> str:
        """Get main force capital flow data for A-share stocks.

        Returns: main force net flow, retail net flow, block trading data,
        dragon-tiger list (龙虎榜) activity, and institutional seat details.
        Critical for identifying institutional distribution vs accumulation
        in the Chinese mainland market.
        """
        return get_capital_flow(ticker)

    @tool
    def get_ashare_margin_data(
        ticker: Annotated[str, "A-share ticker symbol, e.g. 600522.SS or 000100.SZ"],
    ) -> str:
        """Get margin trading (融资融券) data for A-share stocks.

        Returns: finance balance, security balance, margin buy/sell amounts,
        short interest ratio, and leverage sentiment indicators.
        High margin balance percentiles (>80%) historically precede
        stampede-style liquidations in A-share market.
        """
        return get_margin_data(ticker)

    CHINA_MARKET_TOOLS = [get_ashare_capital_flow, get_ashare_margin_data]

except ImportError:
    CHINA_MARKET_TOOLS = []
