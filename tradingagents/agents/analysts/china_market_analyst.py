"""China Market Analyst — A-share capital flow and margin trading analysis.

Covers A-share-specific market microstructure that upstream analysts (yfinance /
Alpha Vantage) cannot provide: main force capital flow (主力资金流向), margin
trading sentiment (融资融券), block trading, and dragon-tiger list activity.
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.china_market_tools import CHINA_MARKET_TOOLS


def create_china_market_analyst(llm):

    def china_market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)
        language_instruction = get_language_instruction()

        tools = list(CHINA_MARKET_TOOLS)

        system_message = (
            "You are an A-share Market Microstructure Analyst specializing in "
            "Chinese mainland stock market capital flow and leverage analysis.\n\n"
            "Your job is to examine capital flow direction (主力资金) and margin "
            "trading data (融资融券) to assess whether smart money is accumulating "
            "or distributing, and whether leverage sentiment is sustainable or "
            "dangerously extended.\n\n"
            "Key principles:\n"
            "- Main force net outflow >1 billion RMB on a high-volume day → "
            "distribution signal, especially if preceded by a rally.\n"
            "- Margin balance in the >80th percentile of past-year range → "
            "leverage is crowded; historically this precedes stampede selloffs.\n"
            "- Compare capital flow direction with price action: divergence is "
            "more informative than confirmation.\n"
            "- Dragon-tiger list (龙虎榜) institutional seats buying vs selling "
            "reveals conviction behind the move.\n\n"
            "Output a concise report in the language specified below covering: "
            "(1) main force flow diagnosis, (2) margin/leverage health, "
            "(3) institutional seat activity, (4) summary verdict — "
            "Accumulation / Distribution / Neutral.\n\n"
            f"{instrument_context}\n"
            f"{language_instruction}\n"
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system_message),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke({"messages": state["messages"]})
        return {"messages": [result]}

    return china_market_analyst_node
