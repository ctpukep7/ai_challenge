"""Read-only fictional support records; stdout is reserved for MCP."""
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
FIXTURE=Path(__file__).resolve().parents[1]/'fixtures/support.json'
server=FastMCP('AI Challenge fictional support')
def records():
    data=json.loads(FIXTURE.read_text())
    users={u['id']:u for u in data['users']}; tickets={t['id']:t for t in data['tickets']}
    if len(users)!=len(data['users']) or len(tickets)!=len(data['tickets']) or any(t['user_id'] not in users for t in tickets.values()):
        raise ValueError('Invalid fixture relationships')
    return users,tickets
@server.tool()
def list_tickets() -> dict:
    """List fictional tickets, without unrelated user records."""
    _,tickets=records()
    return {'tickets':[{'id':t['id'],'title':t['title'],'status':t['status']} for t in tickets.values()]}
@server.tool()
def get_ticket(ticket_id:str) -> dict:
    """Read one fictional ticket by its ID."""
    _,tickets=records()
    if ticket_id not in tickets: raise ValueError('Неизвестный тикет')
    return tickets[ticket_id]
@server.tool()
def get_ticket_user(ticket_id:str) -> dict:
    """Resolve the user only through the selected ticket relationship."""
    users,_=records()
    return users[get_ticket(ticket_id)['user_id']]
if __name__=='__main__': server.run(transport='stdio')
