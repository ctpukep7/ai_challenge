"""Day 21: build and inspect a local document vector index."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from backend.pipeline import build_project_index, corpus_sha256
from corpus import CORPUS_KIND
from embedding import FastEmbedder
from index_store import read_manifest, search, strategy_stats

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "index.sqlite"
DEFAULT_CORPUS = HERE.parent
EXAMPLE_QUERIES = (
    "Как системный cron и расписания SQLite работают вместе в Day 18?",
    "Что происходит с историей Day 07 при ошибке API?",
    "Как в Day 20 реестр разделяет одинаковые имена MCP-инструментов?",
)


def _embedder(args: argparse.Namespace) -> FastEmbedder:
    return FastEmbedder(args.cache_dir)


def command_build(args: argparse.Namespace) -> None:
    manifest = build_project_index(args.db, args.corpus, args.cache_dir,
                                   args.overlap, args.min_words)
    print(f"Индекс: {args.db}")
    print(f"Файлов: {manifest['files']}, разделов: {manifest['chapters']}, "
          f"пустых: {manifest['empty_files']}; слов: {manifest['words']}; "
          f"≈ {manifest['pages_at_500_words']:.1f} страниц")
    print(f"Чанков: fixed={manifest['chunks']['fixed']}, "
          f"structure={manifest['chunks']['structure']}")
    print(f"Модель: {manifest['model']}; размерность: {manifest['dimension']}")


def _print_results(path: Path, embedder: FastEmbedder, query: str, strategy: str, top_k: int) -> None:
    print(f"  {strategy}:")
    for position, item in enumerate(search(path, embedder, query, strategy, top_k), 1):
        excerpt = " ".join(item["text"].split())[:160]
        print(f"    {position}. {item['score']:.3f} {item['source']} » {item['section']}")
        print(f"       id={item['chunk_id']} · {excerpt}")


def current_manifest(db: Path) -> dict:
    manifest = read_manifest(db)
    if (manifest.get("corpus_kind") != CORPUS_KIND
            or manifest.get("corpus_sha256") != corpus_sha256(DEFAULT_CORPUS)):
        raise ValueError("Корпус изменился: перестройте README-индекс")
    return manifest


def command_compare(args: argparse.Namespace) -> None:
    manifest = current_manifest(args.db)
    print(f"Корпус: {manifest['files']} файлов, {manifest['chapters']} разделов, {manifest['words']} слов, "
          f"≈ {manifest['pages_at_500_words']} страниц")
    print(f"Модель: {manifest['model']}; длина: до {manifest['content_token_limit']} токенов текста")
    for strategy in ("fixed", "structure"):
        row = strategy_stats(args.db, strategy)
        print(f"{strategy}: {row['count']} чанков; токены min/median/p95/max="
              f"{row['min']}/{row['median']}/{row['p95']}/{row['max']}; "
              f"перекрытие={row['overlap']} токенов; пересечений разделов={row['crosses']}")
    embedder = _embedder(args)
    for query in args.query or EXAMPLE_QUERIES:
        print(f"\nЗапрос: {query}")
        for strategy in ("fixed", "structure"):
            _print_results(args.db, embedder, query, strategy, args.top_k)
    print("\nЭто описательное сравнение. Качество поиска требует размеченных запросов.")


def command_search(args: argparse.Namespace) -> None:
    current_manifest(args.db)
    embedder = _embedder(args)
    for strategy in ((args.strategy,) if args.strategy != "both" else ("fixed", "structure")):
        _print_results(args.db, embedder, args.query, strategy, args.top_k)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Локальная индексация и поиск по документам")
    result.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite-файл индекса")
    result.add_argument("--cache-dir", type=Path, default=HERE / ".cache" / "fastembed")
    commands = result.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Построить обе стратегии")
    build.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    build.add_argument("--min-words", type=int, default=10000)
    build.add_argument("--overlap", type=float, default=0.18)
    build.set_defaults(func=command_build)
    compare = commands.add_parser("compare", help="Сравнить стратегии из индекса")
    compare.add_argument("--query", action="append", help="Одинаковый запрос для двух стратегий")
    compare.add_argument("--top-k", type=int, default=3)
    compare.set_defaults(func=command_compare)
    find = commands.add_parser("search", help="Точный cosine поиск по сохранённому индексу")
    find.add_argument("query")
    find.add_argument("--strategy", choices=("fixed", "structure", "both"), default="both")
    find.add_argument("--top-k", type=int, default=5)
    find.set_defaults(func=command_search)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser().exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
