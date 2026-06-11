"""
Probe script: inspect raw gpt-5.3-codex request/response via the same
ChatOpenAI subclass used in production.

Usage:
    python scripts/probe_gpt53_response.py

Prints:
  - The exact content type and value returned by the LLM
  - Whether the normalisation in _agenerate fires
  - The full generation dict for debugging
"""
import asyncio
import os
import sys

# Allow running from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from core.mcp_manager import create_llm
from langchain_core.messages import HumanMessage


async def main() -> None:
    llm = create_llm()
    print(f"Deployment ID : {os.getenv('LLM_DEPLOYMENT_ID')}")
    print(f"LLM type      : {type(llm).__name__}")
    print()

    msg = HumanMessage(content='Reply with exactly: {"ok": true}')
    result = await llm._agenerate([msg])

    for i, gen in enumerate(result.generations):
        raw_msg = gen.message
        print(f"--- Generation {i} ---")
        print(f"  message type    : {type(raw_msg).__name__}")
        print(f"  content type    : {type(raw_msg.content).__name__}")
        print(f"  content value   : {raw_msg.content!r}")
        print(f"  tool_calls      : {getattr(raw_msg, 'tool_calls', None)}")
        print(f"  additional_kwargs: {raw_msg.additional_kwargs}")
        print()

    # Also test ainvoke so we see the full ChatResult
    print("--- ainvoke result ---")
    response = await llm.ainvoke([msg])
    print(f"  type            : {type(response).__name__}")
    print(f"  content type    : {type(response.content).__name__}")
    print(f"  content value   : {response.content!r}")


if __name__ == "__main__":
    asyncio.run(main())
