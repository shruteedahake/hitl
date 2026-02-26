from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import os
from dotenv import load_dotenv
from langchain_openai.chat_models import AzureChatOpenAI
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, ToolMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from typing import TypedDict, Annotated
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import tool
from langgraph.types import interrupt, Command
from phoenix.client import Client
from contextlib import asynccontextmanager
from langgraph.checkpoint.memory import InMemorySaver
import asyncio
import json
from openinference.instrumentation.langchain import LangChainInstrumentor
from phoenix.otel import register

# load_dotenv()
email_intent_agent_env_path = ".env"
load_dotenv(dotenv_path=email_intent_agent_env_path)

app = FastAPI(title="HITL Agent Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config_mcp_server = {
    "email_reader_mcp": {
        "url": os.getenv("MCP_URL"),
        "transport": "streamable_http",
    }
}

class State(TypedDict):
    messages: Annotated[list, add_messages]

def router(state: State):
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and getattr(last_message, 'tool_calls', None):
        return "tools"
    if isinstance(last_message, AIMessage) and last_message.content:
        content = last_message.content
        if 'Continue' in content:
            return "tools"
        elif "End" in content:
            return "End"
    return "End" 

def load_prompt() -> str:
    client = Client(base_url=phoenix_endpoint, api_key=PHOENIX_API_KEY)
    prompt = client.prompts.get(prompt_version_id="2134")

    prompt_set = prompt._template["messages"]
    prompt_msg = next(
            (item["content"][0]["text"] for item in prompt_set if item.get("role") == "system"),
            None
        )
    if prompt_msg and isinstance(prompt_msg, str) and len(prompt_msg) > 0:
            system_msg = prompt_msg
    else:
            raise ValueError(f"Error: 'prompt_msg' is empty or improperly structured:{prompt_msg}, {agent_name}")
    return system_msg


@tool
def human_assistance(query: str) -> str:
    """
    A tool to get human assistance for complex queries.
    """
    human_response=interrupt({"query": query})
    return human_response


def create_custom_graph(model, tools, prompt, checkpointer=None):
    graph_builder = StateGraph(State)
    llm_with_tools = model.bind_tools(tools)

    async def agent_node(state: State):
        messages = state["messages"]
        system_prompt = SystemMessage(content=prompt)
        all_messages = [system_prompt] + messages
        message = await llm_with_tools.ainvoke(all_messages)
        return {"messages": [message]}

    graph_builder.add_node("agent", agent_node)
    graph_builder.add_node("tools", ToolNode(tools=tools))
    graph_builder.add_edge(START, "agent")
    graph_builder.add_conditional_edges(
        "agent",
        router,
        {
            "tools": "tools",
            "End": END
        }
    )
    graph_builder.add_edge("tools", "agent")



    return graph_builder.compile(checkpointer=checkpointer) if checkpointer else graph_builder.compile()

async def get_tool_list(config_mcp_server):
    client = MultiServerMCPClient(config_mcp_server)
    tools_list = await client.get_tools()
    print("Tools fetched from MCP:", [tool.name for tool in tools_list])
    return [human_assistance]+tools_list


    # Fetch tools
tools = asyncio.run(get_tool_list(config_mcp_server=config_mcp_server))

    # Setup Azure OpenAI model

model = AzureChatOpenAI(
    api_key=credential,
    api_version=api_version,  # fill if needed
    azure_deployment=deployment_name,
    azure_endpoint=endpoint
)

# Load system prompt
SYSTEM_INSTRUCTION = load_prompt()
 

memory = InMemorySaver()
# Create the graph
graph = create_custom_graph(model=model, tools=tools, prompt=SYSTEM_INSTRUCTION, checkpointer=memory)

@app.post("/chat")
async def chat_stream(checkpoint_id):
    config = {"configurable": {"thread_id": checkpoint_id},"recursion_limit": 100}

    async def stream():
        async for chunk in graph.astream({"messages": ["Start your task as per the prompt"]}, config=config):
            if "agent" in chunk:
                messages = chunk["agent"]["messages"]
                for msg in messages:
                    if hasattr(msg, "content") and msg.content:
                        # Yield SSE formatted string
                        yield f"data: {msg.content}\n\n"
            if "tools" in chunk:
                messages = chunk["tools"]["messages"]
                for msg in messages:
                    if hasattr(msg, "content") and msg.content:
                        yield f"data: [Tool:{msg.name}] {msg.content}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/continue_chat")
async def continue_chat(message: str, checkpoint_id):
    config = {"configurable": {"thread_id": checkpoint_id},"recursion_limit": 100}

    async def stream():

        async for chunk in graph.astream(
            Command(resume={"messages": [{"role": "user", "content": message}]}),
            config=config
        ):
            # for value in chunk.values():
            #     if "messages" in value:
            #         msg = value["messages"][-1]
            #         yield f"data: {json.dumps({'message': msg.content})}\n\n"
            #     else:
            #         interrupt_event = value[0]
            #         yield f"data: {json.dumps({'interrupt': interrupt_event.value})}\n\n"

             for value in chunk.values():
                if isinstance(value, dict) and "messages" in value:
                    messages = value.get("messages", [])
                    if messages: 
                        msg = messages[-1]
                        yield f"data: {json.dumps({'message': msg.content})}\n\n"
                    else:
                        continue

                elif isinstance(value, list) and value:
                    interrupt_event = value[0]
                    yield f"data: {json.dumps({'interrupt': interrupt_event.value})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6721)
