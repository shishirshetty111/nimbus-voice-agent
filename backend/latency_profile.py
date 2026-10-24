"""Low-latency mode: one profile that overrides the agent's settings for speed.

Normal mode keeps whatever the user chose. Fast mode (`latency_mode = "fast"`) applies, per turn:
  - the provider's lite model, minimum reasoning (already the default)
  - knowledge by automatic retrieval (k=3, no rerank) instead of the search tool, which costs a
    whole extra LLM round trip
  - short replies that answer first, 4 verbatim history turns
  - only the tools this message plausibly needs (fewer schemas, fewer accidental tool rounds)
  - a short server-side "Okay." once a tool has run, so audio starts during the follow-up round
Voice-side parts (endpointing, first-clause chunking, stable-partial ASR, spare ASR socket, playback
buffer, fillers) read the same flag in backend/voice/session.py and assets/voice/voice-client.js.
"""

from __future__ import annotations

import re

FAST = "fast"

OVERRIDES = {"length": "low", "verbatim_turns": 4, "knowledge_mode": "rag", "rag_k": 3, "rerank": False}

# Keyword routing: which tool groups a message may need. Generous on purpose: a missing tool costs a
# wrong answer, an extra schema only costs a few tokens.
_GROUPS = {
    "cart": (r"\b(cart|basket|add|put|buy|purchase|remove|delete|drop|clear|empty|seat|seats|users?|licen[cs]es?|"
             r"order|checkout|check out|pay|total|subscribe|sign me up)\b",
             ["add_to_cart", "remove_from_cart", "clear_cart", "get_cart_total", "checkout", "checkout_item"]),
    "pricing": (r"\b(price|prices|pricing|cost|costs|how much|\$|dollars?|cheap|cheaper|cheapest|expensive|annual|"
                r"annually|yearly|monthly|year|month|save|saving|savings|discount|budget|quote)\b",
                ["annual_price", "annual_savings", "get_cart_total"]),
    "catalog": (r"\b(cheap|cheaper|cheapest|expensive|priciest|most|least|sort|rank|top|compare|list|which|all)\b",
                ["sort_products_by_price", "top_k_expensive"]),
}


def tools_for(message: str, enabled: set[str]) -> set[str]:
    """Tools this message may need, within what the user enabled. search_knowledge is never offered
    in fast mode: retrieval happens before the LLM call instead."""
    text = message.lower()
    names: set[str] = set()
    for pattern, tools in _GROUPS.values():
        if re.search(pattern, text):
            names.update(tools)
    return names & (enabled - {"search_knowledge"})


ANSWER_FIRST = """# Speed
Answer in one or two short sentences. The first sentence (five to eight words) directly answers the
question. Say each thing once: never repeat or rephrase a sentence you already said, and never open with
filler like "Great question"."""

# Spoken by the server as soon as a cart-changing tool has run, while the LLM writes its follow-up answer.
TOOL_ACK = "Okay. "

SAY_THEN_ACT = """When you need several independent pieces of information (for example a price and the annual
savings), request all of those tools at once in the same step instead of one after another. Do not
announce tool calls; just call the tool."""
