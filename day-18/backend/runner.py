"""A single cron tick. No permanent worker and no scheduler on module import."""
import argparse
import asyncio
import json
from .agent import AgentRuntime
from .config import settings
from .store import Store


async def run_due(store=None,runtime=None):
    config = settings()
    store = store or Store(config.db_path)
    runtime = runtime or AgentRuntime(store,config=config)
    store.tick()
    results = []
    # Max three jobs; after finish each schedule moves strictly into the future.
    for _ in range(3):
        scope = store.claim_due(config.model)
        if not scope:
            break
        try:
            response = await runtime.run("Выполни сохранённое поручение: получи погоду и сохрани наблюдение.",scope=scope)
            success = store.finish(scope,error=response.get("error"),trace=response["trace"],json_requests=response.get("json_requests"))
            results.append({"run_id":scope["run_id"],"job_id":scope["job_id"],"status":"success" if success else "error","answer":"Наблюдение сохранено" if success else response.get("error") or "Нет подтверждённого сохранённого наблюдения"})
        except BaseException as exc:
            store.finish(scope,error="Запуск прерван; наблюдение не подтверждено",trace=getattr(exc,"trace",[]),json_requests=getattr(exc,"json_requests",[]))
            if isinstance(exc,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):
                raise
            results.append({"run_id":scope["run_id"],"status":"error","answer":"Не удалось завершить запуск"})
    return {"executed":len(results),"results":results,"collected":store.data()}


def main():
    parser = argparse.ArgumentParser(description="Один запуск агента для наступивших заданий")
    parser.add_argument("--run-due",action="store_true",required=True)
    parser.add_argument("--quiet",action="store_true",help="Не печатать накопленные записи (cron)")
    args = parser.parse_args()
    result = asyncio.run(run_due())
    if not args.quiet:
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
