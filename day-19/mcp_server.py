"""stdio entrypoint. stdout is exclusively MCP protocol traffic."""
import sys
from backend.config import Config, ROOT
from backend.store import Store, valid_id
from backend.providers import Tavily, DeepSeek
from backend.service import PipelineService
from backend.mcp_service import create_server
from backend.diagnostics import Diagnostics

def main():
    config = Config.from_env()
    if len(sys.argv) != 2:
        raise SystemExit(2)
    run_id = valid_id(sys.argv[1])
    store = Store(config.data_dir)
    diagnostics = Diagnostics(store, run_id, (config.deepseek_key, config.tavily_key), (ROOT, config.data_dir))
    service = PipelineService(store, run_id, Tavily(config.tavily_key, diagnostics), DeepSeek(config.deepseek_key, config.model, diagnostics))
    create_server(service).run(transport="stdio")

if __name__ == "__main__":
    main()
