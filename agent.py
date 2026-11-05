"""
Odoo CRM Agent
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import uuid
import warnings

from langchain_core._api import LangChainBetaWarning

warnings.filterwarnings("ignore", category=LangChainBetaWarning)

from dotenv import load_dotenv
from fastmcp.client import Client
from langchain.embeddings import init_embeddings
from langchain.mcp import MCPAdapter
from langchain.tools import tool
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.checkpoint.memory import InMemorySaver

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

load_dotenv()

SKILLS_DIR = pathlib.Path.cwd()
PLAYBOOKS_DIR = pathlib.Path("playbooks")
DISPLAY_TOOL_CALLS = False

SYSTEM_PROMPT = """\
You are a CRM assistant for our company. You answer questions about our sales
pipeline using live data from Odoo, and questions about our sales process using
the available playbooks.

## Where to get information
- Live CRM data (leads, opportunities, customers, activities, revenue, stages,
  salespeople, teams) -> ALWAYS use the Odoo MCP tools. Never guess numbers,
  names or dates; if the tools return nothing, say so.
- Process, policy, definitions, playbooks ("what counts as a qualified lead?",
  "what should happen at the Proposition stage?") -> use search_playbooks.
- Some questions need both (e.g. "which opportunities are stuck longer than our
  playbook allows?"). Read the rule from the playbook, then query Odoo.

## How to work with Odoo
1. If you have not done so yet in this conversation, call the Odoo tool that
   retrieves the initial MCP context. It tells you about the user and database.
2. Useful models:
   - crm.lead       leads AND opportunities (field `type` = 'lead' | 'opportunity')
   - crm.stage      pipeline stages
   - crm.team       sales teams
   - res.partner    customers / contacts / companies
   - res.users      salespeople (crm.lead.user_id)
   - mail.activity  scheduled activities (res_model = 'crm.lead')
3. Before filtering on a field you are not sure about, call the Odoo "get fields"
   tool on that model. Do not invent field names.
4. Use the search tool for record lists and the read-group tool for totals,
   counts and breakdowns (e.g. expected_revenue grouped by stage_id). Prefer
   read-group over pulling many records and adding them up yourself.
5. Keep result sets small: request only the fields you need and use a limit.
6. Odoo domains look like [["type","=","opportunity"],["stage_id.name","=","Won"]].
   Open opportunities are usually active=True and not in a won stage.

## Answer style
- Lead with the direct answer, then the supporting detail.
- Use a compact table for lists of records (name, customer, stage, expected
  revenue, salesperson, deadline) when there are more than three rows.
- Show currency amounts with the currency Odoo returns.
- When you used the playbook, name the source file.
- You only have the access rights of the Odoo user who owns the API key; if a
  tool returns an access error, explain that rather than retrying blindly.
- Do not create, modify or delete Odoo records unless the user explicitly asks,
  and confirm what you are about to change before doing it.
"""


def build_vector_store() -> InMemoryVectorStore | None:
    if not PLAYBOOKS_DIR.exists():
        return None
    files = sorted(
        p for p in PLAYBOOKS_DIR.rglob("*") if p.suffix.lower() in {".md"}
    )
    if not files:
        return None

    docs = [
        Document(page_content=p.read_text(encoding="utf-8"), metadata={"source": p.name})
        for p in files
    ]
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = splitter.split_documents(docs)

    store = InMemoryVectorStore(init_embeddings("google_genai:gemini-embedding-2-preview"))
    store.add_documents(chunks)
    print(f"Indexed playbooks: {len(files)} files, {len(chunks)} chunks.")
    return store


def build_search_playbooks_tool(store: InMemoryVectorStore):
    @tool
    def search_playbooks(query: str) -> str:
        """Search the company's playbooks: sales playbook, pipeline
        stage definitions, qualification rules, pricing/discount policy and CRM
        process notes. Use it for 'how do we / what is our rule' questions, not
        for live CRM records (use the Odoo tools for those)."""
        results = store.similarity_search(query, k=4)
        if not results:
            return "No relevant information found."
        return "\n\n---\n\n".join(
            f"[source: {doc.metadata['source']}]\n{doc.page_content}" for doc in results
        )

    return search_playbooks


async def ask_agent(agent, question: str, thread_id: str) -> str:
    config = {"configurable": {"thread_id": thread_id}}
    final_answer = ""

    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": question}]},
        config=config,
        stream_mode="updates",
    ):
        for value in chunk.values():
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if isinstance(msg, AIMessage):
                    if msg.text and not msg.tool_calls:
                        final_answer = msg.text
                    elif msg.tool_calls and DISPLAY_TOOL_CALLS:
                        for tool_call in msg.tool_calls:
                            print(f"  → {tool_call['name']}({tool_call['args']})")
                elif isinstance(msg, ToolMessage) and DISPLAY_TOOL_CALLS:
                    status = "error" if getattr(msg, "status", "") == "error" else "ok"
                    print(f"  ← {msg.name}: {status}")

    return final_answer or "(no answer returned)"


async def chat(agent, tool_names: list[str]) -> None:
    thread_id = str(uuid.uuid4())
    print("\nOdoo CRM agent ready. Commands: /new /quit\n")
    while True:
        try:
            question = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question == "/new":
            thread_id = str(uuid.uuid4())
            print("Started a new conversation.\n")
            continue
        if question in {"/quit"}:
            break

        try:
            answer = await ask_agent(agent, question, thread_id)
        except Exception as exc:
            answer = f"Something went wrong: {exc}"
        print(f"\nAgent: {answer}\n")


async def main() -> None:
    store = build_vector_store()

    api_key=os.environ["ODOO_API_KEY"]

    async with MCPAdapter(Client("https://pharmaceutical.odoo.com/mcp", auth=api_key)) as adapter:
        tools = list(await adapter.list_tools())
        print(f"Connected to Odoo MCP Server.")
        if store is not None:
            tools.append(build_search_playbooks_tool(store))
        tool_names = [t.name for t in tools]

        print("\nAvailable tools:")
        print("\n".join(f"  - {n}" for n in tool_names) + "\n")

        backend = FilesystemBackend(root_dir=SKILLS_DIR)
        agent = create_deep_agent(
            model="google_genai:gemini-3.8-flash",
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            skills=["./skills/"],
            backend=backend,
            checkpointer=InMemorySaver(),
        )

        await chat(agent, tool_names)


if __name__ == "__main__":
    asyncio.run(main())
