import argparse
import asyncio
import json
import logging
import os
import sys
from typing import List

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import httpx
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from core.constants import MCP_SERVERS, TRANSPORT_OPTIONS
from core.validators import _extract_iflow_file, _iter_concatenated_package_files


async def _main(iflow_id: str) -> int:
    logging.basicConfig(level=logging.DEBUG)
    url = MCP_SERVERS.get("integration_suite")
    if not url:
        raise RuntimeError("MCP_SERVERS['integration_suite'] is not configured.")

    opts = TRANSPORT_OPTIONS.get("integration_suite", {})

    def factory(**kw):
        kw["verify"] = opts.get("verify", True)
        # Keep this debug run quick: cap overall request timeouts.
        kw["timeout"] = httpx.Timeout(
            timeout=75.0, connect=10.0, read=75.0, write=20.0, pool=10.0
        )
        return httpx.AsyncClient(**kw)

    transport = StreamableHttpTransport(url, httpx_client_factory=factory)
    client = Client(transport=transport)

    try:
        print("calling get-iflow ...")
        async with client:
            res = await asyncio.wait_for(
                client.call_tool("get-iflow", {"id": iflow_id}),
                timeout=90.0,
            )
    except Exception as exc:
        print(f"tool_success=False tool_error={exc!r}")
        return 1

    parts: List[str] = []
    for c in res.content:
        if getattr(c, "text", None):
            parts.append(c.text)
        elif getattr(c, "json", None):
            parts.append(json.dumps(c.json))
        else:
            parts.append(str(c))
    out_str = "\n".join(parts)

    print("tool_success=True tool_error=''")
    print(f"raw_len={len(out_str)} raw_preview={out_str[:200]!r}")

    paths: List[str] = []
    for fp, _content in _iter_concatenated_package_files(out_str):
        paths.append(fp)
    print(f"paths_found={len(paths)}")
    if paths:
        for p in paths[:50]:
            print(f"PATH: {p}")
        if len(paths) > 50:
            print("... (paths truncated)")

    fp, xml = _extract_iflow_file(out_str, iflow_id=iflow_id)
    print(f"extracted_filepath={fp!r} extracted_len={len(xml)} startswith_xml={xml.lstrip().startswith('<?xml')}")
    if xml:
        print(f"xml_preview={xml[:200]!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iflow_id")
    args = ap.parse_args()
    return asyncio.run(_main(args.iflow_id))


if __name__ == "__main__":
    raise SystemExit(main())
