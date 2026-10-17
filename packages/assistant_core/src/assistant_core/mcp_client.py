"""Fresh real stdio MCP sessions; clean child environment, bounded lifecycle."""
import asyncio, json, os, sys
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def call_tools(server, calls, env=None):
    clean={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG','TMPDIR','SYSTEMROOT'}}
    clean.update(env or {})
    trace=[]; results={}
    async with asyncio.timeout(25):
        async with AsyncExitStack() as stack:
            read,write=await stack.enter_async_context(stdio_client(StdioServerParameters(command=sys.executable,args=[str(server)],env=clean)))
            session=await stack.enter_async_context(ClientSession(read,write))
            await session.initialize()
            discovery=await session.list_tools()
            trace.append({'method':'tools/list','tools':[t.name for t in discovery.tools]})
            for name,args in calls:
                result=await session.call_tool(name,args)
                content=[c.text for c in result.content if c.type=='text']
                if result.isError: raise RuntimeError('MCP tool error: '+name)
                try: value=json.loads('\n'.join(content))
                except ValueError: value='\n'.join(content)
                results[name]=value
                trace.append({'method':'tools/call','tool':name,'arguments':args,'result':value})
    return results,trace
