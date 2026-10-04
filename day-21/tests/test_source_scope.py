import sqlite3

import pytest

from chunking import Chunk
from index_store import build_index, search, source_scope


@pytest.mark.parametrize('query, sources', [
    ('Как работает Day7?', ['day-07/README.md']),
    ('DAY 07 сохраняет контекст?', ['day-07/README.md']),
    ('Что в day-07/README.md?', ['day-07/README.md']),
    ('В DAY-07/readme.MD что хранится?', ['day-07/README.md']),
    ('Как работает день 7?', ['day-07/README.md']),
    ('Настройки дня 7', ['day-07/README.md']),
    ('Что в дне 07?', ['day-07/README.md']),
    ('Сравни Day7 и день 12', ['day-07/README.md', 'day-12/README.md']),
    ('Day13', ['day-13/README.md']),
    ('Day123', ['day-123/README.md']),
    ('Day99', ['day-99/README.md']),
    ('Что такое контекст?', None),
    ('Продолжи через 7 дней', None),
    ('Напомни через 1 day', None),
    ('holiday7 и day7test', None),
    ('day-07-backend', None),
    ('Не про Day7, расскажи про Day12', None),
    ('Кроме дня 7 какие варианты?', None),
    ('Без day-07/README.md', None),
    ('Day7 не рассматривай', None),
    ('Day7 или Day12?', None),
    ('Day7 ли использует SQLite?', None),
    ('Если модель не смогла создать summary в дне 9?', ['day-09/README.md']),
    ('Не удалось сжатие в Day9', ['day-09/README.md']),
    ('В Day7 не сохраняется ответ при ошибке?', ['day-07/README.md']),
])
def test_explicit_day_parser_boundaries_counts_and_ambiguity(query, sources):
    assert source_scope(query)['sources'] == sources


class IdenticalEmbedder:
    model_name = 'identical-synthetic-vectors'
    dimension = 2
    max_tokens = 100

    def count(self, text):
        return len(text.split())

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def index(tmp_path):
    model = IdenticalEmbedder()
    chunks = [Chunk(strategy, f'day-{day:02d}/README.md', f'Day{day}', 'Раздел',
                    f'{strategy}-{day}', 1, f'Текст дня {day}', 3, 0, False)
              for strategy in ('fixed', 'structure') for day in (1, 7, 12, 13)]
    path = tmp_path / 'index.sqlite'
    build_index(path, chunks, model, {'model': model.model_name, 'dimension': model.dimension})
    return path, model


@pytest.mark.parametrize('strategy', ['fixed', 'structure'])
def test_metadata_limits_candidates_before_ranking_and_scores_remain_cosine(index, strategy):
    path, model = index
    # Every vector is identical: without metadata, Day1 wins the deterministic tie.
    hits = search(path, model, 'Day7 память', strategy, 1)
    assert [hit['source'] for hit in hits] == ['day-07/README.md']
    assert hits[0]['score'] == pytest.approx(1.0)
    assert {hit['source'] for hit in search(path, model, 'Day7 и Day12', strategy, 10)} == {
        'day-07/README.md', 'day-12/README.md'}
    assert search(path, model, 'Day99', strategy, 10) == []
    assert len(search(path, model, 'через 7 дней', strategy, 10)) == 4
    assert len(search(path, model, 'Не про Day7', strategy, 10)) == 4
    # Explicit override is used by Day23 to preserve original scope after rewrite.
    hits = search(path, model, 'Day13', strategy, 10, retrieval_scope=source_scope('Day7'))
    assert [hit['source'] for hit in hits] == ['day-07/README.md']
    all_hits = search(path, model, 'Day13', strategy, 10, retrieval_scope=source_scope('общий вопрос'))
    assert len(all_hits) == 4
    with pytest.raises(ValueError, match='лимита модели'):
        search(path, model, 'Day7 ' + 'word ' * 100, strategy)
    # A corrupt out-of-scope vector cannot be read/unpacked: SQL excludes it first.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE chunks SET vector = ? WHERE source = 'day-12/README.md'", (b'broken',))
    assert search(path, model, 'Day7', strategy, 1)[0]['source'] == 'day-07/README.md'
    with pytest.raises(ValueError, match='Повреждён'):
        search(path, model, 'общий запрос', strategy, 1)
