import os
import sys
import json
import asyncio
import gradio as gr
from contextlib import AsyncExitStack
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
from llm_budget import SYSTEM_PROMPT, fetch_breach_context

# Load environment variables
load_dotenv()

# Initialize Azure OpenAI Client
azure_client = AsyncAzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)
DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")

# Global state for MCP connection
mcp_stack = None
mcp_session = None
mcp_tools = []

async def init_mcp():
    """Initialize the MCP connection once for the server."""
    global mcp_stack, mcp_session, mcp_tools
    
    if mcp_session is not None:
        return  # Already connected

    print("Initializing MCP Server connection...")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(os.path.dirname(__file__), "main.py")],
        env=os.environ.copy()
    )
    
    # Use AsyncExitStack to keep the context managers alive globally
    mcp_stack = AsyncExitStack()
    read, write = await mcp_stack.enter_async_context(stdio_client(server_params))
    mcp_session = await mcp_stack.enter_async_context(ClientSession(read, write))
    await mcp_session.initialize()
    
    # Fetch tools
    mcp_tools_response = await mcp_session.list_tools()
    for tool in mcp_tools_response.tools:
        mcp_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema
            }
        })
    print("MCP Server connected and tools loaded!")


async def chat(message, history):
    """
    Gradio chat interface function.
    `message` is the current user input.
    `history` is a list of [user_msg, assistant_msg] pairs.
    """
    await init_mcp()

    # Pull live breaches so the LLM always has alert context
    breach_alert = await fetch_breach_context(mcp_session)

    # Build conversation history for OpenAI
    messages = [{"role": "system", "content": SYSTEM_PROMPT + breach_alert}]
    
    for item in history:
        if isinstance(item, (list, tuple)):
            messages.append({"role": "user", "content": item[0]})
            messages.append({"role": "assistant", "content": item[1]})
        elif isinstance(item, dict):
            messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
        else:
            # For Gradio 5 Message objects
            messages.append({"role": getattr(item, "role", "user"), "content": getattr(item, "content", "")})
        
    # Append the new user message
    messages.append({"role": "user", "content": message})
    
    # 1. Call Azure OpenAI
    response = await azure_client.chat.completions.create(
        model=DEPLOYMENT_NAME,
        messages=messages,
        tools=mcp_tools,
        tool_choice="auto"
    )
    
    response_message = response.choices[0].message
    
    # 2. Handle Tool Calls
    if response_message.tool_calls:
        messages.append(response_message)
        
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)
            print(f"[Tool Execution] '{function_name}' with args: {function_args}")
            
            try:
                tool_result = await mcp_session.call_tool(function_name, function_args)
                result_text = "\n".join([c.text for c in tool_result.content if getattr(c, 'type', '') == 'text'])
                print(f"[Tool Result] {result_text}")
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": result_text
                })
            except Exception as e:
                print(f"[Tool Error] {e}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": f"Error: {str(e)}"
                })
                
        # 3. Get final response after tool execution
        final_response = await azure_client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=messages
        )
        return final_response.choices[0].message.content
        
    else:
        # No tools called, just return the response
        return response_message.content

# Create the Gradio UI
demo = gr.ChatInterface(
    fn=chat,
    title="Airtel Expense Tracker POC",
    description="Test your Airtel App integration with conversational context! The AI remembers what you just said.",
    examples=[
        "Set a monthly budget of 5000 for Food",
        "Set a monthly merchant budget of 2000 for SWIGGY",
        "Have I breached any budgets?",
        "What is my spending summary for this month?",
        "Sync my latest emails.",
    ],
)

if __name__ == "__main__":
    print("Starting Web UI... Go to http://127.0.0.1:7860 to chat!")
    demo.launch()
