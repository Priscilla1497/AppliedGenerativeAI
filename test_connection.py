import asyncio
import os

from dotenv import load_dotenv
from fastmcp.client import Client
from langchain.mcp import MCPAdapter

load_dotenv()


async def main() -> None:
    api_key=os.environ["ODOO_API_KEY"]
    url="https://pharmaceutical.odoo.com/mcp"
    async with MCPAdapter(Client(url, auth=api_key)) as adapter:
        tools = await adapter.list_tools()
        print(f"Connected to {url} — {len(tools)} tools exposed:\n")
        for t in tools:
            first_line = (t.description or "").strip().splitlines()[0:1]
            print(f"- {t.name}: {first_line[0] if first_line else ''}")


if __name__ == "__main__":
    asyncio.run(main())
