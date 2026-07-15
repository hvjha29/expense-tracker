import os
import sys
import json
import asyncio
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

# Load environment variables from .env
# This is a much better practice than keeping a markdown file with secrets.
load_dotenv()

# Ensure we have the required environment variables
if not os.getenv("AZURE_OPENAI_API_KEY"):
    print("Error: Missing AZURE_OPENAI_API_KEY in .env")
    sys.exit(1)

# Initialize Azure OpenAI Client
azure_client = AsyncAzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)
DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")

async def chat_with_mcp(prompt: str):
    # 1. Setup connection to the local MCP server (Expense Tracker)
    # This runs the main.py script which exposes the fastmcp server
    server_params = StdioServerParameters(
        command="python",
        args=["main.py"],
        env=os.environ.copy()
    )

    print("Connecting to local Expense Tracker MCP Server...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Connected to MCP Server successfully!")
            
            # 2. Fetch available tools from the MCP server
            mcp_tools_response = await session.list_tools()
            
            # Format MCP tools into the format expected by OpenAI
            openai_tools = []
            for tool in mcp_tools_response.tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema
                    }
                })

            # 3. Call Azure OpenAI with the user's prompt and available tools
            messages = [
                {"role": "system", "content": "You are a helpful assistant integrated into the Airtel app for expense tracking. Use the provided tools to assist the user."},
                {"role": "user", "content": prompt}
            ]
            
            print(f"\nSending prompt to Azure OpenAI ({DEPLOYMENT_NAME})...")
            response = await azure_client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=messages,
                tools=openai_tools,
                tool_choice="auto"
            )

            response_message = response.choices[0].message

            # 4. Handle tool calls requested by the LLM
            if response_message.tool_calls:
                # Add the assistant's message with tool calls to the history
                messages.append(response_message)
                
                for tool_call in response_message.tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)
                    print(f"\n[Tool Execution] LLM requested '{function_name}' with args: {function_args}")
                    
                    try:
                        # Call the tool on the MCP server via the session
                        tool_result = await session.call_tool(function_name, function_args)
                        
                        # Extract the text content from the MCP tool result
                        result_text = "\n".join([c.text for c in tool_result.content if getattr(c, 'type', '') == 'text'])
                        print(f"[Tool Result]: {result_text}")
                        
                        # Add the tool result back to the message history
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": result_text
                        })
                    except Exception as e:
                        print(f"Error executing tool {function_name}: {e}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": f"Error: {str(e)}"
                        })
                
                # 5. Get the final response from the LLM after providing the tool results
                print("\nFetching final response from Azure OpenAI...")
                final_response = await azure_client.chat.completions.create(
                    model=DEPLOYMENT_NAME,
                    messages=messages
                )
                print(f"\nFinal AI Response: {final_response.choices[0].message.content}")
                
            else:
                # If the LLM didn't call any tools, just print its response
                print(f"\nAI Response: {response_message.content}")


if __name__ == "__main__":
    # Allow passing a prompt via command line arguments, otherwise use a default
    default_prompt = "What are my top 3 merchants by spending?"
    user_prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else default_prompt
    
    # Run the async chat
    asyncio.run(chat_with_mcp(user_prompt))
